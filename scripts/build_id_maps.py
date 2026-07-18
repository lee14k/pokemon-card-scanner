"""Build set_id_map / card_id_map: PokéWallet identifiers -> TCGdex identifiers.

Set-level: each denominator-table entry is matched to an ingested tcgdex_set by
normalized-name equality (method 'name', confidence 1.0), falling back to
denominator == cardCount.official plus fuzzy name containment (method
'name+count', confidence 0.8). Unmapped/ambiguous entries are reported with
their candidates — never guessed silently.

Card-level: each cached `card` row maps through set_id_map, then by numerator
== int-stripped localId within the mapped set (method 'set+number').

Run scripts/ingest_tcgdex.py first. Idempotent: re-runs upsert.
Usage: DATABASE_URL=... .venv/bin/python scripts/build_id_maps.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.db.models import Card, CardIdMap, SetIdMap, TcgdexCard, TcgdexSet  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402

# Same file app/pack/set_resolution.py loads; read directly so this script doesn't
# drag in the scan pipeline's cv2/PIL imports.
DENOMINATOR_TABLE = (
    Path(__file__).resolve().parent.parent / "app" / "pack" / "data" / "set_denominators.json"
)

_LOCAL_ID_RE = re.compile(r"^([A-Z]*?)0*(\d+)$")


def norm_name(name: str) -> str:
    """Casefold, '&' -> 'and', drop punctuation/spaces: 'Champion's Path' == 'Champions Path'."""
    s = name.casefold().replace("&", "and")
    return "".join(ch for ch in s if ch.isalnum())


def norm_local(value: str) -> str:
    """Uppercase and strip zero-padding from the numeric tail: '012' -> '12',
    'TG05' -> 'TG5', 'SV001' -> 'SV1'. Symmetric with the card cache's normalized
    numerators, whose lettered forms are stored as printed (unpadded)."""
    s = value.strip().upper()
    m = _LOCAL_ID_RE.match(s)
    if not m:
        return s
    prefix, digits = m.groups()
    return f"{prefix}{int(digits)}"


def denominator_ints(denominators: list[str]) -> set[int]:
    out: set[int] = set()
    for d in denominators:
        digits = re.sub(r"\D", "", d)
        if digits:
            out.add(int(digits))
    return out


async def build_set_map(session) -> dict[str, str]:
    entries = json.loads(DENOMINATOR_TABLE.read_text(encoding="utf-8"))["sets"]
    tcgdex_sets = (await session.execute(select(TcgdexSet))).scalars().all()
    by_norm_name: dict[str, list[TcgdexSet]] = {}
    for ts in tcgdex_sets:
        by_norm_name.setdefault(norm_name(ts.name), []).append(ts)

    mapping: dict[str, str] = {}
    problems: list[str] = []
    print(f"== set mapping ({len(entries)} denominator-table entries) ==")
    for entry in entries:
        set_id, code, name = str(entry["set_id"]), entry.get("set_code"), entry["set_name"]
        candidates = by_norm_name.get(norm_name(name), [])
        method, confidence = "name", 1.0
        if len(candidates) != 1:
            # Fallback: printed denominator == official count, plus name containment.
            denoms = denominator_ints(entry.get("denominators") or [])
            fallback = [
                ts for ts in tcgdex_sets
                if ts.card_count_official in denoms
                and (norm_name(name) in norm_name(ts.name) or norm_name(ts.name) in norm_name(name))
            ]
            if candidates:  # >1 exact-name hits: ambiguous, don't guess
                cands = ", ".join(f"{ts.id} ({ts.name!r})" for ts in candidates)
                problems.append(f"AMBIGUOUS {set_id} {code} {name!r}: exact-name candidates: {cands}")
                continue
            if len(fallback) != 1:
                cands = ", ".join(
                    f"{ts.id} ({ts.name!r}, official={ts.card_count_official})" for ts in fallback
                ) or "none"
                problems.append(f"UNMAPPED  {set_id} {code} {name!r}: fallback candidates: {cands}")
                continue
            candidates, method, confidence = fallback, "name+count", 0.8

        ts = candidates[0]
        mapping[set_id] = ts.id
        await session.execute(
            pg_insert(SetIdMap)
            .values(pokewallet_set_id=set_id, tcgdex_set_id=ts.id, method=method, confidence=confidence)
            .on_conflict_do_update(
                index_elements=["pokewallet_set_id"],
                set_={"tcgdex_set_id": ts.id, "method": method, "confidence": confidence,
                      "built_at": func.now()},
            )
        )
        print(
            f"  {set_id:>6} {code or '':7} {name!s:38} -> {ts.id:12} "
            f"(official={ts.card_count_official}, total={ts.card_count_total}) "
            f"[{method}, {confidence}]"
        )

    print(f"\nmapped {len(mapping)}/{len(entries)} sets")
    for p in problems:
        print(f"  {p}")
    return mapping


async def build_card_map(session, set_map: dict[str, str]) -> None:
    cached = (await session.execute(select(Card))).scalars().all()
    mapped_set_ids = set(set_map.values())
    by_set_local: dict[tuple[str, str], list[str]] = {}
    if mapped_set_ids:
        rows = (
            await session.execute(select(TcgdexCard).where(TcgdexCard.set_id.in_(mapped_set_ids)))
        ).scalars().all()
        for tc in rows:
            by_set_local.setdefault((tc.set_id, norm_local(tc.local_id)), []).append(tc.id)

    mapped = 0
    misses: list[str] = []
    print(f"\n== card mapping ({len(cached)} cached cards) ==")
    for c in cached:
        tcgdex_set_id = set_map.get(c.set_id)
        if tcgdex_set_id is None:
            misses.append(f"{c.match_id} (set {c.set_id} #{c.numerator}): set not in set_id_map")
            continue
        hits = by_set_local.get((tcgdex_set_id, norm_local(c.numerator)), [])
        if not hits:
            misses.append(
                f"{c.match_id} (set {c.set_id} #{c.numerator}): no localId match in {tcgdex_set_id}"
            )
            continue
        if len(hits) > 1:
            misses.append(
                f"{c.match_id} (set {c.set_id} #{c.numerator}): ambiguous in {tcgdex_set_id}: {hits}"
            )
            continue
        await session.execute(
            pg_insert(CardIdMap)
            .values(pokewallet_match_id=c.match_id, tcgdex_card_id=hits[0], method="set+number")
            .on_conflict_do_update(
                index_elements=["pokewallet_match_id"],
                set_={"tcgdex_card_id": hits[0], "method": "set+number", "built_at": func.now()},
            )
        )
        mapped += 1
        print(f"  {c.match_id:24} (set {c.set_id} #{c.numerator}) -> {hits[0]}")

    print(f"\nmapped {mapped} of {len(cached)} cached cards")
    for m in misses:
        print(f"  MISS {m}")


async def main() -> None:
    async with async_session_maker() as session:
        set_map = await build_set_map(session)
        await build_card_map(session, set_map)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
