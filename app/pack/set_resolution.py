"""Set resolution: denominator table first, symbol hash tiebreak second."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

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
    # Mapping (not dict) signals read-only intent; the indexes are wrapped in
    # MappingProxyType so this frozen, cache-shared singleton can't be mutated.
    sets: tuple[SetEntry, ...]
    by_denominator: Mapping[str, tuple[SetEntry, ...]] = field(default_factory=dict)
    by_code: Mapping[str, SetEntry] = field(default_factory=dict)
    by_promo_prefix: Mapping[str, SetEntry] = field(default_factory=dict)


# Cache one table per distinct path. lru_cache(maxsize=1) would thrash when callers
# mix the default path with a fixture path; an explicit dict avoids that footgun.
_table_cache: dict[Path | None, DenominatorTable] = {}


def load_denominator_table(path: Path | None = None) -> DenominatorTable:
    if path not in _table_cache:
        _table_cache[path] = _build_denominator_table(path)
    return _table_cache[path]


def _build_denominator_table(path: Path | None) -> DenominatorTable:
    p = path or _DATA_PATH
    try:
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
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Failed to load denominator table from {p}: {exc}") from exc
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
        by_denominator=MappingProxyType({k: tuple(v) for k, v in by_denom.items()}),
        by_code=MappingProxyType(by_code),
        by_promo_prefix=MappingProxyType(by_promo),
    )
    log.info("denominator_table.loaded sets=%s denominators=%s", len(sets), len(by_denom))
    return table
