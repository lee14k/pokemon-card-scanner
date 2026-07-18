"""Client for the matcher service. MATCHER_URL unset ⇒ feature off entirely.
Every failure degrades to None/False — the matcher is never load-bearing."""
from __future__ import annotations

import asyncio, logging, os
from typing import Any

import httpx

log = logging.getLogger("pokemon_scanner.matcher")


def _base() -> str | None:
    return os.environ.get("MATCHER_URL", "").strip().rstrip("/") or None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('MATCHER_TOKEN', '')}"}


def enabled() -> bool:
    return _base() is not None


async def match_strips(set_key: str, strip_jpegs: list[bytes],
                       timeout: float = 8.0) -> list[list[dict[str, Any]]] | None:
    """Top-5 [{'id','score'}] per strip, or None (disabled/unindexed/error)."""
    base = _base()
    if base is None or not strip_jpegs:
        return None
    files = [("strips", (f"s{i}.jpg", b, "image/jpeg")) for i, b in enumerate(strip_jpegs)]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/match/{set_key}", files=files, headers=_headers())
        if r.status_code == 404:
            return None  # no index yet — caller may trigger a build
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("matcher.match_failed set=%s err=%r", set_key, e)
        return None


async def build_index(set_key: str, cards: list[dict[str, str]],
                      timeout: float = 600.0) -> dict | None:
    """cards: [{'id','image_url'}]. Returns build report or None."""
    base = _base()
    if base is None or not cards:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/index/{set_key}", json={"cards": cards},
                                  headers=_headers())
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("matcher.index_failed set=%s err=%r", set_key, e)
        return None


_inflight: set[str] = set()


def kick_index_build(set_key: str) -> None:
    """Fire-and-forget: enumerate the set and build its index once."""
    if not enabled() or set_key in _inflight:
        return
    _inflight.add(set_key)

    async def _run() -> None:
        try:
            from app.cards import enumerated_cards_for_index
            cards = await enumerated_cards_for_index(set_key)
            if cards:
                await build_index(set_key, cards)
        except Exception as e:
            log.warning("matcher.kick_failed set=%s err=%r", set_key, e)
        finally:
            _inflight.discard(set_key)

    asyncio.get_running_loop().create_task(_run())
