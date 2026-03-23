"""
Match the bottom-left set symbol crop to reference PNGs.

Reference art from pokesymbols is usually a large canvas (e.g. 600×600) with the
icon centered and transparent margins. Phone crops were often the whole bottom
quarter of the card, so a 16×16 average hash was dominated by artwork/text, not
the tiny symbol. We trim reference alpha to the glyph, pad to a square on white,
and for live crops take a bottom-left square after autocontrast so the hash sees
the same kind of “icon-sized” patch as the PNGs.

Add rows to data/set_symbols/index.json and matching PNG files.
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


def _normalize_reference_for_hash(im: Image.Image) -> Image.Image:
    """Trim transparent margins, flatten onto white, center on a square (matches icon focus)."""
    im = im.convert("RGBA")
    alpha = im.split()[3]
    bbox = alpha.getbbox()
    if bbox:
        im = im.crop(bbox)
        alpha = im.split()[3]
    rgb = Image.new("RGB", im.size, (255, 255, 255))
    rgb.paste(im, mask=alpha)
    w, h = rgb.size
    side = max(w, h, 1)
    sheet = Image.new("RGB", (side, side), (255, 255, 255))
    sheet.paste(rgb, ((side - w) // 2, (side - h) // 2))
    return sheet


def _normalize_live_crop_for_hash(crop: Image.Image) -> Image.Image:
    """
    Emphasize the bottom-left corner (where the expansion symbol sits) so it
    fills the hash instead of the whole tall/wide crop rectangle.
    """
    g = ImageOps.autocontrast(ImageOps.grayscale(crop))
    # Pad to square instead of forcing a bottom-left square. The crop boxes we
    # pass in already isolate the bottom-left area; padding makes y-offsets
    # actually matter during matching.
    w, h = g.size
    side = max(w, h, 1)
    sheet = Image.new("L", (side, side), 255)
    sheet.paste(g, ((side - w) // 2, (side - h) // 2))
    return sheet


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
                norm = _normalize_reference_for_hash(im)
                h = _average_hash_int(norm)
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
    """
    Best reference and Hamming distance, or None if index empty.
    Uses the better of (a) bottom-left square aHash and (b) full-crop aHash so
    tight vs loose framing can both score against the normalized reference PNGs.
    """
    refs = load_symbol_index()
    if not refs:
        return None
    norm = _normalize_live_crop_for_hash(crop)
    h_sq = _average_hash_int(norm)
    g_full = ImageOps.autocontrast(ImageOps.grayscale(crop))
    h_full = _average_hash_int(g_full)
    if log.isEnabledFor(logging.DEBUG):
        cw, ch = crop.size
        nw, nh = norm.size
        log.debug(
            "set_symbol.ahash crop_px=%sx%s norm_px=%sx%s h_sq=%s h_full=%s",
            cw,
            ch,
            nw,
            nh,
            h_sq,
            h_full,
        )
    best: tuple[SymbolRef, int] | None = None
    second_d: int | None = None
    for ref in refs:
        d_sq = _hamming(h_sq, ref.hash_int)
        d_full = _hamming(h_full, ref.hash_int)
        d = d_sq if d_sq <= d_full else d_full
        if best is None or d < best[1]:
            if best is not None:
                prev = best[1]
                if second_d is None or prev < second_d:
                    second_d = prev
            best = (ref, d)
        elif second_d is None or d < second_d:
            second_d = d
    if log.isEnabledFor(logging.DEBUG) and best is not None:
        margin = (second_d - best[1]) if second_d is not None else None
        log.debug(
            "set_symbol.ahash_rank best_dist=%s second_best_dist=%s margin=%s best_set_id=%s",
            best[1],
            second_d,
            margin,
            best[0].set_id,
        )
    return best


def match_set_symbol(crop: Image.Image, max_distance: int | None = None) -> tuple[SymbolRef, int] | None:
    """
    Return best-matching reference and Hamming distance, or None if index empty
    or no match under max_distance (256-bit hash).
    """
    import os

    if max_distance is None:
        max_distance = int(os.environ.get("SET_SYMBOL_MAX_DISTANCE", "34"))

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
        max_distance = int(os.environ.get("SET_SYMBOL_MAX_DISTANCE", "34"))

    w, h = processed.size
    n_refs = len(load_symbol_index())
    log.info(
        "set_symbol.multi_crop start image_px=%sx%s boxes=%s threshold=%s index_refs=%s",
        w,
        h,
        len(boxes),
        max_distance,
        n_refs,
    )
    if not boxes:
        log.warning("set_symbol.multi_crop abort no_boxes")
        return None
    if n_refs == 0:
        log.warning("set_symbol.multi_crop abort index_empty dir=%s", symbol_index_dir())
        return None

    per_crop: list[tuple[tuple[int, int, int, int], int, str, str | None]] = []
    overall: tuple[SymbolRef, int, tuple[int, int, int, int]] | None = None
    for i, box in enumerate(boxes):
        crop = processed.crop(box)
        cw, ch = crop.size
        hit = best_set_symbol_match(crop)
        if hit is None:
            log.info(
                "set_symbol.crop i=%s box=%s size_px=%sx%s result=no_index",
                i,
                box,
                cw,
                ch,
            )
            continue
        ref, dist = hit
        per_crop.append((box, dist, ref.set_id, ref.set_code))
        log.info(
            "set_symbol.crop i=%s box=%s size_px=%sx%s best_set_id=%s best_set_code=%s hamming=%s",
            i,
            box,
            cw,
            ch,
            ref.set_id,
            ref.set_code,
            dist,
        )
        if overall is None or dist < overall[1]:
            overall = (ref, dist, box)

    if overall is None:
        log.info("set_symbol.multi_crop end matched=no reason=no_best_per_crop")
        return None
    ref, dist, box = overall
    if dist > max_distance:
        summary = ", ".join(
            f"[{i}] hamming={d} set_id={sid} box={bx}"
            for i, (bx, d, sid, _) in enumerate(per_crop)
        )
        log.info(
            "set_symbol.multi_crop rejected hamming=%s threshold=%s chosen_box=%s best_set_id=%s | per_crop=%s",
            dist,
            max_distance,
            box,
            ref.set_id,
            summary,
        )
        return None
    log.info(
        "set_symbol.multi_crop accepted set_id=%s set_code=%s hamming=%s box=%s (threshold<=%s)",
        ref.set_id,
        ref.set_code,
        dist,
        box,
        max_distance,
    )
    return overall
