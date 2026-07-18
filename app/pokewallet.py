"""Client for PokéWallet API (https://www.pokewallet.io/api-docs)."""

from __future__ import annotations

import logging
import os
import re
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

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=_base_url(), timeout=30.0)

    try:
        resp = await client.get("/search", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        n = len(data.get("results") or [])
        log.info("pokewallet.search_results count=%s", n)
        return data
    finally:
        if own_client and client is not None:
            await client.aclose()


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
