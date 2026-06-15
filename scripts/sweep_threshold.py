"""Sweep confidence threshold T over calibration records; recommend the lowest T
meeting the spec bars (precision >= 0.99 on high-confidence, flag-recall >= 0.90).

Usage: .venv/bin/python scripts/sweep_threshold.py   (after a calibration run)
"""

from __future__ import annotations

import json
from pathlib import Path

RECORDS = Path("scripts/output/calibration_records.json")


def main() -> None:
    records = json.loads(RECORDS.read_text())
    print(f"{'T':>5} {'highN':>6} {'precision':>10} {'flagRecall':>11}")
    best = None
    for t_pct in range(50, 96):
        t = t_pct / 100
        # At threshold t a card is flagged when confidence < t (reasons follow threshold).
        high = [r for r in records if r["confidence"] >= t]
        wrong = [r for r in records if not r["correct"]]
        precision = sum(r["correct"] for r in high) / max(1, len(high))
        recall = sum(1 for r in wrong if r["confidence"] < t) / max(1, len(wrong))
        marker = ""
        if precision >= 0.99 and recall >= 0.90 and best is None:
            best = t
            marker = "  <-- recommended"
        print(f"{t:5.2f} {len(high):6} {precision:10.4f} {recall:11.4f}{marker}")
    if best is None:
        print("\nNo T meets both bars — pipeline accuracy work needed (see spec Risks: "
              "vision-LLM fallback is the documented escape hatch).")
    else:
        print(f"\nSet PACK_CONFIDENCE_THRESHOLD={best} (Railway env + .env.example)")


if __name__ == "__main__":
    main()
