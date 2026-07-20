"""Integration tests: synthetic fixtures → POST /scan/pack → assertions."""

from __future__ import annotations

import json
from pathlib import Path

E2E = Path(__file__).parent / "fixtures" / "e2e"


def _truth() -> dict:
    return json.loads((E2E / "truth.json").read_text())


def _post_scan(client, *, with_meta: bool, declared_count: int | None = None):
    truth = _truth()
    meta = dict(truth["capture_meta"])
    if declared_count is not None:
        meta["declared_count"] = declared_count
    data = {}
    if with_meta:
        data["capture_meta"] = json.dumps(meta)
    with (E2E / "staircase.jpg").open("rb") as stair, (E2E / "code.jpg").open("rb") as code:
        return client.post(
            "/scan/pack",
            files={
                "staircase": ("staircase.jpg", stair, "image/jpeg"),
                "code_card": ("code.jpg", code, "image/jpeg"),
            },
            data=data,
        )


def test_scan_pack_guided_happy_path(client):
    truth = _truth()
    r = _post_scan(client, with_meta=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["cards"]) == len(truth["cards"])
    assert body["segmentation_warning"] is None
    for card, expected in zip(body["cards"], truth["cards"]):
        assert card["row_index"] == expected["row_index"]
        assert card["card_number"] == expected["number"]
        assert card["set_id"] == expected["set_id"]
        assert card["name"] is not None          # stub lookup matched
        assert card["rarity"] == "Common"        # rarity derived from DB, not OCR
        assert card["low_confidence_reason"] is None
        assert card["confidence"] >= 0.8
    assert body["code_card"]["code"] == truth["code"]
    assert body["code_card"]["format_ok"] is True
    assert body["pack_confidence"] >= 0.8


def test_scan_pack_ungrided_detection_first(client):
    """Ungrided (no capture_meta) now uses whole-photo PP-OCR detection: it finds
    and reads each card number directly, so every real card is identified with no
    phantom rows (a card is created per detected number, not per geometric edge)."""
    truth = _truth()
    r = _post_scan(client, with_meta=False)
    assert r.status_code == 200, r.text
    body = r.json()
    identified = {(c["set_id"], c["card_number"]) for c in body["cards"]}
    expected = {(t["set_id"], t["number"]) for t in truth["cards"]}
    assert expected <= identified, f"missing real cards: {expected - identified}"
    # Detection-first makes one card per detected number — no phantom rows.
    assert len(body["cards"]) == len(truth["cards"])


def test_scan_pack_count_mismatch_warns(client):
    r = _post_scan(client, with_meta=True, declared_count=5)
    assert r.status_code == 200
    body = r.json()
    assert body["segmentation_warning"] is not None
    assert "5" in body["segmentation_warning"]


def test_scan_pack_rejects_non_image(client):
    r = client.post(
        "/scan/pack",
        files={
            "staircase": ("x.txt", b"not an image", "text/plain"),
            "code_card": ("y.txt", b"not an image", "text/plain"),
        },
    )
    assert r.status_code == 400


def test_blurry_row_is_flagged_not_dropped(client, tmp_path):
    """Spec: rows are never silently dropped — unreadable strips come back flagged."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tests.fixtures.synth import make_code_card, make_staircase

    meta = make_staircase(
        [("012/202", ""), ("045/185", ""), ("101/198", "SVI")],
        tmp_path / "stair.jpg",
        blur_rows={1},
    )
    make_code_card("TEST1-CODE2-CARD3", tmp_path / "code.jpg")
    with (tmp_path / "stair.jpg").open("rb") as stair, (tmp_path / "code.jpg").open("rb") as code:
        r = client.post(
            "/scan/pack",
            files={
                "staircase": ("stair.jpg", stair, "image/jpeg"),
                "code_card": ("code.jpg", code, "image/jpeg"),
            },
            data={"capture_meta": json.dumps(meta)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["cards"]) == 3                      # blurred row still present
    flagged = body["cards"][1]
    assert flagged["low_confidence_reason"] is not None
    assert flagged["confidence"] < 0.8


def test_cards_lookup_manual_fix(client):
    truth = _truth()
    expected = truth["cards"][2]
    r = client.get("/cards/lookup",
                   params={"set_id": expected["set_id"], "number": expected["number"]})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["card"]["name"] == "Test Mon C"
    assert body["card"]["image_url"]


def test_cards_lookup_unknown_set_404(client):
    r = client.get("/cards/lookup", params={"set_id": "000000", "number": "1/1"})
    assert r.status_code == 404
