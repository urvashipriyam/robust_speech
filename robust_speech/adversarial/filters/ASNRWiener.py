from audlib.enhance import wiener_iter, asnr, SSFEnhancer
from audlib.sig.window import hamming
import numpy as np
import torch

class ASNRWiener:
    def __init__(self, filter_config):
        super(ASNRWiener,self).__init__()
        assert('sr' in filter_config, "sr not in filter_config")
        assert('nfft' in filter_config, "nfft not in filter_config")
        assert('hop' in filter_config, "hop not in filter_config")

        self.sr=int(filter_config['sr'])
        self.nfft=int(filter_config['nfft'])
        self.hop=int(filter_config['hop'])
        self.window=hamming(self.nfft, hop=self.hop)
        self.gaussian_sigma = 0.0
        if 'gaussian_sigma' in filter_config:
            self.gaussian_sigma=float(filter_config['gaussian_sigma'])
        self.lpc_order=12
        self.high_freq=True
        if 'high_freq' in filter_config:
            self.high_freq=filter_config['high_freq']

    def __call__(self, x):
        x_np=x.cpu().detach().numpy()
        for i in range(len(x_np)):
            if self.high_freq:
                noise = np.random.normal(0, scale=self.gaussian_sigma, size=(x_np[i].shape[0]+1,))
                noise = 0.5 * (noise[1:]-noise[:-1])
            else:
                noise = np.random.normal(0, scale=self.gaussian_sigma, size=x_np[i].shape).astype(ART_NUMPY_DTYPE) if self.gaussian_sigma else None
            filtered_output,_=asnr(x_np[i],self.sr, self.window, self.hop, self.nfft,noise=(noise if self.gaussian_sigma>0 else None),
            snrsmooth=0.98, noisesmooth=0.98, llkthres=.15, zphase=True, rule="wiener")
            if len(filtered_output)<len(x_np[i]):
                filtered_output = np.pad(filtered_output,mode="mean",pad_width=((0,len(x_np[i])-len(filtered_output))))
            elif len(filtered_output)>len(x_np[i]):
                filtered_output=filtered_output[:len(x_np[i])]
            if not np.isnan(filtered_output).any():
                x[i]=torch.from_numpy(filtered_output)
        return x
