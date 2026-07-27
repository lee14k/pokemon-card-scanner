"""Pack scanner API: staircase photo + code card → identified pulls with confidence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.cards import cached_lookup_card
from app.logging_config import configure_logging
from app.pack import scan_followup, vlm_client
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
from app.timing import stage
from app.training_data import router as training_data_router

log = logging.getLogger("pokemon_scanner.api")

_MAX_UPLOAD = 15 * 1024 * 1024
# Whole multipart body ceiling (two images + fields + overhead) — rejected early via
# Content-Length before buffering, so an oversized upload can't be read into memory.
_MAX_BODY = 2 * _MAX_UPLOAD + 1024 * 1024
_MAX_CAPTURE_META = 4096


async def _warm_catalog() -> None:
    """Warm the deferred catalog path a scan's constraint pass takes, and audit
    the promo local_id forms with the very same queries.

    THE WARM-UP: ``_apply_constraints``/``_needs_review`` reach for
    ``app.pack.constraints`` and ``app.cards`` behind function-local imports and
    then make their first DB call, so scan #1 of a process pays the import plus
    the SQLAlchemy engine + asyncpg connect inside its own latency (Task 3
    measured this as the ``pack.constraints`` stall). One real
    ``get_set_numerators`` call walks exactly that path — set_id_map join
    included — so the first scan finds it hot.

    THE AUDIT (data-trust guard — reads only, changes no behavior):
    ``catalog_local_id`` decides whether a promo numerator is compared as
    "SWSH123" or as "123" purely from the denominator table's
    ``local_id_prefixed`` flag. If that flag and the ingested catalog ever
    disagree, every promo card in that set quietly fails its ``_needs_review``
    catalog check — a data defect with no visible symptom, only a page of
    needlessly flagged cells. The flag is therefore checked against the catalog
    it claims to describe, once, at startup: an ERROR names the set and the
    disagreement. Both directions are checked (the brief asks for
    ``local_id_prefixed=True``; the False rows are the same one-line assertion
    against the same query, and a False row whose catalog IS prefixed breaks
    identically), and a set with no rows at all is only a WARNING — that is an
    un-ingested catalog, not a contradiction."""
    from app.cards import get_set_numerators
    from app.pack import constraints  # noqa: F401  (deferred in _apply_constraints)
    from app.pack.set_resolution import load_denominator_table

    for entry in load_denominator_table().sets:
        if not entry.promo_prefix:
            continue
        nums = await get_set_numerators(entry.set_id)
        if not nums:
            log.warning("warm.catalog_empty set=%s (promo set not ingested — its "
                        "numerators cannot be validated)", entry.set_id)
            continue
        prefix = entry.promo_prefix.upper()
        prefixed = sum(1 for n in nums if n.upper().startswith(prefix))
        if entry.local_id_prefixed and prefixed < len(nums):
            log.error("warm.local_id_prefix_mismatch set=%s prefix=%s expected=prefixed "
                      "prefixed=%s of %s — catalog_local_id() will compare the wrong "
                      "form and flag real cards", entry.set_id, prefix, prefixed, len(nums))
        elif not entry.local_id_prefixed and prefixed:
            log.error("warm.local_id_prefix_mismatch set=%s prefix=%s expected=bare "
                      "prefixed=%s of %s — catalog_local_id() will compare the wrong "
                      "form and flag real cards", entry.set_id, prefix, prefixed, len(nums))
        else:
            log.info("warm.catalog_ok set=%s prefixed=%s cards=%s",
                     entry.set_id, entry.local_id_prefixed, len(nums))


async def _warm_start() -> None:
    """Pay the first scan's one-off costs at boot instead of on a user's request.

    Every one of these is lazily built on first use, so without this the FIRST
    scan after a deploy pays all of it inside its own latency: the RapidOCR model
    load (~1s), the 8.4k-row name index, and the deferred catalog/DB path
    (Task 3's ``pack.constraints`` stall).

    Runs as a BACKGROUND task off the lifespan and NEVER inline: Railway's
    healthcheck has to see /health answer within 120s of deploy, and warming must
    never be what stops it. Each component is independently guarded — a warm-up
    is an optimization, so a failure (no DB reachable yet, no model files) logs
    and moves on, leaving the lazy path to run on first use exactly as before.

    Emits one ``timing.warm.<component> ms=`` line per component."""
    from app.pack import rapidocr_reader
    from app.pack.name_index import get_name_index

    try:
        with stage("warm", "rapidocr"):
            # In a thread: building the ONNX sessions is ~1s of blocking CPU, and
            # the whole point is that the app is already serving while it happens.
            ok = await asyncio.to_thread(rapidocr_reader.warmup)
        if not ok:
            log.warning("warm.rapidocr_unavailable — OCR will fall back to Tesseract")
    except Exception as e:
        log.warning("warm.rapidocr_failed err=%r", e)

    try:
        with stage("warm", "name_index"):
            await get_name_index()
    except Exception as e:
        log.warning("warm.name_index_failed err=%r", e)

    try:
        with stage("warm", "catalog"):
            await _warm_catalog()
    except Exception as e:
        log.warning("warm.catalog_failed err=%r", e)


def _warm_done(task: asyncio.Task) -> None:
    """Done-callback (also the strong reference that keeps the task from being
    garbage-collected mid-flight) — the same shape as scan_followup.spawn's."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("warm.crashed err=%r", exc)
    else:
        log.info("warm.done")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()
    log.info("startup log_level=%s", os.environ.get("LOG_LEVEL", "INFO"))
    load_symbol_index()
    load_denominator_table()
    ensure_photo_dir()
    # Eager init, in the BACKGROUND: the app starts serving (and /health answers)
    # immediately while the OCR engine, the name index and the catalog/DB path warm
    # up behind it. Anchored on app.state + a done-callback that logs a crash
    # instead of letting asyncio swallow it — the established pattern
    # (app/pack/scan_followup.py).
    warm = asyncio.create_task(_warm_start())
    app.state.warm_task = warm
    warm.add_done_callback(_warm_done)
    yield
    if not warm.done():
        # Shutdown mid-warm (a deploy that dies young, or a TestClient closing its
        # lifespan): cancel AND await, or the loop can still close under a task that
        # has only been asked to stop. cancel() just schedules the CancelledError;
        # the task is not finished until it has been stepped one more time, which is
        # what awaiting it here guarantees.
        warm.cancel()
        with suppress(asyncio.CancelledError):
            await warm


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
    # Warm the RunPod worker only AFTER the request has proven itself. This route
    # is UNAUTHENTICATED, and each ping is a billable serverless job that also
    # holds the endpoint above zero workers — so a junk/0-byte upload, or a bot
    # polling this path every 5 minutes, must not be able to spend GPU time.
    # Everything below is still ahead of the VLM call, so the load overlaps
    # segmentation + OCR. Debounced + fire-and-forget (app/pack/vlm_client.py).
    vlm_client.kick()

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
    vlm_client.kick()   # after validation, exactly as /scan/pack above — same
                        # unauthenticated route, same billable-ping reasoning

    return StreamingResponse(
        scan_pack_sse(stair_bytes, code_bytes, meta),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/scan/pack/{scan_id}")
async def scan_pack_followup(scan_id: str) -> dict:
    """Poll the background VLM pass for a pack scan → ``{cards, any_pending,
    done}``. The ``scan_id`` comes from the scan response and only exists while a
    follow-up is running; an unknown/expired id (or a binder id, which lives
    behind auth) is a 404.

    Anonymous, exactly like POST /scan/pack — the 128-bit scan_id IS the
    capability, and it never leaves the client that ran the scan."""
    entry = scan_followup.get(scan_id, "pack")
    if entry is None:
        raise HTTPException(404, "scan not found")
    return entry


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
