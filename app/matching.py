"""Search query building and card–text scoring for PokéWallet results."""

from __future__ import annotations

import logging
import re
from typing import Any

from rapidfuzz import fuzz

log = logging.getLogger("pokemon_scanner.matching")

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
    card_number: str | None = None,
    set_id_from_symbol: str | None = None,
    set_code_from_symbol: str | None = None,
    primary_name_guess: str | None = None,
    max_queries: int = 8,
) -> list[str]:
    """
    Build PokéWallet /search queries: prefer set_id + number, then name + number, then text.
    See https://www.pokewallet.io/api-docs (set_id + card number, set codes, names).
    """
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q.strip())
        if len(q) < 2:
            return
        key = q.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(q[:120])

    hint = (card_name_hint or "").strip()
    primary = (primary_name_guess or "").strip()
    name = hint or primary

    num_first: str | None = None
    if card_number and "/" in card_number:
        num_first = card_number.split("/")[0].strip()

    # 1. Numeric set_id + card # (PokéWallet: "24541 148")
    if set_id_from_symbol and num_first:
        add(f"{set_id_from_symbol.strip()} {num_first}")

    # 2. Set code + card # (no slash)
    if set_code_from_symbol and num_first:
        add(f"{set_code_from_symbol.strip()} {num_first}")

    # 3. Set code + full fraction
    if set_code_from_symbol and card_number:
        add(f"{set_code_from_symbol.strip()} {card_number.strip()}")

    # 4. Name + collection number
    if name and card_number:
        add(f"{name} {card_number}")

    # 5. Name + numerator only
    if name and num_first:
        add(f"{name} {num_first}")

    # 6. Collection number alone
    if card_number:
        add(card_number.strip())

    # 7. Explicit user hint as name-only (after structured queries)
    if hint:
        add(hint)

    # 8. OCR name line / fragments (deduped by add())
    usable = [f for f in ocr_fragments if is_usable_search_fragment(f)]
    usable.sort(key=len, reverse=True)
    for f in usable:
        add(f)
        if len(queries) >= max_queries:
            break

    if not queries and ocr_fragments:
        for f in sorted(ocr_fragments, key=len):
            if len(f.strip()) >= 3:
                add(f.strip()[:120])
                break

    out = queries[:max_queries]
    log.info(
        "search.build_queries name=%r card_number=%r set_id=%s set_code=%s queries=%s",
        name or None,
        card_number,
        set_id_from_symbol,
        set_code_from_symbol,
        out,
    )
    return out


def _parse_cn_pair(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def score_card_against_blob(
    card: dict[str, Any],
    blob: str,
    *,
    parsed_collection_number: str | None = None,
) -> float:
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

    want = _parse_cn_pair(parsed_collection_number)
    api_raw = str(info.get("card_number") or "").strip()
    api_pair = _parse_cn_pair(api_raw)
    if want and api_pair and want == api_pair:
        bonus += 14.0
    elif want and api_pair and want[0] == api_pair[0]:
        bonus += 6.0

    return min(100.0, name_best + bonus)
