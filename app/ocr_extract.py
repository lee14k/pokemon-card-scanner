"""Extract text candidates from card images for search."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytesseract
from PIL import Image, ImageOps

from app.card_signals import CardSignals, pick_collection_number
from app.logging_config import preview_text
from app.set_symbol_index import match_set_symbol

if TYPE_CHECKING:
    pass

log = logging.getLogger("pokemon_scanner.ocr")

_TESSERACT_CONFIGURED = False


def _configure_tesseract_cmd() -> None:
    """Point pytesseract at the binary (env override, PATH, or common Homebrew paths)."""
    global _TESSERACT_CONFIGURED
    if _TESSERACT_CONFIGURED:
        return
    _TESSERACT_CONFIGURED = True

    env_cmd = os.environ.get("TESSERACT_CMD", "").strip()
    if env_cmd:
        pytesseract.pytesseract.tesseract_cmd = env_cmd
        return

    if shutil.which("tesseract"):
        return

    if sys.platform == "darwin":
        for candidate in (
            Path("/opt/homebrew/bin/tesseract"),
            Path("/usr/local/bin/tesseract"),
        ):
            if candidate.is_file():
                pytesseract.pytesseract.tesseract_cmd = str(candidate)
                return


def _check_tesseract() -> None:
    _configure_tesseract_cmd()
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError as e:
        raise RuntimeError(
            "Tesseract OCR is not installed or not on PATH. "
            "macOS: brew install tesseract (Apple Silicon: /opt/homebrew/bin/tesseract). "
            "Linux: apt install tesseract-ocr. "
            "Railway: redeploy with deploy.aptPackages including tesseract-ocr, "
            "or set RAILPACK_DEPLOY_APT_PACKAGES=tesseract-ocr. "
            "Override path: TESSERACT_CMD=/path/to/tesseract."
        ) from e


def _preprocess(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    # Improve contrast for glossy cards / phone photos
    return ImageOps.autocontrast(gray)


# Uniform block of text; works better than default for game cards.
_TESS_CONFIG = "--psm 6 --oem 3"


def _ocr_string(img: Image.Image) -> str:
    return pytesseract.image_to_string(img, lang="eng", config=_TESS_CONFIG)


def _collect_lines_from_raw(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if len(s) < 3:
            continue
        letters = sum(1 for c in s if c.isalpha())
        if letters < 2:
            continue
        lines.append(s)
    lines.sort(key=lambda x: len(x), reverse=True)
    return lines


def extract_card_signals(image_bytes: bytes, max_candidates: int = 12) -> CardSignals:
    """
    OCR name band, full card, bottom strip (collection number), and bottom-left crop
    (set symbol hash vs reference table).
    """
    _check_tesseract()
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    processed = _preprocess(img)

    w, h = processed.size
    top_h = max(int(h * 0.28), 48)
    name_band = processed.crop((0, 0, w, top_h))

    bottom_y0 = int(h * 0.72)
    bottom_strip = processed.crop((0, bottom_y0, w, h))

    sym_x1 = max(int(w * 0.26), 28)
    sym_y0 = int(h * 0.76)
    symbol_crop = processed.crop((0, sym_y0, sym_x1, h))

    raw_top = _ocr_string(name_band)
    raw_full = _ocr_string(processed)
    raw_bottom = _ocr_string(bottom_strip)
    raw = raw_top + "\n" + raw_full + "\n" + raw_bottom

    log.info(
        "ocr.image bytes=%s processed_px=%sx%s name_band_h=%s",
        len(image_bytes),
        w,
        h,
        top_h,
    )
    log.info("ocr.raw_top_band %s", preview_text(raw_top, 900))
    log.info("ocr.raw_full_card %s", preview_text(raw_full, 900))
    log.info("ocr.raw_bottom_strip %s", preview_text(raw_bottom, 900))
    log.debug("ocr.raw_combined %s", preview_text(raw, 2000))

    lines = _collect_lines_from_raw(raw)
    top_lines = _collect_lines_from_raw(raw_top)
    primary_name = top_lines[0] if top_lines else None

    words: list[str] = []
    for line in lines[:5]:
        for wtok in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", line):
            if wtok.lower() not in {"the", "and", "for", "basic", "stage", "hp"}:
                words.append(wtok)

    candidates: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        key = s.lower().strip()
        if key and key not in seen and len(key) >= 3:
            seen.add(key)
            candidates.append(s.strip())

    for line in lines:
        add(line)
        if len(candidates) >= max_candidates:
            break

    for wtok in words:
        add(wtok)
        if len(candidates) >= max_candidates:
            break

    out = candidates[:max_candidates]
    log.info("ocr.lines_kept count=%s sample=%s", len(lines), lines[:25])
    log.info("ocr.words_from_lines count=%s sample=%s", len(words), words[:30])
    log.info("ocr.final_candidates count=%s values=%s", len(out), out)

    card_number = pick_collection_number(raw_full + "\n" + raw_top, raw_bottom)

    set_id: str | None = None
    set_code: str | None = None
    sym_dist: int | None = None
    sym_note = f"crop_px={symbol_crop.size[0]}x{symbol_crop.size[1]}"

    matched = match_set_symbol(symbol_crop)
    if matched:
        set_id = matched[0].set_id
        set_code = matched[0].set_code
        sym_dist = matched[1]
        sym_note = f"{sym_note} matched_set_id={set_id} dist={sym_dist}"

    log.info(
        "ocr.card_number=%r primary_name_guess=%r set_id=%s set_code=%s %s",
        card_number,
        primary_name,
        set_id,
        set_code,
        sym_note,
    )

    return CardSignals(
        ocr_fragments=out,
        card_number=card_number,
        primary_name_guess=primary_name,
        bottom_raw_ocr=raw_bottom,
        symbol_raw_note=sym_note,
        set_id_from_symbol=set_id,
        set_code_from_symbol=set_code,
        symbol_hash_distance=sym_dist,
    )


def extract_text_candidates(image_bytes: bytes, max_candidates: int = 12) -> list[str]:
    """Backward-compatible: name/word candidates only."""
    return extract_card_signals(image_bytes, max_candidates=max_candidates).ocr_fragments
