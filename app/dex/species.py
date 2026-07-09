"""Map a card name to its Pokémon species (display name), or None for non-Pokémon.

Lookup uses aggressive alphanumeric keys (diacritics folded, punctuation stripped)
so punctuation/accent variants match; unknown names return None — that is how
Trainer/Item/Energy cards stay out of the dex.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "species.json"

# Trailing tokens that are card mechanics, not part of the species name.
_SUFFIX_TOKENS = {"ex", "gx", "v", "vmax", "vstar", "break", "star"}
# Leading tokens that mark forms/regions and never begin a species name.
_PREFIX_TOKENS = {
    "radiant", "tera", "alolan", "galarian", "hisuian", "paldean",
    "origin", "therian", "incarnate", "bloodmoon", "forme", "shiny", "dark", "light",
}
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


@lru_cache(maxsize=1)
def _species_map() -> dict[str, str]:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def _alnum_key(s: str) -> str:
    s = s.lower().replace("♀", "f").replace("♂", "m")
    # Fold diacritics to ASCII (é -> e) so accented and plain prints share a key.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", s)


def species_of(card_name: str | None) -> str | None:
    """Best-effort species for a card name; None when it isn't a Pokémon card."""
    if not card_name:
        return None
    name = _PAREN_RE.sub("", card_name.strip())
    if "&" in name:  # defensive: multi-Pokémon cards take the first species
        name = name.split("&")[0].strip()
    tokens = name.split()
    while tokens and tokens[-1].lower() in _SUFFIX_TOKENS:
        tokens.pop()
    # "Prism Star" trails as two tokens; "star" is in the suffix set, then "prism":
    if tokens and tokens[-1].lower() == "prism":
        tokens.pop()
    while tokens and tokens[0].lower() in _PREFIX_TOKENS:
        tokens.pop(0)
    if not tokens:
        return None
    return _species_map().get(_alnum_key(" ".join(tokens)))
