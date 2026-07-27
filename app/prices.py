"""Read-side price helpers shared by pull enrichment and battles."""

from __future__ import annotations

import time

from sqlalchemy import select

from app.db.models import CardPrice, PriceSnapshot


def midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2


# Prices move when a stats/pricing batch writes a NEW snapshot — a batch job, not
# a request — so within any one minute every caller is entitled to the same
# answer. Before this, each of them re-read the whole CardPrice table for the
# newest snapshot: the live path did it PER IDENTIFIED FRAME (live_api._attach_price
# opens a session, loads every priced card, and looks up exactly one match_id),
# the binder does it per page, and the pull/battle/collection list endpoints do it
# per request. 60s is the staleness budget, and it is the ceiling rather than the
# usual case: the pricing batch runs in this same process and calls
# invalidate_price_cache() the moment its snapshot is done, so the only way to see
# stale prices is a snapshot written by some OTHER process.
#
# No single-flight lock around the miss: concurrent misses each read and each
# store the same answer, which is exactly what every call did before this memo
# existed. A module-level asyncio.Lock would bind itself to the first event loop
# that blocked on it and raise on every other one — and unlike a one-shot lazy
# init, this entry expires, so a second loop (a TestClient per test, a script's
# second asyncio.run) genuinely reaches the slow path. The pull/battle/collection
# callers do not guard this call, so that RuntimeError would be a 500. See the
# same note in app/cards.py.
_PRICE_TTL_S = 60.0
_price_cache: tuple[float, dict[str, tuple[float | None, float | None]], str | None] | None = None


async def latest_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]:
    """(match_id -> (usd_low, usd_high), snapshot iso date) from the newest done snapshot.

    Cached for ``_PRICE_TTL_S``; ``session`` is used only on a miss. THE RETURNED
    DICT IS SHARED between callers and MUST NOT be mutated — every caller today
    only reads it (``in`` / ``.get`` / indexing).

    Two kinds of "no prices", cached differently on purpose:

      * A DB ERROR is NOT cached. It propagates out of here exactly as it did
        before this memo existed, and each caller's own try/except decides what
        an unpriced response looks like — so a blip costs one request, not sixty
        seconds of them.
      * ``({}, None)`` — no snapshot has finished yet — IS a real answer and IS
        cached for the full TTL. In-process that costs nothing: the pricing batch
        calls ``invalidate_price_cache()`` the moment it commits. But a snapshot
        finished by ANOTHER process (a script, a second web dyno) stays invisible
        here for up to ``_PRICE_TTL_S``, which for battles means
        ``_require_prices`` keeps returning 409 for that long after prices really
        do exist. A minute of that is the accepted price of not re-reading the
        whole CardPrice table per frame; anything that needs it sooner should
        call ``invalidate_price_cache()``."""
    hit = _price_cache
    if hit is not None and time.monotonic() < hit[0]:
        return hit[1], hit[2]
    pmap, asof = await _query_price_map(session)
    _set_price_cache(pmap, asof)
    return pmap, asof


def _set_price_cache(pmap, asof) -> None:
    global _price_cache
    _price_cache = (time.monotonic() + _PRICE_TTL_S, pmap, asof)


def invalidate_price_cache() -> None:
    """Drop the memo — called by the pricing batch (app/stats/pricing.py) the
    moment it commits a new snapshot, so a process does not serve prices it has
    itself just superseded."""
    global _price_cache
    _price_cache = None


async def _query_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]:
    """The uncached body of latest_price_map."""
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
