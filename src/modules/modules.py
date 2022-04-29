import copy
import datetime
import time
import sys
import torch
import models
from collections import OrderedDict
from config import cfg
from utils import to_device, collate, make_optimizer, make_scheduler


class SparsityIndex:
    def __init__(self, q):
        self.q = q
        self.si = {'neuron': [], 'layer': [], 'global': []}

    def sparsity_index(self, x, q, dim):
        d = float(x.size(dim))
        si = (torch.linalg.norm(x, 1, dim=dim).pow(1) / d).pow(1) / \
             (torch.linalg.norm(x, q, dim=dim).pow(q) / d).pow(1 / q)
        ub = min(torch.finfo(x.dtype).max, d ** (1 / q - 1))
        si[si.isnan()] = ub
        return si

    def make_sparsity_index(self, model):
        self.si['neuron'].append(self.make_sparsity_index_(model, 'neuron'))
        self.si['layer'].append(self.make_sparsity_index_(model, 'layer'))
        self.si['global'].append(self.make_sparsity_index_(model, 'global'))
        return

    def make_sparsity_index_(self, model, mode):
        if mode == 'neuron':
            si = []
            for i in range(len(self.q)):
                si_i = OrderedDict()
                for name, param in model.state_dict().items():
                    parameter_type = name.split('.')[-1]
                    if 'weight' in parameter_type:
                        si_i[name] = self.sparsity_index(param, self.q[i], -1)
                si.append(si_i)
        elif mode == 'layer':
            si = []
            for i in range(len(self.q)):
                si_i = OrderedDict()
                for name, param in model.state_dict().items():
                    parameter_type = name.split('.')[-1]
                    if 'weight' in parameter_type:
                        si_i[name] = self.sparsity_index(param.view(-1), self.q[i], -1)
                si.append(si_i)
        elif mode == 'global':
            si = []
            for i in range(len(self.q)):
                param_all = []
                for name, param in model.state_dict().items():
                    parameter_type = name.split('.')[-1]
                    if 'weight' in parameter_type:
                        param_all.append(param.view(-1))
                param_all = torch.cat(param_all, dim=0)
                si_i = self.sparsity_index(param_all, self.q[i], -1)
                si.append(si_i)
        else:
            raise ValueError('Not valid mode')
        return si


class Norm:
    def __init__(self, q):
        self.q = q
        self.norm = []

    def make_norm(self, model):
        norm = []
        for i in range(len(self.q)):
            norm_i = OrderedDict()
            for name, param in model.state_dict().items():
                parameter_type = name.split('.')[-1]
                if 'weight' in parameter_type:
                    norm_i[name] = torch.linalg.norm(param, self.q[i], dim=-1)
            norm.append(norm_i)
        self.norm.append(norm)
        return


class Compression:
    def __init__(self, prune_ratio, prune_mode):
        self.init_model_state_dict = models.load_init_state_dict(cfg['seed'])
        self.mask = [self.make_mask(self.init_model_state_dict)]
        self.prune_ratio = prune_ratio
        self.prune_mode = prune_mode

    def make_mask(self, model_state_dict):
        mask = OrderedDict()
        for name, param in model_state_dict.items():
            parameter_type = name.split('.')[-1]
            if 'weight' in parameter_type:
                mask[name] = param.new_ones(param.size(), dtype=torch.bool)
        return mask

    def prune(self, model):
        if self.prune_mode[1] == 'neuron':
            new_mask = OrderedDict()
            for name, param in model.named_parameters():
                parameter_type = name.split('.')[-1]
                if 'weight' in parameter_type:
                    mask = self.mask[-1][name]
                    masked_param = param.clone()
                    masked_param[mask] = float('nan')
                    pivot_param_i = masked_param.abs()
                    percentile_value = torch.nanquantile(pivot_param_i, self.prune_ratio, dim=-1, keepdim=True)
                    percentile_mask = (param.data.abs() < percentile_value).to('cpu')
                    new_mask[name] = torch.where(percentile_mask, False, mask)
                    param.data = torch.where(new_mask[name].to(param.device), param.data,
                                             torch.tensor(0, dtype=torch.float, device=param.device))
        elif self.prune_mode[1] == 'layer':
            new_mask = OrderedDict()
            for name, param in model.named_parameters():
                parameter_type = name.split('.')[-1]
                if 'weight' in parameter_type:
                    mask = self.mask[-1][name]
                    masked_param = param[mask]
                    pivot_param_i = masked_param.abs()
                    percentile_value = torch.quantile(pivot_param_i, self.prune_ratio)
                    percentile_mask = (param.data.abs() < percentile_value).to('cpu')
                    new_mask[name] = torch.where(percentile_mask, False, mask)
                    param.data = torch.where(new_mask[name].to(param.device), param.data,
                                             torch.tensor(0, dtype=torch.float, device=param.device))
        elif self.prune_mode[1] == 'global':
            pivot_param = []
            for name, param in model.named_parameters():
                parameter_type = name.split('.')[-1]
                if 'weight' in parameter_type:
                    mask = self.mask[-1][name]
                    masked_param = param[mask]
                    pivot_param_i = masked_param.abs()
                    pivot_param.append(pivot_param_i.view(-1))
            pivot_param = torch.cat(pivot_param, dim=0)
            percentile_value = torch.quantile(pivot_param, self.prune_ratio)
            new_mask = OrderedDict()
            for name, param in model.named_parameters():
                parameter_type = name.split('.')[-1]
                if 'weight' in parameter_type:
                    mask = self.mask[-1][name]
                    percentile_mask = (param.data.abs() < percentile_value).to('cpu')
                    new_mask[name] = torch.where(percentile_mask, False, mask)
                    param.data = torch.where(new_mask[name].to(param.device), param.data,
                                             torch.tensor(0, dtype=torch.float, device=param.device))
        else:
            raise ValueError('Not valid prune mode')
        self.mask.append(new_mask)
        return

    def init(self, model):
        for name, param in model.named_parameters():
            parameter_type = name.split('.')[-1]
            if 'weight' in parameter_type:
                mask = self.mask[-1][name]
                param.data = torch.where(mask, self.init_model_state_dict[name],
                                         torch.tensor(0, dtype=torch.float)).to(param.device)
            if "bias" in parameter_type:
                param.data = self.init_model_state_dict[name].to(param.device)
        return

    def freeze_grad(self, model):
        for name, param in model.named_parameters():
            parameter_type = name.split('.')[-1]
            if 'weight' in parameter_type:
                mask = self.mask[-1][name]
                param.grad.data = torch.where(mask.to(param.device), param.grad.data,
                                              torch.tensor(0, dtype=torch.float, device=param.device))
        return
