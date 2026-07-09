"""Weekly (configurable) price snapshots for pulled cards + price-jump anomalies.

Runs inside run_batch's advisory lock. Staleness-gated: most nightly batches skip
pricing entirely. Failure here must never fail the stats batch.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid

from sqlalchemy import func, select

from app.db.models import Anomaly, CardPrice, PriceSnapshot, PullCard, PullCardDerived
from app.db.session import async_session_maker
from app.pokewallet import get_api_key, lookup_card_exact, make_async_client
from app.stats.config import stats_settings

log = logging.getLogger("pokemon_scanner.stats.pricing")


def _mid(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2


def _extract_prices(match: dict | None) -> tuple[float | None, float | None, float | None, dict]:
    """(usd_low, usd_high, eur_trend, raw) from a PokéWallet card hit."""
    if not match:
        return None, None, None, {}
    tp = match.get("tcgplayer") or {}
    cm = match.get("cardmarket") or {}
    markets = [
        r.get("market_price") for r in (tp.get("prices") or [])
        if isinstance(r.get("market_price"), (int, float))
    ]
    trends = [
        r.get("trend") for r in (cm.get("prices") or [])
        if isinstance(r.get("trend"), (int, float))
    ]
    low = min(markets) if markets else None
    high = max(markets) if markets else None
    trend = trends[0] if trends else None
    return low, high, trend, {"tcgplayer": tp, "cardmarket": cm}


async def _card_universe(session) -> list[tuple[str, str | None, str | None, str | None]]:
    """Distinct (match_id, set_id, card_number, name) across confirmed + derived cards.
    Derived (server-authoritative) metadata wins when both exist."""
    universe: dict[str, tuple[str, str | None, str | None, str | None]] = {}
    for model in (PullCard, PullCardDerived):
        rows = (
            await session.execute(
                select(model.match_id, model.set_id, model.card_number, model.name)
                .where(model.match_id.is_not(None))
                .distinct()
            )
        ).all()
        for mid, sid, num, name in rows:
            universe[mid] = (mid, sid, num, name)  # later model (derived) overwrites
    return list(universe.values())


async def refresh_prices_if_stale(stats_snapshot_id: uuid.UUID) -> str | None:
    cfg = stats_settings()
    api_key = get_api_key()
    if not api_key:
        log.info("pricing.skipped no_api_key")
        return None

    async with async_session_maker() as session:
        latest = (
            await session.execute(
                select(func.max(PriceSnapshot.created_at)).where(PriceSnapshot.status == "done")
            )
        ).scalar_one_or_none()
        if latest is not None:
            age = datetime.datetime.now(datetime.timezone.utc) - latest
            if age < datetime.timedelta(days=cfg.price_interval_days):
                log.info("pricing.skipped fresh age_days=%.2f", age.total_seconds() / 86400)
                return None

        universe = await _card_universe(session)
        if not universe:
            log.info("pricing.skipped no_pulled_cards")
            return None

        snap = PriceSnapshot(status="running")
        session.add(snap)
        await session.flush()
        snap_id = snap.id
        try:
            async with make_async_client() as client:
                for mid, sid, num, name in universe:
                    low = high = trend = None
                    raw: dict = {}
                    if sid and num:
                        try:
                            hit = await lookup_card_exact(
                                sid, num.split("/")[0], api_key=api_key, client=client
                            )
                            low, high, trend, raw = _extract_prices(hit)
                        except Exception as e:  # one bad card must not kill the snapshot
                            log.warning("pricing.lookup_failed match=%s err=%r", mid, e)
                    session.add(CardPrice(
                        snapshot_id=snap_id, match_id=mid, set_id=sid, card_number=num,
                        name=name, usd_market_low=low, usd_market_high=high,
                        eur_trend=trend, raw=raw,
                    ))
                    await asyncio.sleep(cfg.price_lookup_delay_ms / 1000)
            await session.flush()

            # price-jump anomalies vs the previous done snapshot
            prev_id = (
                await session.execute(
                    select(PriceSnapshot.id).where(PriceSnapshot.status == "done")
                    .order_by(PriceSnapshot.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if prev_id is not None:
                prev = {
                    r.match_id: r for r in (
                        await session.execute(select(CardPrice).where(CardPrice.snapshot_id == prev_id))
                    ).scalars()
                }
                cur = (
                    await session.execute(select(CardPrice).where(CardPrice.snapshot_id == snap_id))
                ).scalars().all()
                for row in cur:
                    old = prev.get(row.match_id)
                    if old is None:
                        continue
                    o = _mid(old.usd_market_low, old.usd_market_high)
                    n = _mid(row.usd_market_low, row.usd_market_high)
                    if o is None or n is None or o == 0:
                        continue
                    pct = (n - o) / o
                    if abs(pct) >= cfg.price_jump_threshold:
                        session.add(Anomaly(
                            snapshot_id=stats_snapshot_id, detector="price_jump",
                            target_type="card", set_id=row.set_id or "unknown",
                            card_match_id=row.match_id, severity=abs(pct),
                            detail={"old": o, "new": n, "pct": pct,
                                    "from_snapshot": str(prev_id), "to_snapshot": str(snap_id),
                                    "name": row.name},
                        ))

            snap.status = "done"
            await session.commit()
            log.info("pricing.done snapshot=%s cards=%s", snap_id, len(universe))
            return str(snap_id)
        except Exception:
            await session.rollback()
            async with async_session_maker() as s2:
                failed = await s2.get(PriceSnapshot, snap_id)
                if failed is not None:
                    failed.status = "failed"
                    await s2.commit()
            log.exception("pricing.failed snapshot=%s", snap_id)
            return None
