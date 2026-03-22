"""
Match the bottom-left set symbol crop to reference PNGs.

Add rows to data/set_symbols/index.json and matching PNG files (same approximate
crop as the live symbol region: bottom-left of the English card frame).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

log = logging.getLogger("pokemon_scanner.set_symbol")

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DIR = _PACKAGE_DIR / "data" / "set_symbols"


@dataclass
class SymbolRef:
    set_id: str
    set_code: str | None
    hash_int: int
    path: Path


_index: list[SymbolRef] | None = None
_INDEX_PATH_ENV = "SET_SYMBOL_INDEX_DIR"


def _average_hash_int(img: Image.Image, size: int = 16) -> int:
    g = ImageOps.grayscale(img).resize((size, size), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    mean = sum(px) / len(px)
    val = 0
    for i, p in enumerate(px):
        if p >= mean:
            val |= 1 << i
    return val


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def symbol_index_dir() -> Path:
    import os

    raw = os.environ.get(_INDEX_PATH_ENV, "").strip()
    return Path(raw) if raw else _DEFAULT_DIR


def load_symbol_index() -> list[SymbolRef]:
    global _index
    if _index is not None:
        return _index

    base = symbol_index_dir()
    idx_path = base / "index.json"
    out: list[SymbolRef] = []

    if not idx_path.is_file():
        log.info("set_symbol.index missing %s (optional)", idx_path)
        _index = []
        return _index

    try:
        entries: list[dict[str, Any]] = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("set_symbol.index read failed %s", e)
        _index = []
        return _index

    for row in entries:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("set_id", "")).strip()
        fn = str(row.get("file", "")).strip()
        if not sid or not fn:
            continue
        path = base / fn
        if not path.is_file():
            log.warning("set_symbol.skip missing_file set_id=%s path=%s", sid, path)
            continue
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                h = _average_hash_int(im)
        except OSError as e:
            log.warning("set_symbol.skip bad_image set_id=%s err=%s", sid, e)
            continue
        code = row.get("set_code")
        out.append(
            SymbolRef(
                set_id=sid,
                set_code=str(code).strip() if code else None,
                hash_int=h,
                path=path,
            )
        )

    log.info("set_symbol.index loaded refs=%s dir=%s", len(out), base)
    _index = out
    return _index


def reload_symbol_index() -> None:
    global _index
    _index = None
    load_symbol_index()


def best_set_symbol_match(crop: Image.Image) -> tuple[SymbolRef, int] | None:
    """Best reference and Hamming distance for this crop, or None if index empty."""
    refs = load_symbol_index()
    if not refs:
        return None
    h = _average_hash_int(crop)
    best: tuple[SymbolRef, int] | None = None
    for ref in refs:
        d = _hamming(h, ref.hash_int)
        if best is None or d < best[1]:
            best = (ref, d)
    return best


def match_set_symbol(crop: Image.Image, max_distance: int | None = None) -> tuple[SymbolRef, int] | None:
    """
    Return best-matching reference and Hamming distance, or None if index empty
    or no match under max_distance (256-bit hash).
    """
    import os

    if max_distance is None:
        max_distance = int(os.environ.get("SET_SYMBOL_MAX_DISTANCE", "28"))

    best = best_set_symbol_match(crop)
    if best is None:
        return None

    if best[1] > max_distance:
        log.info("set_symbol.no_match best_distance=%s (threshold %s)", best[1], max_distance)
        return None

    log.info(
        "set_symbol.match set_id=%s set_code=%s distance=%s",
        best[0].set_id,
        best[0].set_code,
        best[1],
    )
    return best


def match_set_symbol_best_of_crops(
    processed: Image.Image,
    *,
    boxes: list[tuple[int, int, int, int]],
    max_distance: int | None = None,
) -> tuple[SymbolRef, int, tuple[int, int, int, int]] | None:
    """
    Run average-hash match on several crops (e.g. tighter vs looser bottom-left);
    return the best under max_distance, else None.
    """
    import os

    if max_distance is None:
        max_distance = int(os.environ.get("SET_SYMBOL_MAX_DISTANCE", "28"))

    overall: tuple[SymbolRef, int, tuple[int, int, int, int]] | None = None
    for box in boxes:
        crop = processed.crop(box)
        hit = best_set_symbol_match(crop)
        if hit is None:
            continue
        ref, dist = hit
        if overall is None or dist < overall[1]:
            overall = (ref, dist, box)

    if overall is None:
        return None
    ref, dist, box = overall
    if dist > max_distance:
        log.info(
            "set_symbol.no_match best_distance=%s (threshold %s) box=%s",
            dist,
            max_distance,
            box,
        )
        return None
    log.info(
        "set_symbol.match set_id=%s set_code=%s distance=%s box=%s",
        ref.set_id,
        ref.set_code,
        dist,
        box,
    )
    return overall
