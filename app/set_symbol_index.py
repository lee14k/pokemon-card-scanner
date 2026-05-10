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

import cv2
import numpy as np
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


# pHash sizes: resize to DCT_SIZE x DCT_SIZE, keep top-left HASH_SIZE x HASH_SIZE.
# 32 / 8 is the classic Marr/Buchanan pHash configuration; produces a 64-bit hash.
_PHASH_DCT_SIZE = 32
_PHASH_LOW_SIZE = 8


def _phash_int(img: Image.Image) -> int:
    """
    Perceptual hash via DCT (cv2.dct). 64-bit, far more lighting/JPEG robust than aHash.
    """
    g = ImageOps.grayscale(img).resize(
        (_PHASH_DCT_SIZE, _PHASH_DCT_SIZE), Image.Resampling.LANCZOS
    )
    arr = np.asarray(g, dtype=np.float32)
    dct = cv2.dct(arr)
    low = dct[:_PHASH_LOW_SIZE, :_PHASH_LOW_SIZE].flatten()
    # Standard pHash: median of low-freq coefficients excluding DC; bit per coeff > median.
    med = float(np.median(low[1:]))
    bits = 0
    for i, v in enumerate(low):
        if float(v) > med:
            bits |= 1 << i
    return bits


def _isolate_glyph_crop(crop: Image.Image) -> Image.Image | None:
    """
    Find the symbol glyph inside the bottom-left crop using adaptive thresholding +
    connected components. Returns a tight bbox around the most "symbol-like" blob,
    padded ~10%, or None if nothing plausible found (caller falls back to full crop).

    Robustness vs glare/holo/shadows: adaptive threshold uses local windows so uneven
    lighting doesn't sink the whole image; we then accept either polarity (dark glyph
    on light strip, or light glyph on dark border) and pick the most centered blob
    of plausible size.
    """
    arr = np.asarray(ImageOps.grayscale(crop), dtype=np.uint8)
    h, w = arr.shape
    if h < 14 or w < 14:
        return None

    # Block size must be odd and large enough to span the glyph; tune off the smaller side.
    block = max(11, (min(h, w) // 5) | 1)

    best_bbox: tuple[int, int, int, int] | None = None
    best_score = -1.0
    min_area = max(40, int(0.01 * h * w))
    max_area = int(0.55 * h * w)

    for thresh_type in (cv2.THRESH_BINARY_INV, cv2.THRESH_BINARY):
        bw = cv2.adaptiveThreshold(
            arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, thresh_type, block, 5
        )
        # Close small holes inside the glyph so it stays one component.
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        for i in range(1, n):
            x, y, cw, ch, area = (
                int(stats[i, 0]),
                int(stats[i, 1]),
                int(stats[i, 2]),
                int(stats[i, 3]),
                int(stats[i, 4]),
            )
            if area < min_area or area > max_area:
                continue
            if cw < 6 or ch < 6:
                continue
            aspect = cw / max(1, ch)
            if aspect < 0.25 or aspect > 4.0:
                continue
            # Drop blobs that hug a crop edge over most of its length (card border / shadow).
            edge_hug = (
                (x == 0 and ch > h * 0.85)
                or (y == 0 and cw > w * 0.85)
                or (x + cw == w and ch > h * 0.85)
                or (y + ch == h and cw > w * 0.85)
            )
            if edge_hug:
                continue
            score = float(area)
            cx = x + cw / 2
            cy = y + ch / 2
            # Set symbol typically sits in the left portion, vertically centered in the strip.
            if cx < w * 0.65:
                score *= 1.20
            if h * 0.20 < cy < h * 0.85:
                score *= 1.10
            if score > best_score:
                best_score = score
                best_bbox = (x, y, cw, ch)

    if best_bbox is None:
        return None
    x, y, cw, ch = best_bbox
    pad = max(2, int(round(0.10 * max(cw, ch))))
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(w, x + cw + pad)
    y1 = min(h, y + ch + pad)
    return crop.crop((x0, y0, x1, y1))


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
    Normalize a live crop so reference-style hashes work.

    The incoming crop boxes already isolate the bottom-left area; here we
    just pad to a square on white so the hash isn't skewed by differing crop
    aspect ratios.
    """
    g = ImageOps.autocontrast(ImageOps.grayscale(crop))
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
                h = _phash_int(norm)
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


def _candidate_hashes_for_crop(crop: Image.Image) -> list[int]:
    """
    Build a small set of hashes per crop so framing/lighting variation has multiple
    shots at matching: (a) glyph isolated via adaptive threshold, (b) padded full
    crop, (c) the raw autocontrast'd grayscale crop.
    """
    hashes: list[int] = []
    glyph = _isolate_glyph_crop(crop)
    if glyph is not None:
        gw, gh = glyph.size
        if gw > 0 and gh > 0:
            side = max(gw, gh, 1)
            sheet = Image.new("L", (side, side), 255)
            gray_glyph = ImageOps.autocontrast(ImageOps.grayscale(glyph))
            sheet.paste(gray_glyph, ((side - gw) // 2, (side - gh) // 2))
            hashes.append(_phash_int(sheet))
    hashes.append(_phash_int(_normalize_live_crop_for_hash(crop)))
    hashes.append(_phash_int(ImageOps.autocontrast(ImageOps.grayscale(crop))))
    return hashes


def best_set_symbol_match(crop: Image.Image) -> tuple[SymbolRef, int, int | None] | None:
    """
    Best reference, Hamming distance, and second-best distance (if any), or
    None if index empty.

    Tries multiple normalizations of the live crop (glyph-isolated, padded square,
    raw autocontrast) and picks the lowest distance per reference, so tight vs loose
    framing and uneven lighting both have a chance to match the reference PNGs.
    """
    refs = load_symbol_index()
    if not refs:
        return None
    cand_hashes = _candidate_hashes_for_crop(crop)
    if log.isEnabledFor(logging.DEBUG):
        cw, ch = crop.size
        log.debug(
            "set_symbol.phash crop_px=%sx%s candidate_hashes=%s",
            cw,
            ch,
            len(cand_hashes),
        )
    best: tuple[SymbolRef, int] | None = None
    second_d: int | None = None
    for ref in refs:
        d = min(_hamming(h, ref.hash_int) for h in cand_hashes)
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
            "set_symbol.phash_rank best_dist=%s second_best_dist=%s margin=%s best_set_id=%s",
            best[1],
            second_d,
            margin,
            best[0].set_id,
        )
    if best is None:
        return None
    return (best[0], best[1], second_d)


# Distance thresholds are sized for the 64-bit pHash output of `_phash_int`.
# Default 20 is chosen permissively because real card symbols under varied lighting
# commonly land at 14-22 even when the correct ref is identified; tighten via env
# (SET_SYMBOL_MAX_DISTANCE / SET_SYMBOL_MIN_MARGIN) once you see real distances.
_DEFAULT_MAX_DISTANCE = 20
_DEFAULT_MIN_MARGIN = 2


def _env_int(name: str, default: int) -> int:
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def match_set_symbol(crop: Image.Image, max_distance: int | None = None) -> tuple[SymbolRef, int] | None:
    """
    Return best-matching reference and Hamming distance, or None if index empty
    or no match under max_distance (64-bit pHash).
    """
    if max_distance is None:
        max_distance = _env_int("SET_SYMBOL_MAX_DISTANCE", _DEFAULT_MAX_DISTANCE)

    best = best_set_symbol_match(crop)
    if best is None:
        return None
    ref, dist, second_d = best
    margin = (second_d - dist) if second_d is not None else None
    min_margin = _env_int("SET_SYMBOL_MIN_MARGIN", _DEFAULT_MIN_MARGIN)
    if margin is not None and margin < min_margin:
        log.info(
            "set_symbol.no_match ambiguous best_distance=%s second_best=%s margin=%s (<%s)",
            dist,
            second_d,
            margin,
            min_margin,
        )
        return None

    if dist > max_distance:
        log.info("set_symbol.no_match best_distance=%s (threshold %s)", dist, max_distance)
        return None

    log.info(
        "set_symbol.match set_id=%s set_code=%s distance=%s",
        ref.set_id,
        ref.set_code,
        dist,
    )
    return (ref, dist)


def match_set_symbol_best_of_crops(
    processed_variants: list[Image.Image] | Image.Image,
    *,
    boxes: list[tuple[int, int, int, int]],
    max_distance: int | None = None,
) -> tuple[SymbolRef, int, tuple[int, int, int, int]] | None:
    """
    Run pHash match against several crops × preprocessing variants (e.g. CLAHE'd
    shadow/glare-fixed vs standard autocontrast). Returns the best under max_distance,
    else None.

    Accepts either a single processed image (legacy) or a list of variants, all of
    which must share the same dimensions so the bounding boxes apply uniformly.
    """
    if isinstance(processed_variants, Image.Image):
        processed_variants = [processed_variants]
    if not processed_variants:
        log.warning("set_symbol.multi_crop abort no_variants")
        return None

    if max_distance is None:
        max_distance = _env_int("SET_SYMBOL_MAX_DISTANCE", _DEFAULT_MAX_DISTANCE)
    min_margin = _env_int("SET_SYMBOL_MIN_MARGIN", _DEFAULT_MIN_MARGIN)

    w, h = processed_variants[0].size
    n_refs = len(load_symbol_index())
    log.info(
        "set_symbol.multi_crop start image_px=%sx%s boxes=%s variants=%s threshold=%s index_refs=%s",
        w,
        h,
        len(boxes),
        len(processed_variants),
        max_distance,
        n_refs,
    )
    if not boxes:
        log.warning("set_symbol.multi_crop abort no_boxes")
        return None
    if n_refs == 0:
        log.warning("set_symbol.multi_crop abort index_empty dir=%s", symbol_index_dir())
        return None

    per_crop: list[tuple[int, tuple[int, int, int, int], int, int | None, str, str | None]] = []
    overall: tuple[SymbolRef, int, tuple[int, int, int, int]] | None = None
    # Track absolute closest ref ignoring margin/threshold — purely for calibration logs.
    closest_ever: tuple[SymbolRef, int, int | None, tuple[int, int, int, int], int] | None = None
    for i, box in enumerate(boxes):
        for vi, proc in enumerate(processed_variants):
            crop = proc.crop(box)
            cw, ch = crop.size
            hit = best_set_symbol_match(crop)
            if hit is None:
                log.info(
                    "set_symbol.crop i=%s variant=%s box=%s size_px=%sx%s result=no_index",
                    i,
                    vi,
                    box,
                    cw,
                    ch,
                )
                continue
            ref, dist, second_d = hit
            margin = (second_d - dist) if second_d is not None else None
            per_crop.append((vi, box, dist, second_d, ref.set_id, ref.set_code))
            if closest_ever is None or dist < closest_ever[1]:
                closest_ever = (ref, dist, second_d, box, vi)
            log.info(
                "set_symbol.crop i=%s variant=%s box=%s size_px=%sx%s best_set_id=%s best_set_code=%s hamming=%s second=%s margin=%s",
                i,
                vi,
                box,
                cw,
                ch,
                ref.set_id,
                ref.set_code,
                dist,
                second_d,
                margin,
            )
            if margin is not None and margin < min_margin:
                log.info(
                    "set_symbol.crop i=%s variant=%s rejected_ambiguous margin=%s (<%s)",
                    i,
                    vi,
                    margin,
                    min_margin,
                )
                continue
            if overall is None or dist < overall[1]:
                overall = (ref, dist, box)

    if closest_ever is not None:
        cref, cdist, csecond, cbox, cvariant = closest_ever
        log.info(
            "set_symbol.closest_anyway set_id=%s set_code=%s hamming=%s second=%s margin=%s box=%s variant=%s",
            cref.set_id,
            cref.set_code,
            cdist,
            csecond,
            (csecond - cdist) if csecond is not None else None,
            cbox,
            cvariant,
        )

    if overall is None:
        log.info("set_symbol.multi_crop end matched=no reason=no_best_per_crop")
        return None
    ref, dist, box = overall
    if dist > max_distance:
        summary = ", ".join(
            f"[i={i},v={vi}] hamming={d} second={s2} set_id={sid} box={bx}"
            for i, (vi, bx, d, s2, sid, _) in enumerate(per_crop)
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
