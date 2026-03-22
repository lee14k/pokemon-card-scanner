"""
Pokemon card pricing API: upload a photo, OCR the card, search PokéWallet, return prices.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.matching import build_search_queries, score_card_against_blob
from app.ocr_extract import extract_text_candidates
from app.pokewallet import get_api_key, pokewallet_image_url, search_cards_for_lookup
from app.schemas import CardMatch, PriceLookupResponse

app = FastAPI(
    title="Pokemon Card Price API",
    description="Upload a card photo; returns TCGPlayer & Cardmarket pricing via PokéWallet API.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Drop matches below this unless nothing passes (then return top few with low scores).
_MIN_MATCH_SCORE = 52


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
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Set environment variable POKEWALLET_API_KEY to your PokéWallet API key.",
        )

    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (image/jpeg, image/png, etc.).",
        )

    data = await image.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 15MB).")

    ocr_fragments: list[str] = []
    ocr_sample: str | None = None

    try:
        ocr_fragments = extract_text_candidates(data)
    except RuntimeError as e:
        if not (card_name_hint and card_name_hint.strip()):
            raise HTTPException(status_code=503, detail=str(e)) from e
        ocr_fragments = []

    if ocr_fragments:
        ocr_sample = " ".join(ocr_fragments[:3])[:500]

    # Hint-only search keeps PokéWallet results on-target; OCR still refines scoring.
    search_queries = build_search_queries(
        card_name_hint=card_name_hint,
        ocr_fragments=ocr_fragments,
        max_queries=3,
    )

    if not search_queries:
        raise HTTPException(
            status_code=422,
            detail="No usable search text from image. Try a clearer photo or pass card_name_hint.",
        )

    try:
        cards = await search_cards_for_lookup(
            search_queries,
            limit_per_query=40,
            api_key=api_key,
        )
    except httpx.HTTPStatusError as e:
        detail: str | dict[str, Any] = str(e.response.status_code)
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text or detail
        raise HTTPException(
            status_code=502,
            detail={"message": "PokéWallet request failed", "upstream": detail},
        ) from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"PokéWallet API unreachable: {e}",
        ) from e

    if not cards:
        raise HTTPException(
            status_code=404,
            detail="No cards found for search. Try card_name_hint or a sharper image.",
        )

    hint_part = (card_name_hint or "").strip()
    ocr_blob = " ".join([hint_part, *ocr_fragments]).strip() if (hint_part or ocr_fragments) else ""
    if not ocr_blob:
        ocr_blob = " ".join(search_queries)

    scored: list[tuple[float, dict[str, Any]]] = []
    for c in cards:
        scored.append((score_card_against_blob(c, ocr_blob), c))

    scored.sort(key=lambda x: x[0], reverse=True)
    strong = [(s, c) for s, c in scored if s >= _MIN_MATCH_SCORE]
    if strong:
        top = strong[:max_results]
    else:
        top = scored[: min(5, max_results)]

    matches: list[CardMatch] = []
    for score, c in top:
        info = c.get("card_info") or {}
        cid = c["id"]
        matches.append(
            CardMatch(
                id=cid,
                name=info.get("name") or info.get("clean_name") or "",
                set_name=info.get("set_name"),
                number=info.get("card_number"),
                rarity=info.get("rarity"),
                images={"high": pokewallet_image_url(cid)},
                tcgplayer=c.get("tcgplayer"),
                cardmarket=c.get("cardmarket"),
                match_score=round(float(score), 2),
            )
        )

    _seen_q = set()
    _frag_display: list[str] = []
    for x in (*search_queries, *ocr_fragments):
        k = x.strip().lower()
        if k and k not in _seen_q:
            _seen_q.add(k)
            _frag_display.append(x.strip())

    return PriceLookupResponse(
        ocr_text_sample=ocr_sample,
        query_fragments=_frag_display[:20],
        matches=matches,
    )


# Production (e.g. Railway): Railpack builds frontend/dist; same origin as API.
# Mount last so /health, /docs, /openapi.json, /v1/* stay on FastAPI routes.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _STATIC_DIR.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(_STATIC_DIR), html=True),
        name="spa",
    )
else:

    @app.get("/")
    async def root() -> dict[str, str]:
        """JSON landing when the Vite build is not present (local API-only dev)."""
        return {
            "service": "pokemon-card-scanner",
            "health": "/health",
            "api_docs": "/docs",
            "price_endpoint": "POST /v1/cards/price-from-image",
        }
