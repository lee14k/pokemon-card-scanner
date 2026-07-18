"""Run the real segmentation over a synthetic scene; label detected strips by
matching row centers to ground-truth bands."""
from __future__ import annotations

import numpy as np

from app.pack.segmentation import find_strips
from training.synth import SceneTruth


def harvest(scene: np.ndarray, truth: SceneTruth) -> list[tuple[np.ndarray, str | None]]:
    """[(strip_bgr, card_key_or_None)] — None = no matching band (negative)."""
    seg = find_strips(scene, None)
    out = []
    for s in seg.strips:
        _, y0, _, h = s.bbox
        center = y0 + h / 2
        dists = [abs(center - c) for c in truth.band_centers]
        j = int(np.argmin(dists)) if dists else -1
        if j >= 0 and dists[j] <= truth.band_height * 0.6:
            out.append((s.image, truth.card_keys[j]))
        else:
            out.append((s.image, None))
    return out
