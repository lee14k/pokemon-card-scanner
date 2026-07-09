"""Generate app/dex/data/species.json from PokéAPI (BUILD-TIME ONLY; output is committed).

Usage: .venv/bin/python scripts/build_species_list.py
Fetches the species slug list once, converts slugs to display names via title-casing
plus an exceptions map for punctuation-tricky names, and keys entries by an
aggressive alphanumeric-only key so card-name lookups survive punctuation drift.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "app" / "dex" / "data" / "species.json"

# slug -> canonical display name, for names where title-casing hyphens is wrong.
EXCEPTIONS: dict[str, str] = {
    "mr-mime": "Mr. Mime", "mime-jr": "Mime Jr.", "mr-rime": "Mr. Rime",
    "farfetchd": "Farfetch'd", "sirfetchd": "Sirfetch'd",
    "ho-oh": "Ho-Oh", "porygon-z": "Porygon-Z",
    "nidoran-f": "Nidoran♀", "nidoran-m": "Nidoran♂",
    "flabebe": "Flabébé", "type-null": "Type: Null",
    "jangmo-o": "Jangmo-o", "hakamo-o": "Hakamo-o", "kommo-o": "Kommo-o",
    "wo-chien": "Wo-Chien", "chien-pao": "Chien-Pao", "ting-lu": "Ting-Lu", "chi-yu": "Chi-Yu",
}


def _alnum_key(display: str) -> str:
    s = display.lower().replace("♀", "f").replace("♂", "m")
    # Fold diacritics to ASCII (é -> e) so accented and plain prints share a key.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", s)


def _display_from_slug(slug: str) -> str:
    if slug in EXCEPTIONS:
        return EXCEPTIONS[slug]
    # default: hyphens are spaces ("tapu-koko" -> "Tapu Koko", "great-tusk" -> "Great Tusk")
    return " ".join(w.capitalize() for w in slug.split("-"))


def main() -> None:
    resp = httpx.get("https://pokeapi.co/api/v2/pokemon-species?limit=2000", timeout=60.0)
    resp.raise_for_status()
    results = resp.json()["results"]
    out: dict[str, str] = {}
    for r in results:
        display = _display_from_slug(r["name"])
        key = _alnum_key(display)
        if not key:
            print(f"skipping empty key for slug {r['name']!r}", file=sys.stderr)
            continue
        out[key] = display
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} species)")


if __name__ == "__main__":
    main()
