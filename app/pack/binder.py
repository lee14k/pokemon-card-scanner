"""Binder page scan: one whole-photo OCR pass -> geometric gap-clustering into
grid cells -> the shared identify ladder per cell (no page prior) -> contour-
refined crops for thumbnails/VLM -> prices.

The single photo carries many cards, so unlike the live flow there is no session
prior; each cell resolves on its own name+number. Clustering is pure geometry on
the whole-photo OCR line boxes: gaps in x split columns, gaps in y (within a
column) split cells. The identify ladder itself is ``resolve_identity`` — the
same core the live single-card flow uses — so the two flows can never drift."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from statistics import median

import cv2

from app.pack.card_crop import refine_card_box
from app.pack.confidence import pack_confidence
from app.pack.identify_core import resolve_identity
from app.pack.ocr import parse_number
from app.pack.pipeline import _decode
from app.pack.rapidocr_reader import detect_lines_xy
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.pack.binder")

# (x_center, y_center, TEXT_UPPER, conf, box_w, box_h) in source pixels.
Line = tuple[float, float, str, float, float, float]

_CAP = 2800            # whole-photo detection cap (binder pages are large)
_COL_GAP = 0.08        # new column when x-gap exceeds this fraction of img width
_ROW_GAP = 0.12        # new cell when y-gap exceeds this fraction of img height
_CELL_W_FRAC = 0.92    # coarse cell box = this fraction of the median grid pitch
_THUMB_W = 240
_CARD_ASPECT = 88 / 63  # height / width of a Pokemon card


@dataclass
class BinderCell:
    cell: tuple[int, int, int, int]   # refined (x, y, w, h) in source pixels
    card: PackCard
    thumb_b64: str | None
    needs_vlm: bool


def _is_stage_label(text: str) -> bool:
    """A card's evolution-stage tag ("BASIC" / "STAGE 1" / "STAGE 2") prints at
    the very top, so it sorts ABOVE the title in the y-ordered name band. Left in,
    a bare "BASIC" fuzzy-hits "Basic <type> Energy" via the set-scoped rematch and
    steals the identity — so it is dropped from the name candidates."""
    letters = re.sub(r"[^a-z]", "", text.lower())
    return letters == "basic" or letters.startswith(("stage", "tage"))


def _diffs(vals: list[float]) -> list[float]:
    return [b - a for a, b in zip(vals, vals[1:])]


def _cluster(items: list[Line], key: int, gap: float) -> list[list[Line]]:
    """Split lines into groups: a new group starts when the sorted ``key`` coord
    jumps by more than ``gap`` pixels (columns on x, cells on y within a column)."""
    groups: list[list[Line]] = []
    cur: list[Line] = []
    prev: float | None = None
    for line in sorted(items, key=lambda t: t[key]):
        if prev is not None and line[key] - prev > gap:
            groups.append(cur)
            cur = []
        cur.append(line)
        prev = line[key]
    if cur:
        groups.append(cur)
    return groups


def _number_and_names(cell: list[Line]):
    """(best pattern_ok NumberReading | None, name candidates as [(text, conf)]).
    The number line is excluded from the name candidates; the rest are ordered by
    y ascending then conf descending — a card's title is its top-most line."""
    best = None  # (conf, index, reading)
    for i, line in enumerate(cell):
        r = parse_number(line[2], line[3])
        if r is not None and r.pattern_ok and (best is None or r.confidence > best[0]):
            best = (r.confidence, i, r)
    reading = best[2] if best else None
    num_idx = best[1] if best else None
    others = [line for i, line in enumerate(cell)
              if i != num_idx and not _is_stage_label(line[2])]
    others.sort(key=lambda line: (line[1], -line[3]))
    return reading, [(line[2], line[3]) for line in others]


def _coarse_box(cell: list[Line], col_pitch: float | None, row_pitch: float | None,
                W: int, H: int) -> tuple[int, int, int, int]:
    """Cell crop: the member-line bbox grown to (a fraction of) the grid pitch,
    centered on the bbox centroid, clamped to the image. Falls back to an
    aspect-shaped box off the text width when a pitch is missing (single col/row)."""
    lefts = [x - w / 2 for x, _y, _t, _c, w, _h in cell]
    rights = [x + w / 2 for x, _y, _t, _c, w, _h in cell]
    tops = [y - h / 2 for _x, y, _t, _c, _w, h in cell]
    bots = [y + h / 2 for _x, y, _t, _c, _w, h in cell]
    min_l, max_r, min_t, max_b = min(lefts), max(rights), min(tops), max(bots)
    cx, cy = (min_l + max_r) / 2, (min_t + max_b) / 2
    if col_pitch and row_pitch:
        cw, ch = _CELL_W_FRAC * col_pitch, _CELL_W_FRAC * row_pitch
    else:
        cw = (max_r - min_l) * 1.6
        ch = cw * _CARD_ASPECT
    x0 = max(0, int(round(cx - cw / 2)))
    y0 = max(0, int(round(cy - ch / 2)))
    x1 = min(W, int(round(cx + cw / 2)))
    y1 = min(H, int(round(cy + ch / 2)))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def _thumb(crop) -> str | None:
    if crop.size == 0:
        return None
    h, w = crop.shape[:2]
    nh = max(1, round(h * _THUMB_W / w))
    interp = cv2.INTER_AREA if w > _THUMB_W else cv2.INTER_CUBIC
    small = cv2.resize(crop, (_THUMB_W, nh), interpolation=interp)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


async def _run_vlm(cells: list[BinderCell], crops: list) -> None:
    """Best-effort VLM pass on the still-uncertain cells (off when VLM_ENDPOINT
    unset). One batch, merged in place; any failure leaves Phase-1 cards intact."""
    from app.pack import vlm_client
    if not vlm_client.enabled() or not any(c.needs_vlm for c in cells):
        return
    try:
        from app.pack.set_resolution import load_denominator_table
        from app.pack.vlm_merge import apply_vlm_answer
        table = load_denominator_table()
        payload = [{"row_index": c.card.row_index, "image": crop,
                    "hint_set": None, "hint_denominator": None}
                   for c, crop in zip(cells, crops) if c.needs_vlm]
        result = await vlm_client.identify(payload)
        if not result:
            return
        for c in cells:
            if c.needs_vlm:
                await apply_vlm_answer(c.card, result.get(c.card.row_index) or {}, table)
        log.info("binder.vlm applied cells=%s", len(payload))
    except Exception as e:
        log.warning("binder.vlm_failed err=%r", e)


async def _attach_prices(cells: list[BinderCell]) -> None:
    from app.db.session import async_session_maker
    from app.prices import latest_price_map
    try:
        async with async_session_maker() as session:
            price_map, _asof = await latest_price_map(session)
        for c in cells:
            if c.card.match_id and (lo_hi := price_map.get(c.card.match_id)):
                c.card.price_usd_low, c.card.price_usd_high = lo_hi
    except Exception as e:
        log.warning("binder.price_failed err=%r", e)


async def scan_binder_page(page_bytes: bytes) -> dict:
    """Whole-page scan -> {"cards": [...], "grid": {rows, cols}, "page_confidence"}.
    Raises ValueError("no_cards_found") when the photo can't be decoded or yields
    no usable text lines."""
    img = await asyncio.to_thread(_decode, page_bytes)
    if img is None:
        raise ValueError("no_cards_found")
    H, W = img.shape[:2]

    lines = await asyncio.to_thread(detect_lines_xy, img, _CAP)
    lines = [line for line in lines if line[3] >= 0.5 and len(line[2].strip()) >= 2]
    if not lines:
        raise ValueError("no_cards_found")

    # Columns on x, then cells on y within each column (source-pixel gaps).
    columns = _cluster(lines, key=0, gap=_COL_GAP * W)
    col_cells = [_cluster(col, key=1, gap=_ROW_GAP * H) for col in columns]
    cols = len(columns)
    rows = max(len(cc) for cc in col_cells)

    # Grid pitch: median spacing between column centers (x) and cell centers (y).
    col_centers = sorted(median([line[0] for line in col]) for col in columns)
    col_pitch = median(_diffs(col_centers)) if len(col_centers) >= 2 else None
    row_gaps: list[float] = []
    for cc in col_cells:
        centers = sorted(sum(line[1] for line in cell) / len(cell) for cell in cc)
        row_gaps += _diffs(centers)
    row_pitch = median(row_gaps) if row_gaps else None

    # Reading order: left-to-right, top-to-bottom (cell-row within column, column).
    records = [(ei, ci, cell)
               for ci, cc in enumerate(col_cells) for ei, cell in enumerate(cc)]
    records.sort(key=lambda r: (r[0], r[1]))

    parsed = [_number_and_names(cell) for _ei, _ci, cell in records]
    results = await asyncio.gather(
        *(resolve_identity(names, reading, None) for reading, names in parsed))

    cells: list[BinderCell] = []
    crops: list = []
    for idx, ((_ei, _ci, cell), res) in enumerate(zip(records, results)):
        coarse = _coarse_box(cell, col_pitch, row_pitch, W, H)
        x, y, w, h = refine_card_box(img, coarse)
        crop = img[y:y + h, x:x + w]
        crops.append(crop)
        card = PackCard(
            row_index=idx, card_number=res.display_number,
            set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
            confidence=0.9 if res.confident else 0.3,
            low_confidence_reason=res.low_confidence_reason,
            needs_review=not res.confident, **res.fields)
        cells.append(BinderCell(cell=(x, y, w, h), card=card,
                                thumb_b64=_thumb(crop), needs_vlm=not res.confident))

    await _run_vlm(cells, crops)
    await _attach_prices(cells)

    page_confidence = pack_confidence([c.card.confidence for c in cells])
    log.info("binder.done grid=%sx%s cells=%s flagged=%s page_conf=%.3f",
             rows, cols, len(cells), sum(1 for c in cells if c.card.needs_review),
             page_confidence)
    out = []
    for c in cells:
        d = c.card.model_dump()
        d["cell"] = list(c.cell)
        d["thumb_b64"] = c.thumb_b64
        out.append(d)
    return {"cards": out, "grid": {"rows": rows, "cols": cols},
            "page_confidence": page_confidence}
