"""Live scan session API. All endpoints owner-scoped via CurrentTrainer.

One card at a time: the client POSTs a frame per hold-up, this identifies it
against Task 4's ladder (+ Task 5's session store for dedup/VLM drain/TTL) and
returns the running state. See app/pack/live_session.py for the session
lifecycle and app/pack/live_identify.py for the identification ladder.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db.models import Trainer
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.pack import live_session as store
from app.pack import vlm_client
from app.pack.confidence import pack_confidence
from app.pack.live_identify import identify_frame
from app.pack.pipeline import _decode
from app.prices import latest_price_map
from app.schemas import CodeCardResult, PackCard, PackScanResponse
from app.timing import new_scan_id, stage

log = logging.getLogger("pokemon_scanner.pack.live_api")
router = APIRouter(prefix="/scan/live", tags=["live-scan"])

# Decode admission gate. A live frame is a 12MP phone photo: the decoded ndarray
# alone is ~36MB, and a frame may carry a second (the number strip). The per-session
# frame_lock bounds ONE session to one frame in flight, but nothing bounded the
# number of sessions decoding at once — N scanners holding up cards at the same
# moment is exactly the shape of this feature, and each is a fresh 36MB+ allocation
# in a small container. Four in flight already saturates any cloud vCPU we run on.
# Separate from OCR_GATE on purpose: a frame waiting to decode must not be holding
# an OCR slot, which is the same mistake this task removed from the identify path.
_DECODE_GATE = asyncio.Semaphore(int(os.environ.get("LIVE_DECODE_CONCURRENCY", "4")))


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
    # Warm the RunPod worker the moment a session opens: the first uncertain card
    # is seconds away and its VLM call would otherwise pay the ~16GB cold load.
    # Debounced + fire-and-forget inside kick(), so this adds nothing measurable.
    vlm_client.kick()
    return {"session_id": await store.start_session(str(trainer.id))}


@router.post("/{sid}/frame")
async def frame(
    sid: str,
    trainer: CurrentTrainer,
    card: UploadFile = File(...),
    strip: UploadFile | None = File(None),
) -> dict:
    scan_id = new_scan_id()
    with stage("live", "total", scan_id):
        s = await _sess(sid, trainer)
        if s.frame_lock.locked():
            raise HTTPException(409, "busy")
        async with s.frame_lock:
            card_bytes = await card.read()
            strip_bytes = await strip.read() if strip is not None else None
            with stage("live", "decode", scan_id):
                # Threaded: a phone frame is a 12MP HEIC/JPEG, 100s of ms of
                # blocking CPU that would otherwise stall every concurrent
                # scanner's requests (the whole point of the per-session lock is
                # to serialize ONE session, not the whole process). Bounded by
                # _DECODE_GATE across sessions; this stage's timing therefore
                # includes any decode queueing, like the OCR-gated stages.
                async with _DECODE_GATE:
                    img = await asyncio.to_thread(_decode, card_bytes)
                    if img is None:
                        raise HTTPException(422, "unreadable image")
                    strip_img = (await asyncio.to_thread(_decode, strip_bytes)
                                 if strip_bytes is not None else None)

            # The OCR gate is acquired INSIDE identify_frame now, around its OCR
            # and nothing else (it still logs `timing.live.gate_wait`, plus a
            # `timing.live.ocr` for the guarded span). Holding it out here meant a
            # frame owned one of the three slots through the whole identify ladder
            # — DB lookups, name-index matching, PokéWallet I/O — none of which is
            # the CPU the gate rations. `live.identify` still measures the whole
            # call, so the two are directly comparable in the logs.
            with stage("live", "identify", scan_id):
                res = await identify_frame(img, strip_img, s.prior(), scan_id)

            if res.card is not None:
                with stage("live", "prices", scan_id):
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
