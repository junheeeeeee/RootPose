## Our PoseFormer model was revised from https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/vision_transformer.py

import math
import logging
from functools import partial
from collections import OrderedDict
from einops import rearrange, repeat
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import time

from math import sqrt

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.helpers import load_pretrained
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.registry import register_model
from model.rela import RectifiedLinearAttention
from model.routing_transformer import KmeansAttention
from model.linearattention import LinearMultiheadAttention

import torch
import torch.nn as nn

class HybridArithmeticLayer(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0., changedim=False, currentdim=0, depth=0):
        """
        nn.Linear를 활용하여 Signed Logarithm 기반 곱셈과 나눗셈을 동시에 처리.
        Args:
            input_dim (int): 입력 차원.
            output_dim (int): 출력 차원.
            epsilon (float): 수치적 안정성을 위한 작은 값.
        """
        super(HybridArithmeticLayer, self).__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.linear = nn.Linear(in_features, out_features)
        self.logW = nn.Parameter(torch.zeros(in_features, out_features))

        self.act = act_layer()
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        # 선형 변환 수행 (일반 선형)
        x_l = self.linear(x)
        x_l = self.act(x_l)
        x_l = self.drop(x_l)

        # 로그 기반 변환 수행
        sign = torch.sign(x)
        sign = torch.where(sign == 0, torch.tensor(1.0, device=x.device), sign)
        x = (abs(x) + 1) * sign
        x = torch.log(x.to(torch.complex64))   # 부호와 로그 변환
        x = torch.matmul(x, self.logW.to(torch.complex64)) # 로그 변환된 입력에 가중치를 곱하고 편향을 더함
        x = torch.exp(x)  # 로그의 역함수인 지수 변환
        x = x.real.to(torch.float32) + x.imag.to(torch.float32) * 0
        sign_exp = torch.sign(x)
        x = (abs(x) - 1) * sign_exp
        x = self.drop(x) + x_l
        return x

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0., changedim=False, currentdim=0, depth=0):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        # self.changedim = changedim
        # self.currentdim = currentdim
        # self.depth = depth
        # if self.changedim:
        #     assert self.depth>0
        self.fc1 = nn.Linear(in_features, hidden_features)
        # nn.init.kaiming_normal_(self.fc1.weight)
        # torch.nn.init.xavier_uniform_(self.fc1.weight)
        # torch.nn.init.normal_(self.fc1.bias, std = 1e-6)
        
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        # nn.init.kaiming_normal_(self.fc2.weight)
        # torch.nn.init.xavier_uniform_(self.fc2.weight)
        # torch.nn.init.normal_(self.fc2.bias, std = 1e-6)
        
        self.drop = nn.Dropout(drop)
        # if self.changedim and self.currentdim <= self.depth//2:
        #     self.reduction = nn.Linear(out_features, out_features//2)
        # elif self.changedim and self.currentdim > self.depth//2:
        #     self.improve = nn.Linear(out_features, out_features*2)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        # if self.changedim and self.currentdim <= self.depth//2:
        #     x = self.reduction(x)
        # elif self.changedim and self.currentdim > self.depth//2:
        #     x = self.improve(x)
        return x

class STEQuantize(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        return torch.round(input)  # 정수화

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output  # 그레디언트를 그대로 전달

class QuantizeLayer(nn.Module):
    def __init__(self):
        super(QuantizeLayer, self).__init__()

    def forward(self, x):
        return STEQuantize.apply(x)

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., comb=False, vis=False):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        # nn.init.kaiming_normal_(self.qkv.weight)
        # torch.nn.init.xavier_uniform_(self.qkv.weight)
        # torch.nn.init.zeros_(self.qkv.bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        # nn.init.kaiming_normal_(self.proj.weight)
        # torch.nn.init.xavier_uniform_(self.proj.weight)
        # torch.nn.init.zeros_(self.proj.bias)   

        self.proj_drop = nn.Dropout(proj_drop)
        self.comb = comb
        self.vis = vis
        self.sig = nn.Sigmoid()
        self.quantize = QuantizeLayer()

    def forward(self, x, vis=False):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # Now x shape (3, B, heads, N, C//heads)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)
        if self.comb==True:
            attn = (q.transpose(-2, -1) @ k) * self.scale
        elif self.comb==False:
            attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        # attn = self.binary_atten(attn)
        
        # attn = self.quantize(self.sig(attn))
        attn = self.attn_drop(attn)
        if self.comb==True:
            x = (attn @ v.transpose(-2, -1)).transpose(-2, -1)
            # print(x.shape)
            x = rearrange(x, 'B H N C -> B N (H C)')
            # print(x.shape)
        elif self.comb==False:
            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

    def binary_atten(self, attn):
        # attn = torch.where(attn > 0.5, (torch.ones_like(attn) - attn).detach() + attn, torch.zeros_like(attn).detach())
        # attn = (torch.where(attn > 0.5, torch.ones_like(attn), torch.zeros_like(attn)) - attn).detach() + attn 
        attn = self.sig(attn).round()
        attn = attn / attn.sum(dim=-1, keepdim=True).clamp(min=1)
        return attn
    
class BiasAttention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., comb=False, vis=False, pose_num = 17):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        # nn.init.kaiming_normal_(self.qkv.weight)
        # torch.nn.init.xavier_uniform_(self.qkv.weight)
        # torch.nn.init.zeros_(self.qkv.bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        # nn.init.kaiming_normal_(self.proj.weight)
        # torch.nn.init.xavier_uniform_(self.proj.weight)
        # torch.nn.init.zeros_(self.proj.bias)   

        self.proj_drop = nn.Dropout(proj_drop)
        self.comb = comb
        self.vis = vis
        self.sig = nn.Sigmoid()
        self.quantize = QuantizeLayer()


    def forward(self, x, atten_bias,vis=False):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # Now x shape (3, B, heads, N, C//heads)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)
        if self.comb==True:
            attn = (q.transpose(-2, -1) @ k) * self.scale
        elif self.comb==False:
            attn = (q @ k.transpose(-2, -1)) * self.scale

        attn = attn * self.sig(atten_bias[None, None, :, :])
        attn = attn.softmax(dim=-1)
        # attn = self.binary_atten(attn)
        
        # attn = self.quantize(self.sig(attn))
        attn = self.attn_drop(attn)
        
        if self.comb==True:
            x = (attn @ v.transpose(-2, -1)).transpose(-2, -1)
            # print(x.shape)
            x = rearrange(x, 'B H N C -> B N (H C)')
            # print(x.shape)
        elif self.comb==False:
            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class TemporalAttention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., comb=False, vis=False):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        # nn.init.kaiming_normal_(self.qkv.weight)
        # torch.nn.init.xavier_uniform_(self.qkv.weight)
        # torch.nn.init.zeros_(self.qkv.bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        # nn.init.kaiming_normal_(self.proj.weight)
        # torch.nn.init.xavier_uniform_(self.proj.weight)
        # torch.nn.init.zeros_(self.proj.bias)   

        self.proj_drop = nn.Dropout(proj_drop)
        self.comb = comb
        self.vis = vis

    def forward(self, x, vis=False):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # Now x shape (3, B, heads, N, C//heads)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)
        interval = q.shape[-2]/3
        q1, q2, q3 = q[0:interval], q[interval:interval*2], q[interval*2:interval*3]
        if self.comb==True:
            attn = (q.transpose(-2, -1) @ k) * self.scale
        elif self.comb==False:
            attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        
        if self.comb==True:
            x = (attn @ v.transpose(-2, -1)).transpose(-2, -1)
            # print(x.shape)
            x = rearrange(x, 'B H N C -> B N (H C)')
            # print(x.shape)
        elif self.comb==False:
            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class ProbMask():
    def __init__(self, B, H, L, index, scores, device="cpu"):
        _mask = torch.ones(L, scores.shape[-1], dtype=torch.bool).to(device).triu(1)
        _mask_ex = _mask[None, None, :].expand(B, H, L, scores.shape[-1])
        indicator = _mask_ex[torch.arange(B)[:, None, None],
                             torch.arange(H)[None, :, None],
                             index, :].to(device)
        self._mask = indicator.view(scores.shape).to(device)
    
    @property
    def mask(self):
        return self._mask

class ProbAttention(nn.Module):
    def __init__(self, dim, num_heads=8, factor=5, qkv_bias=False, qk_scale=None, attn_drop=0.1,  
        proj_drop=0., comb=False, vis=False, output_attention=False, mask_flag=False, mix=True):
        super().__init__()
    # def __init__(self, mask_flag=True, factor=5, scale=None, attention_dropout=0.1, output_attention=False):
    #     super(ProbAttention, self).__init__()
        self.factor = factor
        self.scale = qk_scale or ((dim // num_heads) ** -0.5)
        self.mask_flag = mask_flag
        self.output_attention = output_attention
        self.dropout = nn.Dropout(attn_drop)
        self.num_heads = num_heads
        self.mix = mix

        self.query_projection = nn.Linear(dim, dim)
        self.key_projection = nn.Linear(dim, dim)
        self.value_projection = nn.Linear(dim, dim)

        self.proj = nn.Linear(dim, dim)
        # self.proj_drop = nn.Dropout(proj_drop)

    def _prob_QK(self, Q, K, sample_k, n_top): # n_top: c*ln(L_q)
        # Q [B, H, L, D]
        B, H, L_K, E = K.shape
        _, _, L_Q, _ = Q.shape

        # calculate the sampled Q_K
        K_expand = K.unsqueeze(-3).expand(B, H, L_Q, L_K, E)
        index_sample = torch.randint(L_K, (L_Q, sample_k)) # real U = U_part(factor*ln(L_k))*L_q
        K_sample = K_expand[:, :, torch.arange(L_Q).unsqueeze(1), index_sample, :]
        Q_K_sample = torch.matmul(Q.unsqueeze(-2), K_sample.transpose(-2, -1)).squeeze()

        # find the Top_k query with sparisty measurement
        M = Q_K_sample.max(-1)[0] - torch.div(Q_K_sample.sum(-1), L_K)
        M_top = M.topk(n_top, sorted=False)[1]

        # use the reduced Q to calculate Q_K
        Q_reduce = Q[torch.arange(B)[:, None, None],
                     torch.arange(H)[None, :, None],
                     M_top, :] # factor*ln(L_q)
        Q_K = torch.matmul(Q_reduce, K.transpose(-2, -1)) # factor*ln(L_q)*L_k

        return Q_K, M_top

    def _get_initial_context(self, V, L_Q):
        B, H, L_V, D = V.shape
        if not self.mask_flag:
            # V_sum = V.sum(dim=-2)
            V_sum = V.mean(dim=-2)
            contex = V_sum.unsqueeze(-2).expand(B, H, L_Q, V_sum.shape[-1]).clone()
        else: # use mask
            assert(L_Q == L_V) # requires that L_Q == L_V, i.e. for self-attention only
            contex = V.cumsum(dim=-2)
        return contex

    def _update_context(self, context_in, V, scores, index, L_Q, attn_mask):
        B, H, L_V, D = V.shape

        if self.mask_flag:
            attn_mask = ProbMask(B, H, L_Q, index, scores, device=V.device)
            scores.masked_fill_(attn_mask.mask, -np.inf)

        attn = torch.softmax(scores, dim=-1) # nn.Softmax(dim=-1)(scores)

        context_in[torch.arange(B)[:, None, None],
                   torch.arange(H)[None, :, None],
                   index, :] = torch.matmul(attn, V).type_as(context_in)
        if self.output_attention:
            attns = (torch.ones([B, H, L_V, L_V])/L_V).type_as(attn).to(attn.device)
            attns[torch.arange(B)[:, None, None], torch.arange(H)[None, :, None], index, :] = attn
            return (context_in, attns)
        else:
            return (context_in, None)

    def forward(self, x, attn_mask=None, vis=False):
        B, N, C = x.shape
        queries = self.query_projection(x).view(B, N, self.num_heads, -1)
        keys = self.key_projection(x).view(B, N, self.num_heads, -1)
        values = self.value_projection(x).view(B, N, self.num_heads, -1)
        # qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # Now x shape (3, B, heads, N, C//heads)
        # queries, keys, values = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        B, L_Q, H, D = queries.shape
        _, L_K, _, _ = keys.shape

        queries = queries.transpose(2,1)
        keys = keys.transpose(2,1)
        values = values.transpose(2,1)

        U_part = self.factor * np.ceil(np.log(L_K)).astype('int').item() # c*ln(L_k)
        u = self.factor * np.ceil(np.log(L_Q)).astype('int').item() # c*ln(L_q) 
        # U_part = self.factor * np.ceil(np.log(L_K)).numpy().astype('int').item() # c*ln(L_k)
        # u = self.factor * np.ceil(np.log(L_Q)).numpy().astype('int').item() # c*ln(L_q) 

        U_part = U_part if U_part<L_K else L_K
        u = u if u<L_Q else L_Q
        
        scores_top, index = self._prob_QK(queries, keys, sample_k=U_part, n_top=u) 

        # add scale factor
        scale = self.scale or 1./sqrt(D)
        scores_top = scores_top * scale
        # get the context
        context = self._get_initial_context(values, L_Q)
        # update the context with selected top_k queries
        context, attn = self._update_context(context, values, scores_top, index, L_Q, attn_mask)

        context = context.transpose(2,1).contiguous()

        if self.mix:
            context = context.transpose(2,1).contiguous()
        
        context = context.reshape(B, N, -1)

        context = self.proj(context)
        # context = self.proj_drop(context)

        return context

        # if self.output_attention:
        #     return context, attn
        # else:
        #     return context


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., attention=Attention, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, comb=False, changedim=False, currentdim=0, depth=0, vis=False):
        super().__init__()

        self.changedim = changedim
        self.currentdim = currentdim
        self.depth = depth
        if self.changedim:
            assert self.depth>0

        self.norm1 = norm_layer(dim)
        self.attn = attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, comb=comb, vis=vis)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        
        if self.changedim and self.currentdim < self.depth//2:
            self.reduction = nn.Conv1d(dim, dim//2, kernel_size=1)
            # self.reduction = nn.Linear(dim, dim//2)
        elif self.changedim and depth > self.currentdim > self.depth//2:
            self.improve = nn.Conv1d(dim, dim*2, kernel_size=1)
            # self.improve = nn.Linear(dim, dim*2)
        self.vis = vis

    def forward(self, x, vis=False):
        x = x + self.drop_path(self.attn(self.norm1(x), vis=vis))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        
        if self.changedim and self.currentdim < self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.reduction(x)
            x = rearrange(x, 'b c t -> b t c')
        elif self.changedim and self.depth > self.currentdim > self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.improve(x)
            x = rearrange(x, 'b c t -> b t c')
        return x

class BlockHy(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., attention=Attention, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, comb=False, changedim=False, currentdim=0, depth=0, vis=False):
        super().__init__()

        self.changedim = changedim
        self.currentdim = currentdim
        self.depth = depth
        if self.changedim:
            assert self.depth>0

        self.norm1 = norm_layer(dim)
        self.attn = attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, comb=comb, vis=vis)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = HybridArithmeticLayer(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        
        if self.changedim and self.currentdim < self.depth//2:
            self.reduction = nn.Conv1d(dim, dim//2, kernel_size=1)
            # self.reduction = nn.Linear(dim, dim//2)
        elif self.changedim and depth > self.currentdim > self.depth//2:
            self.improve = nn.Conv1d(dim, dim*2, kernel_size=1)
            # self.improve = nn.Linear(dim, dim*2)
        self.vis = vis

    def forward(self, x, vis=False):
        x = x + self.drop_path(self.attn(self.norm1(x), vis=vis))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        
        if self.changedim and self.currentdim < self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.reduction(x)
            x = rearrange(x, 'b c t -> b t c')
        elif self.changedim and self.depth > self.currentdim > self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.improve(x)
            x = rearrange(x, 'b c t -> b t c')
        return x

class BiasBlock(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., attention=BiasAttention, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, comb=False, changedim=False, currentdim=0, depth=0, vis=False):
        super().__init__()

        self.changedim = changedim
        self.currentdim = currentdim
        self.depth = depth
        if self.changedim:
            assert self.depth>0

        self.norm1 = norm_layer(dim)
        self.attn = attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, comb=comb, vis=vis)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        
        if self.changedim and self.currentdim < self.depth//2:
            self.reduction = nn.Conv1d(dim, dim//2, kernel_size=1)
            # self.reduction = nn.Linear(dim, dim//2)
        elif self.changedim and depth > self.currentdim > self.depth//2:
            self.improve = nn.Conv1d(dim, dim*2, kernel_size=1)
            # self.improve = nn.Linear(dim, dim*2)
        self.vis = vis

    def forward(self, x, bias_attaion,vis=False):
        x = x + self.drop_path(self.attn(self.norm1(x), bias_attaion,vis=vis))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        
        if self.changedim and self.currentdim < self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.reduction(x)
            x = rearrange(x, 'b c t -> b t c')
        elif self.changedim and self.depth > self.currentdim > self.depth//2:
            x = rearrange(x, 'b t c -> b c t')
            x = self.improve(x)
            x = rearrange(x, 'b c t -> b t c')
        return x

class TemporalBlock(nn.Module):
    
    def __init__(self, dim, num_heads, mlp_ratio=4., attention=Attention, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, comb=False, changedim=False, currentdim=0, depth=0, vis=False):
        super().__init__()

        self.changedim = changedim
        self.currentdim = currentdim
        self.depth = depth
        if self.changedim:
            assert self.depth>0

        self.norm1 = norm_layer(dim)
        self.attn = attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, comb=comb, vis=vis)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        
        if self.changedim and self.currentdim < self.depth//2:
            self.reduction = nn.Conv1d(dim, dim//2, kernel_size=1)
            # self.reduction = nn.Linear(dim, dim//2)
        elif self.changedim and depth > self.currentdim > self.depth//2:
            self.improve = nn.Conv1d(dim, dim*2, kernel_size=1)
            # self.improve = nn.Linear(dim, dim*2)
        self.vis = vis
        self.reduction = nn.Linear(dim, dim//2)

    def forward(self, x, vis=False):
        x = x + self.drop_path(self.attn(self.norm1(x), vis=vis))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        # print(x.shape)
        # x = rearrange(x, 'b t c -> b c t')
        x = self.reduction(x)
        # print(x.shape)
        # x = rearrange(x, 'b c t -> b t c')
        return x

class attn_pooling(nn.Module):
    def __init__(self, in_feature, out_feature, stride=2, padding_mode='zeros'):
        super(attn_pooling, self).__init__()

        self.conv = nn.Conv1d(in_feature, out_feature, kernel_size=stride + 1,
                              padding=stride // 2, stride=stride,
                              padding_mode=padding_mode, groups=in_feature)
        self.fc = nn.Linear(in_feature, out_feature)

    def forward(self, x):

        x = self.conv(x)

        return x

class  MixSTE2(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , 3),
        )
        # self.head = nn.Sequential(OrderedDict([
        #     ('act', nn.Tanh()),
        #     ('fc2', nn.Linear(embed_dim , 3)),
        # ]))
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)

        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)
        x[..., 0, :] *= 0  # related root joint

        return x

class  MixSTE3(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            BlockHy(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            BlockHy(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            HybridArithmeticLayer(in_features = embed_dim , out_features = 3),
        )
        # self.head = nn.Sequential(OrderedDict([
        #     ('act', nn.Tanh()),
        #     ('fc2', nn.Linear(embed_dim , 3)),
        # ]))
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)

        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)
        x[..., 0, :] *= 0  # related root joint

        return x

class  CSTE(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)

        self.cam_mlp = nn.Linear(4, embed_dim_ratio)

        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [xx.item() for xx in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , out_dim),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x, cam):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed

        cam_embed = self.cam_mlp(cam[:,:4])[:, None, None, :].repeat(1,f,n,1)
        cam_embed = rearrange(cam_embed, 'b f n c  -> (b f) n c', )
        x += cam_embed

        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x, cam):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x, cam)

        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)
        x[..., 0, :] *= 0  # related root joint

        return x

class  MixSTE_seperate(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , out_dim),
        )
        self.head_bone = nn.Sequential(
            nn.LayerNorm(embed_dim * num_joints),
            nn.Linear(embed_dim * num_joints, 16),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def change_bone_length(self, p3d, bone_length):
        index = [[0,1],[1,2],[2,3],[0,4],[4,5],[5,6],[0,7],[7,8],[8,9],[9,10],[8,11],[11,12],[12,13],[8,14], [14,15],[15,16]]
        bone = []
        for i in index:
            bone.append((p3d[:, :, i[1]] - p3d[:, :, i[0]]) / torch.max(torch.linalg.norm(p3d[:, :, i[1]] - p3d[:, :, i[0]], axis=-1, keepdims=True), torch.tensor([1e-6]).cuda()))
        
        for i in range(len(index)):
            p3d[:, :, index[i][1]] = p3d[:, :, index[i][0]] + bone[i] * bone_length[:, None, i, None]

        return p3d
    
    def get_bone_length(self, p3d):
        index = [[0,1],[1,2],[2,3],[0,4],[4,5],[5,6],[0,7],[7,8],[8,9],[9,10],[8,11],[11,12],[12,13],[8,14], [14,15],[15,16]]
        bone = []
        for i in index:
            bone.append(torch.linalg.norm(p3d[:, :, i[1]] - p3d[:, :, i[0]], axis=-1))
        bone = torch.stack(bone, axis=-1)
        return bone

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # with torch.no_grad():
            
        x = self.STE_forward(x)

        x = self.TTE_foward(x)

        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)

        x = self.ST_foward(x)

        p3d = self.head(x)

        # bone = self.get_bone_length(p3d).mean(1)
        # print(p3d.shape)
        # print(x.shape)

        bone = abs(self.head_bone(x.reshape(b,f, -1))).mean(1)

        p3d = self.change_bone_length(p3d, bone)

        p3d = p3d.view(b, f, n, -1)
        
        if self.training:
            return p3d, bone
        else:
            return p3d

class  MixSTE_conf(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim, out_dim),
        )
        self.head_bone = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim, 1),
        )
        self.head_bone_conf = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim, 1),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth-1):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        return x
    
    def BJ_forward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        
        x_j = rearrange(x, 'b f n cw -> (b f) n cw')
        x_b = rearrange(x, 'b f n cw -> (b n) f cw')
        steblock = self.STEblocks[-1]
        tteblock = self.TTEblocks[-1]
        
        x_j = steblock(x_j)
        x_j = self.Spatial_norm(x_j)
        x_j = rearrange(x_j, '(b f) n cw -> b f n cw', f=f)
        x_j = self.head(x_j).reshape(b, f, n, -1)

        x_b = tteblock(x_b)
        x_b = self.Temporal_norm(x_b)
        x_b = rearrange(x_b, '(b n) f cw -> b f n cw', n=n)

        bone = torch.clamp(nn.Softplus()(self.head_bone(x_b.clone())[:,:,1:,0]), min=1e-6)
        bone_conf = nn.Softmax(dim = 1)(self.head_bone_conf(x_b)[:,:,1:,0])

        bone = (bone * bone_conf).mean(dim = 1, keepdim=True)
        bone[..., :3] = bone[..., 3:6] / 2 + bone[..., :3] / 2
        bone[..., 10:13] = bone[..., 13:16] / 2 + bone[..., 10:13] / 2
        bone[..., 3:6] = bone[..., :3] * 1
        bone[..., 13:16] = bone[..., 10:13] * 1

        x, conf = self.change_bone_length(x_j, bone)
        x = x.view(b, f, n, -1)
        
        return x, bone, conf, bone_conf

        


    def change_bone_length(self, p3d, bone_length):
        index = [[0,1],[1,2],[2,3],[0,4],[4,5],[5,6],[0,7],[7,8],[8,9],[9,10],[8,11],[11,12],[12,13],[8,14], [14,15],[15,16]]
        bone = []
        conf = []
        for i in index:
            conf.append(torch.max(torch.linalg.norm(p3d[:, :, i[1]] - p3d[:, :, i[0]], axis=-1, keepdims=True), torch.tensor([1e-6]).cuda()))
            bone.append((p3d[:, :, i[1]] - p3d[:, :, i[0]]) / conf[-1])
        
        for i in range(len(index)):
            p3d[:, :, index[i][1]] = p3d[:, :, index[i][0]] + bone[i] * bone_length[:, :, i, None]
        conf = nn.Sigmoid()(torch.stack(conf, axis=-1).permute(0, 1, 3, 2))
        conf = torch.cat([torch.ones_like(conf[:, :, :1]), conf], dim=-2)
        return p3d, conf
    
    def get_bone_length(self, p3d):
        index = [[0,1],[1,2],[2,3],[0,4],[4,5],[5,6],[0,7],[7,8],[8,9],[9,10],[8,11],[11,12],[12,13],[8,14], [14,15],[15,16]]
        bone = []
        for i in index:
            bone.append(torch.linalg.norm(p3d[:, :, i[1]] - p3d[:, :, i[0]], axis=-1))
        bone = torch.stack(bone, axis=-1)
        return bone

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # with torch.no_grad():
            
        x = self.STE_forward(x)

        x = self.TTE_foward(x)

        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)

        x = self.ST_foward(x)

        p3d, bone, conf, bone_conf = self.BJ_forward(x)

        return p3d

class Cross_Linformer(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None, shared_projection=False, k=64):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.shared_projection = shared_projection
        self.k = k if k is not None else num_frame // 4
        proj_shape = [num_frame, self.k]

        if self.shared_projection:
            self.E = torch.nn.Parameter(torch.randn(proj_shape), requires_grad=True)
            
        self.TTEblocks = nn.ModuleList([
            LinformerBlock(
                dim=embed_dim_ratio, heads=num_heads, shared_projection=self.shared_projection,
                dim_linear_block=int(mlp_ratio)*embed_dim_ratio, proj_shape=proj_shape, trainable_proj=False
            )
            for _ in range(depth)])
            # Block(
            #     dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
            #     drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            # for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
         
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , out_dim),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        if self.shared_projection:
            x = self.TTEblocks[0](x, self.E)
        else:
            blk = self.TTEblocks[0]
            x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)
        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)

        return x
    
# RectifiedLinearAttention implementation in temporal
class  MixSTERELA(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, attention=Attention, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, attention=RectifiedLinearAttention, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , out_dim),
        )


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)
        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)

        return x


class  MixSTEPooling(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=8,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        midep = int(depth//2)

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio//2**i, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(midep)
        ])
        self.STEblocks.extend([
            Block(
                dim=embed_dim_ratio//2**(depth-i-1), num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(midep, depth)
        ])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim//2**i, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=True, currentdim=i+1, depth=depth)
            for i in range(midep)
        ])
        self.TTEblocks.extend([
            Block(
                # 2**(i+1-(i+1-depth/2)*2) = 2**(depth-i-1)
                dim=embed_dim//2**(depth-i-1), num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=True, currentdim=i+1, depth=depth)
            for i in range(midep, depth)
        ])
        # print(self.TTEblocks)

        self.Spatial_norm = nn.ModuleList([
            norm_layer(embed_dim_ratio//2**(i))
            for i in range(midep)
        ])
        self.Spatial_norm.extend([
            norm_layer(embed_dim_ratio//2**(depth-i-1))
            for i in range(midep, depth)
        ])

        self.Temporal_norm = nn.ModuleList([
            norm_layer(embed_dim//2**(i+1))
            for i in range(midep-1)
        ])
        self.Temporal_norm.extend([
            norm_layer(embed_dim//2**(depth-i-2))
            for i in range(midep-1, depth-1)
        ])
        self.Temporal_norm.extend([
            norm_layer(embed_dim)
        ])

        # self.Spatial_norm = norm_layer(embed_dim_ratio)
        # self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
         
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)

        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , out_dim),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)

        spatial_norm = self.Spatial_norm[0]
        x = spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)

        temporal_norm = self.Temporal_norm[0]
        x = temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            snorm = self.Spatial_norm[i]
            tnorm = self.Temporal_norm[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            x = steblock(x)
            x = snorm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            x = tteblock(x)
            x = tnorm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)
        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()
        x = self.head(x)
        # et = time.time()
        # print('head  ', (et-st)*2000)
        # now x shape is (b f (n * 3))

        x = x.view(b, f, n, -1)

        return x

class  MixSTE_cross(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()

        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)
        self.bin_size = 128
        self.index_num = nn.Parameter(torch.linspace(-2, 2, self.bin_size).reshape(1, 1, 1, 1, self.bin_size), requires_grad=False)
        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , self.bin_size * 3),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)
        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()

        x = self.head(x).reshape(b, f, n , 3, self.bin_size) # cw : 512
        x = torch.softmax(x, dim=-1)
        out = torch.sum(x * self.index_num, dim=-1)
        
        # import matplotlib.pyplot as plt
        # distributions = x[:,:,:,2].detach().cpu()

        # plt.figure()
        # distribution = distributions[0, 0, 13].numpy()
        # plt.plot(np.linspace(-2, 2, self.bin_size), distribution)
        # mean_value = out[0, 0, 13, 2].item()
        # plt.axvline(x=mean_value, color='r', linestyle='-')
        # plt.xlabel('Bins')
        # plt.ylabel('Probability')
        # plt.title(f'Distribution for sample {0}, frame {0}, joint {13} (mean={mean_value:.2f})')
        # plt.show()

        return out
    
class  MixSTE_cross_logit(nn.Module):
    def __init__(self, num_frame=9, num_joints=17, in_chans=2, embed_dim_ratio=32, depth=4,
                 num_heads=8, mlp_ratio=2., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2,  norm_layer=None, logit=False):
        """    ##########hybrid_backbone=None, representation_size=None,
        Args:
            num_frame (int, tuple): input frame number
            num_joints (int, tuple): joints number
            in_chans (int): number of input channels, 2D joints have 2 channels: (x,y)
            embed_dim_ratio (int): embedding dimension ratio
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
        """
        super().__init__()
        self.logit = logit
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        embed_dim = embed_dim_ratio   #### temporal embed_dim is num_joints * spatial embedding dim ratio
        out_dim = 3     #### output dimension is num_joints * 3

        ### spatial patch embedding
        self.Spatial_patch_to_embedding = nn.Linear(in_chans, embed_dim_ratio)
        # self.Spatial_patch_to_embedding = nn.Conv1d(in_chans, embed_dim_ratio, kernel_size=1, stride=1)
        self.Spatial_pos_embed = nn.Parameter(torch.zeros(1, num_joints, embed_dim_ratio))
        # nn.init.kaiming_normal_(self.Spatial_pos_embed)
        # torch.nn.init.normal_(self.Spatial_pos_embed, std = .02)

        self.Temporal_pos_embed = nn.Parameter(torch.zeros(1, num_frame, embed_dim))
        # nn.init.kaiming_normal_(self.Temporal_pos_embed)
        # torch.nn.init.normal_(self.Temporal_pos_embed, std = .02)

        self.pos_drop = nn.Dropout(p=drop_rate)


        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.block_depth = depth

        self.STEblocks = nn.ModuleList([
            # Block: Attention Block
            Block(
                dim=embed_dim_ratio, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.TTEblocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, comb=False, changedim=False, currentdim=i+1, depth=depth)
            for i in range(depth)])

        self.Spatial_norm = norm_layer(embed_dim_ratio)
        self.Temporal_norm = norm_layer(embed_dim)

        ####### A easy way to implement weighted mean
        # self.weighted_mean = torch.nn.Conv1d(in_channels=num_frame, out_channels=num_frame, kernel_size=1)
        self.bin_size = 1001
        self.index_num = nn.Parameter(torch.linspace(-1.2, 1.2, self.bin_size).reshape(1, 1, 1, 1, self.bin_size), requires_grad=False)
        self.head = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim , self.bin_size * 3),
        )
        # nn.init.kaiming_normal_(self.head[1].weight)
        # torch.nn.init.xavier_uniform_(self.head[1].weight)
        # torch.nn.init.normal_(self.head[1].bias, std = 1e-6)


    def STE_forward(self, x):
        b, f, n, c = x.shape  ##### b is batch size, f is number of frames, n is number of joints, c is channel size?
        x = rearrange(x, 'b f n c  -> (b f) n c', )
        ### now x is [batch_size, receptive frames, joint_num, 2 channels]
        x = self.Spatial_patch_to_embedding(x)
        # x = rearrange(x, 'bnew c n  -> bnew n c', )
        x += self.Spatial_pos_embed
        x = self.pos_drop(x)

        blk = self.STEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)

        x = self.Spatial_norm(x)
        x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)
        return x

    def TTE_foward(self, x):
        assert len(x.shape) == 3, "shape is equal to 3"
        b, f, _  = x.shape
        x += self.Temporal_pos_embed
        x = self.pos_drop(x)
        blk = self.TTEblocks[0]
        x = blk(x)
        # x = blk(x, vis=True)
        # exit()

        x = self.Temporal_norm(x)
        return x

    def ST_foward(self, x):
        assert len(x.shape)==4, "shape is equal to 4"
        b, f, n, cw = x.shape
        for i in range(1, self.block_depth):
            x = rearrange(x, 'b f n cw -> (b f) n cw')
            steblock = self.STEblocks[i]
            tteblock = self.TTEblocks[i]
            
            # x += self.Spatial_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = steblock(x, vis=True)
            x = steblock(x)
            x = self.Spatial_norm(x)
            x = rearrange(x, '(b f) n cw -> (b n) f cw', f=f)

            # x += self.Temporal_pos_embed
            # x = self.pos_drop(x)
            # if i==7:
            #     x = tteblock(x, vis=True)
            #     exit()
            x = tteblock(x)
            x = self.Temporal_norm(x)
            x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        
        # x = rearrange(x, 'b f n cw -> (b n) f cw', n=n)
        # x = self.weighted_mean(x)
        # x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # x = x.view(b, f, -1)
        return x

    def forward(self, x):
        b, f, n, c = x.shape
        ### now x is [batch_size, 2 channels, receptive frames, joint_num], following image data
        # x shape:(b f n c)
        # torch.cuda.synchronize()
        # st = time.time()
        x = self.STE_forward(x)
        # now x shape is (b n) f cw
        # et = time.time()
        # print('STE_forward  ', (et-st)*2000)

        # st = time.time()
        x = self.TTE_foward(x)
        # et = time.time()
        # print('TTE_foward  ', (et-st)*2000)

        # now x shape is (b n) f cw
        x = rearrange(x, '(b n) f cw -> b f n cw', n=n)
        # st = time.time()
        x = self.ST_foward(x)
        # et = time.time()
        # print('ST_foward  ', (et-st)*2000)

        # st = time.time()

        x = self.head(x).reshape(b, f, n , 3, self.bin_size) # cw : 512
        x = torch.softmax(x, dim=-1)
        # x = x / x.sum(dim=-1, keepdim=True)
        out = torch.sum(x * self.index_num, dim=-1)
        
        # import matplotlib.pyplot as plt
        # distributions = x[:,:,:,2].detach().cpu()

        # plt.figure()
        # distribution = distributions[0, 0, 13].numpy()
        # plt.plot(np.linspace(-1.2, 1.2, self.bin_size), distribution)
        # mean_value = out[0, 0, 13, 2].item()
        # plt.axvline(x=mean_value, color='r', linestyle='-')
        # plt.xlabel('Bins')
        # plt.ylabel('Probability')
        # plt.title(f'Distribution for sample {0}, frame {0}, joint {13} (mean={mean_value})')
        # plt.show()
        if self.logit:
            return out, x
        else:
            return out 