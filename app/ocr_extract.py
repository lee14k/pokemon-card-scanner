"""Extract text candidates from card images for search."""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import pytesseract
from PIL import Image, ImageOps

from app.card_signals import CardSignals, pick_collection_number
from app.logging_config import preview_text
from app.set_symbol_index import match_set_symbol_best_of_crops

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


def _ascii_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


_JUNK_NAME_RE = re.compile(
    r"©|\(c\)|\bcopyright\b|\bpokemon\b|\bnintendo\b|\bcreatures\b|game\s*freak|"
    r"\billus\.?\b|illustrator|\bweakness\b|\bresistance\b|\bretreat\b|wizards\b|"
    r"\bflip a coin\b|\bdamage done\b|\bon your bench\b|\bprevent all\b",
    re.I,
)

_NAME_TOKEN_STOP = frozenset(
    {
        "illus",
        "illustrator",
        "basic",
        "stage",
        "card",
        "your",
        "the",
        "and",
        "pokemon",
        "nintendo",
        "creatures",
        "freak",
        "game",
    }
)


def _lines_from_raw_top_to_bottom(raw: str) -> list[str]:
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if len(s) < 3:
            continue
        letters = sum(1 for c in s if c.isalpha())
        if letters < 2:
            continue
        out.append(s)
    return out


def _is_junk_name_line(s: str) -> bool:
    if len(s) > 52:
        return True
    letters = sum(1 for c in s if c.isalpha())
    if letters < 3:
        return True
    if letters < len(s) * 0.2:
        return True
    if _JUNK_NAME_RE.search(s):
        return True
    low = _ascii_lower(s)
    if "pokemon" in low or "nintendo" in low:
        return True
    if "game" in low and "freak" in low:
        return True
    return False


def _extract_title_style_name(line: str) -> str | None:
    """Whole-line name like 'Charizard' or 'Alolan Raichu' after stripping noise."""
    t = re.sub(r"[^\w\s\-']", " ", line)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t) > 40:
        return None
    m = re.fullmatch(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}(?:\s+[a-z]{1,4})?)",
        t,
    )
    if not m:
        return None
    cand = m.group(1).strip()
    toks = cand.lower().split()
    if not toks or any(tok in _NAME_TOKEN_STOP for tok in toks):
        return None
    if len(cand) < 3:
        return None
    return cand


def _extract_capitalized_name_token(line: str) -> str | None:
    """
    Pull a Pokémon-like token from a noisy OCR line. Prefer 5+ letter words in long
    lines to avoid 4-letter garbage ('Stes'); allow 3+ on short lines or whole-line names.
    """
    stripped = line.strip()
    if len(stripped) <= 22:
        m = re.fullmatch(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s*", stripped)
        if m:
            cand = m.group(1).strip()
            if len(cand) >= 3 and all(t.lower() not in _NAME_TOKEN_STOP for t in cand.split()):
                return cand
    min_len = 4 if len(stripped) > 18 else 2
    pat = rf"\b([A-Z][a-z]{{{min_len},20}})\b"
    for m in re.finditer(pat, line):
        w = m.group(1)
        if w.lower() in _NAME_TOKEN_STOP:
            continue
        return w
    return None


def pick_primary_name_from_top_band(raw_top: str) -> str | None:
    """
    Name lives in the top band; never use the longest line (copyright/footer wins).
    Scan top-to-bottom, skip legal/flavor junk, prefer clean title lines then tokens.
    """
    ordered = _lines_from_raw_top_to_bottom(raw_top)
    for line in ordered:
        if _is_junk_name_line(line):
            continue
        title = _extract_title_style_name(line)
        if title:
            return title
        tok = _extract_capitalized_name_token(line)
        if tok:
            return tok
    return None


def _symbol_crop_boxes(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Several bottom-left boxes; symbol position varies slightly by era / photo angle."""
    return [
        (0, int(h * 0.84), max(int(w * 0.12), 24), h),
        (0, int(h * 0.80), max(int(w * 0.16), 32), h),
        (0, int(h * 0.78), max(int(w * 0.20), 40), h),
        (0, int(h * 0.76), max(int(w * 0.26), 48), h),
    ]


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

    sym_boxes = _symbol_crop_boxes(w, h)

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
    primary_name = pick_primary_name_from_top_band(raw_top)

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
    sym_note = f"symbol_boxes={len(sym_boxes)}"

    matched = match_set_symbol_best_of_crops(processed, boxes=sym_boxes)
    if matched:
        ref, dist, box = matched
        set_id = ref.set_id
        set_code = ref.set_code
        sym_dist = dist
        sym_note = (
            f"crop_box={box} px={box[2] - box[0]}x{box[3] - box[1]} "
            f"matched_set_id={set_id} dist={dist}"
        )

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
