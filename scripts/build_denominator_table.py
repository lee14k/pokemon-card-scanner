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

SYMBOL_INDEX = Path("app/data/set_symbols/index.json")
OUT = Path("app/pack/data/set_denominators.json")

# SWSH-era and SV-era set codes (spec scope), as used in the symbol index.
# Trainer Gallery / Galarian Gallery subsets are separate index entries where present.
SWSH_CODES = {
    "SSH", "RCL", "DAA", "CPA", "VIV", "SHF", "BST", "CRE", "EVS", "CEL",
    "FST", "BRS", "ASR", "PGO", "LOR", "SIT", "CRZ",
}
SV_CODES = {
    "SVI", "PAL", "OBF", "MEW", "PAR", "PAF", "TEF", "TWM", "SFA", "SCR",
    "SSP", "PRE", "JTG", "DRI", "BLK", "WHT", "MEG", "ASC", "PFL",
}
PROMO_PREFIXES = {"SWSH": "swsh", "SVP": "sv"}  # prefix -> era; set ids resolved below


async def main() -> None:
    api_key = get_api_key()
    if not api_key:
        raise SystemExit("Set POKEWALLET_API_KEY")

    entries = json.loads(SYMBOL_INDEX.read_text())
    rows = []
    for e in entries:
        code = (e.get("set_code") or "").upper()
        if code not in SWSH_CODES | SV_CODES:
            continue
        set_id = str(e["set_id"])
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
        rows.append(
            {
                "set_id": set_id,
                "set_code": code,
                "set_name": set_name or code,
                "era": "swsh" if code in SWSH_CODES else "sv",
                "denominators": keep,
                "promo_prefix": None,
            }
        )
        print(f"{code:5} {set_id:>7} {set_name!s:38} denominators={keep} (sampled {len(results)})")

    missing = (SWSH_CODES | SV_CODES) - {r["set_code"] for r in rows}
    if missing:
        print(f"\nWARNING: no symbol-index entry for codes: {sorted(missing)}")
        print("Re-seed via scripts/run_set_symbol_pipeline.sh, then rerun.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"sets": rows}, indent=2) + "\n")
    print(f"\nWrote {OUT} ({len(rows)} sets). Review denominators before committing.")


if __name__ == "__main__":
    asyncio.run(main())
