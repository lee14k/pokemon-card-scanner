"""Orchestrate the stats batch: re-derive -> snapshot -> aggregate -> anomalies.

Runs inside the web service (needs Railway-volume access to read pull photos).
A Postgres advisory lock makes concurrent triggers (cron + manual) a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from app.db.models import StatsSnapshot
from app.db.session import async_session_maker
from app.stats.aggregate import aggregate_snapshot
from app.stats.anomaly import detect
from app.stats.prior import default_prior_source
from app.stats.rederive import rederive_pending

log = logging.getLogger("pokemon_scanner.stats.run_batch")

_LOCK_KEY = 880117  # arbitrary app-wide advisory lock id for the stats batch


async def run_batch(trigger: str = "manual") -> str | None:
    """Run a full batch. Returns the snapshot id, or None if another run holds the lock."""
    await rederive_pending()
    prior = default_prior_source()
    async with async_session_maker() as session:
        got = (await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar()
        if not got:
            log.info("run_batch.skipped lock_held")
            return None
        try:
            snap = StatsSnapshot(trigger=trigger, status="running")
            session.add(snap)
            await session.flush()
            try:
                await aggregate_snapshot(session, snap.id, prior)
                await detect(session, snap.id, prior)
                snap.status = "done"
                await session.commit()
                log.info("run_batch.done snapshot=%s trigger=%s", snap.id, trigger)
                return str(snap.id)
            except Exception:
                snap.status = "failed"
                await session.commit()
                raise
        finally:
            await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            await session.commit()


if __name__ == "__main__":
    print(asyncio.run(run_batch("cli")))
