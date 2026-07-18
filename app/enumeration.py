"""Whole-set card enumeration from PokéWallet into the card table
(source='enumerate'). Primary form: paginated search q=<set_id>. If that
returns nothing (query form unsupported), falls back to iterating numerators
1..denominator+40 through lookup_card_exact, throttled."""
from __future__ import annotations

import asyncio, logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Card
from app.db.session import async_session_maker
from app.pack.set_resolution import load_denominator_table
from app.pokewallet import (get_api_key, lookup_card_exact, make_async_client,
                            pokewallet_image_url, search_cards)

log = logging.getLogger("pokemon_scanner.enumeration")


def _norm_num(card_number: str | None) -> str | None:
    if not card_number:
        return None
    head = str(card_number).split("/")[0].strip().upper()
    return (head.lstrip("0") or "0") if head else None


async def _upsert(set_id: str, results: list[dict[str, Any]]) -> int:
    rows = []
    for c in results:
        info = c.get("card_info") or {}
        cid, num = c.get("id"), _norm_num(info.get("card_number"))
        if not cid or not num:
            continue
        rows.append(dict(
            match_id=str(cid), set_id=set_id, numerator=num,
            set_name=info.get("set_name"), name=info.get("name") or info.get("clean_name"),
            rarity=info.get("rarity"), image_url=pokewallet_image_url(cid),
            payload=c, source="enumerate",
        ))
    if not rows:
        return 0
    async with async_session_maker() as session:
        for r in rows:  # per-row upsert; enumerate never clobbers richer sources
            stmt = pg_insert(Card).values(**r).on_conflict_do_nothing(index_elements=["match_id"])
            await session.execute(stmt)
        await session.commit()
    return len(rows)


async def enumerate_set(set_id: str) -> dict:
    """Returns {"set_id", "cards": n, "method"} — cards upserted into `card`."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("POKEWALLET_API_KEY not configured")
    total, page, method = 0, 1, "search"
    async with make_async_client() as client:
        while True:  # paginated q=<set_id>
            data = await search_cards(str(set_id), limit=100, page=page,
                                      api_key=api_key, client=client)
            results = [c for c in (data.get("results") or [])
                       if str((c.get("card_info") or {}).get("set_id") or set_id) == str(set_id)]
            if not results:
                break
            total += await _upsert(set_id, results)
            pag = data.get("pagination") or {}
            if page >= int(pag.get("total_pages") or page):
                break
            page += 1
            await asyncio.sleep(0.15)
        if total == 0:  # fallback: iterate numerators
            method = "iterate"
            table = load_denominator_table()
            entry = next((s for s in table.sets if s.set_id == str(set_id)), None)
            denoms = [int(d) for d in (entry.denominators if entry else []) if str(d).isdigit()]
            top = (max(denoms) if denoms else 200) + 40
            for n in range(1, top + 1):
                try:
                    m = await lookup_card_exact(str(set_id), str(n), api_key=api_key, client=client)
                except Exception as e:
                    log.warning("enumeration.iterate_failed n=%s err=%r", n, e)
                    m = None
                if m is not None:
                    total += await _upsert(str(set_id), [m])
                await asyncio.sleep(0.15)
    log.info("enumeration.done set=%s cards=%s method=%s", set_id, total, method)
    return {"set_id": str(set_id), "cards": total, "method": method}
