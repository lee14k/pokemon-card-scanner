"""Set resolution: denominator table first, symbol hash tiebreak second."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("pokemon_scanner.pack.set_resolution")

_DATA_PATH = Path(__file__).resolve().parent / "data" / "set_denominators.json"


@dataclass(frozen=True)
class SetEntry:
    set_id: str
    set_code: str | None
    set_name: str
    era: str
    denominators: tuple[str, ...]
    promo_prefix: str | None


@dataclass(frozen=True)
class DenominatorTable:
    sets: tuple[SetEntry, ...]
    by_denominator: dict[str, tuple[SetEntry, ...]] = field(default_factory=dict)
    by_code: dict[str, SetEntry] = field(default_factory=dict)
    by_promo_prefix: dict[str, SetEntry] = field(default_factory=dict)


@lru_cache(maxsize=1)
def load_denominator_table(path: Path | None = None) -> DenominatorTable:
    p = path or _DATA_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    sets = tuple(
        SetEntry(
            set_id=str(r["set_id"]),
            set_code=(r.get("set_code") or None),
            set_name=r["set_name"],
            era=r["era"],
            denominators=tuple(r.get("denominators") or ()),
            promo_prefix=(r.get("promo_prefix") or None),
        )
        for r in raw["sets"]
    )
    by_denom: dict[str, list[SetEntry]] = {}
    by_code: dict[str, SetEntry] = {}
    by_promo: dict[str, SetEntry] = {}
    for s in sets:
        for d in s.denominators:
            by_denom.setdefault(d.upper(), []).append(s)
        if s.set_code:
            by_code[s.set_code.upper()] = s
        if s.promo_prefix:
            by_promo[s.promo_prefix.upper()] = s
    table = DenominatorTable(
        sets=sets,
        by_denominator={k: tuple(v) for k, v in by_denom.items()},
        by_code=by_code,
        by_promo_prefix=by_promo,
    )
    log.info("denominator_table.loaded sets=%s denominators=%s", len(sets), len(by_denom))
    return table
