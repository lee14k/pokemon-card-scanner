"""Tesseract OCR for bottom-strip card numbers and code cards."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract

log = logging.getLogger("pokemon_scanner.pack.ocr")

NUMBER_RE = re.compile(r"([A-Z]{0,3}\d{1,3})\s*/\s*([A-Z]{0,3}\d{1,3})")
PROMO_RE = re.compile(r"\b(SWSH|SVP)\s*0*(\d{1,3})\b")
_WHITELIST = "0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZ "
_NUM_CONFIG = f"--psm 7 -c tessedit_char_whitelist={_WHITELIST}"
_NUM_CONFIG_BLOCK = f"--psm 6 -c tessedit_char_whitelist={_WHITELIST}"


@dataclass
class NumberReading:
    raw: str = ""
    numerator: str | None = None
    denominator: str | None = None
    prefix: str | None = None      # promo prefix (SWSH/SVP) when no denominator
    confidence: float = 0.0        # 0..1, mean char confidence of matched tokens
    pattern_ok: bool = False
    blank: bool = False            # no text found at all
    tokens: list[str] = field(default_factory=list)


def _prep_variants(strip_bgr: np.ndarray) -> list[np.ndarray]:
    """Upscaled binarized variants of the strip's left region + full strip."""
    h, w = strip_bgr.shape[:2]
    left = strip_bgr[:, : max(1, int(w * 0.40))]
    variants = []
    for region in (left, strip_bgr):
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        _, otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(otsu)
        variants.append(cv2.bitwise_not(otsu))
    return variants


def _ocr_tokens(img: np.ndarray, config: str) -> list[tuple[str, float]]:
    data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
    out = []
    for text, conf in zip(data["text"], data["conf"]):
        t = (text or "").strip().upper()
        c = float(conf)
        if t and c >= 0:
            out.append((t, c))
    return out


def read_card_number(strip_bgr: np.ndarray) -> NumberReading:
    """Best card-number reading across preprocessing variants and PSM modes."""
    best = NumberReading()
    any_text = False
    for variant in _prep_variants(strip_bgr):
        for config in (_NUM_CONFIG, _NUM_CONFIG_BLOCK):
            tokens = _ocr_tokens(variant, config)
            if tokens:
                any_text = True
            joined = " ".join(t for t, _ in tokens)
            confs = {t: c for t, c in tokens}

            m = NUMBER_RE.search(joined)
            if m:
                hit_confs = [c for t, c in tokens if any(g in t for g in m.groups())]
                conf = float(np.mean(hit_confs)) / 100.0 if hit_confs else 0.3
                if conf > best.confidence or not best.pattern_ok:
                    best = NumberReading(
                        raw=joined, numerator=m.group(1), denominator=m.group(2),
                        prefix=None, confidence=conf, pattern_ok=True,
                        tokens=[t for t, _ in tokens],
                    )
                continue

            p = PROMO_RE.search(joined)
            if p and not best.pattern_ok:
                conf = float(confs.get(p.group(1), 50.0)) / 100.0
                best = NumberReading(
                    raw=joined, numerator=p.group(2), denominator=None,
                    prefix=p.group(1), confidence=conf, pattern_ok=True,
                    tokens=[t for t, _ in tokens],
                )
            elif joined and not best.raw:
                best = NumberReading(raw=joined, tokens=[t for t, _ in tokens])
    best.blank = not any_text
    log.info(
        "ocr.number raw=%r num=%s den=%s prefix=%s conf=%.2f ok=%s",
        best.raw[:80], best.numerator, best.denominator, best.prefix,
        best.confidence, best.pattern_ok,
    )
    return best
