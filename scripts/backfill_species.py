"""One-off: populate pull_card.species for rows saved before the column existed.

Usage: DATABASE_URL=... AUTH_SECRET=... .venv/bin/python scripts/backfill_species.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import PullCard  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402
from app.dex.species import species_of  # noqa: E402


async def main() -> None:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(PullCard).where(PullCard.species.is_(None), PullCard.name.is_not(None))
            )
        ).scalars().all()
        changed = 0
        for r in rows:
            sp = species_of(r.name)
            if sp is not None:
                r.species = sp
                changed += 1
        await session.commit()
        print(f"backfilled {changed} of {len(rows)} candidate rows")


if __name__ == "__main__":
    asyncio.run(main())
