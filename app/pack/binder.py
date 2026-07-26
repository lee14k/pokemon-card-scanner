"""Binder page scan: STRUCTURE-FIRST card-quad detection into grid cells ->
live-style band OCR per cell (name band + number strip) -> the shared identify
ladder -> a strictly-guarded page-prior second pass -> thumbnails/VLM -> prices.

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
flows can never drift.

A binder page is usually ONE set, so the page gets a second, DB-only identify
pass: ``_page_modal`` elects the page's set from the cells that came back
confident and ``_apply_page_prior`` re-runs the ladder for the ones that didn't.
That prior is identity-DECIDING (see ``_prior_denominator_ok``), so both the
election and the per-cell hand-off are deliberately hard to earn."""
from __future__ import annotations

import asyncio
import base64
import bisect
import logging
import re
from collections import Counter
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from app.cards import normalize_local_id
from app.pack import scan_followup as followup
from app.pack import vlm_client
from app.pack.card_crop import refine_card_box
from app.pack.confidence import pack_confidence
from app.pack.identify_core import IdentityResult, SessionPrior, resolve_identity
from app.pack.name_index import get_name_index, normalize_name
from app.pack.ocr import NumberReading, parse_number
from app.pack.pipeline import OCR_GATE, _decode
from app.pack.rapidocr_reader import detect_lines_xy
from app.pack.set_resolution import entry_for_set_id, load_denominator_table
from app.schemas import PackCard
from app.timing import new_scan_id, stage

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
_MODAL_MIN_SUPPORT = 2  # confident cells needed to elect a page modal set (_page_modal)


@dataclass
class BinderCell:
    cell: tuple[int, int, int, int]   # refined (x, y, w, h) in source pixels
    card: PackCard
    thumb_b64: str | None
    needs_vlm: bool


@dataclass
class CellRead:
    """One cell's PASS-1 read — everything that went INTO the identify ladder,
    retained so the page-prior second pass can re-run the ladder without
    re-doing any OCR (``_apply_page_prior`` is DB-only).

    Both cell finders produce these, which is what lets the page prior live in
    the shared tail (``_finish``) instead of being implemented twice.

    ``name_texts``/``reading`` are the ladder's actual inputs; ``texts`` is the
    cell's raw OCR strings, which are not a ladder input at all — they are the
    VLM merge's pixel-corroboration evidence and only ride along here so the two
    per-cell lists cannot fall out of order."""
    box: tuple[int, int, int, int]
    crop: object                              # BGR ndarray (thumb + VLM encode)
    res: IdentityResult
    texts: list[str]
    name_texts: list[tuple[str, float]]
    reading: NumberReading | None


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
        text = " ".join(l[2] for l in row)
        # An evolution note ("Evolves from Goomy") sits in the name band and its
        # bare species name matched as the TITLE — a confident-wrong on real
        # pages (Sliggoo cell -> "Goomy"). Row reconstruction makes the note one
        # row, so it can be dropped wholesale.
        if "EVOLVES" in text or "VOLVES" in text:
            continue
        joined.append((min(l[1] for l in row), text, max(l[3] for l in row)))
    joined.sort(key=lambda t: (t[0], -t[2]))   # topmost line first (the title)
    return [(t, c) for _y, t, c in joined]


async def _identify_quad_cell(img, box: tuple[int, int, int, int],
                              W: int, H: int) -> CellRead:
    """Identify one card quad the live way: read its NAME BAND (top of the crop)
    and NUMBER STRIP (bottom) with OCR under OCR_GATE, then run the shared
    identify ladder. Returns the cell's ``CellRead`` (pass-1 result plus the
    ladder inputs, which the page-prior pass re-uses without re-reading pixels).

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
    # The cell's raw OCR texts ride along so the VLM merge can demand pixel
    # corroboration of a claimed number (hallucination guard).
    return CellRead(box=(x0, y0, x1 - x0, y1 - y0), crop=crop, res=res,
                    texts=[l[2] for l in lines],
                    name_texts=name_texts, reading=reading)


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


def _page_modal(cells: list[BinderCell]) -> SessionPrior | None:
    """The page's own modal set as a prior, or None when the page hasn't earned
    one. Pure in the sense that matters for reuse: reads only
    ``cell.card.set_id``/``needs_review`` plus the denominator table, mutates
    nothing, and is deterministic on its input. Not I/O-free on the very first
    call in a process — ``entry_for_set_id`` loads the (memoized) table, which
    reads a JSON file; every later call is pure computation.

    Mirrors the pack's modal-hint derivation (pipeline._vlm_payload +
    _apply_constraints): vote among the CONFIDENT cells only, and require
    ``_MODAL_MIN_SUPPORT`` supporters — one confident cell is an anecdote, not a
    page context, and a binder page is legitimately allowed to be a mixed
    scrapbook. The denominator comes from the elected set's catalog row and only
    when that row has exactly ONE denominator: a set with reprint denominators
    (or none recorded) has no single right answer to offer.

    The winner must be the STRICT maximum. A tie means the page has no dominant
    set, and breaking it on ``Counter`` insertion order would hand the worker a
    coin-flip set plus a coin-flip denominator — which is not harmless (see
    ``_vlm_payload``: a hint CAN reach an identity). The synthetic 3x3 fixture is
    exactly this case (me05 x2 vs me02.5 x2, and neither is the set of either
    flagged cell), so it correctly yields no hint.

    ``needs_review`` rather than the frozen ``needs_vlm`` flag, so a caller that
    runs this AFTER a VLM merge sees the cells that are confident NOW — which is
    also what live_session.prior() means by its ``state == "ok"`` filter.

    Returned as a SessionPrior because that is exactly what the shape is: the
    binder VLM hints use ``.set_name``/``.denominator``, and the identify ladder
    (resolve_identity) takes this object as-is, so a page-level prior can reuse
    the vote without recomputing it."""
    votes = Counter(c.card.set_id for c in cells
                    if not c.card.needs_review and c.card.set_id)
    if not votes:
        return None
    top = votes.most_common(2)
    set_id, n = top[0]
    if n < _MODAL_MIN_SUPPORT or (len(top) > 1 and top[1][1] == n):
        return None
    entry = entry_for_set_id(set_id)
    if entry is None:
        return None
    return SessionPrior(
        set_id=set_id, set_name=entry.set_name,
        denominator=entry.denominators[0] if len(entry.denominators) == 1 else None)


def _denominator_owners(den: str) -> tuple:
    """Every catalog row that prints ``den``, comparing NORMALIZED forms.

    A printed denominator is zero-padded to the numerator's width ("096/094")
    while the table stores the bare count ("94"), so raw string equality misses
    12 of the 53 rows outright — every me-era set among them. ``normalize_local_id``
    is the project's existing both-sides normalizer for exactly this shape
    (strips leading zeros off the numeric tail, preserves an alpha prefix), so
    "094" and "94" compare equal while the gallery denominators ("TG30", "GG70",
    "SV122") keep theirs."""
    key = normalize_local_id(den)
    return tuple(s for s in load_denominator_table().sets
                 if any(normalize_local_id(d) == key for d in s.denominators))


def _prior_denominator_ok(reading: NumberReading | None,
                          prior: SessionPrior) -> bool:
    """A3's non-negotiable per-cell guard: may THIS cell be handed the page prior?

    A ``SessionPrior`` is identity-DECIDING inside ``resolve_identity``, not a
    hint: its own rung promotes a cell to ``confident`` on nothing but "the
    numerator exists in the prior's set" (identify_core.py), and its denominator
    substitutes into name matching when the cell read none. A prior handed to
    the wrong cell therefore mints a confident-WRONG identity outright — so the
    page's opinion only reaches a cell that does not CONTRADICT it:

      * no reading at all -> nothing to contradict. (``_apply_page_prior`` will
        not act on such a cell anyway — it requires a read numerator — but the
        hint suppression in ``_vlm_payload`` shares this predicate and a blind
        cell contradicts nothing there either.)
      * a printed denominator -> it must belong to the modal set and to NO OTHER
        SET. "The modal set happens to print it" is not enough: 6 denominators
        are shared across 15 of the 53 catalog rows (182 = Paradox Rift AND
        Destined Rivals; 86 = Chaos Rising / Black Bolt / White Flare; TG30
        spans all four Trainer Galleries; also 159, 189, 198). A correct
        "005/182" Destined Rivals cell on a Paradox-Rift-dominant page satisfies
        "the modal set prints 182" and would be confidently relabelled Paradox
        Rift — a confident-wrong minted from an input with NO OCR error at all.
        So the test is UNIQUE ownership, ``_denominator_owners(den) == (entry,)``,
        which fails closed on those 15 rows and keeps working for the other 38.
      * a promo prefix ("SWSH"/"SVP"/"MEP") -> ``parse_number`` emits this
        INSTEAD of a denominator (``NUMBER_RE`` and ``PROMO_RE`` are exclusive,
        so a promo read has ``denominator=None``), and it names a Black Star
        Promos set outright. Left to the "absent denominator" branch a
        ``MEP 037`` cell would sail through and get promoted into whatever set
        the page voted for, so the prefix is treated as the set-naming alpha
        denominator it is: the prior applies only when that promo set IS the
        modal set. Note this is the same unique-ownership rule — a promo prefix
        maps to exactly one row by construction (``by_promo_prefix``).

    Padding and alpha prefixes are handled by ``_denominator_owners``; a modal
    set with no recorded denominator (the three promo rows) owns nothing, so any
    read denominator vetoes the prior there."""
    if reading is None:
        return True
    if reading.prefix:
        promo = load_denominator_table().by_promo_prefix.get(reading.prefix.upper())
        return promo is not None and promo.set_id == prior.set_id
    den = (reading.denominator or "").strip()
    if not den:
        return True
    entry = entry_for_set_id(prior.set_id)
    return entry is not None and _denominator_owners(den) == (entry,)


def _improves(old: IdentityResult, new: IdentityResult) -> bool:
    """May pass 2's re-run REPLACE the pass-1 result? Forward moves only.

    A prior can hurt as well as help — it can empty a name-match's candidate
    list via the denominator narrowing — so a re-run that came back weaker is
    DISCARDED rather than written back. The accepted gains are the two the plan
    names: a cell that becomes confident, or one that resolves a set it had no
    opinion on.

    The second branch is dormant against today's ``resolve_identity`` (it only
    fills ``set_id``/``set_name`` on a result it is also marking confident, so a
    non-confident result always carries no set). It is written out anyway so the
    "never downgrade" contract is enforced by this predicate and not by that
    incidental property of the core."""
    if new.confident:
        return not old.confident
    return (new.set_id or new.set_name) is not None \
        and (old.set_id or old.set_name) is None


async def _name_contradicts_numerator(read: CellRead, prior: SessionPrior) -> bool:
    """Does the cell's own NAME point at a different card number than its own
    NUMBER? Then the cell disagrees with itself and pass 2 must leave it alone.

    Two distinct harms this prevents, both of which end in a confident-wrong:

      * the ladder's unique-name rung (identify_core.py) can promote on the NAME
        while keeping the READ numerator, producing a confident cell whose name
        and printed number belong to different cards;
      * the prior rung promotes on the numerator and then re-keys the catalog
        lookup by it, OVERWRITING a pass-1 name that may well have been the
        correct one — so a single misread digit silently renames the card.

    The name resolution mirrors ``resolve_identity``'s (first candidate line that
    matches wins; the set/denominator-scoped re-match is the fallback for
    ambiguous or missing hits) computed as PASS 2 would see it, i.e. with the
    prior's denominator standing in when the cell read none. It is a deliberate
    mirror and must stay in step with the core; being read-only it can only ever
    withhold a rescue, never grant one.

    The contradiction test is "does ANY printing of the matched name sit at the
    read numerator", NOT ``name_match.local_id != numerator``. Two reasons, both
    measured against the real index:

      * ``local_id`` is only a representative. A name with several printings
        yields ``cands[0]`` — an arbitrary one — so the direct comparison vetoes
        the honest case where name and number describe the same card in a
        different printing: "Pinsir" reports ``local_id=127`` while the cell
        correctly reads sv06's 168, and both ARE Pinsir.
      * ``NameMatch.ambiguous`` cannot be used to carve that out, because it
        conflates "several printings" with the substring hazard. Empirically it
        is set for essentially every common Pokemon name — "Tangela" narrows to
        exactly one printing and is still ``ambiguous=True`` — so gating on it
        would switch this guard off almost everywhere.

    Printings are narrowed by the effective denominator the same way
    ``NameIndex.match`` narrows (``card_count_official``), so the question asked
    is the precise one: *in a set of this size, is the card this name names
    printed at the number this cell read?* Tangela/004 -> no (contradiction);
    Pinsir/168, Dipplin/170, Infernape/173 -> yes. The narrowing falls back to
    the unscoped printings when it would empty the list, and the private
    ``_entries`` access mirrors ``resolve_identity``'s own use of
    ``idx._official_to_sets``."""
    if not read.name_texts or not (read.reading and read.reading.numerator):
        return False
    idx = await get_name_index()
    den = read.reading.denominator or prior.denominator
    name_match = None
    top_name_text = None
    for text, _conf in read.name_texts:
        if top_name_text is None:
            top_name_text = text
        m = idx.match(text, denominator=den)
        if m is not None:
            name_match = m
            break
    if (name_match is None or name_match.ambiguous) and top_name_text:
        scoped = idx.match_in_set(top_name_text, set_id=prior.set_id, denominator=den)
        if scoped is not None and not scoped.ambiguous:
            name_match = scoped
    if name_match is None:
        return False
    printings = idx._entries.get(normalize_name(name_match.card_name), ())
    if den and den.isdigit():
        # (set_id, set_name, local_id, card_name, card_count_official)
        printings = [e for e in printings if e[4] == int(den)] or printings
    num = normalize_local_id(read.reading.numerator)
    return all(normalize_local_id(e[2]) != num for e in printings)


def _pack_card(row_index: int, res: IdentityResult) -> PackCard:
    """One cell's response card from an identity result. Shared by pass 1 and the
    page-prior pass so a rescued cell cannot drift in shape from a first-pass one."""
    return PackCard(
        row_index=row_index, card_number=res.display_number,
        set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
        confidence=0.9 if res.confident else 0.3,
        low_confidence_reason=res.low_confidence_reason,
        needs_review=not res.confident, **res.fields)


async def _apply_page_prior(cells: list[BinderCell], reads: list[CellRead],
                            prior: SessionPrior | None) -> list[int]:
    """PASS 2 (A3): re-run the identify ladder for the page's NON-confident cells
    with the page's elected set as a prior. Returns the ``row_index`` of every
    cell it actually rescued; ``cells``/``reads`` are updated in place for those
    cells and left untouched for all the others.

    ``prior`` is whatever ``_page_modal`` elected from the PASS-1 confident cells
    — strict unique maximum, >= ``_MODAL_MIN_SUPPORT`` supporters. ``None`` (a
    tie, too little support, or an unknown set) means the page never earned an
    opinion and this is a no-op, which is today's behaviour exactly.

    A cell must clear FOUR filters to be re-run, and a fifth to be written back:

      1. NOT ALREADY CONFIDENT. ``todo`` is the only source of indices this
         function ever writes to, and every candidate is filtered on
         ``not r.res.confident``. There is no other write, so a pass-1 confident
         cell cannot be touched.
      2. IT READ A NUMERATOR. Without one the prior can still reach the cell
         through the NAME path — ``match_in_set`` scoped to the prior's set, and
         the prior's denominator narrowing ``idx.match`` — and on the 17
         self-mapped sets (where the prior's id space happens to match the name
         index's) that path fuzzy-matches a garbled title onto whatever card in
         the modal set scores highest: a bare "ENERGY" became a confident
         "Shadowy Darkness Energy", "Kyurem ex" became "Mega Delphox ex". None of
         that is protected by the denominator guard, because there is no
         denominator. So pass 2 acts ONLY through the numerator-in-set rung the
         guard does protect; number-blind cells stay flagged and go to the VLM
         exactly as they do today.
      3. ITS DENOMINATOR DOES NOT CONTRADICT THE PAGE (``_prior_denominator_ok``).
      4. ITS NAME DOES NOT CONTRADICT ITS OWN NUMBER
         (``_name_contradicts_numerator``).
      5. NO DOWNGRADE. Every re-run goes through ``_improves`` before it is
         written back; a weaker (or equal) result is dropped on the floor.

    DB-only by construction: it consumes the retained ``CellRead.name_texts`` /
    ``CellRead.reading`` and never sees a pixel, so the cost is the ladder's own
    catalog I/O for a handful of cells (issued concurrently), not a second OCR."""
    if prior is None or not prior.set_id:
        return []
    # `not res.confident` is the same population as `card.needs_review` here —
    # the card was built from that very result — but the result is the fact and
    # the card is a projection of it, so filter on the fact.
    candidates = [i for i, r in enumerate(reads)
                  if not r.res.confident
                  and r.reading is not None and r.reading.numerator
                  and _prior_denominator_ok(r.reading, prior)]
    if not candidates:
        return []
    contradicts = await asyncio.gather(
        *(_name_contradicts_numerator(reads[i], prior) for i in candidates))
    todo = [i for i, bad in zip(candidates, contradicts) if not bad]
    if not todo:
        return []
    results = await asyncio.gather(
        *(resolve_identity(reads[i].name_texts, reads[i].reading, prior)
          for i in todo))
    rescued: list[int] = []
    for i, new in zip(todo, results):
        if not _improves(reads[i].res, new):
            continue
        reads[i].res = new
        cells[i].card = _pack_card(cells[i].card.row_index, new)
        cells[i].needs_vlm = not new.confident
        rescued.append(cells[i].card.row_index)
    return rescued


def _vlm_payload(cells: list[BinderCell], reads: list[CellRead],
                 prior: SessionPrior | None) -> list[dict]:
    """The VLM request body for the still-uncertain cells, encoded UP FRONT.

    Blocking (cv2 JPEG encode per cell) — callers run it in a thread. Encoding
    here rather than inside ``identify`` is what lets the background follow-up
    hold a few KB of base64 per flagged cell instead of pinning the crops (and,
    through them, whatever the decoder allocated for a multi-megapixel page) for
    the whole RunPod round trip.

    The page's modal set rides along as a HINT — the pack flow has always sent
    one and the binder sent nulls, so the worker got no context at all for
    exactly the cells that read worst. ``prior`` is the page prior ``_finish``
    already elected with ``_page_modal`` and spent on pass 2; it is passed in
    rather than recomputed so the hint the worker sees is provably the same set
    the second pass acted on (recomputing after pass 2 could elect a DIFFERENT
    set off the cells the prior itself just promoted — a vote counting its own
    output).

    A hint is NOT harmless, which is why ``_page_modal`` demands a strict
    majority-of-one before offering it. The guards in ``apply_vlm_answer`` do not
    contain a hint the model has echoed back:
      * a NAMELESS answer skips the name cross-check entirely (it only runs when
        the worker returned a name), and
      * ``_numerator_corroborated`` deliberately passes when the cell's own OCR
        read no N/N pattern at all ("OCR was blind here: no contradiction") —
        which is precisely the state of the cells we send, and
      * the echoed set_name/denominator is what pins ``set_id``, which then fills
        ``name`` via the keyed re-lookup,
    so on an OCR-blind cell a hint-consistent nameless answer clears
    needs_review on the strength of the hint alone. In the other direction an
    echoed denominator can empty the name-first candidate list (the
    denominator-contradiction veto) and kill a CORRECT name resolution.

    Hence: hint only when the page really has one dominant set, and never
    override a guard to make room for one.

    Cells are selected on ``needs_vlm``, which ``_apply_page_prior`` has already
    cleared for anything it rescued — a cell the page prior settled is not sent
    to the worker at all.

    The hint is applied PER CELL under the same veto pass 2 used
    (``_prior_denominator_ok``). A cell reaches the worker precisely because pass
    2 refused it, and the commonest reason to refuse is that its printed
    denominator contradicts the page — so handing that cell the modal set as
    context would push the model toward the one set the cell's own pixels rule
    out, and an echoed answer can carry an identity through the merge guards
    (see above). A vetoed cell is therefore sent hint-free."""
    payload: list[dict] = []
    hinted = 0
    for c, r in zip(cells, reads):
        if not c.needs_vlm:
            continue
        b64 = vlm_client.jpeg_b64(r.crop)
        if b64 is None:
            continue
        hint = prior if (prior and _prior_denominator_ok(r.reading, prior)) else None
        hinted += hint is not None
        # kind="full_card": a binder cell crop is a whole card, never a strip.
        payload.append({"row_index": c.card.row_index, "image_b64": b64,
                        "hint_set": hint.set_name if hint else None,
                        "hint_denominator": hint.denominator if hint else None,
                        "kind": "full_card"})
    if payload:
        log.info("binder.vlm_hints set=%s den=%s cells=%s hinted=%s",
                 prior.set_name if prior else "-",
                 prior.denominator if prior else "-", len(payload), hinted)
    return payload


async def _run_vlm(cells: list[BinderCell], payload: list[dict],
                   texts_by_row: dict[int, list[str]]) -> None:
    """Best-effort VLM pass on the still-uncertain cells. One batch, merged in
    place; any failure leaves Phase-1 cards intact. Hallucination guards:
    identical answers claimed for 3+ cells collapse, and a claimed number must be
    corroborated by the cell's own OCR text to clear the review flag (production
    evidence: garbage crops minted the same confident identity three times on one
    page).

    ``payload`` comes from ``_vlm_payload`` — the enabled/anything-flagged checks
    live in ``_vlm_prepare``, and ``_finish`` never spawns this with an empty one."""
    try:
        from app.pack.vlm_merge import apply_vlm_answer, collapse_duplicate_answers
        table = load_denominator_table()
        result = await vlm_client.identify(payload)
        if not result:
            return
        result = collapse_duplicate_answers(result)
        for c in cells:
            if c.needs_vlm:
                await apply_vlm_answer(
                    c.card, result.get(c.card.row_index) or {}, table,
                    ocr_texts=texts_by_row.get(c.card.row_index) or None)
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


def _cell_dict(c: BinderCell, state: str | None = None) -> dict:
    """One response/follow-up card: the PackCard plus the binder-only fields the
    review grid draws with. ``state`` is attached ONLY when a follow-up exists —
    with no VLM configured the dict is exactly what it was before follow-ups."""
    d = c.card.model_dump()
    d["cell"] = list(c.cell)
    d["thumb_b64"] = c.thumb_b64
    if state is not None:
        d["state"] = state
    return d


async def _vlm_followup(followup_id: str, cells: list[BinderCell],
                        payload: list[dict], texts_by_row: dict[int, list[str]],
                        scan_id: str | None) -> None:
    """The background half of a binder scan: the VLM batch that used to be awaited
    inside the request, plus a price refresh for the cells it re-identified (a
    merged answer can change match_id, and the response's prices were attached
    before this ran).

    Every flagged cell ends TERMINAL — ``ok`` or ``vlm_failed`` — and ``finish``
    runs in a ``finally`` so a crash or a timeout still stops the client's poll.
    ``cells`` are this task's own objects: the response body was serialized from
    ``_cell_dict`` copies before the task was spawned, so mutating the cards here
    cannot reach it."""
    try:
        with stage("binder", "vlm", scan_id):
            await _run_vlm(cells, payload, texts_by_row)
        with stage("binder", "vlm_prices", scan_id):
            await _attach_prices([c for c in cells if c.needs_vlm])
    finally:
        resolved = 0
        # finish() is itself in a finally: a patch that raises (a card that won't
        # serialize, say) must not be able to strand the client's poll on a
        # forever-pending row.
        try:
            for c in cells:
                if not c.needs_vlm:
                    continue
                state = "vlm_failed" if c.card.needs_review else "ok"
                resolved += state == "ok"
                followup.patch(followup_id, c.card.row_index, _cell_dict(c, state))
        finally:
            followup.finish(followup_id)
            log.info("binder.followup_done id=%s scan=%s cells=%s resolved=%s",
                     followup_id, scan_id, len(payload), resolved)


async def _vlm_prepare(cells: list[BinderCell], reads: list[CellRead],
                       prior: SessionPrior | None) -> list[dict]:
    """The VLM request body, or ``[]`` for "no follow-up" — every case the old
    awaited call would itself have no-op'd on (no worker configured, no flagged
    cell, nothing encodable). An empty list is what keeps a VLM-less response
    byte-identical to the pre-follow-up one.

    Runs AFTER the page-prior pass, so ``needs_vlm`` here is the post-pass-2
    state and a rescued cell is already gone from the population."""
    if not vlm_client.enabled() or not any(c.needs_vlm for c in cells):
        return []
    # Threaded: N cv2 JPEG encodes is real CPU, and it is the one piece of VLM
    # work that stays on the request path.
    return await asyncio.to_thread(_vlm_payload, cells, reads, prior)


async def _finish(reads: list[CellRead], rows: int, cols: int,
                  scan_id: str | None = None) -> dict:
    """Shared tail for both cell finders. ``reads`` is an ordered list of
    ``CellRead`` (pass-1 identity plus the ladder inputs that produced it); runs
    the page-prior second pass, builds the PackCards, thumbs and prices,
    assembles the response, and hands the still-flagged cells to a BACKGROUND VLM
    pass. Row indices are (re)assigned in the given reading order.

    Both cell finders funnel through here, so A3's page prior is implemented once
    and the quad path and the text-cluster fallback cannot diverge on it.

    The VLM used to be awaited here, which put a RunPod round trip (90s timeout,
    serverless cold start) inside the HTTP response for exactly the pages that
    were hardest to read. It now runs in one background task that patches a
    follow-up entry; the response ships the Phase-1 cards immediately with
    ``state: "pending_vlm"`` on the flagged cells and a top-level ``scan_id`` the
    client polls (GET /scan/binder/{scan_id}). With no worker configured nothing
    of this happens and the response is unchanged.

    ``scan_id`` is carried purely so the price/VLM timing lines correlate with the
    rest of this page's ``timing.binder.*`` lines — it is NOT the follow-up id."""
    cells: list[BinderCell] = []
    texts_by_row: dict[int, list[str]] = {}
    for idx, r in enumerate(reads):
        texts_by_row[idx] = r.texts
        cells.append(BinderCell(cell=r.box, card=_pack_card(idx, r.res),
                                thumb_b64=_thumb(r.crop),
                                needs_vlm=not r.res.confident))

    # A3 — the page's own set as a prior for the cells that failed. Elected from
    # the PASS-1 confident cells only (so the vote can never count a cell the
    # prior itself promoted) and spent under a per-cell denominator veto. The
    # elected prior is then reused as the VLM hint instead of being recomputed.
    flagged_1 = sum(1 for c in cells if c.card.needs_review)
    with stage("binder", "prior_pass", scan_id):
        prior = _page_modal(cells)
        rescued = await _apply_page_prior(cells, reads, prior)
    log.info("binder.page_prior set=%s den=%s flagged=%s rescued=%s",
             prior.set_name if prior else "-", prior.denominator if prior else "-",
             flagged_1, len(rescued))

    with stage("binder", "prices", scan_id):
        await _attach_prices(cells)

    page_confidence = pack_confidence([c.card.confidence for c in cells])

    with stage("binder", "vlm_dispatch", scan_id):
        payload = await _vlm_prepare(cells, reads, prior)
    pending_rows = {p["row_index"] for p in payload}

    # Build the response dicts BEFORE spawning anything: from here to the return
    # there is no await, so the drain task cannot start, and create() copies these
    # dicts — the response body and the follow-up entry are independent from the
    # first instant, however FastAPI interleaves its serialization.
    out = [_cell_dict(c, None if not payload else
                      (followup.PENDING if c.card.row_index in pending_rows else "ok"))
           for c in cells]
    body: dict = {"cards": out, "grid": {"rows": rows, "cols": cols},
                  "page_confidence": page_confidence}
    if payload:
        followup_id = followup.create("binder", out)
        followup.spawn(followup_id,
                       _vlm_followup(followup_id, cells, payload, texts_by_row, scan_id))
        body["scan_id"] = followup_id
        log.info("binder.followup id=%s scan=%s cells=%s", followup_id, scan_id,
                 len(payload))

    log.info("binder.done grid=%sx%s cells=%s flagged=%s page_conf=%.3f followup=%s",
             rows, cols, len(cells), sum(1 for c in cells if c.card.needs_review),
             page_confidence, body.get("scan_id", "-"))
    return body


async def _scan_text_clusters(img, W: int, H: int, scan_id: str | None = None) -> dict:
    """FALLBACK cell finder: whole-photo OCR + geometric gap-clustering.

    Kept unchanged as the fallback for borderless/edge-case photos where quad
    detection finds fewer than two cards. Columns split on x-gaps; per-card
    cells split on the collector-number anchor (with a y-gap fallback for
    numberless columns), then contour-refined crops feed the shared tail."""
    # Same stage name as the quad path's per-cell burst: this whole-photo pass is
    # the fallback's equivalent OCR step, so one grep covers both cell finders.
    with stage("binder", "ocr_cells", scan_id):
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

    reads: list[CellRead] = []
    for (_ei, _ci, cell), (reading, names), res in zip(records, parsed, results):
        coarse = _coarse_box(cell, col_pitch, row_pitch, W, H)
        x, y, w, h = refine_card_box(img, coarse)
        crop = img[y:y + h, x:x + w]
        # name_texts/reading are the very inputs `parsed` fed the ladder above —
        # retained so the page-prior pass re-runs it without touching pixels.
        reads.append(CellRead(box=(x, y, w, h), crop=crop, res=res,
                              texts=[line[2] for line in cell],
                              name_texts=names, reading=reading))
    return await _finish(reads, rows, cols, scan_id)


async def scan_binder_page(page_bytes: bytes) -> dict:
    """Whole-page scan -> {"cards": [...], "grid": {rows, cols}, "page_confidence"}.
    Raises ValueError("no_cards_found") when the photo can't be decoded or neither
    cell finder yields anything.

    PRIMARY path: structure-first card-quad detection (survives full-art faces).
    FALLBACK path: whole-photo text clustering (borderless/edge-case photos)."""
    scan_id = new_scan_id()
    with stage("binder", "total", scan_id):
        with stage("binder", "decode", scan_id):
            img = await asyncio.to_thread(_decode, page_bytes)
        if img is None:
            raise ValueError("no_cards_found")
        H, W = img.shape[:2]

        with stage("binder", "quad_find", scan_id):
            quads = await asyncio.to_thread(_find_card_quads, img)
        if len(quads) >= 2:
            log.info("binder.quads found=%s fallback=%s", len(quads), False)
            ordered, rows, cols = _quad_reading_order(quads)
            with stage("binder", "ocr_cells", scan_id):
                # Warm the lazily-loaded RapidOCR engine single-threaded BEFORE the
                # concurrent per-cell burst: several cells' first OCR calls would
                # otherwise race the engine init and some return empty (dropped reads).
                await asyncio.to_thread(detect_lines_xy, img[:64, :64], 64)
                reads = await asyncio.gather(
                    *(_identify_quad_cell(img, box, W, H) for box in ordered))
            return await _finish(list(reads), rows, cols, scan_id)

        log.info("binder.quads found=%s fallback=%s", len(quads), True)
        return await _scan_text_clusters(img, W, H, scan_id)
