"""Ingest the TCGdex card catalog (swsh + sv series) into tcgdex_set / tcgdex_card.

Set-level summaries only: 2 series listings + one set-detail request per set
(~47 requests total), throttled and sequential — never the per-card endpoints.
Idempotent: re-runs upsert (ON CONFLICT DO UPDATE) and refresh raw/fetched_at.

Usage: DATABASE_URL=... .venv/bin/python scripts/ingest_tcgdex.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import func  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.db.models import TcgdexCard, TcgdexSet  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402

BASE_URL = "https://api.tcgdex.net/v2/en"
SERIES = ("swsh", "sv")
REQUEST_DELAY_S = 0.15  # politeness throttle between sequential requests


async def _get_json(client: httpx.AsyncClient, path: str) -> Any:
    await asyncio.sleep(REQUEST_DELAY_S)
    resp = await client.get(f"{BASE_URL}{path}")
    resp.raise_for_status()
    return resp.json()


async def main() -> None:
    per_series: dict[str, int] = {}
    total_cards = 0

    async with httpx.AsyncClient(timeout=30.0) as client, async_session_maker() as session:
        for series in SERIES:
            listing = await _get_json(client, f"/series/{series}")
            set_ids = [s["id"] for s in listing.get("sets") or []]
            per_series[series] = len(set_ids)
            print(f"series {series}: {len(set_ids)} sets")

            for set_id in set_ids:
                detail = await _get_json(client, f"/sets/{set_id}")
                cards = detail.get("cards") or []
                card_count = detail.get("cardCount") or {}
                # cards[] rows live in tcgdex_card; keep the set raw blob lean.
                set_raw = {k: v for k, v in detail.items() if k != "cards"}

                await session.execute(
                    pg_insert(TcgdexSet)
                    .values(
                        id=detail["id"],
                        name=detail["name"],
                        series=series,
                        card_count_official=card_count.get("official"),
                        card_count_total=card_count.get("total"),
                        raw=set_raw,
                    )
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "name": detail["name"],
                            "series": series,
                            "card_count_official": card_count.get("official"),
                            "card_count_total": card_count.get("total"),
                            "raw": set_raw,
                            "fetched_at": func.now(),
                        },
                    )
                )

                for card in cards:
                    await session.execute(
                        pg_insert(TcgdexCard)
                        .values(
                            id=card["id"],
                            set_id=detail["id"],
                            local_id=str(card["localId"]),
                            name=card.get("name"),
                            image_base=card.get("image"),
                            raw=card,
                        )
                        .on_conflict_do_update(
                            index_elements=["id"],
                            set_={
                                "set_id": detail["id"],
                                "local_id": str(card["localId"]),
                                "name": card.get("name"),
                                "image_base": card.get("image"),
                                "raw": card,
                            },
                        )
                    )
                total_cards += len(cards)
                print(f"  {detail['id']:12} {detail['name']!s:38} cards={len(cards)}")

        await session.commit()

    summary = ", ".join(f"{s}={n}" for s, n in per_series.items())
    print(f"\ningested sets per series: {summary}; total cards: {total_cards}")


if __name__ == "__main__":
    asyncio.run(main())
