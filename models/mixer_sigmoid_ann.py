import torch.nn as nn

from spikingjelly.activation_based import layer
from einops.layers.torch import Rearrange, Reduce
from models.layers import BatchNorm1d


class MlpBlock(nn.Module):
    def __init__(self, dim, hidden_dim, bn_dim):
        super(MlpBlock, self).__init__()
        self.skip_bn = BatchNorm1d(bn_dim)

        self.mlp = nn.Sequential(
            layer.Linear(dim, hidden_dim),
            BatchNorm1d(bn_dim),
            nn.Sigmoid(),
            layer.Linear(hidden_dim, dim),
            BatchNorm1d(bn_dim)
        )

        self.lif = nn.Sigmoid()

    def forward(self, x):
        return self.lif(self.skip_bn(x) + self.mlp(x))


class MixerBlock(nn.Module):
    def __init__(self, config):
        super(MixerBlock, self).__init__()
        self.model = nn.Sequential(
            Rearrange('t b n c -> t b c n'),
            MlpBlock(config.n_patches, config.token_hidden_dim, config.hidden_dim),
            Rearrange('t b c n -> t b n c'),
            MlpBlock(config.hidden_dim, config.channel_hidden_dim, config.n_patches)
        )

    def forward(self, x):
        return self.model(x)


class MixerNet(nn.Module):
    def __init__(self, config):
        super(MixerNet, self).__init__()
        config.n_patches = (config.img_size // config.patch_size) ** 2

        self.model = nn.Sequential(
            layer.Conv2d(3, config.encode_dim, kernel_size=3, stride=1, padding=1),
            layer.BatchNorm2d(config.encode_dim),
            nn.Sigmoid(),

            Rearrange('t b c (h p1) (w p2) -> t b (h w) (p1 p2 c)', p1=config.patch_size, p2=config.patch_size),
            layer.Linear((config.patch_size ** 2) * config.encode_dim, config.hidden_dim),
            BatchNorm1d(config.n_patches),
            nn.Sigmoid(),

            *[MixerBlock(config) for _ in range(config.num_blocks)],

            BatchNorm1d(config.n_patches),
            Reduce('t b n c -> t b c', 'mean'),
            nn.Sigmoid(),
            layer.Linear(config.hidden_dim, config.num_classes)
        )

    def forward(self, x):
        return self.model(x)