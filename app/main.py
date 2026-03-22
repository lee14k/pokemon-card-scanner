"""
Pokemon card pricing API: upload a photo, OCR the card, search PokéWallet, return prices.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.card_signals import CardSignals
from app.matching import build_search_queries, score_card_against_blob
from app.ocr_extract import extract_card_signals
from app.pokewallet import get_api_key, pokewallet_image_url, search_cards_for_lookup
from app.logging_config import configure_logging
from app.schemas import CardAnalyzeResponse, CardMatch, PriceLookupResponse
from app.set_symbol_index import load_symbol_index

log = logging.getLogger("pokemon_scanner.api")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()
    log.info("startup log_level=%s", os.environ.get("LOG_LEVEL", "INFO"))
    load_symbol_index()
    yield


app = FastAPI(
    title="Pokemon Card Price API",
    description="Upload a card photo; returns TCGPlayer & Cardmarket pricing via PokéWallet API.",
    version="0.1.0",
    lifespan=_lifespan,
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


def _signals_from_image_bytes(data: bytes, hint_raw: str) -> CardSignals:
    try:
        return extract_card_signals(data)
    except RuntimeError as e:
        if not hint_raw:
            raise
        log.warning("ocr.unavailable_using_hint hint=%r err=%s", hint_raw, e)
        return CardSignals.empty()


def _compose_ocr_sample(signals: CardSignals) -> str | None:
    ocr_fragments = signals.ocr_fragments
    ocr_sample: str | None = None
    if ocr_fragments:
        ocr_sample = " ".join(ocr_fragments[:3])[:500]
    extras: list[str] = []
    if signals.card_number:
        extras.append(f"#{signals.card_number}")
    if signals.set_id_from_symbol:
        sid = f"set_id={signals.set_id_from_symbol}"
        if signals.symbol_hash_distance is not None:
            sid += f"(dist={signals.symbol_hash_distance})"
        extras.append(sid)
    if signals.set_code_from_symbol:
        extras.append(f"set_code={signals.set_code_from_symbol}")
    if extras:
        ocr_sample = (ocr_sample + " | " if ocr_sample else "") + " | ".join(extras)
    return ocr_sample


def _merge_reviewed_card_fields(
    signals: CardSignals,
    *,
    use_reviewed_fields: bool,
    collection_number: str | None,
    set_id: str | None,
    set_code: str | None,
) -> CardSignals:
    if not use_reviewed_fields:
        return signals
    s = replace(signals)
    s.card_number = (collection_number or "").strip() or None
    s.set_id_from_symbol = (set_id or "").strip() or None
    s.set_code_from_symbol = (set_code or "").strip() or None
    s.symbol_hash_distance = None
    return s


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/cards/analyze-image", response_model=CardAnalyzeResponse)
async def analyze_image(
    image: UploadFile = File(..., description="JPEG/PNG/WebP of the card"),
    card_name_hint: str | None = Form(
        None,
        description="Optional name hint before OCR (same as price step)",
    ),
) -> CardAnalyzeResponse:
    """OCR + symbol index only — does not call PokéWallet (no API key required)."""
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (image/jpeg, image/png, etc.).",
        )

    data = await image.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 15MB).")

    hint_raw = (card_name_hint or "").strip()
    try:
        signals = _signals_from_image_bytes(data, hint_raw)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    ocr_sample = _compose_ocr_sample(signals)
    search_queries = build_search_queries(
        card_name_hint=card_name_hint,
        ocr_fragments=signals.ocr_fragments,
        card_number=signals.card_number,
        set_id_from_symbol=signals.set_id_from_symbol,
        set_code_from_symbol=signals.set_code_from_symbol,
        primary_name_guess=signals.primary_name_guess,
        max_queries=8,
    )

    return CardAnalyzeResponse(
        pokemon_name=signals.primary_name_guess,
        set_id=signals.set_id_from_symbol,
        set_code=signals.set_code_from_symbol,
        symbol_match_distance=signals.symbol_hash_distance,
        collection_number=signals.card_number,
        ocr_text_sample=ocr_sample,
        suggested_search_queries=search_queries,
        ocr_fragments=signals.ocr_fragments[:20],
    )


@app.post("/v1/cards/price-from-image", response_model=PriceLookupResponse)
async def price_from_image(
    image: UploadFile = File(..., description="JPEG/PNG/WebP of the card"),
    card_name_hint: str | None = Form(
        None,
        description="Pokémon name for search (use review-step value when applicable)",
    ),
    max_results: int = Form(8, ge=1, le=25),
    use_reviewed_fields: bool = Form(
        False,
        description="When true, collection_number / set_id / set_code replace OCR values",
    ),
    collection_number: str | None = Form(
        None,
        description="Collection number e.g. 15/198 (from review step)",
    ),
    set_id: str | None = Form(
        None,
        description="PokéWallet set_id from review step",
    ),
    set_code: str | None = Form(
        None,
        description="Set code from review step",
    ),
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

    hint_raw = (card_name_hint or "").strip()
    log.info(
        "lookup.start filename=%r content_type=%r bytes=%s name_hint=%r max_results=%s reviewed=%s",
        image.filename,
        image.content_type,
        len(data),
        hint_raw or None,
        max_results,
        use_reviewed_fields,
    )

    ocr_sample: str | None = None

    try:
        signals = _signals_from_image_bytes(data, hint_raw)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    signals = _merge_reviewed_card_fields(
        signals,
        use_reviewed_fields=use_reviewed_fields,
        collection_number=collection_number,
        set_id=set_id,
        set_code=set_code,
    )

    ocr_fragments = signals.ocr_fragments
    ocr_sample = _compose_ocr_sample(signals)

    search_queries = build_search_queries(
        card_name_hint=card_name_hint,
        ocr_fragments=ocr_fragments,
        card_number=signals.card_number,
        set_id_from_symbol=signals.set_id_from_symbol,
        set_code_from_symbol=signals.set_code_from_symbol,
        primary_name_guess=signals.primary_name_guess,
        max_queries=8,
    )

    if not search_queries:
        raise HTTPException(
            status_code=422,
            detail="No usable search text from image. Try a clearer photo or pass card_name_hint.",
        )

    log.info("lookup.search_queries %s", search_queries)

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

    log.info("lookup.pokewallet_pool unique_cards=%s", len(cards))

    hint_part = hint_raw
    ocr_blob = " ".join(
        p
        for p in (
            hint_part,
            signals.card_number,
            signals.set_id_from_symbol,
            signals.set_code_from_symbol,
            *ocr_fragments,
        )
        if p
    ).strip()
    if not ocr_blob:
        ocr_blob = " ".join(search_queries)

    log.info(
        "lookup.scoring_blob hint=%r card_number=%r set_id=%s ocr_fragment_count=%s blob=%r",
        hint_part or None,
        signals.card_number,
        signals.set_id_from_symbol,
        len(ocr_fragments),
        ocr_blob[:500] + ("..." if len(ocr_blob) > 500 else ""),
    )

    scored: list[tuple[float, dict[str, Any]]] = []
    for c in cards:
        scored.append(
            (
                score_card_against_blob(
                    c,
                    ocr_blob,
                    parsed_collection_number=signals.card_number,
                ),
                c,
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    strong = [(s, c) for s, c in scored if s >= _MIN_MATCH_SCORE]
    if strong:
        top = strong[:max_results]
    else:
        top = scored[: min(5, max_results)]

    preview_rows = []
    for s, c in scored[:15]:
        info = c.get("card_info") or {}
        preview_rows.append(
            f"{s:.1f} | {info.get('name')} | {info.get('set_name')} #{info.get('card_number')}"
        )
    log.info(
        "lookup.rankings min_score_gate=%s used_strong=%s top_preview:\n%s",
        _MIN_MATCH_SCORE,
        bool(strong),
        "\n".join(preview_rows) if preview_rows else "(none)",
    )

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
    for x in (
        *search_queries,
        *(
            [
                f"[parsed] collection_number={signals.card_number}",
            ]
            if signals.card_number
            else []
        ),
        *(
            [
                f"[symbol] set_id={signals.set_id_from_symbol}"
                + (
                    f" set_code={signals.set_code_from_symbol}"
                    if signals.set_code_from_symbol
                    else ""
                )
                + (
                    f" hash_dist={signals.symbol_hash_distance}"
                    if signals.symbol_hash_distance is not None
                    else ""
                ),
            ]
            if signals.set_id_from_symbol
            else []
        ),
        *ocr_fragments,
    ):
        k = x.strip().lower()
        if k and k not in _seen_q:
            _seen_q.add(k)
            _frag_display.append(x.strip())

    log.info("lookup.done matches_returned=%s", len(matches))

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
            "analyze_endpoint": "POST /v1/cards/analyze-image",
            "price_endpoint": "POST /v1/cards/price-from-image",
        }
