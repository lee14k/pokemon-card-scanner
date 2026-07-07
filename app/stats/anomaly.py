"""Anomaly detectors over a freshly-aggregated snapshot.

deviation_from_prior: observed card/rarity rate is statistically far from the prior.
submitter_concentration: one trainer contributes an outsized share of a set's packs.
Both only consider sets/cards with packs >= min_sample. Findings are flagged for review.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Anomaly, CardStat, Pull, PullCardDerived, SetStat
from app.stats.config import stats_settings
from app.stats.prior import PriorSource

log = logging.getLogger("pokemon_scanner.stats.anomaly")


def _z(observed_rate: float, prior_rate: float, packs: int) -> float:
    # Binomial SE under the prior mean; guard tiny denominators.
    se = math.sqrt(max(prior_rate * (1 - prior_rate), 1e-9) / max(packs, 1))
    return (observed_rate - prior_rate) / se if se > 0 else 0.0


async def detect(session: AsyncSession, snapshot_id: uuid.UUID, prior: PriorSource) -> int:
    cfg = stats_settings()
    found = 0

    set_packs = {
        s.set_id: s.verified_pack_count
        for s in (await session.execute(select(SetStat).where(SetStat.snapshot_id == snapshot_id))).scalars()
    }

    # deviation_from_prior on card stats
    cards = (await session.execute(select(CardStat).where(CardStat.snapshot_id == snapshot_id))).scalars().all()
    for c in cards:
        if c.packs < cfg.min_sample:
            continue
        a, b = prior.card_prior(c.set_id, c.match_id)
        prior_rate = a / (a + b) if (a + b) > 0 else 0.0
        z = _z(c.raw_rate, prior_rate, c.packs)
        if abs(z) > cfg.z_threshold:
            session.add(Anomaly(
                snapshot_id=snapshot_id, detector="deviation_from_prior", target_type="card",
                set_id=c.set_id, card_match_id=c.match_id, severity=abs(z),
                detail={"observed": c.raw_rate, "prior": prior_rate, "z": z, "packs": c.packs},
            ))
            found += 1

    # submitter_concentration per set (needs trainer attribution from verified pulls)
    rows = (
        await session.execute(
            select(Pull.id, Pull.trainer_id, PullCardDerived.set_id)
            .join(PullCardDerived, PullCardDerived.pull_id == Pull.id)
            .where(Pull.verified.is_(True))
        )
    ).all()
    # determine each pull's set (modal) + trainer
    pull_set: dict[uuid.UUID, Counter] = defaultdict(Counter)
    pull_trainer: dict[uuid.UUID, uuid.UUID] = {}
    for pull_id, trainer_id, set_id in rows:
        pull_trainer[pull_id] = trainer_id
        if set_id:
            pull_set[pull_id][set_id] += 1
    set_trainer_packs: dict[str, Counter] = defaultdict(Counter)
    for pull_id, ctr in pull_set.items():
        if not ctr:
            continue
        sid = ctr.most_common(1)[0][0]
        set_trainer_packs[sid][pull_trainer[pull_id]] += 1

    for sid, packs in set_packs.items():
        if packs < cfg.min_sample:
            continue
        tc = set_trainer_packs.get(sid)
        if not tc:
            continue
        top_trainer, top_packs = tc.most_common(1)[0]
        share = top_packs / packs
        if share > cfg.concentration:
            session.add(Anomaly(
                snapshot_id=snapshot_id, detector="submitter_concentration", target_type="set",
                set_id=sid, card_match_id=None, severity=share,
                detail={"top_trainer": str(top_trainer), "share": share, "packs": packs},
            ))
            found += 1

    await session.flush()
    log.info("anomaly.done found=%s", found)
    return found
