"""Local contour refinement of a coarse card box. The FIRST quad-detection
code in the repo: deliberately local-scope (searches only a slightly-expanded
window around the coarse box) with a HARD fallback — any failure or missing
candidate returns the coarse box unchanged. Pure function, never raises, so
callers can wrap every crop without guarding. Glare-safe by design: when the
card edges are washed out we simply keep the geometric box."""
from __future__ import annotations

import cv2
import numpy as np


def refine_card_box(
    img_bgr: np.ndarray, coarse: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Tighten a coarse (x, y, w, h) card box to the card's real quad edges,
    searching within the coarse box expanded 1.15x (clamped to image). Returns
    the refined upright bounding rect in source pixels, or `coarse` unchanged on
    any failure / no candidate."""
    try:
        H, W = img_bgr.shape[:2]
        x, y, w, h = coarse
        cx, cy = x + w / 2.0, y + h / 2.0                     # expand 1.15x about center
        ex0 = max(0, int(round(cx - w * 1.15 / 2.0)))
        ey0 = max(0, int(round(cy - h * 1.15 / 2.0)))
        ex1 = min(W, int(round(cx + w * 1.15 / 2.0)))
        ey1 = min(H, int(round(cy + h * 1.15 / 2.0)))
        if ex1 - ex0 < 2 or ey1 - ey0 < 2:
            return coarse
        region = img_bgr[ey0:ey1, ex0:ex1]
        region_area = (ex1 - ex0) * (ey1 - ey0)
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0.0
        for cnt in contours:
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
            if not (4 <= len(approx) <= 6):                   # card-ish quad, some slack
                continue
            (_c, (rw, rh), _a) = cv2.minAreaRect(cnt)
            if rw < 1 or rh < 1:
                continue
            if not (0.63 <= min(rw, rh) / max(rw, rh) <= 0.80):  # Pokemon card aspect
                continue
            area = rw * rh
            if area < 0.40 * region_area:                     # ignore inner-detail contours
                continue
            if area > best_area:
                best, best_area = cnt, area
        if best is None:
            return coarse
        bx, by, bw, bh = cv2.boundingRect(best)               # upright rect, region coords
        rx0 = max(0, ex0 + bx)                                # -> source coords, intersect image
        ry0 = max(0, ey0 + by)
        rx1 = min(W, ex0 + bx + bw)
        ry1 = min(H, ey0 + by + bh)
        if rx1 <= rx0 or ry1 <= ry0:
            return coarse
        return (rx0, ry0, rx1 - rx0, ry1 - ry0)
    except Exception:                                         # hard fallback, never raises
        return coarse
