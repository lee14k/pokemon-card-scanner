"""Client for Pokémon TCG API v2 (https://docs.pokemontcg.io/)."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

BASE_URL = "https://api.pokemontcg.io/v2"


def _escape_lucene_term(term: str) -> str:
    """Minimal escaping for name search; keep letters, numbers, spaces."""
    cleaned = re.sub(r"[^\w\s\-]", " ", term, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:80] if cleaned else ""


async def search_cards_by_name(
    name_fragment: str,
    *,
    page_size: int = 8,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    fragment = _escape_lucene_term(name_fragment)
    if len(fragment) < 2:
        return []

    # Wildcard prefix/suffix helps when OCR drops characters
    q = f'name:"*{fragment}*"'
    params = {"q": q, "pageSize": min(page_size, 250)}

    headers = {}
    key = os.environ.get("POKEMON_TCG_API_KEY", "").strip()
    if key:
        headers["X-Api-Key"] = key

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    try:
        resp = await client.get("/cards", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") or []
    finally:
        if own_client and client is not None:
            await client.aclose()


async def search_cards_multi(
    fragments: list[str],
    *,
    per_fragment_limit: int = 6,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    try:
        for frag in fragments:
            cards = await search_cards_by_name(
                frag, page_size=per_fragment_limit, client=client
            )
            for c in cards:
                cid = c.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    merged.append(c)
    finally:
        if own_client and client is not None:
            await client.aclose()

    return merged
