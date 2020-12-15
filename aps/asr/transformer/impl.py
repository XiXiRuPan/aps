#!/usr/bin/env python

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import copy
import torch as th
import torch.nn as nn
import torch.nn.functional as tf

from torch.nn import TransformerEncoderLayer
from typing import Optional, Tuple, List


def _get_activation_fn(activation: str) -> nn.Module:
    if activation == "relu":
        return nn.ReLU()
    elif activation == "gelu":
        return nn.GELU()
    raise RuntimeError(f"activation should be relu/gelu, not {activation}")


class ApsMultiheadAttention(nn.Module):
    """
    NOTE: my own MultiheadAttention and make sure it's same as torch.nn.MultiheadAttention
    """

    def __init__(self,
                 embed_dim: int,
                 num_heads: int,
                 dropout: float = 0,
                 bias: bool = True) -> None:
        super(ApsMultiheadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        self.in_proj_weight = nn.Parameter(th.empty(3 * embed_dim, embed_dim))
        nn.init.xavier_uniform_(self.in_proj_weight)
        if bias:
            self.in_proj_bias = nn.Parameter(th.empty(3 * embed_dim))
            nn.init.constant_(self.in_proj_bias, 0)
        else:
            self.register_parameter("in_proj_bias", None)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=True)
        self.dropout = nn.Dropout(p=dropout)

    def inp_proj(self, inps: List[th.Tensor]) -> List[th.Tensor]:
        """
        Args:
            inps (list[Tensor]): T x N x E
        Return:
            outs (list[Tensor]): T x N x H x D
        """

        def _proj(base: int, mat: th.Tensor) -> th.Tensor:
            idx = slice(base * self.embed_dim, (base + 1) * self.embed_dim)
            mat = tf.linear(mat, self.in_proj_weight[idx],
                            self.in_proj_bias[idx])
            return mat.view(mat.size(0), -1, self.num_heads, self.head_dim)

        return [_proj(i, inp) for i, inp in enumerate(inps)]

    def context_weight(
            self,
            logit: th.Tensor,
            value: th.Tensor,
            key_padding_mask: Optional[th.Tensor] = None,
            attn_mask: Optional[th.Tensor] = None) -> Tuple[th.Tensor]:
        """
        Return self-attention weight and context
        Args:
            logit (Tensor): L x N x H x S
            value (Tensor): S x N x H x D
        Return:
            context (Tensor): L x N x H x D
            weight (Tensor): L x N x H x S
        """
        logit = logit / (self.head_dim)**0.5
        if key_padding_mask is not None:
            logit = logit.masked_fill(key_padding_mask[None, :, None, :],
                                      float("-inf"))
        if attn_mask is not None:
            logit += attn_mask[:, None, None, :]
        # L x N x H x S
        weight = self.dropout(th.softmax(logit, dim=-1))
        # L x N x H x D
        context = th.einsum("lnhs,snhd->lnhd", weight, value)
        return context, weight

    def torch_forward(
            self,
            query: th.Tensor,
            key: th.Tensor,
            value: th.Tensor,
            key_padding_mask: Optional[th.Tensor] = None,
            attn_mask: Optional[th.Tensor] = None
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Args:
            query (Tensor): L x N x E
            key (Tensor): S x N x E
            value (Tensor): S x N x E
            key_padding_mask (Tensor): N x S
            attn_mask (Tensor): L x S, additional mask
        Return:
            output (Tensor): L x N x E
            att_weights (Tensor): N x L x S
        """
        return tf.multi_head_attention_forward(
            query,
            key,
            value,
            self.embed_dim,
            self.num_heads,
            self.in_proj_weight,
            self.in_proj_bias,
            None,
            None,
            False,
            self.dropout.p,
            self.out_proj.weight,
            self.out_proj.bias,
            training=self.training,
            key_padding_mask=key_padding_mask,
            need_weights=True,
            attn_mask=attn_mask)

    def wrap_out(self, context: th.Tensor,
                 weight: th.Tensor) -> Tuple[th.Tensor]:
        """
        Return context & weight tensor
        Args:
            context (Tensor): L x N x H x D
            weight (Tensor): L x N x H x S
        Return:
            context (Tensor): L x N x E
            weight (Tensor): N x L x S
        """
        L, _, _, _ = context.shape
        # L x N x HD
        context = context.contiguous().view(L, -1, self.embed_dim)
        # L x N x E
        context = self.out_proj(context)
        # L x N x S => N x L x S
        weight = weight.mean(-2).transpose(0, 1)
        # return
        return context, weight

    def forward(
            self,
            query: th.Tensor,
            key: th.Tensor,
            value: th.Tensor,
            key_padding_mask: Optional[th.Tensor] = None,
            attn_mask: Optional[th.Tensor] = None
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Args:
            query (Tensor): L x N x E
            key (Tensor): S x N x E
            value (Tensor): S x N x E
            key_padding_mask (Tensor): N x S
            attn_mask (Tensor): L x S, additional mask
        Return:
            context (Tensor): L x N x E
            weight (Tensor): N x L x S
        """
        # query: L x N x H x D
        # key, value: S x N x H x D
        query, key, value = self.inp_proj([query, key, value])
        # L x N x H x S
        logit = th.einsum("lnhd,snhd->lnhs", query, key)
        context, weight = self.context_weight(logit,
                                              value,
                                              attn_mask=attn_mask,
                                              key_padding_mask=key_padding_mask)
        return self.wrap_out(context, weight)


class RelMultiheadAttention(ApsMultiheadAttention):
    """
    MultiheadAttention with relative position embedding described in:
        Self-Attention with Relative Position Representations
    """

    def __init__(self,
                 embed_dim: int,
                 num_heads: int,
                 dropout: float = 0,
                 bias: bool = True) -> None:
        super(RelMultiheadAttention, self).__init__(embed_dim,
                                                    num_heads,
                                                    dropout=dropout,
                                                    bias=bias)

    def forward(
            self,
            query: th.Tensor,
            key: th.Tensor,
            value: th.Tensor,
            key_rel_pose: th.Tensor,
            value_rel_pose: Optional[th.Tensor],
            key_padding_mask: Optional[th.Tensor] = None,
            attn_mask: Optional[th.Tensor] = None
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Args:
            query (Tensor): L x N x E
            key (Tensor): S x N x E
            value (Tensor): S x N x E
            key_rel_pose (Tensor): L x S x D
            value_rel_pose (Tensor): L x S x D
            key_padding_mask (Tensor): N x S
            attn_mask (Tensor): L x S, additional mask
        Return:
            context (Tensor): L x N x E
            weight (Tensor): N x L x S
        """
        # query: L x N x H x D
        # key, value: S x N x H x D
        query, key, value = self.inp_proj([query, key, value])
        # L x N x H x S
        term_a = th.einsum("lnhd,snhd->lnhs", query, key)
        L, N, H, D = query.shape
        query = query.view(L, N * H, D)
        term_b = th.einsum("...hd,...sd->...hs", query, key_rel_pose)
        term_b = term_b.view(L, N, H, -1)
        logit = term_a + term_b
        context, weight = self.context_weight(logit,
                                              value,
                                              attn_mask=attn_mask,
                                              key_padding_mask=key_padding_mask)
        if value_rel_pose is not None:
            weights = weight.view(L, N * H, -1)
            to_add = th.einsum("...hs,...sd->...hd", weights, value_rel_pose)
            context += to_add.view(L, N, H, -1)
        return self.wrap_out(context, weight)


class XlMultiheadAttention(ApsMultiheadAttention):
    """
    MultiheadAttention with relative position embedding described in:
        Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context
    Reference code from "RelPartialLearnableMultiHeadAttn" in
        https://github.com/kimiyoung/transformer-xl/blob/master/pytorch/mem_transformer.py#L212
    """

    def __init__(self,
                 embed_dim: int,
                 num_heads: int,
                 dropout: float = 0,
                 bias: bool = True,
                 rel_u: Optional[nn.Parameter] = None,
                 rel_v: Optional[nn.Parameter] = None) -> None:
        super(XlMultiheadAttention, self).__init__(embed_dim,
                                                   num_heads,
                                                   dropout=dropout,
                                                   bias=bias)
        if rel_u is None or rel_v is None:
            self.rel_u = nn.Parameter(th.Tensor(self.num_heads, self.head_dim))
            self.rel_v = nn.Parameter(th.Tensor(self.num_heads, self.head_dim))
            nn.init.normal_(self.rel_u, std=0.02)
            nn.init.normal_(self.rel_v, std=0.02)
        else:
            self.rel_u = rel_u
            self.rel_v = rel_v
        self.rel_proj = nn.Linear(embed_dim, embed_dim, bias=False)

    def _rel_shift(self, rel_pos: th.Tensor) -> th.Tensor:
        """
        Args:
            rel_pos (Tensor): L x N x H x S
        Return:
            rel_pos (Tensor): L x N x H x S
        """
        L, N, H, S = rel_pos.shape
        zero_pad = th.zeros((L, N, H, 1),
                            device=rel_pos.device,
                            dtype=rel_pos.dtype)
        # L x N x H x S+1
        rel_pos_pad = th.cat([rel_pos, zero_pad], dim=-1)
        # L x S+1 x N x H
        rel_pos_pad = th.einsum("lnhs->lsnh", rel_pos_pad).contiguous()
        # S+1 x L x N x H
        rel_pos_pad = rel_pos_pad.view([S + 1, L, N, H])[:1]
        # S x L x N x H
        rel_pos_pad = th.einsum("slnh->lnhs", rel_pos_pad).contiguous()
        return rel_pos_pad

    def forward(
            self,
            query: th.Tensor,
            key: th.Tensor,
            value: th.Tensor,
            sin_pose: th.Tensor,
            key_padding_mask: Optional[th.Tensor] = None,
            attn_mask: Optional[th.Tensor] = None
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Args:
            query (Tensor): L x N x E
            key (Tensor): S x N x E
            value (Tensor): S x N x E
            sin_pose (Tensor): S x E
            key_padding_mask (Tensor): N x S
            attn_mask (Tensor): L x S, additional mask
        Return:
            output (Tensor): L x N x E
            att_weights (Tensor): N x L x S
        """
        # query: L x N x H x D
        # key, value: S x N x H x D
        query, key, value = self.inp_proj([query, key, value])
        # S x E
        rel_pos = self.rel_proj(sin_pose)
        # S x H x D
        rel_pos = rel_pos.view(rel_pos.size(0), self.num_heads, self.head_dim)
        # L x N x H x S
        term_ac = th.einsum("lnhd,snhd->lnhs", query + self.rel_u, key)
        # L x N x H x S
        term_bd = th.einsum("lnhd,shd->lnhs", query + self.rel_v, rel_pos)
        term_bd = self._rel_shift(term_bd)
        # L x N x H x S
        logit = term_ac + term_bd
        context, weight = self.context_weight(logit,
                                              value,
                                              attn_mask=attn_mask,
                                              key_padding_mask=key_padding_mask)
        return self.wrap_out(context, weight)


class TransformerTorchEncoderLayer(TransformerEncoderLayer):
    """
    Wrapper for TransformerEncoderLayer (add pre-norm)
    """

    def __init__(self,
                 d_model: int,
                 nhead: int,
                 dim_feedforward: int = 2048,
                 pre_norm: bool = False,
                 dropout: bool = 0.1,
                 activation: str = "relu") -> None:
        super(TransformerTorchEncoderLayer,
              self).__init__(d_model,
                             nhead,
                             dim_feedforward=dim_feedforward,
                             dropout=dropout,
                             activation=activation)
        self.pre_norm = pre_norm

    def ffn(self, src: th.Tensor) -> th.Tensor:
        """
        Get output of the feedforward network
        """
        return self.dropout2(
            self.linear2(self.dropout(self.activation(self.linear1(src)))))

    def forward(self,
                src: th.Tensor,
                src_mask: Optional[th.Tensor] = None,
                src_key_padding_mask: Optional[th.Tensor] = None) -> th.Tensor:
        """
        Support for both pre-norm & post-norm
        """
        inp = src
        if self.pre_norm:
            src = self.norm1(src)
        att = self.self_attn(src,
                             src,
                             src,
                             attn_mask=src_mask,
                             key_padding_mask=src_key_padding_mask)[0]
        src = inp + self.dropout1(att)
        if self.pre_norm:
            src = src + self.dropout2(self.ffn(self.norm2(src)))
        else:
            src = self.norm1(src)
            src = self.norm2(src + self.ffn(src))
        return src


class ApsTransformerEncoderLayer(nn.Module):
    """
    A base class for TransformerEncoderLayer
    """

    def __init__(self,
                 d_model: int,
                 self_attn: nn.Module,
                 dim_feedforward: int = 2048,
                 dropout: float = 0.1,
                 activation: str = "relu",
                 pre_norm: bool = False) -> None:
        super(ApsTransformerEncoderLayer, self).__init__()
        self.self_attn = self_attn
        # Implementation of Feedforward model
        self.feedforward = nn.Sequential(nn.Linear(d_model, dim_feedforward),
                                         _get_activation_fn(activation),
                                         nn.Dropout(dropout),
                                         nn.Linear(dim_feedforward, d_model),
                                         nn.Dropout(dropout))
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout = nn.Dropout(dropout)
        self.activation = _get_activation_fn(activation)
        self.pre_norm = pre_norm

    def __setstate__(self, state: str) -> None:
        if "activation" not in state:
            state["activation"] = tf.relu
        super(ApsTransformerEncoderLayer, self).__setstate__(state)


class TransformerRelEncoderLayer(ApsTransformerEncoderLayer):
    """
    TransformerEncoderLayer using relative position encodings
    """

    def __init__(self,
                 d_model: int,
                 nhead: int,
                 dim_feedforward: int = 2048,
                 dropout: float = 0.1,
                 activation: str = "relu",
                 pre_norm: bool = False) -> None:
        self_attn = RelMultiheadAttention(d_model, nhead, dropout=dropout)
        super(TransformerRelEncoderLayer,
              self).__init__(d_model,
                             self_attn,
                             dim_feedforward=dim_feedforward,
                             dropout=dropout,
                             activation=activation,
                             pre_norm=pre_norm)

    def forward(self,
                src: th.Tensor,
                key_rel_pose: Optional[th.Tensor] = None,
                value_rel_pose: Optional[th.Tensor] = None,
                src_mask: Optional[th.Tensor] = None,
                src_key_padding_mask: Optional[th.Tensor] = None) -> th.Tensor:
        inp = src
        if self.pre_norm:
            src = self.norm1(src)
        att = self.self_attn(src,
                             src,
                             src,
                             key_rel_pose,
                             value_rel_pose,
                             attn_mask=src_mask,
                             key_padding_mask=src_key_padding_mask)[0]
        src = inp + self.dropout(att)
        if self.pre_norm:
            src = src + self.feedforward(self.norm2(src))
        else:
            src = self.norm1(src)
            src = self.norm2(src + self.feedforward(src))
        return src


class TransformerXLEncoderLayer(ApsTransformerEncoderLayer):
    """
    TransformerEncoderLayer using relative position encodings
    """

    def __init__(self,
                 d_model: int,
                 nhead: int,
                 dim_feedforward: int = 2048,
                 dropout: float = 0.1,
                 activation: str = "relu",
                 pre_norm: bool = False,
                 rel_u: Optional[nn.Parameter] = None,
                 rel_v: Optional[nn.Parameter] = None) -> None:
        self_attn = XlMultiheadAttention(d_model,
                                         nhead,
                                         dropout=dropout,
                                         rel_u=rel_u,
                                         rel_v=rel_v)
        super(TransformerXLEncoderLayer,
              self).__init__(d_model,
                             self_attn,
                             dim_feedforward=dim_feedforward,
                             dropout=dropout,
                             activation=activation,
                             pre_norm=pre_norm)

    def forward(self,
                src: th.Tensor,
                sin_pose: Optional[th.Tensor] = None,
                src_mask: Optional[th.Tensor] = None,
                src_key_padding_mask: Optional[th.Tensor] = None) -> th.Tensor:
        inp = src
        if self.pre_norm:
            src = self.norm1(src)
        att = self.self_attn(src,
                             src,
                             src,
                             sin_pose,
                             attn_mask=src_mask,
                             key_padding_mask=src_key_padding_mask)[0]
        src = inp + self.dropout(att)
        if self.pre_norm:
            src = src + self.feedforward(self.norm2(src))
        else:
            src = self.norm1(src)
            src = self.norm2(src + self.feedforward(src))
        return src


class ApsTransformerEncoder(nn.Module):
    """
    Wrapper for a stack of N Transformer encoder layers
    """
    __constants__ = ['norm']

    def __init__(self,
                 encoder_layer: nn.Module,
                 num_layers: int,
                 norm: Optional[nn.Module] = None) -> None:
        super(ApsTransformerEncoder, self).__init__()
        self.layers = nn.ModuleList(
            [copy.deepcopy(encoder_layer) for i in range(num_layers)])
        self.num_layers = num_layers
        self.norm = norm

    def forward(self, src: th.Tensor, **kwargs) -> th.Tensor:
        output = src

        for mod in self.layers:
            output = mod(output, **kwargs)

        if self.norm is not None:
            output = self.norm(output)

        return output


def padding_mask(vec, device=None):
    N = vec.nelement()
    M = vec.max().item()
    templ = th.arange(M, device=vec.device).repeat([N, 1])
    mask = (templ >= vec.unsqueeze(1))
    return mask.to(device) if device is not None else mask


def prep_sub_mask(T, device="cpu"):
    mask = (th.triu(th.ones(T, T, device=device), diagonal=1) == 1).float()
    mask = mask.masked_fill(mask == 1, float("-inf"))
    return mask


def check_self_attn(round):
    S, L, N, E = 100, 100, 8, 256
    self_attn = ApsMultiheadAttention(E, 4, dropout=0)
    self_attn.train()
    key = th.rand(S, N, E)
    value = th.rand(S, N, E)
    query = th.rand(L, N, E)

    key_len = th.randint(S // 2, S, (N,))
    key_len[0] = S
    key_padding_mask = padding_mask(key_len)
    attn_mask = prep_sub_mask(S)

    my1, my2 = self_attn(query,
                         key,
                         value,
                         key_padding_mask=key_padding_mask,
                         attn_mask=attn_mask)
    th1, th2 = self_attn.torch_forward(query,
                                       key,
                                       value,
                                       key_padding_mask=key_padding_mask,
                                       attn_mask=attn_mask)
    assert my1.shape == th1.shape
    assert my2.shape == th2.shape
    th.testing.assert_allclose(my2, th2)
    th.testing.assert_allclose(my1, th1)
    print(f"Test ApsMultiheadAttention Pass - round: {round}")


if __name__ == "__main__":
    for i in range(4):
        check_self_attn(i)
