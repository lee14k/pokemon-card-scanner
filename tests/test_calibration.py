"""Corpus calibration: spec acceptance gate.

Run: POKEWALLET_API_KEY=... .venv/bin/python -m pytest tests/test_calibration.py -m calibration -v -s
Skips when corpus or API key is missing. Writes per-card records for
scripts/sweep_threshold.py and a metrics report to scripts/output/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.pack.config import settings

CORPUS = Path(__file__).parent / "corpus"
OUT = Path("scripts/output")

pytestmark = pytest.mark.calibration

_packs = sorted(
    p for p in (CORPUS.iterdir() if CORPUS.is_dir() else [])
    if p.is_dir() and (p / "truth.json").exists()
)


@pytest.fixture(scope="module")
def real_client():
    if not os.environ.get("POKEWALLET_API_KEY"):
        pytest.skip("POKEWALLET_API_KEY not set")
    os.environ.pop("POKEWALLET_BASE_URL", None)  # real API, not the stub
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.skipif(not _packs, reason="no labeled corpus packs in tests/corpus/")
def test_calibration_acceptance_gate(real_client):
    T = settings().confidence_threshold
    records = []  # one per card row: confidence, flagged, correct
    code_total = code_correct = 0

    for pack in _packs:
        truth = json.loads((pack / "truth.json").read_text())
        data = {}
        if truth.get("capture_meta"):
            data["capture_meta"] = json.dumps(truth["capture_meta"])
        with (pack / "staircase.jpg").open("rb") as stair, (pack / "code.jpg").open("rb") as code:
            r = real_client.post(
                "/scan/pack",
                files={
                    "staircase": ("staircase.jpg", stair, "image/jpeg"),
                    "code_card": ("code.jpg", code, "image/jpeg"),
                },
                data=data,
            )
        assert r.status_code == 200, f"{pack.name}: {r.text}"
        body = r.json()

        by_row = {c["row_index"]: c for c in truth["cards"]}
        for card in body["cards"]:
            expected = by_row.get(card["row_index"])
            if expected is None:
                continue
            correct = (
                card["card_number"] == expected["number"]
                and card["set_id"] == expected["set_id"]
            )
            records.append(
                {
                    "pack": pack.name,
                    "row": card["row_index"],
                    "confidence": card["confidence"],
                    "flagged": card["low_confidence_reason"] is not None,
                    "correct": correct,
                }
            )
        code_total += 1
        code_correct += int(body["code_card"]["code"] == truth["code"])

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "calibration_records.json").write_text(json.dumps(records, indent=2) + "\n")

    high = [r for r in records if not r["flagged"]]
    wrong = [r for r in records if not r["correct"]]
    precision_high = sum(r["correct"] for r in high) / max(1, len(high))
    flag_recall = sum(r["flagged"] for r in wrong) / max(1, len(wrong))
    code_rate = code_correct / max(1, code_total)

    report = {
        "threshold_T": T,
        "packs": len(_packs),
        "cards": len(records),
        "high_confidence_cards": len(high),
        "precision_high_confidence": round(precision_high, 4),
        "mistakes_total": len(wrong),
        "mistake_flag_recall": round(flag_recall, 4),
        "code_card_success": round(code_rate, 4),
    }
    (OUT / "calibration_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("\nCALIBRATION REPORT:", json.dumps(report, indent=2))

    # Spec acceptance gate (success criteria table)
    assert precision_high >= 0.99, f"high-confidence precision {precision_high:.3f} < 0.99"
    assert flag_recall >= 0.90, f"mistake flag recall {flag_recall:.3f} < 0.90"
    assert code_rate >= 0.99, f"code card success {code_rate:.3f} < 0.99"
