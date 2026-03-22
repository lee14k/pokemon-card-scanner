#!/usr/bin/env python3
"""
Match pokesymbols.com slugs + PNGs to PokéWallet set_id / set_code.

Prerequisites:
  1. Symbols + metadata (or regenerate):
         python scripts/scrape_pokesymbols.py \\
           --json-out app/data/set_symbols/pokesymbols_sets.json \\
           --download-dir app/data/set_symbols
  2. POKEWALLET_API_KEY in the environment, or in a project ``.env`` file.

Writes app/data/set_symbols/index.json for use by app/set_symbol_index.py.

Dependencies: httpx (see scripts/requirements-scrape.txt), rapidfuzz (requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx
from rapidfuzz import fuzz


def _maybe_load_dotenv() -> None:
    """Load POKEWALLET_API_KEY from repo-root .env if not already set."""
    if (os.environ.get("POKEWALLET_API_KEY") or "").strip():
        return
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("POKEWALLET_API_KEY="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                os.environ["POKEWALLET_API_KEY"] = val
            return

PW_BASE = "https://api.pokewallet.io"

# Pokesymbols slug -> substring that must appear in the matched PokéWallet set name (lowercase).
# Use when fuzzy match picks the wrong set.
_NAME_PIN_SLUGS: dict[str, str] = {
    "151": "151",
    "pokemon-go": "pokémon go",
    "pokemon-trading-card-game-classic-blastoise": "classic",
    "pokemon-trading-card-game-classic-charizard": "classic",
    "pokemon-trading-card-game-classic-venusaur": "classic",
}


def _normalize(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_pokesymbols(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_pokewallet_sets(client: httpx.Client, api_key: str) -> list[dict]:
    r = client.get(
        f"{PW_BASE}/sets",
        headers={"X-API-Key": api_key},
        timeout=120.0,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "sets", "results"):
            if key in data and isinstance(data[key], list):
                return data[key]
    raise ValueError(f"Unexpected /sets JSON shape: {type(data)}")


def _pw_set_name(row: dict) -> str:
    for k in ("name", "set_name", "title"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _best_pw_match(
    ps_name: str,
    slug: str,
    pw_rows: list[dict],
    min_score: float,
) -> tuple[dict | None, float]:
    names = [(_pw_set_name(r), r) for r in pw_rows]
    choices = [(n, r) for n, r in names if n]
    if not choices:
        return None, 0.0

    query = ps_name
    pin = _NAME_PIN_SLUGS.get(slug)
    best_row: dict | None = None
    best_sc = 0.0

    for name, row in choices:
        if pin and pin not in name.lower():
            continue
        sc = fuzz.WRatio(_normalize(query), _normalize(name))
        if sc > best_sc:
            best_sc = sc
            best_row = row

    if best_row is None and pin:
        return _best_pw_match(ps_name, "__no_pin__", pw_rows, min_score)

    if best_sc < min_score:
        return None, best_sc
    return best_row, best_sc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pokesymbols",
        type=Path,
        default=Path("app/data/set_symbols/pokesymbols_sets.json"),
    )
    ap.add_argument(
        "--png-dir",
        type=Path,
        default=Path("app/data/set_symbols"),
    )
    ap.add_argument(
        "--index-out",
        type=Path,
        default=Path("app/data/set_symbols/index.json"),
    )
    ap.add_argument("--min-score", type=float, default=82.0)
    ap.add_argument(
        "--unmatched-out",
        type=Path,
        default=Path("scripts/output/pokesymbols_unmatched.json"),
    )
    args = ap.parse_args()

    _maybe_load_dotenv()
    api_key = (os.environ.get("POKEWALLET_API_KEY") or "").strip()
    if not api_key:
        print("Set POKEWALLET_API_KEY in the environment.", file=sys.stderr)
        sys.exit(1)

    if not args.pokesymbols.is_file():
        print(f"Missing {args.pokesymbols} — run scrape_pokesymbols.py first.", file=sys.stderr)
        sys.exit(1)

    ps_rows = _load_pokesymbols(args.pokesymbols)
    with httpx.Client() as client:
        pw_rows = _fetch_pokewallet_sets(client, api_key)

    index: list[dict[str, str]] = []
    unmatched: list[dict] = []

    for row in ps_rows:
        slug = row["slug"]
        ps_name = row.get("name") or slug
        png = args.png_dir / f"{slug}.png"
        if not png.is_file():
            unmatched.append({**row, "reason": "missing_png"})
            continue

        pw_row, score = _best_pw_match(ps_name, slug, pw_rows, args.min_score)
        if pw_row is None:
            unmatched.append({**row, "reason": "no_fuzzy_match", "best_score": score})
            continue

        set_id = str(pw_row.get("set_id") or "").strip()
        if not set_id:
            unmatched.append({**row, "reason": "pokewallet_row_no_set_id", "pw_row": pw_row})
            continue

        code = pw_row.get("set_code")
        set_code = str(code).strip() if code else None

        index.append(
            {
                "set_id": set_id,
                "set_code": set_code or "",
                "file": f"{slug}.png",
                "pokesymbols_slug": slug,
                "pokesymbols_name": ps_name,
                "pokewallet_name": _pw_set_name(pw_row),
                "match_score": round(score, 1),
            }
        )

    # index.json consumed by app: only set_id, set_code, file
    lean = [
        {
            "set_id": e["set_id"],
            "set_code": e["set_code"] or None,
            "file": e["file"],
        }
        for e in index
    ]
    # JSON null for empty set_code
    for e in lean:
        if e["set_code"] == "":
            e["set_code"] = None

    args.index_out.parent.mkdir(parents=True, exist_ok=True)
    args.index_out.write_text(
        json.dumps(lean, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    args.unmatched_out.parent.mkdir(parents=True, exist_ok=True)
    args.unmatched_out.write_text(
        json.dumps(unmatched, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"Wrote {len(lean)} entries to {args.index_out}; "
        f"{len(unmatched)} unmatched -> {args.unmatched_out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
