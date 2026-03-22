#!/usr/bin/env bash
# Re-scrape pokesymbols.com and rebuild app/data/set_symbols/index.json (needs POKEWALLET_API_KEY).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"
test -x "$PY" || PY="python3"

"$PY" scripts/scrape_pokesymbols.py \
  --json-out app/data/set_symbols/pokesymbols_sets.json \
  --download-dir app/data/set_symbols \
  --sleep 0.3

"$PY" scripts/build_set_symbol_index.py \
  --pokesymbols app/data/set_symbols/pokesymbols_sets.json \
  --png-dir app/data/set_symbols \
  --index-out app/data/set_symbols/index.json

echo "Done. Check scripts/output/pokesymbols_unmatched.json for rows that need manual fixes."
