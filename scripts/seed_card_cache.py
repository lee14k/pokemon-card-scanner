"""One-off: seed the card cache from pull_card history (source='seed').

Existing rows (e.g. from live lookups) are left untouched: ON CONFLICT DO NOTHING.
Usage: DATABASE_URL=... AUTH_SECRET=... .venv/bin/python scripts/seed_card_cache.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.cards import normalize_numerator  # noqa: E402
from app.db.models import Card, Pull, PullCard  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402


async def main() -> None:
    async with async_session_maker() as session:
        # Newest first so the freshest pull's metadata wins for a repeated match_id.
        rows = (
            await session.execute(
                select(PullCard)
                .join(Pull, PullCard.pull_id == Pull.id)
                .where(PullCard.match_id.is_not(None))
                .order_by(Pull.created_at.desc())
            )
        ).scalars().all()

        seen: set[str] = set()
        skipped = inserted = 0
        for r in rows:
            if r.match_id in seen:
                continue
            seen.add(r.match_id)
            if not r.set_id or not r.card_number:
                skipped += 1  # can't key the cache without a set + number
                continue
            numerator = normalize_numerator(r.card_number.split("/")[0].strip())
            result = await session.execute(
                pg_insert(Card)
                .values(
                    match_id=r.match_id,
                    set_id=r.set_id,
                    numerator=numerator,
                    set_name=r.set_name,
                    name=r.name,
                    rarity=r.rarity,
                    image_url=r.image_url,
                    payload={
                        "id": r.match_id,
                        "card_info": {
                            "name": r.name,
                            "rarity": r.rarity,
                            "set_name": r.set_name,
                            "card_number": r.card_number,
                        },
                    },
                    source="seed",
                )
                .on_conflict_do_nothing(index_elements=["match_id"])
            )
            inserted += result.rowcount
        await session.commit()
        print(
            f"seeded {inserted} new card rows "
            f"({len(seen)} distinct match_ids in history, {skipped} skipped unresolvable)"
        )


if __name__ == "__main__":
    asyncio.run(main())
