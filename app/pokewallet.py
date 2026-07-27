"""Client for PokéWallet API (https://www.pokewallet.io/api-docs)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.pokewallet.io"


def _base_url() -> str:
    return os.environ.get("POKEWALLET_BASE_URL", "").strip() or BASE_URL


def make_async_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Shared-client factory bound to the (env-overridable) base URL."""
    return httpx.AsyncClient(base_url=_base_url(), timeout=timeout)

log = logging.getLogger("pokemon_scanner.pokewallet")

# --- the process-wide client for callers that bring none -----------------------
# Every search_cards() without a `client` used to build an httpx.AsyncClient and
# throw it away one request later: a fresh TCP connect and TLS handshake to
# api.pokewallet.io per card lookup, plus the client construction itself (measured
# at 13-35ms on the loop in Task 5's review). A binder page misses cache on
# several cells; a live session does it a card at a time, forever. One client
# keeps the connection pool — and the TLS session — alive across all of them.
#
# Kept only while it still belongs to the CURRENT base url and the CURRENT event
# loop, because both change under it in practice: tests point POKEWALLET_BASE_URL
# at a local stub mid-process, and scripts call asyncio.run() more than once,
# which would otherwise hand out a connection pool bound to a closed loop.
#
# The loop is held as a WEAK REFERENCE and compared by identity — never by id().
# CPython reuses the address: six successive asyncio.run() calls in one process
# were measured returning the same id() every time, so an id-keyed cache would
# have decided a brand-new loop was the old one and served it a pool of dead
# sockets. A weakref compares the object itself and simply reads None once the
# old loop is collected.
_shared_client: httpx.AsyncClient | None = None
_shared_base: str | None = None
_shared_loop: "weakref.ref | None" = None
_shared_lock = asyncio.Lock()


def _client_is_current(c: httpx.AsyncClient | None, base: str, loop) -> bool:
    return (c is not None and not c.is_closed and _shared_base == base
            and _shared_loop is not None and _shared_loop() is loop)


async def shared_client() -> httpx.AsyncClient:
    """The module's own AsyncClient, built on first use. Callers must NOT close
    it — its lifetime is the process's (see close_shared_client, wired into the
    app lifespan). Same 30s timeout the throwaway clients used."""
    global _shared_client, _shared_base, _shared_loop
    loop, base = asyncio.get_running_loop(), _base_url()
    if _client_is_current(_shared_client, base, loop):
        return _shared_client
    superseded = None
    async with _shared_lock:
        # NOTHING IN HERE AWAITS, deliberately. Constructing the client is
        # synchronous, so this block cannot yield and two tasks cannot both build
        # one even without the lock; the lock states the invariant and keeps it
        # true if a line here ever grows an await. Await-free also means never
        # CONTENDED, and that is load-bearing: an asyncio.Lock binds to the first
        # event loop that ever blocks on it and raises `RuntimeError: bound to a
        # different event loop` for every other one, while this module is used
        # from several loops in one process (a TestClient per test, a script
        # calling asyncio.run twice). The retiring client is therefore closed
        # after the block, not inside it.
        c = _shared_client
        if _client_is_current(c, base, loop):
            return c
        if c is not None and not c.is_closed and _shared_loop is not None \
                and _shared_loop() is loop:
            # Same live loop, only the base URL moved: this client is ours to
            # close properly. One from a DEAD loop is instead dropped WITHOUT
            # aclose — its transports belong to a loop that no longer runs, and
            # closing them from this one is not a close, it is an error.
            superseded = c
        _shared_client = make_async_client()
        _shared_base, _shared_loop = base, weakref.ref(loop)
        log.debug("pokewallet.shared_client_created base=%s", base)
        out = _shared_client
    if superseded is not None:
        await superseded.aclose()
    return out


async def close_shared_client() -> None:
    """Release the shared client — called from the app lifespan's shutdown so the
    pool's sockets are closed cleanly instead of at interpreter exit."""
    global _shared_client, _shared_base, _shared_loop
    c, _shared_client, _shared_base, _shared_loop = _shared_client, None, None, None
    if c is not None and not c.is_closed:
        await c.aclose()


def get_api_key() -> str | None:
    return os.environ.get("POKEWALLET_API_KEY", "").strip() or None


def _sanitize_query_fragment(term: str) -> str:
    cleaned = re.sub(r"[^\w\s\-/]", " ", term, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120] if cleaned else ""


async def search_cards(
    query: str,
    *,
    limit: int = 20,
    page: int = 1,
    api_key: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Call GET /search. Returns full JSON (results, pagination, metadata)."""
    q = _sanitize_query_fragment(query)
    if len(q) < 2:
        log.info("pokewallet.search skipped (query too short after sanitize) raw=%r", query[:80])
        return {"results": [], "pagination": {}, "metadata": {}}

    params = {"q": q, "limit": min(limit, 100), "page": max(page, 1)}
    headers = {"X-API-Key": api_key}
    log.info(
        "pokewallet.search q=%r limit=%s page=%s",
        q,
        params["limit"],
        params["page"],
    )

    # A caller that passed a client still owns it; otherwise use — and do NOT
    # close — the module's shared one.
    if client is None:
        client = await shared_client()

    resp = await client.get("/search", params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    n = len(data.get("results") or [])
    log.info("pokewallet.search_results count=%s", n)
    return data


def pokewallet_image_url(card_id: str, size: str = "high") -> str:
    safe = quote(str(card_id), safe="")
    return f"{_base_url()}/images/{safe}?size={size}"


async def lookup_card_exact(
    set_id: str,
    numerator: str,
    *,
    set_name: str | None = None,
    api_key: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """
    Keyed lookup: query "<set_id> <number>" (PokéWallet supports this form) and
    exact-filter results on card number numerator (and set name when provided).
    Returns the raw card dict or None.
    """
    num = numerator.lstrip("0") or "0"
    data = await search_cards(f"{set_id} {num}", limit=25, api_key=api_key, client=client)
    results = data.get("results") or []
    for c in results:
        info = c.get("card_info") or {}
        raw = str(info.get("card_number") or "").strip()
        raw_num = raw.split("/")[0].strip().lstrip("0") or "0"
        if raw_num.upper() != num.upper():
            continue
        if not _set_name_matches(set_name, info.get("set_name")):
            continue
        return c
    log.info(
        "pokewallet.lookup_exact miss set_id=%s num=%s candidates=%s",
        set_id, num,
        [
            {"card_number": (c.get("card_info") or {}).get("card_number"),
             "set_name": (c.get("card_info") or {}).get("set_name")}
            for c in results[:3]
        ],
    )
    return None


def _set_name_matches(expected: str | None, actual: object) -> bool:
    """Set-name filter tolerant of formatting differences ("Twilight Masquerade"
    vs "SV06: Twilight Masquerade"). The query is already set_id-scoped and the
    numerator is exact-matched, so containment is safe here."""
    if not expected:
        return True
    e = expected.strip().lower()
    a = str(actual or "").strip().lower()
    return e == a or e in a or a in e
