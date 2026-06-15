"""Interactive corpus labeling: run the pipeline, accept/correct each row.

Usage: POKEWALLET_API_KEY=... .venv/bin/python scripts/label_corpus.py
Walks tests/corpus/*/ lacking truth.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pack.pipeline import scan_pack  # noqa: E402

CORPUS = Path("tests/corpus")


def main() -> None:
    for pack_dir in sorted(p for p in CORPUS.iterdir() if p.is_dir()):
        truth_path = pack_dir / "truth.json"
        if truth_path.exists():
            continue
        stair = (pack_dir / "staircase.jpg").read_bytes()
        code = (pack_dir / "code.jpg").read_bytes()
        meta_path = pack_dir / "capture_meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None

        print(f"\n=== {pack_dir.name} ===")
        resp = asyncio.run(scan_pack(stair, code, meta))
        rows = []
        for c in resp.cards:
            label = f"{c.card_number} {c.set_code or c.set_id} ({c.name})"
            ans = input(f"row {c.row_index}: {label}  [enter=correct, or 'number set_id']: ").strip()
            if ans:
                number, set_id = ans.split()
                rows.append({"row_index": c.row_index, "number": number, "set_id": set_id})
            else:
                rows.append({"row_index": c.row_index, "number": c.card_number,
                             "set_id": c.set_id})
        code_ans = input(f"code: {resp.code_card.code}  [enter=correct, or type code]: ").strip()
        truth = {
            "capture_meta": meta,
            "cards": rows,
            "code": code_ans or resp.code_card.code,
        }
        truth_path.write_text(json.dumps(truth, indent=2) + "\n")
        print(f"wrote {truth_path}")


if __name__ == "__main__":
    main()
