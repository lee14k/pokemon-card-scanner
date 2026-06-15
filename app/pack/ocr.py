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


def _matched_token_confidence(
    tokens: list[tuple[str, float]], matched_span: str
) -> float:
    """Mean confidence of the tokens that literally compose a regex match.

    matched_span is m.group(0); its whitespace-split pieces are exactly the OCR
    tokens that formed the match. Using whole-token membership (not substring
    containment) avoids crediting unrelated tokens that merely share digits
    (e.g. a stray "4012" when the number is "012/202", or a "SV1" set code when
    the number is "1/10"). Handles both single-token reads ("012/202") and
    split reads ("012", "/", "202"). Falls back to 0.3 if attribution is empty.
    """
    span_tokens = set(matched_span.split())
    hit = [c for t, c in tokens if t in span_tokens]
    return float(np.mean(hit)) / 100.0 if hit else 0.3


def read_card_number(strip_bgr: np.ndarray) -> NumberReading:
    """Best card-number reading across preprocessing variants and PSM modes."""
    if strip_bgr.ndim != 3 or strip_bgr.shape[2] != 3 or strip_bgr.size == 0:
        log.warning("ocr.number invalid_input shape=%s", getattr(strip_bgr, "shape", None))
        return NumberReading(blank=True)
    best = NumberReading()
    any_text = False
    for variant in _prep_variants(strip_bgr):
        for config in (_NUM_CONFIG, _NUM_CONFIG_BLOCK):
            tokens = _ocr_tokens(variant, config)
            if tokens:
                any_text = True
            joined = " ".join(t for t, _ in tokens)

            m = NUMBER_RE.search(joined)
            if m:
                conf = _matched_token_confidence(tokens, m.group(0))
                if conf > best.confidence or not best.pattern_ok:
                    best = NumberReading(
                        raw=joined, numerator=m.group(1), denominator=m.group(2),
                        prefix=None, confidence=conf, pattern_ok=True,
                        tokens=[t for t, _ in tokens],
                    )
                continue

            p = PROMO_RE.search(joined)
            if p and not best.pattern_ok:
                conf = _matched_token_confidence(tokens, p.group(0))
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


CODE_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9-]{8,}")
CODE_FORMAT_RE = re.compile(r"[A-Z0-9]{3,6}(-[A-Z0-9]{3,6}){2,4}")
_CODE_CONFIG = "--psm 6 -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ- "


@dataclass
class CodeReading:
    code: str | None = None
    confidence: float = 0.0
    format_ok: bool = False


_CODE_SEGMENT_RE = re.compile(r"[A-Z0-9]{3,6}")


def _recover_split_code(tokens: list[tuple[str, float]]) -> CodeReading | None:
    """Recover a code whose hyphens OCR dropped AND split it into separate tokens
    (e.g. ["TEST1","CODE2","CARD3"] — each too short for CODE_TOKEN_RE alone).

    Conservative: only fires when 2-5 segment-shaped tokens (3-6 alnum chars) are
    present and their concatenation is 9-30 chars, so a framed code card with a
    little stray text still recovers but a page of text does not. The reconstructed
    code has no hyphens, so format_ok is False (advisory only); downstream dedup
    must compare codes hyphen-insensitively.
    """
    segs = [(t, c) for t, c in tokens if _CODE_SEGMENT_RE.fullmatch(t)]
    if not (2 <= len(segs) <= 5):
        return None
    joined = "".join(t for t, _ in segs)
    if not (9 <= len(joined) <= 30) or not CODE_TOKEN_RE.fullmatch(joined):
        return None
    conf = float(np.mean([c for _, c in segs])) / 100.0
    return CodeReading(code=joined, confidence=conf, format_ok=False)


def read_code_card(image_bgr: np.ndarray) -> CodeReading:
    """OCR the TCG Live code from a framed close-up. Format check is advisory."""
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.size == 0:
        log.warning("ocr.code invalid_input shape=%s", getattr(image_bgr, "shape", None))
        return CodeReading()
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if max(h, w) < 900:
        scale = 900 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    best: CodeReading = CodeReading()
    saw_tokens = False
    for img in (bw, cv2.bitwise_not(bw)):
        tokens = _ocr_tokens(img, _CODE_CONFIG)
        saw_tokens = saw_tokens or bool(tokens)
        for t, c in tokens:
            if not CODE_TOKEN_RE.fullmatch(t):
                continue
            conf = c / 100.0
            if conf > best.confidence:
                best = CodeReading(
                    code=t, confidence=conf,
                    format_ok=bool(CODE_FORMAT_RE.fullmatch(t)),
                )
        # Hyphen-loss recovery only when no whole-token code has been found yet.
        if best.code is None:
            recovered = _recover_split_code(tokens)
            if recovered is not None and recovered.confidence > best.confidence:
                log.info("ocr.code recovered_from_segments code=%s", recovered.code)
                best = recovered
    if best.code is None and saw_tokens:
        log.info("ocr.code tokens_present_but_no_code_matched")
    log.info("ocr.code code=%s conf=%.2f format_ok=%s", best.code, best.confidence, best.format_ok)
    return best
