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
    if w > 2400:
        # Strips cropped from 12MP uploads don't need full width: the number
        # type stays OCR-large at 2400, and the 3x upscale below would otherwise
        # produce ~12k-wide variants (memory, Tesseract time).
        s = 2400 / w
        strip_bgr = cv2.resize(strip_bgr, (2400, max(1, int(h * s))),
                               interpolation=cv2.INTER_AREA)
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
    try:
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
    except (pytesseract.TesseractError, OSError) as e:
        # A single Tesseract subprocess failure (e.g. killed by a signal on a
        # degenerate strip) degrades to "no tokens read" — one bad strip must
        # never abort the whole scan.
        log.warning("ocr.tesseract_failed err=%s", e)
        return []
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
    if strip_bgr.shape[0] < 8:  # degenerate crop (row at the image border)
        log.warning("ocr.number strip_too_small h=%s", strip_bgr.shape[0])
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


def _code_from_tokens(tokens: list[tuple[str, float]]) -> CodeReading | None:
    """Best full-format code among tokens, joining adjacent OCR-split fragments
    whose hyphens survived (e.g. "2CM-2ZY7-WK" + "D-DTM")."""
    best: CodeReading | None = None
    for t, c in tokens:
        if CODE_FORMAT_RE.fullmatch(t):
            r = CodeReading(code=t, confidence=c / 100.0, format_ok=True)
            if best is None or r.confidence > best.confidence:
                best = r
    if best is not None:
        return best
    frags = [(t, c) for t, c in tokens if re.fullmatch(r"[A-Z0-9-]{2,}", t)]
    for i in range(len(frags)):
        joined, confs = "", []
        for j in range(i, min(i + 4, len(frags))):
            joined += frags[j][0]
            confs.append(frags[j][1])
            if j > i and CODE_FORMAT_RE.fullmatch(joined):
                r = CodeReading(code=joined, confidence=float(np.mean(confs)) / 100.0,
                                format_ok=True)
                if best is None or r.confidence > best.confidence:
                    best = r
    return best


def _read_code_via_qr(image_bgr: np.ndarray) -> CodeReading | None:
    """QR-anchored read for real code-card photos. The QR locates reliably even
    in cluttered scenes; when its payload decodes we take the code from it, and
    otherwise its corners give the card's rotation and scale, letting us deskew
    and OCR just the code-text band printed to the QR's right."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    det = cv2.QRCodeDetector()
    try:
        data, pts, _ = det.detectAndDecode(gray)
    except cv2.error:
        return None
    if pts is None:
        return None
    if data:
        m = CODE_FORMAT_RE.search(data.upper())
        if m:
            log.info("ocr.code qr_payload code=%s", m.group(0))
            return CodeReading(code=m.group(0), confidence=0.99, format_ok=True)
    corners = pts.reshape(-1, 2)
    tl, tr = corners[0], corners[1]
    qw = float(np.linalg.norm(tr - tl))
    if qw < 40:  # too small to anchor a readable text band
        return None
    angle = float(np.degrees(np.arctan2(tr[1] - tl[1], tr[0] - tl[0])))
    ctr = corners.mean(axis=0)
    rot = cv2.warpAffine(
        gray, cv2.getRotationMatrix2D((float(ctr[0]), float(ctr[1])), angle, 1.0),
        (gray.shape[1], gray.shape[0]),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
    )
    h, w = rot.shape
    # Band extents (in QR widths) map to the code card's printed layout: the
    # code line starts just right of the QR and spans ~3 QR widths. Wider crops
    # admit background clutter that corrupts the read.
    x0, x1 = int(ctr[0] + qw * 0.55), int(ctr[0] + qw * 3.4)
    y0, y1 = int(ctr[1] - qw * 0.9), int(ctr[1] + qw * 0.9)
    roi = rot[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    if roi.size == 0:
        return None
    best: CodeReading | None = None
    # Multiple upscales: low-resolution uploads misread single glyphs at one
    # scale ("C" -> "0"); the best-confidence read across scales wins.
    for fx in (2, 3):
        up = cv2.resize(roi, None, fx=fx, fy=fx, interpolation=cv2.INTER_CUBIC)
        for img in (up, cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            tokens = _ocr_tokens(img, _CODE_CONFIG)
            r = _code_from_tokens(tokens) or _recover_split_code(tokens)
            if r is not None and (
                best is None or (r.format_ok, r.confidence) > (best.format_ok, best.confidence)
            ):
                best = r
    if best is not None:
        log.info("ocr.code qr_anchored code=%s conf=%.2f", best.code, best.confidence)
    return best


def read_code_card(image_bgr: np.ndarray) -> CodeReading:
    """OCR the TCG Live code from a framed close-up. Format check is advisory."""
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.size == 0:
        log.warning("ocr.code invalid_input shape=%s", getattr(image_bgr, "shape", None))
        return CodeReading()
    qr = _read_code_via_qr(image_bgr)
    if qr is not None and qr.format_ok:
        return qr
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if max(h, w) < 900:
        scale = 900 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    elif max(h, w) > 2400:
        # Whole-frame fallback OCR doesn't need phone-camera resolution; the
        # code text is large. Bounds Tesseract memory on 12MP uploads.
        scale = 2400 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
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
    if best.code is None and qr is not None:
        best = qr  # partial QR-anchored read beats nothing
    if best.code is None and saw_tokens:
        log.info("ocr.code tokens_present_but_no_code_matched")
    log.info("ocr.code code=%s conf=%.2f format_ok=%s", best.code, best.confidence, best.format_ok)
    return best
