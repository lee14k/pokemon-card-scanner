"""Keyed PokéWallet lookups for resolved cards (parallel, failure-tolerant)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.pack.set_resolution import SetResolution
from app.pokewallet import lookup_card_exact, make_async_client, pokewallet_image_url

log = logging.getLogger("pokemon_scanner.pack.matching")


async def lookup_resolved_cards(
    items: list[tuple[str | None, SetResolution]],  # (numerator, resolution) per row
    *,
    api_key: str | None,
) -> list[dict[str, Any] | None]:
    """One PokéWallet hit per resolvable row, gathered concurrently.
    Unresolvable rows and upstream failures yield None (graceful degradation):
    no single row's lookup may abort the whole pack scan."""
    if not api_key:
        return [None] * len(items)

    async with make_async_client() as client:

        async def one(numerator: str | None, res: SetResolution) -> dict[str, Any] | None:
            if not numerator or not res.set_id:
                return None
            try:
                return await lookup_card_exact(
                    res.set_id, numerator,
                    set_name=res.set_name, api_key=api_key, client=client,
                )
            # httpx.HTTPError = transport/status; ValueError = JSONDecodeError on a
            # non-JSON 200 (e.g. an upstream maintenance page).
            except (httpx.HTTPError, ValueError) as e:
                log.warning("matching.lookup_failed set=%s num=%s err=%s",
                            res.set_id, numerator, e)
                return None

        # return_exceptions=True: an unexpected error in one row degrades that row to
        # None instead of cancelling every other row's lookup.
        results = await asyncio.gather(
            *(one(n, r) for n, r in items), return_exceptions=True
        )

    out: list[dict[str, Any] | None] = []
    for r in results:
        if isinstance(r, BaseException):
            log.warning("matching.lookup_unexpected err=%r", r)
            out.append(None)
        else:
            out.append(r)
    return out


def card_fields_from_match(match: dict[str, Any] | None) -> dict[str, Any]:
    if match is None:
        return {"name": None, "rarity": None, "image_url": None, "match_id": None}
    info = match.get("card_info") or {}
    cid = match.get("id")
    return {
        "name": info.get("name") or info.get("clean_name"),
        "rarity": info.get("rarity"),
        "image_url": pokewallet_image_url(cid) if cid else None,
        "match_id": cid,
    }
