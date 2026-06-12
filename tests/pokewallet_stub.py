"""Minimal PokéWallet stand-in for integration/E2E tests (GET /search, /images)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

FIXTURE = Path(__file__).parent / "fixtures" / "pokewallet_cards.json"

app = FastAPI()
_cards: list[dict] = json.loads(FIXTURE.read_text()) if FIXTURE.is_file() else []

# 1x1 white PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8ffff3f0005fe02fea7356c2a0000000049454e44ae426082"
)


@app.get("/search")
def search(q: str, limit: int = 20, page: int = 1) -> dict:
    terms = [t.lstrip("0") or "0" for t in q.lower().split()]
    hits = []
    for c in _cards:
        info = c.get("card_info") or {}
        num = str(info.get("card_number") or "").split("/")[0].strip().lstrip("0") or "0"
        blob = {
            str(c.get("set_id", "")).lower(),
            num.lower(),
            *str(info.get("name", "")).lower().split(),
            *str(info.get("set_name", "")).lower().split(),
        }
        if all(t in blob for t in terms):
            hits.append(c)
    return {"results": hits[:limit], "pagination": {}, "metadata": {}}


@app.get("/images/{card_id}")
def image(card_id: str, size: str = "high") -> Response:
    return Response(content=_PNG, media_type="image/png")
