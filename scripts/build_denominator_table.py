"""
Build app/pack/data/set_denominators.json from PokéWallet data.

Usage: POKEWALLET_API_KEY=... .venv/bin/python scripts/build_denominator_table.py
Review the printed table, then commit the JSON.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pokewallet import get_api_key, search_cards  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
SYMBOL_INDEX = _ROOT / "app" / "data" / "set_symbols" / "index.json"
OUT = _ROOT / "app" / "pack" / "data" / "set_denominators.json"

# Canonical code + era per pokesymbols file slug. The index's own set_code values
# are scraped and non-canonical (e.g. "S-P" for SWSH base) — never join on them.
SLUG_TO_SET: dict[str, tuple[str, str]] = {
    "sword-and-shield": ("SSH", "swsh"),
    "rebel-clash": ("RCL", "swsh"),
    "darkness-ablaze": ("DAA", "swsh"),
    "champions-path": ("CPA", "swsh"),
    "vivid-voltage": ("VIV", "swsh"),
    "shining-fates": ("SHF", "swsh"),
    "battle-styles": ("BST", "swsh"),
    "chilling-reign": ("CRE", "swsh"),
    "evolving-skies": ("EVS", "swsh"),
    "celebrations": ("CEL", "swsh"),
    "fusion-strike": ("FST", "swsh"),
    "brilliant-stars": ("BRS", "swsh"),
    "astral-radiance": ("ASR", "swsh"),
    "pokemon-go": ("PGO", "swsh"),
    "lost-origin": ("LOR", "swsh"),
    "silver-tempest": ("SIT", "swsh"),
    "crown-zenith": ("CRZ", "swsh"),
    "brilliant-stars-trainer-gallery": ("BRS-TG", "swsh"),
    "astral-radiance-trainer-gallery": ("ASR-TG", "swsh"),
    "lost-origin-trainer-gallery": ("LOR-TG", "swsh"),
    "silver-tempest-trainer-gallery": ("SIT-TG", "swsh"),
    "crown-zenith-galarian-gallery": ("CRZ-GG", "swsh"),
    "shining-fates-shiny-vault": ("SHF-SV", "swsh"),
    "scarlet-and-violet": ("SVI", "sv"),
    "paldea-evolved": ("PAL", "sv"),
    "obsidian-flames": ("OBF", "sv"),
    "151": ("MEW", "sv"),
    "paradox-rift": ("PAR", "sv"),
    "paldean-fates": ("PAF", "sv"),
    "temporal-forces": ("TEF", "sv"),
    "twilight-masquerade": ("TWM", "sv"),
    "shrouded-fable": ("SFA", "sv"),
    "stellar-crown": ("SCR", "sv"),
    "surging-sparks": ("SSP", "sv"),
    "prismatic-evolutions": ("PRE", "sv"),
    "journey-together": ("JTG", "sv"),
    "destined-rivals": ("DRI", "sv"),
    "black-bolt": ("BLK", "sv"),
    "white-flare": ("WHT", "sv"),
    # Black-star promo sets. Currently carry placeholder set_id=-172 in the symbol
    # index (skipped by the non-positive guard). Listed so that once a real id is
    # scraped, the build picks them up automatically instead of silently omitting them.
    "swsh-black-star-promos": ("SWSHP", "swsh"),
    "scarlet-and-violet-black-star-promos": ("SVP", "sv"),
}


async def main() -> None:
    api_key = get_api_key()
    if not api_key:
        raise SystemExit("Set POKEWALLET_API_KEY")

    entries = json.loads(SYMBOL_INDEX.read_text())
    rows = []
    seen_slugs: set[str] = set()
    for e in entries:
        slug = str(e.get("file", "")).removesuffix(".png")
        if slug not in SLUG_TO_SET:
            continue
        code, era = SLUG_TO_SET[slug]
        seen_slugs.add(slug)
        set_id = str(e["set_id"])
        if not set_id.lstrip("-").isdigit() or int(set_id) <= 0:
            print(f"{code:5} SKIPPED: placeholder set_id={set_id} in symbol index "
                  f"(fix via scripts/scrape_pokesymbols.py / build_set_symbol_index.py)")
            continue
        # limit=100 is enough: the printed denominator appears on every base card, so
        # >=3 hits are virtually guaranteed even in large sets (FST 264, SSH 202, EVS 203).
        data = await search_cards(set_id, limit=100, api_key=api_key)
        results = data.get("results") or []
        denoms: Counter[str] = Counter()
        set_name = None
        for c in results:
            info = c.get("card_info") or {}
            set_name = set_name or info.get("set_name")
            raw = str(info.get("card_number") or "")
            if "/" in raw:
                denoms[raw.split("/")[1].strip().upper()] += 1
        # Keep denominators seen on >= 3 cards (filters misparses); secret rares
        # share the printed denominator so the modal value is the right one.
        keep = sorted([d for d, n in denoms.items() if n >= 3])
        if not keep:
            print(f"{code:5} WARNING: NO denominators found "
                  f"(API returned {len(results)} results — fill this row by hand)")
        rows.append(
            {
                "set_id": set_id,
                "set_code": code,
                "set_name": set_name or code,
                "era": era,
                "denominators": keep,
                "promo_prefix": None,
            }
        )
        print(f"{code:5} {set_id:>7} {set_name!s:38} denominators={keep} (sampled {len(results)})")

    missing = set(SLUG_TO_SET) - seen_slugs
    if missing:
        print(f"\nWARNING: no symbol-index entry for slugs: {sorted(missing)}")
        print("Re-seed via scripts/run_set_symbol_pipeline.sh, then rerun.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"sets": rows}, indent=2) + "\n")
    print(f"\nWrote {OUT} ({len(rows)} sets). Review denominators before committing.")


if __name__ == "__main__":
    asyncio.run(main())
