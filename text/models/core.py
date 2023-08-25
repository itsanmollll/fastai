# AUTOGENERATED! DO NOT EDIT! File to edit: ../../../nbs/33_text.models.core.ipynb.

# %% ../../../nbs/33_text.models.core.ipynb 1
from __future__ import annotations
from ...data.all import *
from ..core import *
from .awdlstm import *

# %% auto 0
__all__ = ['LinearDecoder', 'SequentialRNN', 'get_language_model', 'SentenceEncoder', 'masked_concat_pool',
           'PoolingLinearClassifier', 'get_text_classifier']

# %% ../../../nbs/33_text.models.core.ipynb 5
_model_meta = {AWD_LSTM: {'hid_name':'emb_sz', 'url':URLs.WT103_FWD, 'url_bwd':URLs.WT103_BWD,
                          'config_lm':awd_lstm_lm_config, 'split_lm': awd_lstm_lm_split,
                          'config_clas':awd_lstm_clas_config, 'split_clas': awd_lstm_clas_split},}
              # Transformer: {'hid_name':'d_model', 'url':URLs.OPENAI_TRANSFORMER,
              #               'config_lm':tfmer_lm_config, 'split_lm': tfmer_lm_split,
              #               'config_clas':tfmer_clas_config, 'split_clas': tfmer_clas_split},
              # TransformerXL: {'hid_name':'d_model',
              #                'config_lm':tfmerXL_lm_config, 'split_lm': tfmerXL_lm_split,
              #                'config_clas':tfmerXL_clas_config, 'split_clas': tfmerXL_clas_split}}

# %% ../../../nbs/33_text.models.core.ipynb 7
class LinearDecoder(Module):
    "To go on top of a RNNCore module and create a Language Model."
    initrange=0.1

    def __init__(self, 
        n_out:int, # Number of output channels 
        n_hid:int, # Number of features in encoder last layer output
        output_p:float=0.1, # Input dropout probability
        tie_encoder:nn.Module=None, # If module is supplied will tie decoder weight to `tie_encoder.weight`  
        bias:bool=True # If `False` the layer will not learn additive bias
    ):
        self.decoder = nn.Linear(n_hid, n_out, bias=bias)
        self.decoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.output_dp = RNNDropout(output_p)
        if bias: self.decoder.bias.data.zero_()
        if tie_encoder: self.decoder.weight = tie_encoder.weight

    def forward(self, input):
        dp_inp = self.output_dp(input)
        return self.decoder(dp_inp), input, dp_inp

# %% ../../../nbs/33_text.models.core.ipynb 10
class SequentialRNN(nn.Sequential):
    "A sequential module that passes the reset call to its children."
    def reset(self):
        for c in self.children(): getcallable(c, 'reset')()

# %% ../../../nbs/33_text.models.core.ipynb 12
def get_language_model(
    arch, # Function or class that can generate a language model architecture
    vocab_sz:int, # Size of the vocabulary
    config:dict=None, # Model configuration dictionary
    drop_mult:float=1. # Multiplicative factor to scale all dropout probabilities in `config`
) -> SequentialRNN: # Language model with `arch` encoder and linear decoder
    "Create a language model from `arch` and its `config`."
    meta = _model_meta[arch]
    config = ifnone(config, meta['config_lm']).copy()
    for k in config.keys():
        if k.endswith('_p'): config[k] *= drop_mult
    tie_weights,output_p,out_bias = map(config.pop, ['tie_weights', 'output_p', 'out_bias'])
    init = config.pop('init') if 'init' in config else None
    encoder = arch(vocab_sz, **config)
    enc = encoder.encoder if tie_weights else None
    decoder = LinearDecoder(vocab_sz, config[meta['hid_name']], output_p, tie_encoder=enc, bias=out_bias)
    model = SequentialRNN(encoder, decoder)
    return model if init is None else model.apply(init)

# %% ../../../nbs/33_text.models.core.ipynb 17
def _pad_tensor(t:Tensor, bs:int) -> Tensor:
    if t.size(0) < bs: return torch.cat([t, t.new_zeros(bs-t.size(0), *t.shape[1:])])
    return t

# %% ../../../nbs/33_text.models.core.ipynb 18
class SentenceEncoder(Module):
    "Create an encoder over `module` that can process a full sentence."
    def __init__(self, 
        bptt:int, # Backpropagation through time
        module:nn.Module, # A module that can process up to [`bs`, `bptt`] tokens
        pad_idx:int=1, # Padding token id 
        max_len:int=None # Maximal output length
    ): 
        store_attr('bptt,module,pad_idx,max_len')
    
    def reset(self): getcallable(self.module, 'reset')()

    def forward(self, input):
        bs,sl = input.size()
        self.reset()
        mask = input == self.pad_idx
        outs,masks = [],[]
        for i in range(0, sl, self.bptt):
            #Note: this expects that sequence really begins on a round multiple of bptt
            real_bs = (input[:,i] != self.pad_idx).long().sum()
            o = self.module(input[:real_bs,i: min(i+self.bptt, sl)])
            if self.max_len is None or sl-i <= self.max_len:
                outs.append(o)
                masks.append(mask[:,i: min(i+self.bptt, sl)])
        outs = torch.cat([_pad_tensor(o, bs) for o in outs], dim=1)
        mask = torch.cat(masks, dim=1)
        return outs,mask

# %% ../../../nbs/33_text.models.core.ipynb 21
def masked_concat_pool(
    output:Tensor, # Output of sentence encoder
    mask:Tensor, # Boolean mask as returned by sentence encoder
    bptt:int # Backpropagation through time
) -> Tensor: # Concatenation of [last_hidden, max_pool, avg_pool]
    "Pool `MultiBatchEncoder` outputs into one vector [last_hidden, max_pool, avg_pool]"
    lens = output.shape[1] - mask.long().sum(dim=1)
    last_lens = mask[:,-bptt:].long().sum(dim=1)
    avg_pool = output.masked_fill(mask[:, :, None], 0).sum(dim=1)
    avg_pool.div_(lens.type(avg_pool.dtype)[:,None])
    max_pool = output.masked_fill(mask[:,:,None], -float('inf')).max(dim=1)[0]
    x = torch.cat([output[torch.arange(0, output.size(0)),-last_lens-1], max_pool, avg_pool], 1) #Concat pooling.
    return x

# %% ../../../nbs/33_text.models.core.ipynb 24
class PoolingLinearClassifier(Module):
    "Create a linear classifier with pooling"
    def __init__(self, 
        dims:list, # List of hidden sizes for MLP as `int`s 
        ps:list, # List of dropout probabilities as `float`s
        bptt:int, # Backpropagation through time
        y_range:tuple=None # Tuple of (low, high) output value bounds
     ):
        if len(ps) != len(dims)-1: raise ValueError("Number of layers and dropout values do not match.")
        acts = [nn.ReLU(inplace=True)] * (len(dims) - 2) + [None]
        layers = [LinBnDrop(i, o, p=p, act=a) for i,o,p,a in zip(dims[:-1], dims[1:], ps, acts)]
        if y_range is not None: layers.append(SigmoidRange(*y_range))
        self.layers = nn.Sequential(*layers)
        self.bptt = bptt

    def forward(self, input):
        out,mask = input
        x = masked_concat_pool(out, mask, self.bptt)
        x = self.layers(x)
        return x, out, out

# %% ../../../nbs/33_text.models.core.ipynb 27
def get_text_classifier(
    arch:callable, # Function or class that can generate a language model architecture
    vocab_sz:int, # Size of the vocabulary 
    n_class:int, # Number of classes
    seq_len:int=72, # Backpropagation through time
    config:dict=None, # Encoder configuration dictionary
    drop_mult:float=1., # Multiplicative factor to scale all dropout probabilities in `config`
    lin_ftrs:list=None, # List of hidden sizes for classifier head as `int`s
    ps:list=None, # List of dropout probabilities for classifier head as `float`s
    pad_idx:int=1, # Padding token id
    max_len:int=72*20, # Maximal output length for `SentenceEncoder`
    y_range:tuple=None # Tuple of (low, high) output value bounds
):
    "Create a text classifier from `arch` and its `config`, maybe `pretrained`"
    meta = _model_meta[arch]
    cfg = meta['config_clas'].copy()
    cfg.update(ifnone(config, {}))
    config = cfg
    for k in config.keys():
        if k.endswith('_p'): config[k] *= drop_mult
    if lin_ftrs is None: lin_ftrs = [50]
    if ps is None:  ps = [0.1]*len(lin_ftrs)
    layers = [config[meta['hid_name']] * 3] + lin_ftrs + [n_class]
    ps = [config.pop('output_p')] + ps
    init = config.pop('init') if 'init' in config else None
    encoder = SentenceEncoder(seq_len, arch(vocab_sz, **config), pad_idx=pad_idx, max_len=max_len)
    model = SequentialRNN(encoder, PoolingLinearClassifier(layers, ps, bptt=seq_len, y_range=y_range))
    return model if init is None else model.apply(init)
