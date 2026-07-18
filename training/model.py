"""Strip encoder: mobilenet_v3_large backbone + projection head -> 256-d.
Normalization is part of forward() so the exported ONNX takes raw 0..1 input."""
import torch
import torch.nn as nn
from torchvision.models import MobileNet_V3_Large_Weights, mobilenet_v3_large

from training.config import EMBED_DIM

_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
_STD = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


class StripEncoder(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        backbone = mobilenet_v3_large(weights=MobileNet_V3_Large_Weights.DEFAULT)
        self.features = backbone.features
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Sequential(
            nn.Linear(960, 512), nn.GELU(), nn.Linear(512, EMBED_DIM),
        )
        self.register_buffer("mean", _MEAN)
        self.register_buffer("std", _STD)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # x: [N,3,224,224] in 0..1
        x = (x - self.mean) / self.std
        x = self.pool(self.features(x)).flatten(1)
        x = self.head(x)
        return nn.functional.normalize(x, dim=1)
