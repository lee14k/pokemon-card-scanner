"""Live scan session API. All endpoints owner-scoped via CurrentTrainer.

One card at a time: the client POSTs a frame per hold-up, this identifies it
against Task 4's ladder (+ Task 5's session store for dedup/VLM drain/TTL) and
returns the running state. See app/pack/live_session.py for the session
lifecycle and app/pack/live_identify.py for the identification ladder.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db.models import Trainer
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.pack import live_session as store
from app.pack.confidence import pack_confidence
from app.pack.live_identify import identify_frame
from app.pack.pipeline import OCR_GATE, _decode
from app.prices import latest_price_map
from app.schemas import CodeCardResult, PackCard, PackScanResponse

log = logging.getLogger("pokemon_scanner.pack.live_api")
router = APIRouter(prefix="/scan/live", tags=["live-scan"])


class DuplicateBody(BaseModel):
    add: bool = True


async def _sess(sid: str, trainer: Trainer) -> store.LiveSession:
    """Ownership-enforced session fetch shared by every route below. 404s for an
    unknown session id OR one owned by a different trainer (store.get_session
    already collapses both cases so we never leak which one it was)."""
    s = await store.get_session(sid, str(trainer.id))
    if s is None:
        raise HTTPException(404, "session not found")
    return s


async def _attach_price(card: PackCard) -> None:
    """Best-effort price enrichment. Never lets a price-lookup hiccup fail the
    frame — an un-priced card is still a usable identification."""
    if not card.match_id:
        return
    try:
        async with async_session_maker() as session:
            price_map, _asof = await latest_price_map(session)
        lo_hi = price_map.get(card.match_id)
        if lo_hi:
            card.price_usd_low, card.price_usd_high = lo_hi
    except Exception as e:
        log.warning("live.price_failed match_id=%s err=%r", card.match_id, e)


@router.post("/start")
async def start(trainer: CurrentTrainer) -> dict:
    return {"session_id": await store.start_session(str(trainer.id))}


@router.post("/{sid}/frame")
async def frame(
    sid: str,
    trainer: CurrentTrainer,
    card: UploadFile = File(...),
    strip: UploadFile | None = File(None),
) -> dict:
    s = await _sess(sid, trainer)
    if s.frame_lock.locked():
        raise HTTPException(409, "busy")
    async with s.frame_lock:
        card_bytes = await card.read()
        img = _decode(card_bytes)
        if img is None:
            raise HTTPException(422, "unreadable image")
        strip_img = None
        if strip is not None:
            strip_img = _decode(await strip.read())

        async with OCR_GATE:
            res = await identify_frame(img, strip_img, s.prior())

        if res.card is not None:
            await _attach_price(res.card)

        event = s.add_frame_result(res, card_bytes)
        return {
            "event": event.event,
            "card": event.card,
            "pending_vlm": event.pending_vlm,
            "code_card": s.code,
            "cards_count": len(s.cards),
        }


@router.get("/{sid}")
async def state(sid: str, trainer: CurrentTrainer) -> dict:
    s = await _sess(sid, trainer)
    return {
        "cards": [{**lc.card.model_dump(), "state": lc.state} for lc in s.cards],
        "code_card": s.code,
        "any_pending": any(lc.state == "pending_vlm" for lc in s.cards),
    }


@router.get("/{sid}/card/{row}/image")
async def card_image(sid: str, row: int, trainer: CurrentTrainer) -> FileResponse:
    s = await _sess(sid, trainer)
    p = s.frame_path(row)
    if not p.exists():
        raise HTTPException(404, "no frame")
    return FileResponse(p, media_type="image/jpeg")


@router.post("/{sid}/card/{row}/duplicate")
async def duplicate(sid: str, row: int, trainer: CurrentTrainer, body: DuplicateBody) -> dict:
    s = await _sess(sid, trainer)
    s.resolve_duplicate(row, body.add)
    return {"ok": True}


@router.post("/{sid}/card/{row}/replace")
async def replace(sid: str, row: int, trainer: CurrentTrainer) -> dict:
    s = await _sess(sid, trainer)
    s.mark_replaceable(row)
    return {"ok": True}


@router.post("/{sid}/finish", response_model=PackScanResponse)
async def finish(sid: str, trainer: CurrentTrainer) -> PackScanResponse:
    s = await _sess(sid, trainer)
    cards = s.finish()
    return PackScanResponse(
        cards=cards,
        code_card=s.code or CodeCardResult(code=None, confidence=0.0, format_ok=False),
        pack_confidence=pack_confidence([c.confidence for c in cards]),
        segmentation_warning=None,
    )
