"""Pack scanner API: staircase photo + code card → identified pulls with confidence."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.cards import cached_lookup_card
from app.logging_config import configure_logging
from app.pack.matching import card_fields_from_match
from app.pack.pipeline import scan_pack
from app.pack.scan_stream import scan_pack_sse
from app.pack.set_resolution import load_denominator_table
from app.pokewallet import get_api_key
from app.schemas import CardLookupResponse, PackCard, PackScanResponse, SetInfo
from app.set_symbol_index import load_symbol_index
from app.db.users import (
    UserCreate,
    UserRead,
    UserUpdate,
    auth_backend,
    fastapi_users,
)
from app.admin import router as admin_router
from app.battles import router as battles_router
from app.collection import router as collection_router
from app.dex.routes import router as dex_router
from app.pack.live_api import router as live_api_router
from app.pulls import router as pulls_router
from app.stats_api import router as stats_router
from app.storage import ensure_photo_dir
from app.training_data import router as training_data_router

log = logging.getLogger("pokemon_scanner.api")

_MAX_UPLOAD = 15 * 1024 * 1024
# Whole multipart body ceiling (two images + fields + overhead) — rejected early via
# Content-Length before buffering, so an oversized upload can't be read into memory.
_MAX_BODY = 2 * _MAX_UPLOAD + 1024 * 1024
_MAX_CAPTURE_META = 4096


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()
    log.info("startup log_level=%s", os.environ.get("LOG_LEVEL", "INFO"))
    load_symbol_index()
    load_denominator_table()
    ensure_photo_dir()
    yield


app = FastAPI(
    title="Pokemon Pack Scanner API",
    description="Scan a staircase photo of a pack + its code card; returns identified cards.",
    version="0.2.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    # The SPA is same-origin (served by this app), so cookie auth needs no credentialed
    # CORS. allow_credentials stays off; a "*" origin with credentials would be unsafe.
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > _MAX_BODY:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
    return await call_next(request)


async def _read_image(upload: UploadFile, field: str) -> bytes:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(400, f"{field}: upload an image file")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, f"{field}: image too large (max 15MB)")
    return data


def _parse_capture_meta(capture_meta: str | None) -> dict | None:
    if not capture_meta:
        return None
    if len(capture_meta) > _MAX_CAPTURE_META:
        raise HTTPException(400, "capture_meta: payload too large")
    try:
        return json.loads(capture_meta)
    except (json.JSONDecodeError, RecursionError):
        raise HTTPException(400, "capture_meta: invalid JSON")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scan/pack", response_model=PackScanResponse)
async def scan_pack_endpoint(
    staircase: UploadFile = File(..., description="Staircase photo of the pack"),
    code_card: UploadFile = File(..., description="Close-up of the TCG Live code card"),
    capture_meta: str | None = Form(
        None, description='Guided-capture metadata JSON: {"guide_positions":[y...],"image_dims":[w,h],"declared_count":n}'
    ),
) -> PackScanResponse:
    stair_bytes = await _read_image(staircase, "staircase")
    code_bytes = await _read_image(code_card, "code_card")
    meta = _parse_capture_meta(capture_meta)

    try:
        return await scan_pack(stair_bytes, code_bytes, meta)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/scan/pack/stream")
async def scan_pack_stream_endpoint(
    staircase: UploadFile = File(..., description="Staircase photo of the pack"),
    code_card: UploadFile = File(..., description="Close-up of the TCG Live code card"),
    capture_meta: str | None = Form(
        None, description='Guided-capture metadata JSON: {"guide_positions":[y...],"image_dims":[w,h],"declared_count":n}'
    ),
) -> StreamingResponse:
    """SSE variant of /scan/pack: streams {stage} progress events while the
    scan runs, then a terminal `result` (or `error`) event. Purely additive —
    /scan/pack above is untouched and remains the non-streaming fallback."""
    stair_bytes = await _read_image(staircase, "staircase")
    code_bytes = await _read_image(code_card, "code_card")
    meta = _parse_capture_meta(capture_meta)

    return StreamingResponse(
        scan_pack_sse(stair_bytes, code_bytes, meta),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/sets", response_model=list[SetInfo])
async def sets() -> list[SetInfo]:
    table = load_denominator_table()
    return [
        SetInfo(set_id=s.set_id, set_code=s.set_code, set_name=s.set_name,
                denominators=list(s.denominators), era=s.era)
        for s in table.sets
    ]


@app.get("/cards/lookup", response_model=CardLookupResponse)
async def cards_lookup(set_id: str, number: str) -> CardLookupResponse:
    """Manual-fix flow: hand-entered (set, number) → card preview."""
    api_key = get_api_key()
    table = load_denominator_table()
    entry = next((s for s in table.sets if s.set_id == set_id), None)
    if entry is None:
        raise HTTPException(404, f"unknown set_id {set_id}")
    numerator = number.split("/")[0].strip()
    try:
        match = await cached_lookup_card(set_id, numerator, set_name=entry.set_name,
                                         api_key=api_key)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"PokéWallet returned {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise HTTPException(503, f"PokéWallet unreachable: {e}") from e
    if match is None:
        # Cache-only lookups work without a key; only a full miss needs the API.
        if not api_key:
            raise HTTPException(503, "POKEWALLET_API_KEY not configured")
        return CardLookupResponse(found=False, card=None)
    fields = card_fields_from_match(match)
    info = match.get("card_info") or {}
    return CardLookupResponse(
        found=True,
        card=PackCard(
            card_number=str(info.get("card_number") or number),
            set_id=entry.set_id, set_code=entry.set_code, set_name=entry.set_name,
            confidence=1.0, **fields,
        ),
    )


# --- Auth & user routes (FastAPI-Users) ---
app.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/cookie", tags=["auth"]
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"]
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
app.include_router(pulls_router)
app.include_router(live_api_router)
app.include_router(admin_router)
app.include_router(training_data_router)
app.include_router(stats_router)
app.include_router(dex_router)
app.include_router(battles_router)
app.include_router(collection_router)

# Production (Railway): Railpack builds frontend/dist; same origin as API.
# Mount last so /health, /docs, /scan/* stay on FastAPI routes.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="spa")
else:

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "pokemon-pack-scanner",
            "health": "/health",
            "api_docs": "/docs",
            "scan_endpoint": "POST /scan/pack",
        }
