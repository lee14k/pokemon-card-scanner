"""
Pokemon card pricing API: upload a photo, OCR the card, search Pokémon TCG API, return prices.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from rapidfuzz import fuzz

from app.ocr_extract import extract_text_candidates
from app.schemas import CardMatch, PriceLookupResponse
from app.tcg_api import search_cards_multi

app = FastAPI(
    title="Pokemon Card Price API",
    description="Upload a card photo; returns TCGPlayer / Cardmarket pricing via Pokémon TCG API v2.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _score_card(ocr_blob: str, card: dict[str, Any]) -> float:
    name = (card.get("name") or "").lower()
    set_name = (card.get("set", {}) or {}).get("name", "") or ""
    set_name = set_name.lower()
    blob = ocr_blob.lower()
    a = fuzz.token_set_ratio(name, blob)
    b = fuzz.partial_ratio(name, blob)
    c = fuzz.token_set_ratio(set_name, blob) if set_name else 0
    return max(a, b, c * 0.85)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/cards/price-from-image", response_model=PriceLookupResponse)
async def price_from_image(
    image: UploadFile = File(..., description="JPEG/PNG/WebP of the card"),
    card_name_hint: str | None = Form(
        None,
        description="Optional name hint if OCR is weak (e.g. Charizard)",
    ),
    max_results: int = Form(8, ge=1, le=25),
) -> PriceLookupResponse:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (image/jpeg, image/png, etc.).",
        )

    data = await image.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 15MB).")

    fragments: list[str] = []
    ocr_sample: str | None = None

    if card_name_hint and card_name_hint.strip():
        fragments.append(card_name_hint.strip())
    else:
        try:
            fragments = extract_text_candidates(data)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        if fragments:
            ocr_sample = " ".join(fragments[:3])[:500]

    if not fragments:
        raise HTTPException(
            status_code=422,
            detail="No readable text from image. Try a clearer photo or pass card_name_hint.",
        )

    cards = await search_cards_multi(fragments, per_fragment_limit=10)
    if not cards:
        raise HTTPException(
            status_code=404,
            detail="No cards found for OCR fragments. Try card_name_hint or a sharper image.",
        )

    ocr_blob = " ".join(fragments)
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in cards:
        scored.append((_score_card(ocr_blob, c), c))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_results]

    matches: list[CardMatch] = []
    for score, c in top:
        set_obj = c.get("set") or {}
        matches.append(
            CardMatch(
                id=c["id"],
                name=c.get("name") or "",
                set_name=set_obj.get("name"),
                number=c.get("number"),
                rarity=c.get("rarity"),
                images=c.get("images"),
                tcgplayer=c.get("tcgplayer"),
                cardmarket=c.get("cardmarket"),
                match_score=round(float(score), 2),
            )
        )

    return PriceLookupResponse(
        ocr_text_sample=ocr_sample,
        query_fragments=fragments[:15],
        matches=matches,
    )
