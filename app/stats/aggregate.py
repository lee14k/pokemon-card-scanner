"""Aggregate server-derived cards of verified pulls into snapshot-scoped stats."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CardStat, Pull, PullCardDerived, RarityStat, SetStat
from app.stats.prior import PriorSource

log = logging.getLogger("pokemon_scanner.stats.aggregate")


def _pull_set_id(cards: list[PullCardDerived]) -> str | None:
    ids = [c.set_id for c in cards if c.set_id]
    if not ids:
        return None
    return Counter(ids).most_common(1)[0][0]


async def aggregate_snapshot(session: AsyncSession, snapshot_id: uuid.UUID, prior: PriorSource) -> None:
    """Compute set/card/rarity stats for all verified pulls into the given snapshot."""
    # Load verified pulls + their derived cards.
    pulls = (
        await session.execute(select(Pull).where(Pull.verified.is_(True)))
    ).scalars().all()
    derived = (
        await session.execute(
            select(PullCardDerived).join(Pull, PullCardDerived.pull_id == Pull.id).where(Pull.verified.is_(True))
        )
    ).scalars().all()

    by_pull: dict[uuid.UUID, list[PullCardDerived]] = defaultdict(list)
    for c in derived:
        by_pull[c.pull_id].append(c)

    set_pack_count: Counter[str] = Counter()
    # per set: card match_id -> packs containing it; rarity -> packs containing it
    card_hits: dict[str, Counter[str]] = defaultdict(Counter)
    card_meta: dict[str, dict[str, tuple[str | None, str | None]]] = defaultdict(dict)
    rarity_hits: dict[str, Counter[str]] = defaultdict(Counter)

    for pull in pulls:
        cards = by_pull.get(pull.id, [])
        set_id = _pull_set_id(cards)
        if set_id is None:
            continue
        set_pack_count[set_id] += 1
        seen_cards = {c.match_id for c in cards if c.match_id}
        for mid in seen_cards:
            card_hits[set_id][mid] += 1
            sample = next(c for c in cards if c.match_id == mid)
            card_meta[set_id][mid] = (sample.card_number, sample.name)
        seen_rarities = {c.rarity for c in cards if c.rarity}
        for rar in seen_rarities:
            rarity_hits[set_id][rar] += 1

    for set_id, packs in set_pack_count.items():
        session.add(SetStat(snapshot_id=snapshot_id, set_id=set_id, verified_pack_count=packs))
        for mid, hits in card_hits[set_id].items():
            a, b = prior.card_prior(set_id, mid)
            cn, nm = card_meta[set_id][mid]
            session.add(CardStat(
                snapshot_id=snapshot_id, set_id=set_id, match_id=mid, card_number=cn, name=nm,
                hits=hits, packs=packs, raw_rate=hits / packs,
                blended_rate=(a + hits) / (b + packs),
            ))
        for rar, hits in rarity_hits[set_id].items():
            a, b = prior.rarity_prior(set_id, rar)
            session.add(RarityStat(
                snapshot_id=snapshot_id, set_id=set_id, rarity=rar,
                packs_with_rarity=hits, raw_rate=hits / packs,
                blended_rate=(a + hits) / (b + packs),
            ))
    await session.flush()
    log.info("aggregate.done sets=%s", len(set_pack_count))
