"""Prior source + Beta-Binomial blend.

A prior is encoded as pseudo-counts (alpha hits out of beta pseudo-packs). The blend
is (alpha + hits) / (beta + packs): prior-dominated at low N, data-dominated at high N.
The seed-file source ships approximate per-rarity rates; a live scraper would be a
drop-in PriorSource later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.stats.config import stats_settings

_DATA = Path(__file__).resolve().parent / "data" / "priors.json"


def beta_binomial_blend(hits: int, packs: int, alpha: float, beta: float) -> float:
    denom = beta + packs
    if denom <= 0:
        return 0.0
    return (alpha + hits) / denom


class PriorSource(Protocol):
    def rarity_prior(self, set_id: str, rarity: str) -> tuple[float, float]: ...
    def card_prior(self, set_id: str, match_id: str) -> tuple[float, float]: ...


class SeedFilePriorSource:
    def __init__(self, path: Path | None = None) -> None:
        raw = json.loads((path or _DATA).read_text(encoding="utf-8"))
        self._strength = float(raw.get("default_strength", 20))
        self._default_card_rate = float(raw.get("default_card_rate", 0.05))
        self._rarity: dict[str, float] = raw.get("rarity", {})

    def _ab(self, rate: float, strength: float) -> tuple[float, float]:
        rate = min(max(rate, 0.0), 1.0)
        return rate * strength, (1.0 - rate) * strength

    def rarity_prior(self, set_id: str, rarity: str) -> tuple[float, float]:
        rate = self._rarity.get(rarity, self._default_card_rate)
        return self._ab(rate, self._strength)

    def card_prior(self, set_id: str, match_id: str) -> tuple[float, float]:
        # No per-card seed in v1 -> a weak generic prior that just smooths low N.
        return self._ab(self._default_card_rate, self._strength)


def default_prior_source() -> SeedFilePriorSource:
    return SeedFilePriorSource()
