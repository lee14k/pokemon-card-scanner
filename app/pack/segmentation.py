"""Locate per-card bottom strips in a staircase photo."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from app.pack.config import settings

log = logging.getLogger("pokemon_scanner.pack.segmentation")


@dataclass
class Strip:
    row_index: int
    image: np.ndarray  # BGR, deskewed
    bbox: tuple[int, int, int, int]  # x, y, w, h in source image
    angle: float


@dataclass
class SegmentationResult:
    strips: list[Strip]
    warning: str | None


# Hough min-line-length ladder, as fractions of image width. The strict floor is
# right for protocol photos (stack fills the frame); handheld fans at an angle
# yield shorter edge runs, so the ungrided path relaxes stepwise only when the
# strict pass finds too few rows.
_MIN_LINE_FRACS = (0.45, 0.30, 0.20)

# Cap for the detection working copy (long side, px). Strips are still cropped
# from the full-resolution original for OCR. 2800 preserves row recovery on
# 12MP handheld-fan photos; smaller caps distort the Hough geometry enough to
# admit junk row sets.
_DETECT_MAX_DIM = 2800


def _candidate_bottom_edges(
    gray: np.ndarray, min_frac: float = _MIN_LINE_FRACS[0]
) -> list[tuple[float, float]]:
    """(y_at_image_center, angle_deg) for long near-horizontal lines."""
    h, w = gray.shape
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=120,
        minLineLength=int(w * min_frac), maxLineGap=int(w * 0.05),
    )
    out: list[tuple[float, float]] = []
    if lines is None:
        return out
    # HoughLinesP output shape differs across OpenCV majors: (N, 1, 4) on 4.x,
    # (N, 4) on 5.x. reshape(-1, 4) iterates segments correctly under both.
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        dx, dy = float(x2 - x1), float(y2 - y1)
        if dx == 0:
            continue
        angle = float(np.degrees(np.arctan2(dy, dx)))
        if abs(angle) > 10:
            continue
        yc = y1 + dy * ((w / 2 - x1) / dx)
        out.append((float(yc), angle))
    return out


def _cluster_rows(cands: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    """Merge candidates closer than min_gap; return (median_y, median_angle) per cluster."""
    cands = sorted(cands)
    clusters: list[list[tuple[float, float]]] = []
    for y, a in cands:
        if clusters and y - clusters[-1][-1][0] < min_gap:
            clusters[-1].append((y, a))
        else:
            clusters.append([(y, a)])
    return [
        (float(np.median([y for y, _ in c])), float(np.median([a for _, a in c])))
        for c in clusters
    ]


def _gap_cv(rows: list[tuple[float, float]]) -> float:
    """Coefficient of variation of row gaps — low means uniform staircase
    spacing (real card edges); junk row sets score high."""
    if len(rows) < 3:
        return float("inf")
    gaps = np.diff([y for y, _ in rows])
    mean = float(np.mean(gaps))
    return float(np.std(gaps) / mean) if mean > 0 else float("inf")


def _rows_from_cands(
    cands: list[tuple[float, float]], img_h: int
) -> tuple[list[tuple[float, float]], float]:
    """Cluster candidates into rows and prune non-uniform spacing (shadows,
    table edges). Returns (rows, median_gap)."""
    if not cands:
        return [], img_h * 0.1
    rough = _cluster_rows(cands, min_gap=img_h * 0.02)
    if len(rough) < 2:
        return rough, img_h * 0.1
    median_gap = float(np.median(np.diff([y for y, _ in rough])))
    rows = [rough[0]]
    for y, a in rough[1:]:
        if y - rows[-1][0] >= median_gap * 0.5:
            rows.append((y, a))
    return rows, median_gap


def _parse_capture_meta(
    capture_meta: dict, img_h: int
) -> tuple[list[float], float, int] | None:
    """Validate + scale guided-capture metadata. capture_meta is untrusted client
    JSON (no schema upstream), so every field is parsed defensively. Returns
    (sorted scaled guide y-positions, median gap, declared_count) or None when the
    metadata is missing, malformed, or degenerate (caller emits bad_capture_meta).
    """
    try:
        guides_raw = [float(g) for g in capture_meta.get("guide_positions", [])]
    except (TypeError, ValueError):
        return None
    if len(guides_raw) < 2:
        return None
    if len(guides_raw) > 20:  # real packs have <=13 cards; cap fan-out (DoS guard)
        return None
    # Guides are y-pixel positions; scale by img_h / capture_height. Vertical-only
    # resize is safe (assumes no vertical crop between capture and upload).
    dims = capture_meta.get("image_dims")
    if isinstance(dims, (list, tuple)) and len(dims) >= 2:
        try:
            meta_h = float(dims[1]) or float(img_h)
        except (TypeError, ValueError):
            meta_h = float(img_h)
    else:
        meta_h = float(img_h)
    guides = sorted(g * (img_h / meta_h) for g in guides_raw)
    median_gap = float(np.median(np.diff(guides)))
    if median_gap < 1.0:  # duplicate/degenerate guides — would yield identical strips
        return None
    try:
        declared = int(capture_meta.get("declared_count") or len(guides))
    except (TypeError, ValueError):
        declared = len(guides)
    return guides, median_gap, declared


def _extract_strip(img: np.ndarray, y_edge: float, band: int, angle: float, idx: int) -> Strip:
    h, w = img.shape[:2]
    y1 = int(min(h, round(y_edge)))
    y0 = int(max(0, y1 - band))
    if y1 - y0 < 1:
        log.debug("segmentation.zero_height_strip idx=%s y_edge=%.1f band=%s", idx, y_edge, band)
    crop = img[y0:y1, :].copy()
    if abs(angle) > 0.3 and crop.shape[0] > 8:
        ch, cw = crop.shape[:2]
        m = cv2.getRotationMatrix2D((cw / 2, ch / 2), angle, 1.0)
        crop = cv2.warpAffine(crop, m, (cw, ch), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
    return Strip(row_index=idx, image=crop, bbox=(0, y0, w, y1 - y0), angle=angle)


def find_strips(img: np.ndarray, capture_meta: dict | None) -> SegmentationResult:
    """
    Guided path (capture_meta given): one strip per guide position, snapped to a
    detected edge when one lies within tolerance — rows are NEVER dropped.
    Ungrided path: best self-consistent cluster set within [min_rows, max_rows].
    """
    cfg = settings()
    h, w = img.shape[:2]
    # Detection geometry doesn't need phone-camera resolution: denoise + Hough on
    # a 12MP frame costs hundreds of MB. Detect on a capped copy, then map row
    # positions back and crop OCR strips from the full-resolution original.
    scale = 1.0
    if max(h, w) > _DETECT_MAX_DIM:
        scale = _DETECT_MAX_DIM / max(h, w)
        small = cv2.resize(img, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.fastNlMeansDenoising(gray, h=7)
    cands = [(y / scale, a) for y, a in _candidate_bottom_edges(gray)]

    if capture_meta:
        parsed = _parse_capture_meta(capture_meta, h)
        if parsed is None:
            return SegmentationResult(strips=[], warning="bad_capture_meta")
        guides, median_gap, declared = parsed
        clusters = _cluster_rows(cands, min_gap=median_gap * 0.5)
        tol = median_gap * cfg.guide_snap_tolerance
        band = max(20, int(median_gap * cfg.strip_band_frac))
        strips = []
        for i, gy in enumerate(guides):  # already sorted by _parse_capture_meta
            near = [(abs(cy - gy), cy, a) for cy, a in clusters if abs(cy - gy) <= tol]
            if near:
                _, cy, a = min(near)
                strips.append(_extract_strip(img, cy, band, a, i))
            else:
                strips.append(_extract_strip(img, gy, band, 0.0, i))
        warning = None if declared == len(strips) else (
            f"detected {len(strips)} rows, declared {declared}"
        )
        log.info("segmentation.guided rows=%s snap_tol=%.1f", len(strips), tol)
        return SegmentationResult(strips=strips, warning=warning)

    # Ungrided: derive rows purely from detected edges. Uploads arrive at many
    # resolutions (browsers downscale phone photos), which shifts where true
    # rows appear along the relaxation ladder — and junky sets can clear a
    # naive minimum-count bar first. Run the whole ladder, then keep the row
    # set with the most uniform staircase spacing among in-range counts.
    trials = [(_MIN_LINE_FRACS[0], *_rows_from_cands(cands, h))]
    for frac in _MIN_LINE_FRACS[1:]:
        relaxed = [(y / scale, a) for y, a in _candidate_bottom_edges(gray, frac)]
        trials.append((frac, *_rows_from_cands(relaxed, h)))
    in_range = [t for t in trials if cfg.min_rows <= len(t[1]) <= cfg.max_rows]
    if in_range:
        frac_used, rows, median_gap = min(in_range, key=lambda t: _gap_cv(t[1]))
    else:  # no plausible count anywhere: keep the biggest set (strictest first)
        frac_used, rows, median_gap = max(trials, key=lambda t: len(t[1]))
    log.info("segmentation.ladder frac=%.2f rows=%s gap_cv=%.2f",
             frac_used, len(rows), _gap_cv(rows))
    if not rows:
        return SegmentationResult(strips=[], warning="no rows detected")
    band = max(20, int(median_gap * cfg.strip_band_frac))
    strips = [_extract_strip(img, y, band, a, i) for i, (y, a) in enumerate(rows)]
    warning = None
    if not (cfg.min_rows <= len(strips) <= cfg.max_rows):
        warning = f"detected {len(strips)} rows (expected {cfg.min_rows}-{cfg.max_rows})"
    log.info("segmentation.ungrided rows=%s median_gap=%.1f", len(strips), median_gap)
    return SegmentationResult(strips=strips, warning=warning)
