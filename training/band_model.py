"""Band-detector segmentation net: MobileNetV3-small encoder + light decoder
-> 1-channel band-probability logit mask. Normalization is baked into forward()
so the exported ONNX takes raw 0..1 input."""
import torch
import torch.nn as nn
from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

INPUT = 384   # letterboxed square scene input
MASK = 192    # output mask side (INPUT / 2) — fine enough to separate close bands

_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
_STD = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


def _up(cin: int, cout: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1), nn.BatchNorm2d(cout), nn.GELU(),
        nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
    )


class BandNet(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.features = mobilenet_v3_small(
            weights=MobileNet_V3_Small_Weights.DEFAULT).features  # 576ch @ /32
        self.dec = nn.Sequential(
            _up(576, 256),   # /16
            _up(256, 128),   # /8
            _up(128, 64),    # /4
            _up(64, 32),     # /2  == MASK
        )
        self.head = nn.Conv2d(32, 1, 1)
        self.register_buffer("mean", _MEAN)
        self.register_buffer("std", _STD)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # x: [N,3,384,384] in 0..1
        x = (x - self.mean) / self.std
        x = self.features(x)
        x = self.dec(x)
        return self.head(x)  # [N,1,96,96] logits
