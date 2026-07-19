"""In-app learned band detector: an ONNX segmentation model predicts a
number-band probability mask; connected components become deskewed strips.

Never raises — any failure returns None so find_strips falls back to Hough.
Enabled by app.pack.config band_detector (PACK_BAND_DETECTOR)."""
from __future__ import annotations

import json
import logging
import os

import cv2
import numpy as np

from app.pack.config import settings
from app.pack.segmentation import Strip

log = logging.getLogger("pokemon_scanner.pack.band_detector")

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "band_model")
_session = None
_input = 384
_mask = 96
_loaded = False


def _load() -> bool:
    """Lazy one-time load; returns True when a session is ready."""
    global _session, _input, _mask, _loaded
    if _loaded:
        return _session is not None
    _loaded = True
    path = os.path.join(_MODEL_DIR, "model.onnx")
    if not os.path.exists(path):
        log.info("band_detector.no_model path=%s", path)
        return False
    try:
        import onnxruntime as ort

        _session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        meta = json.load(open(os.path.join(_MODEL_DIR, "version.json")))
        _input, _mask = int(meta.get("input", 384)), int(meta.get("mask", 96))
        log.info("band_detector.loaded input=%s mask=%s", _input, _mask)
    except Exception as e:  # missing dep, corrupt model, etc.
        log.warning("band_detector.load_failed err=%r", e)
        _session = None
    return _session is not None


def _predict_mask(img: np.ndarray) -> np.ndarray | None:
    """Run the model on a letterboxed copy; return a source-resolution 0..1 mask."""
    h, w = img.shape[:2]
    s = _input / max(h, w)
    nh, nw = max(1, round(h * s)), max(1, round(w * s))
    resized = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (nw, nh))
    canvas = np.full((_input, _input, 3), 128, np.uint8)
    oy, ox = (_input - nh) // 2, (_input - nw) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    x = (canvas.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    (logits,) = _session.run(None, {"scene": x})
    prob = 1.0 / (1.0 + np.exp(-logits[0, 0]))               # [mask,mask]
    prob = cv2.resize(prob, (_input, _input))                # -> letterboxed square
    prob = prob[oy:oy + nh, ox:ox + nw]                      # remove padding
    return cv2.resize(prob, (w, h))                          # -> source resolution


def _deskew_crop(img: np.ndarray, rect) -> tuple[np.ndarray, float]:
    (cx, cy), (rw, rh), angle = rect
    if rw < rh:                       # force landscape (band is wide)
        rw, rh = rh, rw
        angle += 90
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    rotated = cv2.warpAffine(img, m, (img.shape[1], img.shape[0]),
                             flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    crop = cv2.getRectSubPix(rotated, (int(round(rw)), int(round(rh))), (cx, cy))
    return crop, float(angle)


def detect_bands(img: np.ndarray) -> list[Strip] | None:
    """Detected number-band strips top->bottom, or None (disabled/no model/no
    bands/error) so the caller falls back to the geometric path."""
    if not _load():
        return None
    try:
        cfg = settings()
        prob = _predict_mask(img)
        if prob is None:
            return None
        h, w = img.shape[:2]
        binary = (prob >= cfg.band_threshold).astype(np.uint8) * 255
        binary = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, w // 60), 3)))
        n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary)
        cands = []
        for i in range(1, n):
            x, y, cw, chh, area = stats[i]
            if area < 0.0005 * h * w:                 # noise
                continue
            long_side, short_side = max(cw, chh), max(1, min(cw, chh))
            if long_side < 0.12 * w or long_side / short_side < 2.0:
                continue                              # bands are wide, not blobs
            pts = cv2.findNonZero((labels == i).astype(np.uint8))
            rect = cv2.minAreaRect(pts)
            cands.append((float(centroids[i][1]), rect,
                          (int(x), int(y), int(cw), int(chh))))
        cands.sort(key=lambda c: c[0])
        strips = []
        for idx, (_, rect, bbox) in enumerate(cands):
            crop, angle = _deskew_crop(img, rect)
            if crop.size == 0 or crop.shape[0] < 4:
                continue
            strips.append(Strip(row_index=idx, image=crop, bbox=bbox, angle=angle))
        log.info("band_detector.detect bands=%s", len(strips))
        return strips or None
    except Exception as e:
        log.warning("band_detector.detect_failed err=%r", e)
        return None
