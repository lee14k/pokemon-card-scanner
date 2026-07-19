"""Strip-embedding image encoder via onnxruntime. Embeds letterboxed 224x224
RGB images (raw 0..1 input; normalization lives inside the ONNX graph) to
L2-normalized float32 vectors."""
from __future__ import annotations

import json, os

import numpy as np
import onnxruntime as ort
from PIL import Image

from matcher import config

_SIZE = 224

_session: ort.InferenceSession | None = None

def load(model_path: str) -> None:
    global _session
    _session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

def ready() -> bool:
    return _session is not None

def version() -> str:
    vf = os.path.join(os.path.dirname(config.model_path()), "version.json")
    try:
        return json.loads(open(vf).read()).get("model_version", "unknown")
    except OSError:
        return "unknown"

def _letterbox(im: Image.Image) -> np.ndarray:
    # Stretch-to-fill, NOT aspect-preserving: must mirror training-side
    # preprocessing exactly (training/train.py letterbox). Aspect-preserving
    # padding starves ~13:1 strips to ~17px of content — run v1a failed on it.
    im = im.convert("RGB").resize((_SIZE, _SIZE), Image.BICUBIC)
    arr = np.asarray(im, dtype=np.float32) / 255.0
    return arr.transpose(2, 0, 1)  # CHW

def embed(images: list[Image.Image], batch: int = 16) -> np.ndarray:
    """float32 [N, dim], L2-normalized."""
    assert _session is not None, "model not loaded"
    out: list[np.ndarray] = []
    name = _session.get_inputs()[0].name
    for i in range(0, len(images), batch):
        x = np.stack([_letterbox(im) for im in images[i:i + batch]])
        (y,) = _session.run(None, {name: x})
        out.append(y.astype(np.float32))
    v = np.concatenate(out) if out else np.zeros((0, 0), np.float32)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-8)
