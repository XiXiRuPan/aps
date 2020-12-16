#!/usr/bin/env python

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import torch as th
import torch.nn as nn
import torch.nn.functional as tf

from typing import Optional, Dict, Tuple, List
from aps.asr.xfmr.encoder import support_xfmr_encoder
from aps.asr.transducer.decoder import TorchTransformerDecoder, PyTorchRNNDecoder
from aps.asr.base.encoder import encoder_instance
from aps.libs import ApsRegisters

TransducerOutputType = Tuple[th.Tensor, Optional[th.Tensor]]


class TransducerASRBase(nn.Module):
    """
    Base class for Transducer ASR
    """

    def __init__(self,
                 input_size: int = 80,
                 vocab_size: int = 40,
                 blank: int = -1,
                 asr_transform: Optional[nn.Module] = None,
                 enc_type: str = "xfmr",
                 enc_proj: Optional[int] = None,
                 enc_kwargs: Dict = {}) -> None:
        super(TransducerASRBase, self).__init__()
        if blank < 0:
            raise RuntimeError(f"Unsupported blank value: {blank}")
        self.blank = blank
        self.asr_transform = asr_transform
        xfmr_encoder_cls = support_xfmr_encoder(enc_type)
        if xfmr_encoder_cls:
            self.is_xfmr_encoder = True
            self.encoder = xfmr_encoder_cls(input_size, **enc_kwargs)
        else:
            self.is_xfmr_encoder = False
            if enc_proj is None:
                raise ValueError(
                    "For non-transformer encoder, enc_proj can not be None")
            self.encoder = encoder_instance(enc_type, input_size, enc_proj,
                                            enc_kwargs)


@ApsRegisters.asr.register("transducer")
class TransducerASR(TransducerASRBase):
    """
    Transducer based ASR model with (Non-)Transformer encoder + RNN decoder
    """

    def __init__(self,
                 input_size: int = 80,
                 vocab_size: int = 40,
                 blank: int = -1,
                 asr_transform: Optional[nn.Module] = None,
                 enc_type: str = "xfmr",
                 enc_proj: Optional[int] = None,
                 dec_type: str = "rnn",
                 enc_kwargs: Dict = {},
                 dec_kwargs: Dict = {}) -> None:
        super(TransducerASR, self).__init__(input_size=input_size,
                                            vocab_size=vocab_size,
                                            blank=blank,
                                            asr_transform=asr_transform,
                                            enc_type=enc_type,
                                            enc_proj=enc_proj,
                                            enc_kwargs=enc_kwargs)
        if dec_type != "rnn":
            raise ValueError(
                "TorchTransducerASR: currently decoder must be rnn")
        if self.is_xfmr_encoder:
            dec_kwargs["enc_dim"] = enc_kwargs["att_dim"]
        else:
            dec_kwargs["enc_dim"] = enc_proj
        self.decoder = PyTorchRNNDecoder(vocab_size, **dec_kwargs)

    def forward(self, x_pad: th.Tensor, x_len: Optional[th.Tensor],
                y_pad: th.Tensor,
                y_len: Optional[th.Tensor]) -> TransducerOutputType:
        """
        Args:
            x_pad: N x Ti x D or N x S
            x_len: N or None
            y_pad: N x To
            y_len: N or None (not used here)
        Return:
            dec_out: N x Ti x To+1 x V
        """
        # feature transform
        if self.asr_transform:
            x_pad, x_len = self.asr_transform(x_pad, x_len)
        # Ti x N x D or N x Ti x D
        enc_out, enc_len = self.encoder(x_pad, x_len)
        # Ti x N x D => N x Ti x D
        if self.is_xfmr_encoder:
            enc_out = enc_out.transpose(0, 1)
        # N x To+1
        y_pad = tf.pad(y_pad, (1, 0), value=self.blank)
        # N x Ti x To+1 x V
        dec_out = self.decoder(enc_out, y_pad)
        return dec_out, enc_len

    def _dec_prep(self, x: th.Tensor) -> th.Tensor:
        """
        Parepare data for decoding
        """
        # raw wave
        if self.asr_transform:
            if x.dim() != 1:
                raise RuntimeError("Now only support for one utterance")
            x, _ = self.asr_transform(x[None, ...], None)
        else:
            # T x F or Beam x T x F
            if x.dim() not in [2, 3]:
                raise RuntimeError(f"Expect 2/3D tensor, but got {x.dim()}")
            x = x[None, ...]
        # Ti x N x D
        enc_out, _ = self.encoder(x, None)
        # Ti x N x D => N x Ti x D
        if self.is_xfmr_encoder:
            enc_out = enc_out.transpose(0, 1)
        return enc_out

    def greedy_search(self, x: th.Tensor) -> List[Dict]:
        """
        Beam search for TorchTransducerASR
        """
        with th.no_grad():
            enc_out = self._dec_prep(x)
            return self.decoder.greedy_search(enc_out, blank=self.blank)

    def beam_search(self,
                    x: th.Tensor,
                    lm: Optional[nn.Module] = None,
                    lm_weight: float = 0,
                    beam: int = 16,
                    nbest: int = 8,
                    normalized: bool = True,
                    max_len: int = -1,
                    vectorized: bool = True) -> List[Dict]:
        """
        Beam search for TorchTransducerASR
        """
        with th.no_grad():
            enc_out = self._dec_prep(x)
            return self.decoder.beam_search(enc_out,
                                            beam=beam,
                                            blank=self.blank,
                                            nbest=nbest,
                                            lm=lm,
                                            lm_weight=lm_weight,
                                            normalized=normalized)


@ApsRegisters.asr.register("xfmr_transducer")
class XfmrTransducerASR(TransducerASRBase):
    """
    Transducer based ASR model with (Non-)Transformer encoder + Transformer decoder
    """

    def __init__(self,
                 input_size: int = 80,
                 vocab_size: int = 40,
                 blank: int = -1,
                 asr_transform: Optional[nn.Module] = None,
                 enc_type: str = "xfmr",
                 enc_proj: Optional[int] = None,
                 enc_kwargs: Dict = {},
                 dec_type: str = "xfmr",
                 dec_kwargs: Dict = {}) -> None:
        super(XfmrTransducerASR, self).__init__(input_size=input_size,
                                                vocab_size=vocab_size,
                                                blank=blank,
                                                asr_transform=asr_transform,
                                                enc_type=enc_type,
                                                enc_proj=enc_proj,
                                                enc_kwargs=enc_kwargs)
        if dec_type != "xfmr":
            raise ValueError("TransformerTransducerASR: currently decoder "
                             "must be xfmr")
        if not self.is_xfmr_encoder and enc_proj != dec_kwargs["att_dim"]:
            raise ValueError("enc_proj should be equal to att_dim")
        self.decoder = TorchTransformerDecoder(vocab_size, **dec_kwargs)

    def forward(self, x_pad: th.Tensor, x_len: Optional[th.Tensor],
                y_pad: th.Tensor,
                y_len: Optional[th.Tensor]) -> TransducerOutputType:
        """
        Args:
            x_pad: N x Ti x D or N x S
            x_len: N or None
            y_pad: N x To
            y_len: N or None
        Return:
            dec_out: N x Ti x To+1 x V
        """
        # feature transform
        if self.asr_transform:
            x_pad, x_len = self.asr_transform(x_pad, x_len)
        # Ti x N x D or N x Ti x D
        enc_out, enc_len = self.encoder(x_pad, x_len)
        # N x Ti x D => Ti x N x D
        if not self.is_xfmr_encoder:
            enc_out = enc_out.transpose(0, 1)
        # N x To+1
        y_pad = tf.pad(y_pad, (1, 0), value=self.blank)
        # N x Ti x To+1 x V
        dec_out = self.decoder(enc_out, y_pad, y_len)
        return dec_out, enc_len

    def _dec_prep(self, x: th.Tensor) -> th.Tensor:
        """
        Prepare data for decoding
        """
        # raw wave
        if self.asr_transform:
            if x.dim() != 1:
                raise RuntimeError("Now only support for one utterance")
            x, _ = self.asr_transform(x[None, ...], None)
        else:
            if x.dim() not in [2, 3]:
                raise RuntimeError(f"Expect 2/3D tensor, but got {x.dim()}")
            x = x[None, ...]
        # Ti x N x D
        enc_out, _ = self.encoder(x, None)
        # N x Ti x D => Ti x N x D
        if not self.is_xfmr_encoder:
            enc_out = enc_out.transpose(0, 1)
        return enc_out

    def greedy_search(self, x: th.Tensor) -> List[Dict]:
        """
        Greedy search for TransformerTransducerASR
        """
        with th.no_grad():
            enc_out = self._dec_prep(x)
            return self.decoder.greedy_search(enc_out, blank=self.blank)

    def beam_search(self,
                    x: th.Tensor,
                    lm: Optional[nn.Module] = None,
                    lm_weight: float = 0,
                    beam: int = 16,
                    nbest: int = 8,
                    normalized: bool = True,
                    max_len: int = -1,
                    vectorized: bool = True) -> List[Dict]:
        """
        Beam search for TransformerTransducerASR
        """
        with th.no_grad():
            enc_out = self._dec_prep(x)
            return self.decoder.beam_search(enc_out,
                                            beam=beam,
                                            blank=self.blank,
                                            nbest=nbest,
                                            lm=lm,
                                            lm_weight=lm_weight,
                                            normalized=normalized)
