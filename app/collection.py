"""Personal Collection API: binder-page scan + qty-aware CRUD.

Collection is NOT pull history — it never feeds training harvest or pull stats.
A binder photo is scanned (``scan_binder_page``) into PackCard-shaped cells; the
client confirms them and POSTs them here, where each card is upserted into
``collection_card`` keyed by a server-derived ``identity_key`` (re-saving the
same card bumps ``qty`` instead of duplicating the row). Auth/ownership/error
idioms mirror ``app/pulls.py``."""

from __future__ import annotations

import logging
import uuid
from collections import Counter

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import CollectionCard
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.dex.species import species_of
from app.pack.binder import scan_binder_page
from app.pack.name_index import normalize_name
from app.pack.set_resolution import load_denominator_table
from app.prices import latest_price_map, midpoint
from app.pulls import EncounterOut  # reuse the pulls encounter shape (species/count/new)
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.collection")

router = APIRouter(tags=["collection"])

_MAX_UPLOAD = 15 * 1024 * 1024


# ── Request / response models ────────────────────────────────────────────────
class CollectionSaveIn(BaseModel):
    cards: list[PackCard]


class CollectionSaveOut(BaseModel):
    added: int
    incremented: int
    total_cards: int
    encounters: list[EncounterOut] = []


class QtyIn(BaseModel):
    qty: int


class CollectionCardOut(BaseModel):
    id: uuid.UUID
    tcgdex_card_id: str | None
    set_id: str | None
    set_code: str | None
    set_name: str | None
    card_number: str | None
    numerator: str | None
    name: str | None
    image_url: str | None
    match_id: str | None
    identity_key: str
    qty: int
    price_usd_low: float | None = None
    price_usd_high: float | None = None
    estimated_value_each: float | None = None


class CollectionOut(BaseModel):
    cards: list[CollectionCardOut]
    total_qty: int
    estimated_value: float | None = None
    priced_as_of: str | None = None


# ── Server-side derivations (client cannot spoof identity/tcgdex ids) ─────────
def _numerator(card_number: str | None) -> str | None:
    """The collector number's numerator part with leading zeros stripped:
    "004/198" -> "4", "TG12/TG30" -> "TG12", "SWSH123" -> "SWSH123"."""
    if not card_number:
        return None
    num = card_number.split("/")[0].strip()
    if not num:
        return None
    return num.lstrip("0") or num


def _identity_key(set_code: str | None, set_name: str | None,
                  numerator: str | None, name: str | None) -> str:
    """Stable per-trainer dedup key: set (code, else name, else '?') + the
    numerator, falling back to the normalized name when there is no number."""
    left = set_code or set_name or "?"
    right = numerator or normalize_name(name or "")
    return f"{left}:{right}"


def _tcgdex_card_id(card: PackCard, numerator: str | None) -> str | None:
    """`<tdx>-<numerator zero-padded to 3>` where tdx is the denominator-table
    entry's tcgdex_id (else its set_code). None when the set is unresolvable or
    there is no numerator."""
    if not numerator:
        return None
    table = load_denominator_table()
    entry = None
    if card.set_id:
        entry = next((s for s in table.sets if s.set_id == card.set_id), None)
    if entry is None and card.set_code:
        entry = table.by_code.get(card.set_code.upper())
    if entry is None:
        return None
    tdx = entry.tcgdex_id or entry.set_code
    if not tdx:
        return None
    return f"{tdx}-{numerator.zfill(3)}"


async def _collection_encounters(session, trainer_id,
                                 cards: list[PackCard]) -> list[EncounterOut]:
    """Wild-encounter callouts for a just-saved batch. count = total qty of that
    species now in the trainer's collection; new = the species only exists here
    because of this save. Species reads from the stored ``species`` column (set at
    save time), summed by qty in SQL — mirrors ``_compute_encounters`` in pulls."""
    in_batch = Counter(sp for c in cards if (sp := species_of(c.name)))
    if not in_batch:
        return []
    totals = dict(
        (
            await session.execute(
                select(CollectionCard.species, func.sum(CollectionCard.qty))
                .where(
                    CollectionCard.trainer_id == trainer_id,
                    CollectionCard.species.in_(in_batch.keys()),
                )
                .group_by(CollectionCard.species)
            )
        ).all()
    )
    out = [
        EncounterOut(species=sp, count=totals.get(sp, n), new=totals.get(sp, n) == n)
        for sp, n in in_batch.items()
    ]
    out.sort(key=lambda e: (not e.new, e.species))
    return out


async def _read_image(upload: UploadFile, field: str) -> bytes:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(400, f"{field}: upload an image file")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, f"{field}: image too large (max 15MB)")
    return data


# ── Routes ────────────────────────────────────────────────────────────────────
@router.post("/scan/binder")
async def scan_binder(trainer: CurrentTrainer, page: UploadFile = File(...)) -> dict:
    """Scan one binder page → grid of PackCard-shaped cells (with thumbs). Decode
    failures / no readable cards → 422 {"detail": "no_cards_found"}."""
    data = await _read_image(page, "page")
    try:
        return await scan_binder_page(data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/collection", response_model=CollectionSaveOut)
async def save_collection(
    trainer: CurrentTrainer, body: CollectionSaveIn
) -> CollectionSaveOut:
    """Upsert confirmed cards into the trainer's Collection. Re-saving a card
    bumps its qty (ON CONFLICT (trainer_id, identity_key) DO UPDATE qty+1)."""
    cards = body.cards
    async with async_session_maker() as session:
        existing = set(
            (
                await session.execute(
                    select(CollectionCard.identity_key)
                    .where(CollectionCard.trainer_id == trainer.id)
                )
            ).scalars().all()
        )
        seen = set(existing)
        added = incremented = 0
        for card in cards:
            numerator = _numerator(card.card_number)
            identity_key = _identity_key(card.set_code, card.set_name, numerator, card.name)
            species = species_of(card.name) if card.name else None
            if identity_key in seen:
                incremented += 1
            else:
                added += 1
                seen.add(identity_key)
            stmt = (
                pg_insert(CollectionCard)
                .values(
                    trainer_id=trainer.id,
                    tcgdex_card_id=_tcgdex_card_id(card, numerator),
                    set_id=card.set_id,
                    set_code=card.set_code,
                    set_name=card.set_name,
                    card_number=card.card_number,
                    numerator=numerator,
                    name=card.name,
                    species=species,
                    image_url=card.image_url,
                    match_id=card.match_id,
                    identity_key=identity_key,
                    qty=1,
                )
                .on_conflict_do_update(
                    # A VLM/manual name fix can change species between saves.
                    constraint="uq_collection_trainer_identity",
                    set_={"qty": CollectionCard.qty + 1, "species": species, "updated_at": func.now()},
                )
            )
            await session.execute(stmt)
        await session.commit()

        total_cards = (
            await session.execute(
                select(func.count()).select_from(CollectionCard)
                .where(CollectionCard.trainer_id == trainer.id)
            )
        ).scalar_one()

        try:
            encounters = await _collection_encounters(session, trainer.id, cards)
        except Exception:  # the dex moment must never break persistence
            encounters = []

    return CollectionSaveOut(
        added=added, incremented=incremented,
        total_cards=int(total_cards), encounters=encounters,
    )


def _sort_key(row: CollectionCard) -> tuple:
    num = row.numerator
    is_digit = bool(num) and num.isdigit()
    return (row.set_code or "", 0 if is_digit else 1, int(num) if is_digit else 0, num or "")


@router.get("/collection", response_model=CollectionOut)
async def get_collection(trainer: CurrentTrainer) -> CollectionOut:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(CollectionCard).where(CollectionCard.trainer_id == trainer.id)
            )
        ).scalars().all()
        prices, as_of = await latest_price_map(session)

    rows = sorted(rows, key=_sort_key)
    cards: list[CollectionCardOut] = []
    total_qty = 0
    total_value = 0.0
    any_priced = False
    for r in rows:
        total_qty += r.qty
        lo = hi = each = None
        if r.match_id and r.match_id in prices:
            lo, hi = prices[r.match_id]
            each = midpoint(lo, hi)
            if each is not None:
                total_value += each * r.qty
                any_priced = True
        cards.append(CollectionCardOut(
            id=r.id, tcgdex_card_id=r.tcgdex_card_id, set_id=r.set_id,
            set_code=r.set_code, set_name=r.set_name, card_number=r.card_number,
            numerator=r.numerator, name=r.name, image_url=r.image_url,
            match_id=r.match_id, identity_key=r.identity_key, qty=r.qty,
            price_usd_low=lo, price_usd_high=hi, estimated_value_each=each,
        ))
    return CollectionOut(
        cards=cards, total_qty=total_qty,
        estimated_value=round(total_value, 2) if any_priced else None,
        priced_as_of=as_of if any_priced else None,
    )


@router.patch("/collection/{card_id}")
async def patch_collection_qty(
    trainer: CurrentTrainer, card_id: uuid.UUID, body: QtyIn
) -> dict:
    async with async_session_maker() as session:
        row = await session.get(CollectionCard, card_id)
        if row is None or row.trainer_id != trainer.id:
            raise HTTPException(404, "collection card not found")
        if body.qty < 1:
            raise HTTPException(422, "qty must be >= 1")
        row.qty = body.qty
        await session.commit()
        return {"id": str(row.id), "qty": row.qty}


@router.delete("/collection/{card_id}", status_code=204)
async def delete_collection_card(
    trainer: CurrentTrainer, card_id: uuid.UUID
) -> Response:
    async with async_session_maker() as session:
        row = await session.get(CollectionCard, card_id)
        if row is None or row.trainer_id != trainer.id:
            raise HTTPException(404, "collection card not found")
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)
