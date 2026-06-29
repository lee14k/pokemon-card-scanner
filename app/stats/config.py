"""Env-driven tuning for the stats batch."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class StatsSettings:
    min_sample: int = field(default_factory=lambda: _i("PACK_STATS_MIN_SAMPLE", 30))
    z_threshold: float = field(default_factory=lambda: _f("PACK_STATS_Z_THRESHOLD", 3.0))
    concentration: float = field(default_factory=lambda: _f("PACK_STATS_CONCENTRATION", 0.5))
    prior_strength: float = field(default_factory=lambda: _f("PACK_STATS_PRIOR_STRENGTH", 20.0))
    cron_token: str = field(default_factory=lambda: os.environ.get("STATS_CRON_TOKEN", "").strip())


def stats_settings() -> StatsSettings:
    return StatsSettings()
