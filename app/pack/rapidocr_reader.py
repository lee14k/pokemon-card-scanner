"""RapidOCR (PP-OCR models on onnxruntime) number reader — far stronger than
Tesseract on real-photo small/tilted/low-contrast text. Lazily loaded; any
failure returns None so read_card_number falls back to the Tesseract path."""
from __future__ import annotations

import logging

import cv2
import numpy as np

log = logging.getLogger("pokemon_scanner.pack.rapidocr")

_engine = None
_loaded = False


def _get():
    global _engine, _loaded
    if _loaded:
        return _engine
    _loaded = True
    try:
        from rapidocr_onnxruntime import RapidOCR

        import os as _os
        _threads = int(_os.environ.get("OCR_THREADS", "0"))
        kwargs = {}
        if _threads > 0:
            # rapidocr-onnxruntime 1.4.x accepts intra_op_num_threads per stage
            kwargs = {
                "det_use_cuda": False, "rec_use_cuda": False, "cls_use_cuda": False,
                "intra_op_num_threads": _threads, "inter_op_num_threads": 1,
            }
        try:
            import cv2 as _cv2
            if _threads > 0:
                _cv2.setNumThreads(_threads)
        except Exception:
            pass
        try:
            _engine = RapidOCR(**kwargs)
        except TypeError:
            _engine = RapidOCR()
        log.info("rapidocr.loaded")
    except Exception as e:  # not installed / init failure
        log.warning("rapidocr.load_failed err=%r", e)
        _engine = None
    return _engine


def detect_lines_xy(
    img_bgr: np.ndarray, cap: int = 2600
) -> list[tuple[float, float, str, float, float, float]]:
    """Run detection+recognition over the WHOLE photo; return (x_center,
    y_center, text, conf, box_w, box_h) per detected line, all coords scaled
    back to SOURCE pixels. PP-OCR's real-photo-trained detector localizes the
    number rows far better than geometric cropping. [] on failure."""
    eng = _get()
    if eng is None:
        return None if False else []
    h, w = img_bgr.shape[:2]
    scale = 1.0
    if max(h, w) > cap:
        scale = cap / max(h, w)
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
    try:
        res, _ = eng(img_bgr)
    except Exception as e:
        log.warning("rapidocr.detect_failed err=%r", e)
        return []
    out: list[tuple[float, float, str, float, float, float]] = []
    for box, txt, conf in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x = float(np.mean(xs)) / scale          # back to source coords
        y = float(np.mean(ys)) / scale
        bw = (float(max(xs)) - float(min(xs))) / scale
        bh = (float(max(ys)) - float(min(ys))) / scale
        out.append((x, y, txt.upper(), float(conf), bw, bh))
    return out


def detect_lines(img_bgr: np.ndarray, cap: int = 2600) -> list[tuple[float, str, float]]:
    """Run detection+recognition over the WHOLE photo; return (y_center, text,
    conf) per detected line. PP-OCR's real-photo-trained detector localizes the
    number rows far better than geometric cropping. [] on failure. Thin wrapper
    over detect_lines_xy dropping the x/box-geometry fields."""
    return [(y, t, c) for _x, y, t, c, _w, _h in detect_lines_xy(img_bgr, cap)]


def read_text(strip_bgr: np.ndarray) -> tuple[str, float] | None:
    """(joined uppercase text, mean confidence) for a strip, or None."""
    eng = _get()
    if eng is None:
        return None
    h, w = strip_bgr.shape[:2]
    if max(h, w) > 2400:                      # bound memory/time on 12MP crops
        s = 2400 / max(h, w)
        strip_bgr = cv2.resize(strip_bgr, (int(w * s), int(h * s)),
                               interpolation=cv2.INTER_AREA)
    elif max(h, w) < 1200:                    # upscale tiny strips for the recognizer
        s = 1200 / max(h, w)
        strip_bgr = cv2.resize(strip_bgr, (int(w * s), int(h * s)),
                               interpolation=cv2.INTER_CUBIC)
    try:
        res, _ = eng(strip_bgr)
    except Exception as e:
        log.warning("rapidocr.infer_failed err=%r", e)
        return None
    if not res:
        return None
    joined = " ".join(t for _, t, _ in res).upper()
    conf = float(np.mean([c for *_, c in res]))
    return joined, conf
