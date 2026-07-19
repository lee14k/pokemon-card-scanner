"""Env-driven tuning knobs for the pack pipeline. No code edits needed to tune."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Confidence threshold T: cards below this get low_confidence_reason set.
    # Tuned by scripts/sweep_threshold.py against the corpus (Task 16).
    confidence_threshold: float = field(
        default_factory=lambda: _env_float("PACK_CONFIDENCE_THRESHOLD", 0.80)
    )
    # Ungrided segmentation accepts this many rows; outside → segmentation_warning.
    min_rows: int = field(default_factory=lambda: _env_int("PACK_MIN_ROWS", 5))
    max_rows: int = field(default_factory=lambda: _env_int("PACK_MAX_ROWS", 13))
    # Guided path: snap detected edges to guides within this fraction of median gap.
    guide_snap_tolerance: float = field(
        default_factory=lambda: _env_float("PACK_GUIDE_SNAP_TOL", 0.35)
    )
    # Strip band height as a fraction of detected median row gap.
    strip_band_frac: float = field(
        default_factory=lambda: _env_float("PACK_STRIP_BAND_FRAC", 0.85)
    )
    # Learned band detector (sub-project J): when on AND the ONNX model is
    # present, the ungrided path uses it instead of Hough (falls back on any
    # miss/error). Off by default — exact current behavior.
    band_detector: bool = field(
        default_factory=lambda: os.environ.get("PACK_BAND_DETECTOR", "").strip() in ("1", "true", "True")
    )
    band_threshold: float = field(
        default_factory=lambda: _env_float("PACK_BAND_THRESHOLD", 0.5)
    )


def settings() -> Settings:
    """Fresh read each call so env changes (tests) take effect without reload."""
    return Settings()
