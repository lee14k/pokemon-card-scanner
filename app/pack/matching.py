"""Keyed PokéWallet lookups for resolved cards (parallel, failure-tolerant)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.pack.set_resolution import SetResolution
from app.pokewallet import _base_url, lookup_card_exact, pokewallet_image_url

log = logging.getLogger("pokemon_scanner.pack.matching")


async def lookup_resolved_cards(
    items: list[tuple[str | None, SetResolution]],  # (numerator, resolution) per row
    *,
    api_key: str | None,
) -> list[dict[str, Any] | None]:
    """One PokéWallet hit per resolvable row, gathered concurrently.
    Unresolvable rows and upstream failures yield None (graceful degradation)."""
    if not api_key:
        return [None] * len(items)

    async with httpx.AsyncClient(base_url=_base_url(), timeout=30.0) as client:

        async def one(numerator: str | None, res: SetResolution) -> dict[str, Any] | None:
            if not numerator or not res.set_id:
                return None
            try:
                return await lookup_card_exact(
                    res.set_id, numerator,
                    set_name=res.set_name, api_key=api_key, client=client,
                )
            except httpx.HTTPError as e:
                log.warning("matching.lookup_failed set=%s num=%s err=%s",
                            res.set_id, numerator, e)
                return None

        return list(await asyncio.gather(*(one(n, r) for n, r in items)))


def card_fields_from_match(match: dict[str, Any] | None) -> dict[str, Any]:
    if not match:
        return {"name": None, "rarity": None, "image_url": None, "match_id": None}
    info = match.get("card_info") or {}
    cid = match.get("id")
    return {
        "name": info.get("name") or info.get("clean_name"),
        "rarity": info.get("rarity"),
        "image_url": pokewallet_image_url(cid) if cid else None,
        "match_id": cid,
    }
