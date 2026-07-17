"""Read-side price helpers shared by pull enrichment and battles."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import CardPrice, PriceSnapshot


def midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2


async def latest_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]:
    """(match_id -> (usd_low, usd_high), snapshot iso date) from the newest done snapshot."""
    snap = (
        await session.execute(
            select(PriceSnapshot).where(PriceSnapshot.status == "done")
            .order_by(PriceSnapshot.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if snap is None:
        return {}, None
    rows = (
        await session.execute(
            select(CardPrice.match_id, CardPrice.usd_market_low, CardPrice.usd_market_high)
            .where(CardPrice.snapshot_id == snap.id)
        )
    ).all()
    return {m: (lo, hi) for m, lo, hi in rows}, snap.created_at.isoformat()
