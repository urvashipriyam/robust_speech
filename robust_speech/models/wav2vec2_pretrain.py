"""A Wav2Vec2 Pretraining system with librispeech supporting adversarial attacks, 
and specifically the contrastive attack.
The HuggingFace implementation of the wav2vec 2.0 pretraining is used and wrapped
to fit properly the SpeechBrain framework.

Contrary to ASR models this one requires some additional work over SpeechBrain
(https://github.com/speechbrain/speechbrain/blob/develop/recipes/CommonVoice/self-supervised-learning/wav2vec2/train.py)
in order to:
    -support loading of pretrained models from Huggingface 
    (Speechbrain handles it for Wav2Vec2 for ASR but not pretraining)
    -support the quantized_representation argument to fix the quantized labels
     used by Wav2Vec2 (required for the contrastive attack).
    -backpropagate gradients to the inputs
Some transformers and SpeechBrain models have been rewritten below for that purpose.
"""
import logging
import sys

import numpy as np
import speechbrain as sb
import torch
import torch.nn.functional as F
import transformers
from speechbrain.lobes.models.huggingface_wav2vec import HuggingFaceWav2Vec2Pretrain
from transformers import Wav2Vec2ForPreTraining
from transformers.file_utils import (
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from transformers.models.wav2vec2.configuration_wav2vec2 import Wav2Vec2Config
from transformers.models.wav2vec2.modeling_wav2vec2 import (
    _CONFIG_FOR_DOC,
    WAV_2_VEC_2_INPUTS_DOCSTRING,
    Wav2Vec2FeatureExtractor,
    Wav2Vec2ForPreTrainingOutput,
    _compute_mask_indices,
)

import robust_speech as rs
from robust_speech.adversarial.brain import AdvASRBrain

logger = logging.getLogger(__name__)


class AdvWav2Vec2FeatureEncoder(Wav2Vec2FeatureExtractor):
    """
    Slight modification of the HF feature extractor.
    The original class assumes that input is a leaf tensor,
    which when running attacks isn't always the case.
    """

    def forward(self, input_values):
        hidden_states = input_values[:, None]
        # make sure hidden_states require grad for gradient_checkpointing
        if (
            self._requires_grad and self.training and hidden_states.is_leaf
        ):  # not always true when attacking
            hidden_states.requires_grad = True

        for conv_layer in self.conv_layers:
            if self._requires_grad and self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs)

                    return custom_forward

                hidden_states = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(conv_layer),
                    hidden_states,
                )
            else:
                hidden_states = conv_layer(hidden_states)

        return hidden_states


class AdvWav2Vec2ForPreTraining(Wav2Vec2ForPreTraining):
    """
    This class modifies the transformers Wav2Vec2ForPreTraining module in order to
        -replace the Feature Extractor with AdvWav2Vec2FeatureEncoder
        -handle contrastive attacks in forward
    """

    def __init__(self, config: Wav2Vec2Config):
        super().__init__(config)
        self.wav2vec2.feature_extractor = AdvWav2Vec2FeatureEncoder(config)

    @add_start_docstrings_to_model_forward(WAV_2_VEC_2_INPUTS_DOCSTRING)
    @replace_return_docstrings(
        output_type=Wav2Vec2ForPreTrainingOutput, config_class=_CONFIG_FOR_DOC
    )
    def forward(
        self,
        input_values,
        attention_mask=None,
        mask_time_indices=None,
        sampled_negative_indices=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        quantized_representation=None,
    ):
        """
        New argument quantized_representation contains an optional
        precomputed value for (quantized_features, codevector_perplexity).
        If available, this value is not recomputed in the foward pass.

        Returns:
        --------
        """

        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        if mask_time_indices is not None:
            mask_time_indices = mask_time_indices.to(torch.bool)
        outputs = self.wav2vec2(
            input_values,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            mask_time_indices=mask_time_indices,
            return_dict=return_dict,
        )

        # 1. project all transformed features (including masked) to final vq
        # dim
        transformer_features = self.project_hid(outputs[0])

        # 2. quantize all (unmasked) extracted features and project to final vq
        # dim
        extract_features = self.dropout_features(outputs[1])

        if attention_mask is not None:
            # compute reduced attention_mask correponding to feature vectors
            attention_mask = self._get_feature_vector_attention_mask(
                extract_features.shape[1], attention_mask, add_adapter=False
            )
        if quantized_representation is not None:
            quantized_features, codevector_perplexity = quantized_representation
        else:
            quantized_features, codevector_perplexity = self.quantizer(
                extract_features, mask_time_indices=mask_time_indices
            )
            quantized_features = self.project_q(quantized_features)

        loss = contrastive_loss = diversity_loss = None
        if sampled_negative_indices is not None:
            batch_size, sequence_length, hidden_size = quantized_features.shape

            # for training, we sample negatives
            # 3. sample K negatives (distractors) quantized states for contrastive loss
            # if attention_mask is passed,
            # make sure that padded feature vectors cannot be sampled
            # sample negative quantized vectors BTC => (BxT)C
            negative_quantized_features = quantized_features.view(-1, hidden_size)[
                sampled_negative_indices.long().view(-1)
            ]
            negative_quantized_features = negative_quantized_features.view(
                batch_size, sequence_length, -1, hidden_size
            ).permute(2, 0, 1, 3)

            # 4. compute logits, corresponding to `logs = sim(c_t, [q_t, \sim{q}_t]) / \kappa`
            # of equation (3) in https://arxiv.org/pdf/2006.11477.pdf
            logits = self.compute_contrastive_logits(
                quantized_features[None, :],
                negative_quantized_features,
                transformer_features,
                self.config.contrastive_logits_temperature,
            )

            # 5. if a negative vector is identical to the positive
            # (i.e. when codebook utilization is low),
            # its cosine similarity will be masked
            neg_is_pos = (quantized_features == negative_quantized_features).all(-1)

            if neg_is_pos.any():
                logits[1:][neg_is_pos] = float("-inf")

            # 6. compute contrastive loss \mathbf{L}_m = cross_entropy(logs) =
            # -log(exp(sim(c_t, q_t)/\kappa) / \sum_{\sim{q}} exp(sim(c_t, \sim{q})/\kappa))
            logits = logits.transpose(0, 2).reshape(-1, logits.size(0))
            target = ((1 - mask_time_indices.long()) * -100).transpose(0, 1).flatten()

            contrastive_loss = torch.nn.functional.cross_entropy(
                logits.float(), target, reduction="sum"
            )
            # 7. compute diversity loss: \mathbf{L}_d
            num_codevectors = (
                self.config.num_codevectors_per_group
                * self.config.num_codevector_groups
            )
            diversity_loss = (
                (num_codevectors - codevector_perplexity) / num_codevectors
            ) * mask_time_indices.sum()

            # 8. \mathbf{L} = \mathbf{L}_m + \alpha * \mathbf{L}_d
            loss = contrastive_loss + self.config.diversity_loss_weight * diversity_loss
        if not return_dict:
            if loss is not None:
                return (
                    loss,
                    transformer_features,
                    quantized_features,
                    codevector_perplexity,
                ) + outputs[2:]
            return (
                transformer_features,
                quantized_features,
                codevector_perplexity,
            ) + outputs[2:]

        return Wav2Vec2ForPreTrainingOutput(
            loss=loss,
            projected_states=transformer_features,
            projected_quantized_states=quantized_features,
            codevector_perplexity=codevector_perplexity,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            contrastive_loss=contrastive_loss,
            diversity_loss=diversity_loss,
        )


class AdvHuggingFaceWav2Vec2Pretrain(HuggingFaceWav2Vec2Pretrain):
    """This lobe enables the integration of HuggingFace
     wav2vec2.0 models to be pretrained.
     It also enables contrastive attacks and parameter loading from HuggingFace.

    Arguments
    ---------
    source : str
        HuggingFace hub name: e.g "facebook/wav2vec2-large-lv60"
    save_path : str
        Path (dir) of the downloaded model.
    mask_prob : float (default: 0.65)
        Probability of masking a given frame. Default is taken from the paper.
    mask_length : float (default: 10)
        Length (i.e. number of consecutive masked frames). Default is taken from
        the paper.
    """

    def __init__(
        self,
        source,
        save_path,
        mask_prob=0.65,
        mask_length=10,
        normalize_wav=True,
    ):
        super(AdvHuggingFaceWav2Vec2Pretrain, self).__init__(
            source,
            save_path,
            mask_prob=mask_prob,
            mask_length=mask_length,
            normalize_wav=normalize_wav,
        )
        self.model = AdvWav2Vec2ForPreTraining.from_pretrained(source)

    def forward(self, wav, quantized_representation=None):
        """Takes an input waveform and return its corresponding wav2vec encoding.

        Arguments
        ---------
        wav : torch.Tensor (signal)
            A batch of audio signals to transform to features.
        quantized_representation : Optional[torch.Tensor,torch.Tensor]
            A precomputed quantized representation of the audio signal.
        """
        batch_size, raw_sequence_length = wav.shape
        if self.normalize_wav:
            wav = F.layer_norm(wav, wav.shape)
        sequence_length = self.model._get_feat_extract_output_lengths(
            raw_sequence_length
        )

        # 1. Compute the indices that will be masked
        mask_time_indices = _compute_mask_indices(
            (batch_size, sequence_length),
            mask_prob=self.mask_prob,
            mask_length=self.mask_length,
        )
        torch_mask_time_indices = torch.tensor(
            mask_time_indices,
            device=wav.device,
            dtype=torch.long,
        )

        # 2. Sample the negative samples from the entire sequence.
        # Fairseq does it only on the masked indices, but this only work if you
        # have long sentences. For more versatily, we sample on the entire sequence.
        # value.
        full_sentence_indices = np.ones((batch_size, sequence_length))
        # print(np.sum(mask_time_indices, axis=1))
        negative_sample_indices = torch.tensor(
            transformers.models.wav2vec2.modeling_wav2vec2._sample_negative_indices(
                (batch_size, sequence_length),
                num_negatives=self.config.num_negatives,
                mask_time_indices=full_sentence_indices,
            ),
            device=wav.device,
            dtype=torch.long,
        )
        return (
            self.model(
                wav,
                mask_time_indices=torch_mask_time_indices,
                sampled_negative_indices=negative_sample_indices,
                quantized_representation=quantized_representation,
            ),
            torch_mask_time_indices,
        )


# Define training procedure


class W2VPretrain(AdvASRBrain):
    """
    Wav2Vec 2.0 base model for pretraining
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.checkpointer and not hasattr(self.hparams, "pretrainer"):
            # model was loaded from HuggingFace but not preloaded with
            # SpeechBrain: saving it at initialization
            self.checkpointer.save_checkpoint()

    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the w2v2 loss."""
        wavs, wav_lens = batch.sig

        if not stage == rs.Stage.ATTACK:
            batch = batch.to(self.device)
            wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)
        # Forward on w2v2 and take the loss.
        # It has to be on train mode even for eval. Otherwise it would deactivate
        # the loss computation ...
        # used saved quantized representation (prior to attack)
        if hasattr(batch, "quantized_representation"):
            out, mask = self.modules.wav2vec2(
                wavs, quantized_representation=batch.quantized_representation
            )
        else:
            # compute quantized representation on the fly
            out, mask = self.modules.wav2vec2(wavs, quantized_representation=None)

        if stage == rs.Stage.ATTACK:
            loss = out.contrastive_loss
        else:
            loss = out.loss

        if stage != sb.Stage.TRAIN and stage != rs.Stage.ATTACK:
            return loss, out, mask
        return loss

    def compute_objectives(
        self, predictions, batch, stage, adv=False, targeted=False, reduction="mean"
    ):
        """Computes the loss (CTC+NLL) given predictions and targets."""
        if stage == sb.Stage.TRAIN or stage == rs.Stage.ATTACK:
            # We don't have to compute anything as the HF model directly returns
            # the constrative loss.
            loss = predictions
        else:
            # We compute the accuracy between embeddings with cosing sim.
            loss, out, mask_time_indices = predictions
            cosine_sim = torch.cosine_similarity(
                out.projected_states, out.projected_quantized_states, dim=-1
            )
            # acc = cosine_sim[mask_time_indices].mean()
            acc = (
                torch.masked_select(cosine_sim, mask_time_indices.bool())
                .mean()
                .detach()
            )
            if adv:
                if targeted:
                    self.adv_acc_metric_target.append(acc)
                else:
                    self.adv_acc_metric.append(acc)
            else:
                self.acc_metric.append(acc)
        return loss

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""

        # Here we manage mixed precision
        if self.auto_mix_prec:
            with torch.cuda.amp.autocast():
                predictions = self.compute_forward(batch, sb.Stage.TRAIN)
                loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)

            # normalize the loss by gradient_accumulation step
            self.scaler.scale(loss / self.hparams.gradient_accumulation).backward()

            if self.step % self.hparams.gradient_accumulation == 0:
                # gradient clipping & early stop if loss is not fini
                self.check_gradients(loss)

                self.scaler.unscale_(self.optimizer)
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()

                # anneal lr every update
                self.hparams.noam_annealing(self.optimizer)
        else:
            predictions = self.compute_forward(batch, sb.Stage.TRAIN)
            loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)

            # normalize the loss by gradient_accumulation step
            (loss / self.hparams.gradient_accumulation).backward()

            if self.step % self.hparams.gradient_accumulation == 0:
                # gradient clipping & early stop if loss is not fini
                self.check_gradients(loss)

                self.optimizer.step()
                self.optimizer.zero_grad()

                # anneal lr every update
                self.hparams.noam_annealing(self.optimizer)

        return loss.detach()

    def fit_batch_adversarial(self, batch):
        """Train the parameters given a single batch in input"""

        # Here we manage mixed precision
        if self.auto_mix_prec:
            with torch.cuda.amp.autocast():
                predictions, _ = self.compute_forward_adversarial(batch, sb.Stage.TRAIN)
                loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)

            # normalize the loss by gradient_accumulation step
            self.scaler.scale(loss / self.hparams.gradient_accumulation).backward()

            if self.step % self.hparams.gradient_accumulation == 0:
                # gradient clipping & early stop if loss is not fini
                self.check_gradients(loss)

                self.scaler.unscale_(self.optimizer)
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()

                # anneal lr every update
                self.hparams.noam_annealing(self.optimizer)
        else:
            predictions = self.compute_forward(batch, sb.Stage.TRAIN)
            loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)

            # normalize the loss by gradient_accumulation step
            (loss / self.hparams.gradient_accumulation).backward()

            if self.step % self.hparams.gradient_accumulation == 0:
                # gradient clipping & early stop if loss is not fini
                self.check_gradients(loss)

                self.optimizer.step()
                self.optimizer.zero_grad()

                # anneal lr every update
                self.hparams.noam_annealing(self.optimizer)

        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        if stage == sb.Stage.VALID or stage == sb.Stage.TEST:
            self.acc_metric = []
            self.adv_acc_metric = []
            self.adv_acc_metric_target = []

    def on_stage_end(
        self, stage, stage_loss, epoch, stage_adv_loss=None, stage_adv_loss_target=None
    ):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage_adv_loss is not None:
            stage_stats["adv_loss"] = stage_adv_loss
        if stage_adv_loss_target is not None:
            stage_stats["adv_loss target"] = stage_adv_loss_target
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["acc"] = sum(self.acc_metric) / len(self.acc_metric)
            if stage_adv_loss is not None:
                stage_stats["adv acc"] = sum(self.adv_acc_metric) / len(
                    self.adv_acc_metric
                )
            if stage_adv_loss_target is not None:
                stage_stats["adv acc target"] = sum(self.adv_acc_metric_target) / len(
                    self.adv_acc_metric_target
                )

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            current_lr = self.hparams.noam_annealing.current_lr
            steps = self.hparams.noam_annealing.n_steps
            optimizer = self.optimizer.__class__.__name__

            epoch_stats = {
                "epoch": epoch,
                "lr": current_lr,
                "steps": steps,
                "optimizer": optimizer,
            }
            self.hparams.train_logger.log_stats(
                stats_meta=epoch_stats,
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"acc": stage_stats["acc"], "epoch": epoch},
                max_keys=["acc"],
            )

        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Evaluation stage": "TEST"},
                test_stats=stage_stats,
            )
