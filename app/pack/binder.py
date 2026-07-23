"""Binder page scan: STRUCTURE-FIRST card-quad detection into grid cells ->
live-style band OCR per cell (name band + number strip) -> the shared identify
ladder (no page prior) -> thumbnails/VLM -> prices.

Real binder pages carry their structure VISUALLY: card rectangles sit in sleeve
pockets. That structure survives full-art cards whose printed text sprawls
unreadably over the whole card face — exactly the case where whole-photo text
clustering shatters a 2x2 page into a dozen fragment cells. So the PRIMARY cell
finder is ``_find_card_quads`` (contour geometry); text clustering is kept only
as the FALLBACK for borderless/edge-case photos where no quads are found.

Each quad cell then identifies the live way from a small name band (top of the
crop) and a number strip (bottom) — stacked into one OCR pass per cell, all cells
run concurrently under ``OCR_GATE``. The identify ladder itself is
``resolve_identity`` — the same core the live single-card flow uses — so the two
flows can never drift."""
from __future__ import annotations

import asyncio
import base64
import bisect
import logging
import re
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from app.pack.card_crop import refine_card_box
from app.pack.confidence import pack_confidence
from app.pack.identify_core import resolve_identity
from app.pack.ocr import parse_number
from app.pack.pipeline import OCR_GATE, _decode
from app.pack.rapidocr_reader import detect_lines_xy
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.pack.binder")

# (x_center, y_center, TEXT_UPPER, conf, box_w, box_h) in source pixels.
Line = tuple[float, float, str, float, float, float]

_CAP = 2800            # whole-photo detection cap (binder pages are large)
_COL_GAP = 0.08        # new column when x-gap exceeds this fraction of img width
_ROW_GAP = 0.12        # numberless fallback: new cell when y-gap exceeds this * H
_FOOTER_TOL = 0.04     # a number's footer (lines just below it) within this * H
                       # of the number snaps up to that number's card
_CELL_W_FRAC = 0.92    # coarse cell box = this fraction of the median grid pitch
_THUMB_W = 240
_CARD_ASPECT = 88 / 63  # height / width of a Pokemon card

# --- quad detection (primary cell finder) ---
_QUAD_LONG = 1600      # downscale long side to this before contour search
_QUAD_ASPECT_MIN = 0.55  # minAreaRect short/long side ratio window (card ~0.716,
_QUAD_ASPECT_MAX = 0.90  # widened from 0.60/0.85 for real-photo perspective tilt)
_QUAD_AREA_MIN = 0.03  # upright-rect area as a fraction of the page
_QUAD_AREA_MAX = 0.35
_QUAD_IOU = 0.4        # NMS: drop a box overlapping a kept box more than this
_QUAD_MED_MULT = 3.0   # grid sanity: keep boxes within this factor of median area
_NAME_BAND = 0.28      # per-cell name band = top this fraction of the crop
_NUM_STRIP = 0.20      # per-cell number strip = bottom this fraction of the crop
_BAND_CAP = 1000       # stacked name-band+strip OCR downscale cap (text stays large)


@dataclass
class BinderCell:
    cell: tuple[int, int, int, int]   # refined (x, y, w, h) in source pixels
    card: PackCard
    thumb_b64: str | None
    needs_vlm: bool


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection-over-union of two upright (x, y, w, h) boxes."""
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _find_card_quads(img) -> list[tuple[int, int, int, int]]:
    """PRIMARY cell finder: locate card rectangles by contour geometry.

    Real binder pages have strong visual structure (cards in sleeve pockets)
    that survives full-art faces where text scatters. Downscale, edge-detect,
    find contours, keep the card-shaped quads, non-max-suppress the overlaps,
    and sanity-check the grid by median area. Returns upright (x, y, w, h) boxes
    in SOURCE pixels, or [] on any failure."""
    try:
        H, W = img.shape[:2]
        long_side = max(H, W)
        scale = _QUAD_LONG / long_side if long_side > _QUAD_LONG else 1.0
        small = (cv2.resize(img, (int(round(W * scale)), int(round(H * scale))),
                            interpolation=cv2.INTER_AREA)
                 if scale != 1.0 else img)
        sh, sw = small.shape[:2]
        page_area = float(sh * sw)

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)
        # One dilation pass only: two passes bridge the thin sleeve gutter and
        # merge neighbouring cards into a single blob (real-page evidence).
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        # RETR_LIST, not RETR_EXTERNAL: on a real page the dilated page-perimeter
        # (binder frame + stitching) connects into one outer contour, leaving the
        # card-border rectangles NESTED — external retrieval would miss them all.
        contours, _ = cv2.findContours(
            edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[tuple[int, int, int, int]] = []
        for cnt in contours:
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
            if not (4 <= len(approx) <= 8):
                continue
            (_c, (rw, rh), _a) = cv2.minAreaRect(cnt)
            if rw < 1 or rh < 1:
                continue
            if not (_QUAD_ASPECT_MIN <= min(rw, rh) / max(rw, rh) <= _QUAD_ASPECT_MAX):
                continue
            bx, by, bw, bh = cv2.boundingRect(cnt)
            if not (_QUAD_AREA_MIN <= (bw * bh) / page_area <= _QUAD_AREA_MAX):
                continue
            boxes.append((bx, by, bw, bh))
        if not boxes:
            return []

        # NMS: largest first, drop any box that overlaps a kept box too much.
        boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
        kept: list[tuple[int, int, int, int]] = []
        for b in boxes:
            if all(_iou(b, k) <= _QUAD_IOU for k in kept):
                kept.append(b)

        # Grid sanity: a real page's pockets are uniform — drop outliers whose
        # area strays beyond 3x of the median (stray sub-detail / merged pairs).
        med_area = median([k[2] * k[3] for k in kept])
        kept = [k for k in kept
                if med_area / _QUAD_MED_MULT <= k[2] * k[3] <= med_area * _QUAD_MED_MULT]

        inv = 1.0 / scale
        return [(int(round(x * inv)), int(round(y * inv)),
                 int(round(w * inv)), int(round(h * inv))) for x, y, w, h in kept]
    except Exception:
        return []


def _quad_reading_order(
    boxes: list[tuple[int, int, int, int]]
) -> tuple[list[tuple[int, int, int, int]], int, int]:
    """Order card quads into reading order (row bands top->bottom, left->right)
    and report the grid. Boxes group into a row band when their y-centers fall
    within 0.5x the median box height; bands sort by y, boxes within by x."""
    med_h = median([h for _x, _y, _w, h in boxes])
    tol = 0.5 * med_h
    by_y = sorted(boxes, key=lambda b: b[1] + b[3] / 2.0)
    bands: list[list[tuple[int, int, int, int]]] = []
    cur: list[tuple[int, int, int, int]] = []
    prev_yc: float | None = None
    for b in by_y:
        yc = b[1] + b[3] / 2.0
        if prev_yc is not None and yc - prev_yc > tol and cur:
            bands.append(cur)
            cur = []
        cur.append(b)
        prev_yc = yc
    if cur:
        bands.append(cur)
    ordered: list[tuple[int, int, int, int]] = []
    for band in bands:
        ordered.extend(sorted(band, key=lambda b: b[0] + b[2] / 2.0))
    return ordered, len(bands), max(len(band) for band in bands)


def _is_noise_name(text: str) -> bool:
    """Name-band tokens that are NOT part of a card title: the stage tag, the
    "HP" label, and the bare HP value (a pure number). Dropped before the title
    rows are rebuilt so they can't pollute the joined name."""
    t = text.strip()
    return _is_stage_label(t) or t == "HP" or t.isdigit()


def _name_texts_from_band(name_xy: list) -> list[tuple[str, float]]:
    """Rebuild a card's title lines from the name-band OCR detections.

    A printed title is one horizontal line, but the recognizer frequently
    fragments it — most importantly it splits a "Trainer's Pokemon" possessive
    ("Erika's Oddish") into two boxes, and matched line-by-line the bare Pokemon
    name resolves to a commoner printing in the WRONG set (a confident-wrong). So
    group the detections into rows (y-centers within 0.5x the median box height),
    order each row LEFT-TO-RIGHT and join it into one candidate — reconstructing
    exactly the whole-line text the whole-photo pass would have read. Rows sort
    by y then confidence; ``name_xy`` is detect_lines_xy output."""
    kept = [l for l in name_xy if not _is_noise_name(l[2])]
    if not kept:
        return []
    tol = 0.5 * median([l[5] for l in kept])  # box height
    kept.sort(key=lambda l: l[1])             # y-center ascending
    rows: list[list] = []
    cur: list = []
    prev_y: float | None = None
    for l in kept:
        if prev_y is not None and l[1] - prev_y > tol and cur:
            rows.append(cur)
            cur = []
        cur.append(l)
        prev_y = l[1]
    if cur:
        rows.append(cur)
    joined = []
    for row in rows:
        row.sort(key=lambda l: l[0])          # x-center: left-to-right
        joined.append((min(l[1] for l in row),
                       " ".join(l[2] for l in row),
                       max(l[3] for l in row)))
    joined.sort(key=lambda t: (t[0], -t[2]))   # topmost line first (the title)
    return [(t, c) for _y, t, c in joined]


async def _identify_quad_cell(img, box: tuple[int, int, int, int], W: int, H: int):
    """Identify one card quad the live way: read its NAME BAND (top of the crop)
    and NUMBER STRIP (bottom) with OCR under OCR_GATE, then run the shared
    identify ladder. Returns (clamped_box, crop, IdentityResult).

    The two bands are the only card regions live-style identification needs, so
    rather than OCR the whole card (dense, slow) or fire two separate detector
    passes (double the fixed per-call cost), the name band and number strip are
    stacked into ONE small image and detected in a single pass — halving OCR wall
    time on multi-megapixel pages, which is what lets the per-cell path beat the
    whole-photo pass. The stack keeps the two bands vertically separated, so
    lines split cleanly back by y (name band above the seam, number strip below).
    Cells run concurrently and OCR_GATE bounds the batch onto the CPU; the ladder
    that follows is DB I/O, not OCR, so it runs free. The engine is pre-warmed
    (see scan_binder_page) so the concurrent burst never races the lazy RapidOCR
    init. A tight cap keeps the large title/number text sharp while cutting time
    (the synthetic-fixture bands sit below the cap, so it is unaffected there)."""
    x, y, w, h = box
    x0 = max(0, min(x, W - 1))
    y0 = max(0, min(y, H - 1))
    x1 = max(x0 + 1, min(x + w, W))
    y1 = max(y0 + 1, min(y + h, H))
    crop = img[y0:y1, x0:x1]
    ch = crop.shape[0]
    seam = max(1, int(ch * _NAME_BAND))              # name band = rows [0, seam)
    name_band = crop[:seam]
    strip = crop[max(0, int(ch * (1.0 - _NUM_STRIP))):]
    stacked = np.vstack([name_band, strip])          # bands share the crop width

    async with OCR_GATE:
        # detect_lines_xy keeps x/box-height (needed to rebuild the title rows);
        # one pass over the stacked bands, split by the seam afterwards.
        lines = await asyncio.to_thread(detect_lines_xy, stacked, _BAND_CAP)

    name_xy = [l for l in lines if l[1] < seam]        # above the seam = title band
    strip_lines = [(l[1], l[2], l[3]) for l in lines if l[1] >= seam]  # number strip

    # number: best pattern_ok parse, strip first (the collector number lives in
    # the bottom strip; the name band is only a fallback source).
    name_lines = [(l[1], l[2], l[3]) for l in name_xy]
    reading = None
    for _y, text, conf in (sorted(strip_lines, key=lambda t: -t[2])
                           + sorted(name_lines, key=lambda t: -t[2])):
        r = parse_number(text, conf)
        if r is not None and r.pattern_ok:
            reading = r
            break

    name_texts = _name_texts_from_band(name_xy)
    res = await resolve_identity(name_texts, reading, None)
    return (x0, y0, x1 - x0, y1 - y0), crop, res


def _is_stage_label(text: str) -> bool:
    """A card's evolution-stage tag ("BASIC" / "STAGE 1" / "STAGE 2") prints at
    the very top, so it sorts ABOVE the title in the y-ordered name band. Left in,
    a bare "BASIC" fuzzy-hits "Basic <type> Energy" via the set-scoped rematch and
    steals the identity — so it is dropped from the name candidates."""
    letters = re.sub(r"[^a-z]", "", text.lower())
    return letters == "basic" or letters.startswith(("stage", "tage"))


def _diffs(vals: list[float]) -> list[float]:
    return [b - a for a, b in zip(vals, vals[1:])]


def _columns(lines: list[Line], gap: float) -> list[list[Line]]:
    """Split lines into columns: a new column starts when the sorted x-center
    jumps by more than ``gap`` pixels."""
    cols: list[list[Line]] = []
    cur: list[Line] = []
    prev: float | None = None
    for line in sorted(lines, key=lambda t: t[0]):
        if prev is not None and line[0] - prev > gap:
            cols.append(cur)
            cur = []
        cur.append(line)
        prev = line[0]
    if cur:
        cols.append(cur)
    return cols


def _gap_split(seg: list[Line], gap: float) -> list[list[Line]]:
    """Split an (already y-sorted) segment on y-gaps over ``gap``."""
    out: list[list[Line]] = []
    cur: list[Line] = []
    prev_y: float | None = None
    for line in seg:
        if prev_y is not None and line[1] - prev_y > gap and cur:
            out.append(cur)
            cur = []
        cur.append(line)
        prev_y = line[1]
    if cur:
        out.append(cur)
    return out


def _cells(column: list[Line], H: int) -> list[list[Line]]:
    """Split a column's lines (top->bottom) into per-card cells.

    PRIMARY: the printed collector number anchors a card's BOTTOM edge — assign
    every line to its nearest number-anchor. This is immune to a card's interior
    artwork band, whose vertical text gap EXCEEDS the inter-card gutter gap, so no
    y-gap threshold can separate cards (the gutter is the smaller gap — a pure gap
    rule splits real cards in two and merges neighbors). A card's content sits above
    its number and its footer (copyright/illustrator) a hair below, so a line joins
    the first anchor at or above ``line_y - _FOOTER_TOL*H`` (the tolerance snaps the
    footer up to its own card instead of leaking into the next one's name band).

    FALLBACK (0.12*H y-gap): a column whose numbers all went unread has no anchors,
    so split it on the row-gap instead."""
    ordered = sorted(column, key=lambda t: t[1])
    anchors = [line[1] for line in ordered
               if (r := parse_number(line[2], line[3])) is not None and r.pattern_ok]
    if not anchors:
        return _gap_split(ordered, _ROW_GAP * H)
    tol = _FOOTER_TOL * H
    cells: list[list[Line]] = [[] for _ in anchors]
    for line in ordered:
        k = bisect.bisect_left(anchors, line[1] - tol)
        cells[min(k, len(anchors) - 1)].append(line)
    return [c for c in cells if c]


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


async def _finish(cell_specs: list, rows: int, cols: int) -> dict:
    """Shared tail for both cell finders. ``cell_specs`` is an ordered list of
    (cell_box, crop, IdentityResult); builds the PackCards, thumbs, runs the VLM
    batch on the flagged cells (with their crops) and prices, and assembles the
    response. Row indices are (re)assigned in the given reading order."""
    cells: list[BinderCell] = []
    crops: list = []
    for idx, (box, crop, res) in enumerate(cell_specs):
        crops.append(crop)
        card = PackCard(
            row_index=idx, card_number=res.display_number,
            set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
            confidence=0.9 if res.confident else 0.3,
            low_confidence_reason=res.low_confidence_reason,
            needs_review=not res.confident, **res.fields)
        cells.append(BinderCell(cell=box, card=card,
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


async def _scan_text_clusters(img, W: int, H: int) -> dict:
    """FALLBACK cell finder: whole-photo OCR + geometric gap-clustering.

    Kept unchanged as the fallback for borderless/edge-case photos where quad
    detection finds fewer than two cards. Columns split on x-gaps; per-card
    cells split on the collector-number anchor (with a y-gap fallback for
    numberless columns), then contour-refined crops feed the shared tail."""
    lines = await asyncio.to_thread(detect_lines_xy, img, _CAP)
    lines = [line for line in lines if line[3] >= 0.5 and len(line[2].strip()) >= 2]
    if not lines:
        raise ValueError("no_cards_found")

    # Columns on x (source-pixel gap), then per-card cells on y within each column
    # (anchored on the collector number, with a y-gap fallback for numberless cols).
    columns = _columns(lines, gap=_COL_GAP * W)
    col_cells = [_cells(col, H) for col in columns]
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

    cell_specs: list = []
    for (_ei, _ci, cell), res in zip(records, results):
        coarse = _coarse_box(cell, col_pitch, row_pitch, W, H)
        x, y, w, h = refine_card_box(img, coarse)
        crop = img[y:y + h, x:x + w]
        cell_specs.append(((x, y, w, h), crop, res))
    return await _finish(cell_specs, rows, cols)


async def scan_binder_page(page_bytes: bytes) -> dict:
    """Whole-page scan -> {"cards": [...], "grid": {rows, cols}, "page_confidence"}.
    Raises ValueError("no_cards_found") when the photo can't be decoded or neither
    cell finder yields anything.

    PRIMARY path: structure-first card-quad detection (survives full-art faces).
    FALLBACK path: whole-photo text clustering (borderless/edge-case photos)."""
    img = await asyncio.to_thread(_decode, page_bytes)
    if img is None:
        raise ValueError("no_cards_found")
    H, W = img.shape[:2]

    quads = await asyncio.to_thread(_find_card_quads, img)
    if len(quads) >= 2:
        log.info("binder.quads found=%s fallback=%s", len(quads), False)
        ordered, rows, cols = _quad_reading_order(quads)
        # Warm the lazily-loaded RapidOCR engine single-threaded BEFORE the
        # concurrent per-cell burst: several cells' first OCR calls would
        # otherwise race the engine init and some return empty (dropped reads).
        await asyncio.to_thread(detect_lines_xy, img[:64, :64], 64)
        specs = await asyncio.gather(
            *(_identify_quad_cell(img, box, W, H) for box in ordered))
        return await _finish(list(specs), rows, cols)

    log.info("binder.quads found=%s fallback=%s", len(quads), True)
    return await _scan_text_clusters(img, W, H)
