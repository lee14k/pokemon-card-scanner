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


import cv2  # noqa: E402
from PIL import Image  # noqa: E402

from app.pack.ocr import NumberReading  # noqa: E402
from app.set_symbol_index import match_symbol_among  # noqa: E402


@dataclass
class SetResolution:
    set_id: str | None = None
    set_code: str | None = None
    set_name: str | None = None
    method: str = "unresolved"
    # one of: promo_prefix | code_text | denominator_unique | symbol_tiebreak | unresolved
    candidates: tuple[str, ...] = ()   # candidate set_ids considered
    margin: int | None = None          # symbol hash margin when method=symbol_tiebreak


def _entry_to_resolution(entry: SetEntry, method: str, candidates: tuple[str, ...],
                         margin: int | None = None) -> SetResolution:
    return SetResolution(
        set_id=entry.set_id, set_code=entry.set_code, set_name=entry.set_name,
        method=method, candidates=candidates, margin=margin,
    )


def resolve_set(reading: NumberReading, strip_bgr) -> SetResolution:
    """
    Resolution order (cheap, reliable signals first):
      1. promo prefix (SWSH###/SVP###)
      2. set-code text in the OCR tokens (SV-era cards print it, e.g. "SVI")
      3. unique denominator
      4. symbol perceptual-hash tiebreak among denominator candidates
    """
    table = load_denominator_table()

    if reading.prefix:
        entry = table.by_promo_prefix.get(reading.prefix.upper())
        if entry:
            return _entry_to_resolution(entry, "promo_prefix", (entry.set_id,))
        return SetResolution(method="unresolved")

    for token in reading.tokens:
        entry = table.by_code.get(token.upper())
        if entry:
            return _entry_to_resolution(entry, "code_text", (entry.set_id,))

    if not reading.denominator:
        return SetResolution(method="unresolved")

    candidates = table.by_denominator.get(reading.denominator.upper(), ())
    if len(candidates) == 1:
        return _entry_to_resolution(candidates[0], "denominator_unique",
                                    (candidates[0].set_id,))
    if not candidates:
        return SetResolution(method="unresolved")

    # Tiebreak: symbol hash over the strip's left region, candidates only.
    h, w = strip_bgr.shape[:2]
    left = strip_bgr[:, : max(1, int(w * 0.40))]
    crop = Image.fromarray(cv2.cvtColor(left, cv2.COLOR_BGR2RGB))
    cand_ids = tuple(c.set_id for c in candidates)
    hit = match_symbol_among(crop, set(cand_ids))
    if hit is None:
        return SetResolution(method="unresolved", candidates=cand_ids)
    ref, dist, second = hit
    margin = (second - dist) if second is not None else None
    winner = next(c for c in candidates if c.set_id == ref.set_id)
    log.info("set_resolution.tiebreak winner=%s dist=%s margin=%s", ref.set_id, dist, margin)
    return _entry_to_resolution(winner, "symbol_tiebreak", cand_ids, margin)
