"""Search query building and card–text scoring for PokéWallet results."""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

# Single-token searches are too noisy (e.g. "ex", "gx", "go").
_NOISE_FRAGMENTS = frozenset(
    {
        "basic",
        "stage",
        "hp",
        "weakness",
        "resistance",
        "retreat",
        "energy",
        "pokemon",
        "trainer",
        "item",
        "stadium",
        "tool",
        "supporter",
        "ability",
        "attack",
        "attacks",
        "vmax",
        "vstar",
        "ex",
        "gx",
        "tag",
        "team",
        "rule",
        "illus",
        "illustrator",
    }
)


def normalize_card_title(name: str) -> str:
    """Lowercase and drop trailing parenthetical (set / promo suffixes from API)."""
    s = (name or "").strip()
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def is_usable_search_fragment(s: str) -> bool:
    t = s.strip()
    if len(t) < 4:
        return False
    if t.lower() in _NOISE_FRAGMENTS:
        return False
    if re.fullmatch(r"\d+", t):
        return False
    if re.fullmatch(r"\d+/\d+", t):
        return False
    letters = sum(1 for c in t if c.isalpha())
    if letters < 3:
        return False
    return True


def build_search_queries(
    *,
    card_name_hint: str | None,
    ocr_fragments: list[str],
    max_queries: int = 3,
) -> list[str]:
    """
    Fewer, cleaner queries → fewer unrelated PokéWallet hits.
    """
    if card_name_hint and card_name_hint.strip():
        q = re.sub(r"\s+", " ", card_name_hint.strip())
        return [q[:120]]

    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q.strip())
        if len(q) < 4:
            return
        key = q.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(q[:120])

    usable = [f for f in ocr_fragments if is_usable_search_fragment(f)]
    # Prefer longer lines (usually the Pokémon name line).
    usable.sort(key=len, reverse=True)

    for f in usable[:max_queries]:
        add(f)

    if not queries and ocr_fragments:
        # Last resort: shortest reasonable fragment
        for f in sorted(ocr_fragments, key=len):
            if len(f.strip()) >= 3:
                add(f.strip()[:120])
                break

    return queries[:max_queries]


def score_card_against_blob(card: dict[str, Any], blob: str) -> float:
    """
    Name-first score 0–100. Set name is a small bonus only when the name already matches.
    """
    info = card.get("card_info") or {}
    raw = info.get("name") or info.get("clean_name") or ""
    norm = normalize_card_title(raw)
    if not norm:
        return 0.0

    b = (blob or "").lower()
    if not b.strip():
        return 0.0

    name_scores = [
        fuzz.WRatio(norm, b),
        fuzz.token_set_ratio(norm, b),
        fuzz.partial_ratio(norm, b),
    ]
    name_best = max(name_scores)

    bonus = 0.0
    set_name = (info.get("set_name") or "").strip().lower()
    if set_name and name_best >= 42:
        # Cap set influence so "Scarlet"/"Paldea" alone can't drown the Pokémon name.
        set_hit = fuzz.partial_ratio(set_name, b)
        bonus = min(12.0, set_hit * 0.12)

    return min(100.0, name_best + bonus)
