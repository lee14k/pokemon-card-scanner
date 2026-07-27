"""RapidOCR (PP-OCR models on onnxruntime) number reader — far stronger than
Tesseract on real-photo small/tilted/low-contrast text. Lazily loaded (and
eagerly warmed at app startup, see app.main._warm_start); any failure returns
None so read_card_number falls back to the Tesseract path."""
from __future__ import annotations

import logging
import threading

import cv2
import numpy as np

log = logging.getLogger("pokemon_scanner.pack.rapidocr")

_engine = None
_ready = False          # engine BUILT and usable — set only after construction
_failed = False         # construction raised once; permanent for this process
_init_lock = threading.Lock()


def _get():
    """The process-wide RapidOCR engine, built on first use. None when RapidOCR
    is unavailable, which every caller treats as "fall back".

    THREAD-SAFE BY CONSTRUCTION, and it has to be: every caller arrives from a
    worker thread (``asyncio.to_thread``) and the binder fires nine cells at
    once. The previous version set its ``_loaded`` flag BEFORE building the
    engine, so concurrent first callers sailed past the guard, read ``_engine``
    while it was still None, and silently returned no lines — dropped reads on
    exactly the first page of a fresh process (binder papered over this with a
    single-threaded warm-up call before its gather; that hack is gone). The flag
    is now set only after a successful build, under a lock, so concurrent first
    callers block for the build and then all get the same engine.

    A build FAILURE is PERMANENT for the process (``_failed``) — the same
    contract the old flag-first code had, kept deliberately. Callers already
    degrade correctly (``detect_lines_xy`` -> [], ``read_text`` -> None, and
    ``ocr.read_card_number`` then uses Tesseract), and the realistic failures
    here — package not installed, model files missing or corrupt, no memory for
    the ONNX graphs — do not heal inside one process. Retrying per call would
    re-pay a failed model load on every OCR of every scan and re-log it each
    time. A restart is the retry."""
    global _engine, _ready, _failed
    if _ready or _failed:                 # fast path: no lock once decided
        return _engine
    with _init_lock:
        if _ready or _failed:             # another thread built it while we waited
            return _engine
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
                engine = RapidOCR(**kwargs)
            except TypeError:
                engine = RapidOCR()
            # Publish the engine BEFORE the ready flag: a reader outside the lock
            # must never see _ready True with _engine still None (the old bug).
            _engine = engine
            _ready = True
            log.info("rapidocr.loaded")
        except Exception as e:  # not installed / init failure
            _engine = None
            _failed = True
            log.warning("rapidocr.load_failed err=%r", e)
    return _engine


def warmup() -> bool:
    """Build the engine and push one tiny image through the full detect+recognize
    path, so the first real scan pays neither the model load nor the first-call
    graph/allocator warm-up. BLOCKING (that is the point) — callers run it in a
    thread. True when the engine is usable. Called from the startup warm task."""
    if _get() is None:
        return False
    detect_lines_xy(np.full((64, 64, 3), 255, np.uint8), 64)
    return True


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
