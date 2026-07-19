"""Lazy local card cache: DB first, PokéWallet on a miss (then upsert).

Failure philosophy mirrors app.pack.matching: a broken/unreachable DB must never
break a lookup — every DB error degrades to the plain API call (or a miss)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import re

from app.db.models import Card, SetIdMap, TcgdexCard
from app.db.session import async_session_maker
from app.pokewallet import lookup_card_exact, pokewallet_image_url

log = logging.getLogger("pokemon_scanner.cards")


async def get_set_numerators(set_id: str) -> set[str]:
    """Normalized (no leading zero) numeric card numbers of a set, via
    set_id_map -> tcgdex_card. Feeds OCR constraint repair. {} on any failure
    or when the set isn't mapped/ingested — the caller then simply skips
    catalog correction."""
    try:
        async with async_session_maker() as session:
            tdx = (await session.execute(
                select(SetIdMap.tcgdex_set_id)
                .where(SetIdMap.pokewallet_set_id == str(set_id))
            )).scalar_one_or_none()
            if not tdx:
                return set()
            rows = (await session.execute(
                select(TcgdexCard.local_id).where(TcgdexCard.set_id == tdx)
            )).scalars().all()
        out: set[str] = set()
        for lid in rows:
            if re.fullmatch(r"\d+", lid or ""):
                out.add(str(int(lid)))  # "012" -> "12"
        return out
    except Exception as e:
        log.warning("cards.set_numerators_failed set=%s err=%r", set_id, e)
        return set()


def normalize_numerator(numerator: str) -> str:
    """Same normalization as lookup_card_exact, uppercased for storage."""
    return (numerator.lstrip("0") or "0").upper()


async def _cache_get(set_id: str, num: str) -> dict[str, Any] | None:
    async with async_session_maker() as session:
        return (
            await session.execute(
                select(Card.payload)
                .where(Card.set_id == set_id, Card.numerator == num)
                .order_by(Card.last_fetched.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _cache_put(set_id: str, num: str, set_name: str | None, match: dict[str, Any]) -> None:
    cid = match.get("id")
    if not cid:
        return
    info = match.get("card_info") or {}
    stmt = pg_insert(Card).values(
        match_id=str(cid),
        set_id=set_id,
        numerator=num,
        set_name=info.get("set_name") or set_name,
        # Same field extraction as card_fields_from_match (app.pack.matching).
        name=info.get("name") or info.get("clean_name"),
        rarity=info.get("rarity"),
        image_url=pokewallet_image_url(cid),
        payload=match,
        source="lookup",
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["match_id"],
        set_={
            "payload": stmt.excluded.payload,
            "last_fetched": func.now(),
            "name": stmt.excluded.name,
            "rarity": stmt.excluded.rarity,
            "image_url": stmt.excluded.image_url,
            "set_name": stmt.excluded.set_name,
        },
    )
    async with async_session_maker() as session:
        await session.execute(stmt)
        await session.commit()


async def cached_lookup_card(
    set_id: str,
    numerator: str,
    *,
    set_name: str | None = None,
    api_key: str | None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Cached lookup_card_exact. Opens its own short-lived sessions so callers
    without a DB session (e.g. the scan pipeline) need no signature changes.
    Returns the raw PokéWallet card dict (from cache or API) or None."""
    num = normalize_numerator(numerator)

    try:
        cached = await _cache_get(set_id, num)
    except Exception as e:
        log.warning("cards.cache_read_failed set=%s num=%s err=%r", set_id, num, e)
        cached = None
    if cached is not None:
        log.info("cards.cache_hit set=%s num=%s", set_id, num)
        return cached

    if not api_key:
        return None
    match = await lookup_card_exact(
        set_id, numerator, set_name=set_name, api_key=api_key, client=client
    )
    if match is None:
        return None
    try:
        await _cache_put(set_id, num, set_name, match)
    except Exception as e:
        log.warning("cards.cache_write_failed set=%s num=%s err=%r", set_id, num, e)
    return match


async def get_cached_by_match_ids(match_ids: list[str]) -> dict[str, dict]:
    """match_id → payload for known cards; missing ids simply absent.
    DB failure degrades to {} (matching philosophy: never break a scan)."""
    if not match_ids:
        return {}
    try:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(Card.match_id, Card.payload).where(Card.match_id.in_(match_ids))
            )).all()
        return {m: p for m, p in rows}
    except Exception as e:
        log.warning("cards.by_match_ids_failed err=%r", e)
        return {}


async def enumerated_cards_for_index(set_id: str) -> list[dict]:
    """[{'id','image_url'}] for a set — enumerating from PokéWallet if the
    card table doesn't already hold the set. Used by matcher index builds."""
    from app.pokewallet import get_api_key

    async def _rows() -> list[dict]:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(Card.match_id, Card.image_url).where(Card.set_id == str(set_id))
            )).all()
        return [{"id": m, "image_url": u} for m, u in rows if u]

    try:
        rows = await _rows()
        # A handful of cache rows isn't a set: enumerate when clearly partial.
        if len(rows) < 40 and get_api_key():
            from app.enumeration import enumerate_set
            await enumerate_set(str(set_id))
            rows = await _rows()
        return rows
    except Exception as e:
        log.warning("cards.enumerated_for_index_failed set=%s err=%r", set_id, e)
        return []
