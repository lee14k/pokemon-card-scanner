"""Extract text candidates from card images for search."""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING

import pytesseract
from PIL import Image, ImageOps

if TYPE_CHECKING:
    pass


def _check_tesseract() -> None:
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError as e:
        raise RuntimeError(
            "Tesseract OCR is not installed or not on PATH. "
            "macOS: brew install tesseract. "
            "Ubuntu: apt install tesseract-ocr."
        ) from e


def _preprocess(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    # Improve contrast for glossy cards / phone photos
    return ImageOps.autocontrast(gray)


def extract_text_candidates(image_bytes: bytes, max_candidates: int = 12) -> list[str]:
    """
    Run OCR and return distinct string candidates (longest / most word-like first).
    """
    _check_tesseract()
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    processed = _preprocess(img)

    raw = pytesseract.image_to_string(processed, lang="eng")
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
