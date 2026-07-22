"""Trainer pull persistence: save (with photos + server-verified code), list, detail, photo serving."""

from __future__ import annotations

import datetime
import json
import logging
import re
import uuid
from collections import Counter

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import DeriveStatus, Pull, PullCard, PullCardDerived
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.dex.species import species_of
from app.prices import latest_price_map
from app.pack.ocr import read_code_card
from app.storage import move_session_frames, open_photo, save_code_photo, save_pull_photos

log = logging.getLogger("pokemon_scanner.pulls")

router = APIRouter(prefix="/pulls", tags=["pulls"])

_MAX_UPLOAD = 15 * 1024 * 1024


def _normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    norm = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return norm or None


# ── Response models ──────────────────────────────────────────────────────────
class CardOut(BaseModel):
    row_index: int
    card_number: str | None
    set_id: str | None
    set_code: str | None
    set_name: str | None
    name: str | None
    rarity: str | None
    low_confidence_reason: str | None
    match_id: str | None
    image_url: str | None
    confidence: float
    price_usd_low: float | None = None
    price_usd_high: float | None = None


class EncounterOut(BaseModel):
    species: str
    count: int      # total cards of this species the trainer has ever saved (incl. this pull)
    new: bool       # True when this pull contains their first-ever card(s) of the species


class PullOut(BaseModel):
    id: uuid.UUID
    created_at: str
    capture_path: str
    pack_confidence: float
    segmentation_warning: str | None
    code: str | None
    code_format_ok: bool
    verified: bool
    cards: list[CardOut]
    encounters: list[EncounterOut] = []
    estimated_value: float | None = None
    priced_as_of: str | None = None


def _pull_to_out(pull: Pull) -> PullOut:
    return PullOut(
        id=pull.id,
        created_at=pull.created_at.isoformat(),
        capture_path=pull.capture_path,
        pack_confidence=pull.pack_confidence,
        segmentation_warning=pull.segmentation_warning,
        code=pull.code,
        code_format_ok=pull.code_format_ok,
        verified=pull.verified,
        cards=[
            CardOut(
                row_index=c.row_index, card_number=c.card_number, set_id=c.set_id,
                set_code=c.set_code, set_name=c.set_name, name=c.name, rarity=c.rarity,
                low_confidence_reason=c.low_confidence_reason, match_id=c.match_id,
                image_url=c.image_url, confidence=c.confidence,
            )
            for c in pull.cards
        ],
    )


def _enrich_prices(out: PullOut, prices: dict[str, tuple[float | None, float | None]],
                   priced_as_of: str | None) -> PullOut:
    if not prices:
        return out
    total = 0.0
    any_priced = False
    for card in out.cards:
        if card.match_id and card.match_id in prices:
            lo, hi = prices[card.match_id]
            card.price_usd_low, card.price_usd_high = lo, hi
            if lo is not None and hi is not None:
                total += (lo + hi) / 2
                any_priced = True
    if any_priced:
        out.estimated_value = round(total, 2)
        out.priced_as_of = priced_as_of
    return out


async def _read_image(upload: UploadFile, field: str) -> bytes:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(400, f"{field}: upload an image file")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, f"{field}: image too large (max 15MB)")
    return data


@router.post("", response_model=PullOut, status_code=201)
async def save_pull(
    trainer: CurrentTrainer,
    staircase: UploadFile = File(...),
    code_card: UploadFile = File(...),
    cards: str = Form(..., description="JSON array of confirmed cards"),
    capture_path: str = Form("upload"),
    pack_confidence: float = Form(0.0),
    segmentation_warning: str | None = Form(None),
    capture_meta: str | None = Form(None, description="Guided-capture metadata JSON"),
    live_session_id: str | None = Form(None, description="Live-scan session id (frames to adopt)"),
) -> PullOut:
    stair_bytes = await _read_image(staircase, "staircase")
    code_bytes = await _read_image(code_card, "code_card")
    try:
        card_list = json.loads(cards)
        assert isinstance(card_list, list)
        # Coerce/validate numeric fields up front so a malformed entry is a clean 400,
        # not a 500 deep in the insert loop (after photos are already on disk).
        for i, c in enumerate(card_list):
            assert isinstance(c, dict)
            c["row_index"] = int(c.get("row_index", i))
            c["confidence"] = float(c.get("confidence", 0.0))
    except (json.JSONDecodeError, AssertionError, ValueError, TypeError):
        raise HTTPException(400, "cards: must be a JSON array of card objects")

    meta_obj: dict | None = None
    if capture_meta:
        try:
            meta_obj = json.loads(capture_meta)
            assert isinstance(meta_obj, dict)
        except (json.JSONDecodeError, AssertionError):
            raise HTTPException(400, "capture_meta: must be a JSON object")

    pull_id = uuid.uuid4()
    staircase_path, code_path = save_pull_photos(trainer.id, pull_id, stair_bytes, code_bytes)

    # Server re-OCRs the code (authoritative — clients cannot spoof the verified flag).
    code_img = cv2.imdecode(np.frombuffer(code_bytes, np.uint8), cv2.IMREAD_COLOR)
    cr = read_code_card(code_img) if code_img is not None else None
    code = cr.code if cr else None
    code_norm = _normalize_code(code)
    code_ok = bool(cr and cr.format_ok)
    code_conf = float(cr.confidence) if cr else 0.0

    want_verified = bool(code_norm) and code_ok

    async with async_session_maker() as session:
        saved = await _insert_pull(
            session, trainer_id=trainer.id, pull_id=pull_id, capture_path=capture_path,
            pack_confidence=pack_confidence, segmentation_warning=segmentation_warning,
            code=code, code_norm=code_norm, code_conf=code_conf, code_ok=code_ok,
            want_verified=want_verified, staircase_path=staircase_path, code_path=code_path,
            card_list=card_list, capture_meta=meta_obj,
        )

        # Live pulls: their staircase is a SYNTHETIC contact-sheet, so rederive can't
        # re-OCR them. Instead the client-confirmed cards ARE the server's own /finish
        # output (server-OCR'd, authoritative) — write them straight to the derived
        # table and mark the pull done so stats aggregation reads them and rederive
        # skips it. Runs once here on the committed pull (kept out of _insert_pull's
        # verified-retry loop) and regardless of verified state. Non-live pulls are
        # completely unchanged by this block.
        if capture_path == "live":
            for i, c in enumerate(card_list):
                session.add(PullCardDerived(
                    pull_id=saved.id, row_index=int(c.get("row_index", i)),
                    card_number=c.get("card_number"), set_id=c.get("set_id"),
                    set_code=c.get("set_code"), set_name=c.get("set_name"),
                    name=c.get("name"), rarity=c.get("rarity"),
                    match_id=c.get("match_id"), confidence=float(c.get("confidence", 0.0)),
                ))
            saved.derive_status = DeriveStatus.done
            saved.derived_at = datetime.datetime.now(datetime.timezone.utc)
            await session.commit()
            # Adopt the live session's per-card frames into the pull dir. Purely a
            # bonus (training data / review thumbnails) — a swept session or any
            # filesystem hiccup must never fail an otherwise-saved pull.
            if live_session_id:
                try:
                    moved = move_session_frames(live_session_id, trainer.id, saved.id)
                    log.info("live_save.frames_moved pull=%s session=%s count=%s",
                             saved.id, live_session_id, moved)
                except Exception as e:  # pragma: no cover - defensive
                    log.warning("live_save.frame_move_failed pull=%s session=%s err=%r",
                                saved.id, live_session_id, e)

        out = _pull_to_out(saved)
        try:
            out.encounters = await _compute_encounters(session, trainer.id, saved)
        except Exception:  # the dex moment must never break persistence
            out.encounters = []
        prices, as_of = await latest_price_map(session)
        return _enrich_prices(out, prices, as_of)


async def _insert_pull(session: AsyncSession, *, trainer_id, pull_id, capture_path,
                       pack_confidence, segmentation_warning, code, code_norm, code_conf,
                       code_ok, want_verified, staircase_path, code_path, card_list,
                       capture_meta) -> Pull:
    """Insert pull (+cards). Tries verified=want_verified; on the partial-unique-index
    conflict (code already verified by someone), retries verified=False."""
    for verified in ([True, False] if want_verified else [False]):
        try:
            pull = Pull(
                id=pull_id, trainer_id=trainer_id, capture_path=capture_path,
                pack_confidence=pack_confidence, segmentation_warning=segmentation_warning,
                code=code, code_normalized=code_norm, code_confidence=code_conf,
                code_format_ok=code_ok, verified=verified,
                staircase_photo_path=staircase_path, code_photo_path=code_path,
                capture_meta=capture_meta,
            )
            session.add(pull)
            await session.flush()  # surfaces the unique-index violation here
            for i, c in enumerate(card_list):
                session.add(PullCard(
                    pull_id=pull_id, row_index=int(c.get("row_index", i)),
                    card_number=c.get("card_number"), set_id=c.get("set_id"),
                    set_code=c.get("set_code"), set_name=c.get("set_name"),
                    name=c.get("name"), rarity=c.get("rarity"),
                    low_confidence_reason=c.get("low_confidence_reason"),
                    match_id=c.get("match_id"), image_url=c.get("image_url"),
                    confidence=float(c.get("confidence", 0.0)),
                    species=species_of(c.get("name")),
                ))
            await session.commit()
            await session.refresh(pull, attribute_names=["cards"])
            return pull
        except IntegrityError as exc:
            await session.rollback()
            # Only the verified-code dedup conflict is retryable (fall to verified=False).
            # Any other constraint failure (FK, NOT NULL, …) is a real error — surface it.
            if "uq_pull_verified_code" not in str(exc.orig):
                raise HTTPException(500, "database error saving pull") from exc
            # Clear the identity map so the retry's fresh Pull(id=pull_id) can't collide
            # with the rolled-back instance still tracked under the same primary key.
            session.expunge_all()
            continue
    raise HTTPException(500, "could not persist pull")


async def _compute_encounters(session: AsyncSession, trainer_id, pull: Pull) -> list[EncounterOut]:
    """Wild-encounter callouts for a just-saved pull. count = total cards of that
    species ever saved by this trainer; new = nothing existed before this pull."""
    in_pull = Counter(c.species for c in pull.cards if c.species)
    if not in_pull:
        return []
    totals = dict(
        (
            await session.execute(
                select(PullCard.species, func.count())
                .join(Pull, PullCard.pull_id == Pull.id)
                .where(Pull.trainer_id == trainer_id, PullCard.species.in_(in_pull.keys()))
                .group_by(PullCard.species)
            )
        ).all()
    )
    out = [
        EncounterOut(species=sp, count=totals.get(sp, n), new=totals.get(sp, n) == n)
        for sp, n in in_pull.items()
    ]
    out.sort(key=lambda e: (not e.new, e.species))
    return out


@router.get("", response_model=list[PullOut])
async def list_pulls(trainer: CurrentTrainer) -> list[PullOut]:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Pull)
                .where(Pull.trainer_id == trainer.id)
                .options(selectinload(Pull.cards))  # eager-load cards: 2 queries, not N+1
                .order_by(Pull.created_at.desc())
            )
        ).scalars().all()
        prices, as_of = await latest_price_map(session)
        return [_enrich_prices(_pull_to_out(p), prices, as_of) for p in rows]


@router.get("/{pull_id}", response_model=PullOut)
async def get_pull(trainer: CurrentTrainer, pull_id: uuid.UUID) -> PullOut:
    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        if pull is None or pull.trainer_id != trainer.id:
            raise HTTPException(404, "pull not found")
        await session.refresh(pull, attribute_names=["cards"])
        prices, as_of = await latest_price_map(session)
        return _enrich_prices(_pull_to_out(pull), prices, as_of)


@router.patch("/{pull_id}/code")
async def rescue_pull_code(
    trainer: CurrentTrainer,
    pull_id: uuid.UUID,
    code_card: UploadFile = File(...),
) -> dict:
    """Re-submit a clearer code photo for an unverified pull. Owner-only. Re-OCRs the
    code exactly like save_pull and, if it now reads a valid unique code, flips the
    pull to verified. Mirrors the verified-code uniqueness handling: if another pull
    already owns this code, the rescue still records the read code but stays unverified.
    """
    code_bytes = await _read_image(code_card, "code_card")

    # Server re-OCRs the code (authoritative — same block as save_pull).
    code_img = cv2.imdecode(np.frombuffer(code_bytes, np.uint8), cv2.IMREAD_COLOR)
    cr = read_code_card(code_img) if code_img is not None else None
    code = cr.code if cr else None
    code_norm = _normalize_code(code)
    code_ok = bool(cr and cr.format_ok)
    code_conf = float(cr.confidence) if cr else 0.0
    want_verified = bool(code_norm) and code_ok

    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        if pull is None or pull.trainer_id != trainer.id:
            raise HTTPException(404, "pull not found")
        if pull.verified:
            raise HTTPException(409, "pull already verified")

        pull.code = code
        pull.code_normalized = code_norm
        pull.code_confidence = code_conf
        pull.code_format_ok = code_ok

        if want_verified:
            try:
                pull.verified = True
                await session.flush()  # surfaces the partial-unique-index violation here
            except IntegrityError as exc:
                await session.rollback()
                # Only the verified-code dedup conflict is retryable (someone else
                # already owns this code) — fall back to unverified. Anything else is real.
                if "uq_pull_verified_code" not in str(exc.orig):
                    raise HTTPException(500, "database error updating code") from exc
                pull = await session.get(Pull, pull_id)
                pull.code = code
                pull.code_normalized = code_norm
                pull.code_confidence = code_conf
                pull.code_format_ok = code_ok
                pull.verified = False
        await session.commit()
        result = {"verified": pull.verified, "code": pull.code}

    # Overwrite the stored code photo with the rescued frame. Non-transactional, so
    # it runs after the DB update commits and never gates it.
    save_code_photo(trainer.id, pull_id, code_bytes)
    return result


@router.get("/{pull_id}/photo/{kind}")
async def get_pull_photo(trainer: CurrentTrainer, pull_id: uuid.UUID, kind: str) -> Response:
    if kind not in ("staircase", "code"):
        raise HTTPException(404, "unknown photo kind")
    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        if pull is None or pull.trainer_id != trainer.id:
            raise HTTPException(404, "pull not found")
        rel = pull.staircase_photo_path if kind == "staircase" else pull.code_photo_path
    try:
        data = open_photo(rel)
    except FileNotFoundError:
        raise HTTPException(404, "photo missing")
    return Response(content=data, media_type="image/jpeg")
