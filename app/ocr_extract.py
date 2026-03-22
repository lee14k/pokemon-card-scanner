"""Extract text candidates from card images for search."""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytesseract
from PIL import Image, ImageOps

if TYPE_CHECKING:
    pass

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


def extract_text_candidates(image_bytes: bytes, max_candidates: int = 12) -> list[str]:
    """
    Run OCR and return distinct string candidates (longest / most word-like first).
    """
    _check_tesseract()
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    processed = _preprocess(img)

    # Name + HP usually sit in the top band; OCR that first so real lines rank higher.
    w, h = processed.size
    top_h = max(int(h * 0.28), 48)
    name_band = processed.crop((0, 0, w, top_h))

    raw_top = _ocr_string(name_band)
    raw_full = _ocr_string(processed)
    raw = raw_top + "\n" + raw_full

    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if len(s) < 3:
            continue
        # Skip obvious noise (mostly punctuation / digits)
        letters = sum(1 for c in s if c.isalpha())
        if letters < 2:
            continue
        lines.append(s)

    # Longer lines often contain the Pokémon name (title area)
    lines.sort(key=lambda x: len(x), reverse=True)

    # Also add first 1–3 "words" from top lines as shorter queries
    words: list[str] = []
    for line in lines[:5]:
        for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", line):
            if w.lower() not in {"the", "and", "for", "basic", "stage", "hp"}:
                words.append(w)

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

    for w in words:
        add(w)
        if len(candidates) >= max_candidates:
            break

    return candidates[:max_candidates]
