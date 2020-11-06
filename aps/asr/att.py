#!/usr/bin/env python

# Copyright 2019 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import torch as th
import torch.nn as nn

from typing import Optional, Dict, Tuple, List
from aps.asr.base.decoder import TorchDecoder
from aps.asr.base.encoder import encoder_instance
from aps.asr.base.attention import att_instance


class AttASR(nn.Module):
    """
    Attention-based ASR model
    """

    def __init__(self,
                 input_size: int = 80,
                 vocab_size: int = 30,
                 sos: int = -1,
                 eos: int = -1,
                 ctc: bool = False,
                 asr_transform: Optional[nn.Module] = None,
                 att_type: str = "ctx",
                 att_kwargs: Optional[Dict] = None,
                 encoder_type: str = "common",
                 encoder_proj: int = 256,
                 encoder_kwargs: Optional[Dict] = None,
                 decoder_dim: int = 512,
                 decoder_kwargs: Optional[Dict] = None) -> None:
        super(AttASR, self).__init__()
        self.encoder = encoder_instance(encoder_type, input_size, encoder_proj,
                                        **encoder_kwargs)
        att = att_instance(att_type, encoder_proj, decoder_dim, **att_kwargs)
        self.decoder = TorchDecoder(encoder_proj,
                                    vocab_size - 1 if ctc else vocab_size,
                                    attention=att,
                                    **decoder_kwargs)
        if eos < 0 or sos < 0:
            raise RuntimeError(f"Unsupported SOS/EOS value: {sos}/{eos}")
        self.sos = sos
        self.eos = eos
        self.ctc = nn.Linear(encoder_proj, vocab_size) if ctc else None
        self.asr_transform = asr_transform

    def forward(
        self,
        x_pad: th.Tensor,
        x_len: Optional[th.Tensor],
        y_pad: th.Tensor,
        ssr: int = 0
    ) -> Tuple[th.Tensor, th.Tensor, Optional[th.Tensor], Optional[th.Tensor]]:
        """
        Args:
            x_pad: N x Ti x D or N x S
            x_len: N or None
            y_pad: N x To
            ssr: schedule sampling rate
        Return:
            outs: N x (To+1) x V
            alis: N x (To+1) x T
        """
        # asr feature transform
        if self.asr_transform:
            x_pad, x_len = self.asr_transform(x_pad, x_len)
        # N x Ti x D
        enc_out, enc_len = self.encoder(x_pad, x_len)
        # N x (To+1), pad SOS
        outs, alis = self.decoder(enc_out,
                                  enc_len,
                                  y_pad,
                                  sos=self.sos,
                                  schedule_sampling=ssr)
        ctc_branch = self.ctc(enc_out) if self.ctc else None
        return outs, alis, ctc_branch, enc_len

    def beam_search(self,
                    x: th.Tensor,
                    lm: Optional[nn.Module] = None,
                    lm_weight: float = 0,
                    beam: int = 16,
                    nbest: int = 8,
                    max_len: int = -1,
                    vectorized: bool = False,
                    normalized: bool = True) -> List[Dict]:
        """
        Args
            x: audio samples or acoustic features, S or Ti x F
        """
        with th.no_grad():
            if self.asr_transform:
                if x.dim() != 1:
                    raise RuntimeError("Now only support for one utterance")
                x, _ = self.asr_transform(x[None, ...], None)
                # 1 x C x T x ... or 1 x T x F
                inp_len = x.shape[-2]
                enc_out, _ = self.encoder(x, None)
            else:
                if x.dim() != 2:
                    raise RuntimeError("Now only support for one utterance")
                # Ti x F
                inp_len = x.shape[0]
                enc_out, _ = self.encoder(x[None, ...], None)
            max_len = inp_len if max_len <= 0 else min(inp_len, max_len)
            if vectorized:
                return self.decoder.beam_search_vectorized(
                    enc_out,
                    beam=beam,
                    lm=lm,
                    lm_weight=lm_weight,
                    nbest=nbest,
                    max_len=max_len,
                    sos=self.sos,
                    eos=self.eos,
                    normalized=normalized)
            else:
                return self.decoder.beam_search(enc_out,
                                                beam=beam,
                                                nbest=nbest,
                                                max_len=max_len,
                                                sos=self.sos,
                                                eos=self.eos,
                                                normalized=normalized)

    def beam_search_batch(self,
                          x: th.Tensor,
                          x_len: Optional[th.Tensor],
                          beam: int = 16,
                          nbest: int = 8,
                          max_len: int = -1,
                          normalized=True) -> List[Dict]:
        """
        args
            x: audio samples or acoustic features, N x S or N x Ti x F
        """
        with th.no_grad():
            if self.asr_transform:
                if x.dim() == 1:
                    raise RuntimeError(
                        "Got one utterance, use beam_search(...) instead")
                x, x_len = self.asr_transform(x, x_len)
                inp_len = x.shape[-2]
                enc_out, enc_len = self.encoder(x, x_len)
            else:
                # N x Ti x F
                if x.dim() == 2:
                    raise RuntimeError(
                        "Got one utterance, use beam_search(...) instead")
                inp_len = x.shape[1]
                enc_out, enc_len = self.encoder(x, x_len)
            max_len = inp_len if max_len <= 0 else min(inp_len, max_len)
            return self.decoder.beam_search_batch(enc_out,
                                                  enc_len,
                                                  beam=beam,
                                                  nbest=nbest,
                                                  max_len=max_len,
                                                  sos=self.sos,
                                                  eos=self.eos,
                                                  normalized=normalized)
