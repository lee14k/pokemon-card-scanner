"""Lazy local card cache: DB first, PokéWallet on a miss (then upsert).

Failure philosophy mirrors app.pack.matching: a broken/unreachable DB must never
break a lookup — every DB error degrades to the plain API call (or a miss)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import re

from app.db.models import Card, SetIdMap, TcgdexCard
from app.db.session import async_session_maker
from app.pokewallet import lookup_card_exact, pokewallet_image_url

log = logging.getLogger("pokemon_scanner.cards")


def normalize_local_id(lid: str) -> str:
    """Uppercase a local_id / OCR numerator with leading zeros stripped from its
    numeric tail while any alpha (gallery/promo) prefix is preserved:
    "012"->"12", "TG22"->"TG22", "GG07"->"GG7", "SWSH009"->"SWSH9". A form that
    doesn't split cleanly into <alpha?><digits> is returned uppercased unchanged.
    Used on BOTH sides wherever an OCR numerator is compared against a catalog
    local_id, so gallery-prefixed numbers compare equal despite tail zeros."""
    s = (lid or "").strip().upper()
    m = re.fullmatch(r"([A-Z]*)0*(\d+)", s)
    return f"{m.group(1)}{m.group(2)}" if m else s


# A set's numerator catalog changes only when scripts/ingest_tcgdex.py or
# scripts/build_id_maps.py runs — i.e. never inside a serving process — so a
# populated answer is cached for ten minutes. AN EMPTY ANSWER IS NOT: it is
# fail-closed evidence (see get_set_numerators), and the two ways to get one are
# a genuinely unmapped/un-ingested set and a TRANSIENT DB FAILURE, which this
# layer cannot tell apart. Pinning the failure for ten minutes would keep every
# card in the session's set flagged long after the database came back, so both
# empties share a 30-second TTL instead — short enough that recovery is invisible
# to a user, long enough that the next page or live frame is not another round
# trip. The un-ingested set pays one cheap query every 30s for that, which is the
# right side of the trade.
#
# NO LOCK AROUND THE MISS, deliberately. A single-flight lock would save the
# duplicate queries when a binder page's nine cells miss together — worth about
# eight cheap queries once per TTL — and would cost a crash mode: an
# asyncio.Lock binds to the first event loop that ever BLOCKS on it and raises
# `RuntimeError: bound to a different event loop` on every other one (verified on
# 3.12), and a process can absolutely have several loops (a TestClient per test,
# a script calling asyncio.run twice). Unlike name_index's one-shot lock, these
# entries EXPIRE, so a second loop really can reach the slow path. That
# RuntimeError would surface inside resolve_identity, which does not guard this
# call — a flagged or failed cell, to save eight queries. Concurrent misses
# instead each run the query and each store the same answer, which is what
# happens today on every single call anyway.
_NUMERATORS_TTL_S = 600.0
_NUMERATORS_EMPTY_TTL_S = 30.0
_numerators_cache: dict[str, tuple[float, frozenset[str]]] = {}


async def get_set_numerators(set_id: str) -> frozenset[str]:
    """Normalized card numbers of a set, via set_id_map -> tcgdex_card. Numeric
    ids lose leading zeros ("012" -> "12"); gallery/promo ids keep their alpha
    prefix and lose only their tail zeros ("TG22" -> "TG22", "GG07" -> "GG7") so
    a printed "TG22"/"GG7" numerator validates against the catalog. Feeds OCR
    constraint repair. Empty on any failure or when the set isn't mapped/ingested
    — the caller then simply skips catalog correction.

    Memoized per set_id with the two TTLs above; the result is a frozenset so the
    shared cached value cannot be mutated by a caller. Callers only ever test
    membership and truthiness."""
    key = str(set_id)
    hit = _numerators_cache.get(key)
    if hit is not None and time.monotonic() < hit[0]:
        return hit[1]
    out = await _query_set_numerators(key)
    ttl = _NUMERATORS_TTL_S if out else _NUMERATORS_EMPTY_TTL_S
    _numerators_cache[key] = (time.monotonic() + ttl, out)
    return out


async def _query_set_numerators(set_id: str) -> frozenset[str]:
    """The uncached body of get_set_numerators."""
    try:
        async with async_session_maker() as session:
            tdx = (await session.execute(
                select(SetIdMap.tcgdex_set_id)
                .where(SetIdMap.pokewallet_set_id == str(set_id))
            )).scalar_one_or_none()
            if not tdx:
                return frozenset()
            rows = (await session.execute(
                select(TcgdexCard.local_id).where(TcgdexCard.set_id == tdx)
            )).scalars().all()
        out: set[str] = set()
        for lid in rows:
            if not lid:
                continue
            if re.fullmatch(r"\d+", lid):
                out.add(str(int(lid)))  # "012" -> "12"
            elif re.match(r"[A-Za-z]", lid):  # gallery/promo, e.g. "TG22", "GG07"
                out.add(normalize_local_id(lid))
        return frozenset(out)
    except Exception as e:
        log.warning("cards.set_numerators_failed set=%s err=%r", set_id, e)
        return frozenset()


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
