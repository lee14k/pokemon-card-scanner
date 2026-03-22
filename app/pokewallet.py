"""Client for PokéWallet API (https://www.pokewallet.io/api-docs)."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.pokewallet.io"


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
        return {"results": [], "pagination": {}, "metadata": {}}

    params = {"q": q, "limit": min(limit, 100), "page": max(page, 1)}
    headers = {"X-API-Key": api_key}

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    try:
        resp = await client.get("/search", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()
    finally:
        if own_client and client is not None:
            await client.aclose()


async def search_cards_multi(
    fragments: list[str],
    *,
    per_fragment_limit: int = 15,
    api_key: str,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    try:
        for frag in fragments:
            data = await search_cards(
                frag,
                limit=per_fragment_limit,
                api_key=api_key,
                client=client,
            )
            for c in data.get("results") or []:
                cid = c.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    merged.append(c)
    finally:
        if own_client and client is not None:
            await client.aclose()

    return merged


async def search_cards_for_lookup(
    search_queries: list[str],
    *,
    limit_per_query: int = 40,
    api_key: str,
) -> list[dict[str, Any]]:
    """Run a small set of search queries and merge unique cards (breadth, not 12× noisy queries)."""
    if not search_queries:
        return []
    return await search_cards_multi(
        search_queries,
        per_fragment_limit=limit_per_query,
        api_key=api_key,
    )


def pokewallet_image_url(card_id: str, size: str = "high") -> str:
    safe = quote(card_id, safe="")
    return f"{BASE_URL}/images/{safe}?size={size}"
