"""Personal Collection API: binder-page scan + qty-aware CRUD.

Collection is NOT pull history — it never feeds training harvest or pull stats.
A binder photo is scanned (``scan_binder_page``) into PackCard-shaped cells; the
client confirms them and POSTs them here, where each card is upserted into
``collection_card`` keyed by a server-derived ``identity_key`` (re-saving the
same card bumps ``qty`` instead of duplicating the row). Auth/ownership/error
idioms mirror ``app/pulls.py``.

A cell the scanner FLAGGED is persisted only with the user's explicit go-ahead
(``confirmed``); everything the save path refuses is counted back to the client
rather than dropped silently — see ``save_collection``."""

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
from app.pack import scan_followup, vlm_client
from app.pack.binder import scan_binder_page
from app.pack.name_index import normalize_name
from app.pack.set_resolution import catalog_local_id, load_denominator_table
from app.prices import latest_price_map, midpoint
from app.pulls import EncounterOut  # reuse the pulls encounter shape (species/count/new)
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.collection")

router = APIRouter(tags=["collection"])

_MAX_UPLOAD = 15 * 1024 * 1024


# ── Request / response models ────────────────────────────────────────────────
class CollectionSaveCardIn(PackCard):
    """A card the client is asking to save, plus the review verdict.

    ``confirmed`` is the user's explicit "yes, save this one" for a cell the
    scanner FLAGGED — it is set only by an action the user took in review (fixing
    the card, or keeping it as-is). It defaults to False so an old client, which
    sends no such field, is read as "not confirmed" and its flagged cells are
    skipped rather than persisted: the failure mode of this guard has to be a
    card missing from the collection, never a wrong card silently in it.

    It lives on THIS model rather than on PackCard because PackCard is the shape
    of every scan RESPONSE; adding a field there would put ``"confirmed": false``
    into every card of every scan payload (see PackCard._drop_unset_state for the
    same argument about ``state``)."""
    confirmed: bool = False


class CollectionSaveIn(BaseModel):
    cards: list[CollectionSaveCardIn]


class CollectionSaveOut(BaseModel):
    added: int
    incremented: int
    total_cards: int
    encounters: list[EncounterOut] = []
    # Cards the server refused to persist: flagged-but-unconfirmed, or carrying no
    # usable identity at all. New fields with defaults, so an old client that
    # ignores them sees exactly the response it always saw.
    skipped: int = 0
    skipped_rows: list[int] = []


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


def _is_degenerate_key(identity_key: str) -> bool:
    """A key that identifies no particular card, so a row filed under it is a
    shared bucket that the NEXT such card would increment instead of adding.

    Two shapes:

    * an EMPTY right-hand side — no numerator, and no name that survives
      normalization. "?:" is the no-evidence cell, but "SVI:" is the same damage
      one set down: every unreadable Scarlet & Violet card in the binder would
      land on one row and read back as "qty 4" of a card nobody can name.
      Split on the LAST colon, because the left side is a set NAME and may
      contain one ("Sword & Shield: Astral Radiance"); the right side never can
      (a numerator has no colon, and normalize_name strips punctuation).
    * the literal "?:unknown" — the same no-evidence cell from a client that
      spells its blank name out, which is what identify_core's old fallback did.

    A key with a real right-hand side is left alone even when its set is unknown
    ("?:126", "?:pikachu"): that is a partial identity, not an absent one, and it
    can only be persisted by a user explicitly confirming the flagged cell."""
    _, _, right = identity_key.rpartition(":")
    return not right or identity_key == "?:unknown"


def _is_flagged(card: CollectionSaveCardIn) -> bool:
    """Did the scanner tell the user this card needs checking?

    Mirrors BinderReview's own test (``needs_review ?? low_confidence_reason !==
    null``) so the cell the user saw outlined in red is exactly the cell this
    guard protects, and adds the two background-VLM states: a row still being
    identified — or one whose identification gave up — is by definition not a
    card the user vouched for."""
    return bool(card.needs_review) or card.low_confidence_reason is not None \
        or card.state in ("pending_vlm", "vlm_failed")


def _tcgdex_card_id(card: PackCard, numerator: str | None) -> str | None:
    """`<tdx>-<local_id>` where tdx is the denominator-table entry's tcgdex_id
    (else its set_code) and local_id is the numerator in the form that set's
    catalog rows actually use. None when the set is unresolvable or there is no
    numerator.

    The local_id form is NOT just a zero-pad: a promo numerator arrives glued to
    its printed prefix ("MEP37"), and only swshp keeps that prefix in its
    local_ids. Padding blindly produced "mep-MEP037", a card id that does not
    exist, so the form conversion lives in catalog_local_id()."""
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
    return f"{tdx}-{catalog_local_id(entry, numerator)}"


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
    # Warm the RunPod worker FIRST, so its ~16GB cold load overlaps this page's
    # upload read + OCR instead of landing inside the background VLM call.
    # Debounced + fire-and-forget inside kick() (app/pack/vlm_client.py).
    vlm_client.kick()
    data = await _read_image(page, "page")
    try:
        return await scan_binder_page(data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/scan/binder/{scan_id}")
async def scan_binder_followup(trainer: CurrentTrainer, scan_id: str) -> dict:
    """Poll the background VLM pass for a binder page → ``{cards, any_pending,
    done}``. ``scan_id`` comes from the scan response and only exists while a
    follow-up is running; unknown/expired (or a pack id) is a 404.

    Auth mirrors POST /scan/binder. There is deliberately no per-trainer binding
    on the entry itself: this is a single-trainer collection flow and the 128-bit
    scan_id is unguessable, so the auth check plus the id is the whole scope. If
    the app ever becomes multi-tenant in a shared process, bind the entry to
    trainer.id here the way live_session.get_session does."""
    entry = scan_followup.get(scan_id, "binder")
    if entry is None:
        raise HTTPException(404, "scan not found")
    return entry


@router.post("/collection", response_model=CollectionSaveOut)
async def save_collection(
    trainer: CurrentTrainer, body: CollectionSaveIn
) -> CollectionSaveOut:
    """Upsert CONFIRMED cards into the trainer's Collection. Re-saving a card
    bumps its qty (ON CONFLICT (trainer_id, identity_key) DO UPDATE qty+1).

    Two cards never reach the database:

    * a FLAGGED card the user did not confirm. The scanner told the user this one
      was uncertain; saving it anyway is how a mis-read becomes a permanent wrong
      row in the collection, indistinguishable from a card the user checked. The
      client marks a flagged card ``confirmed`` only when the user fixed it or
      explicitly kept it, so "the user never touched the flag" now means "not
      saved" rather than "saved as if confirmed".
    * a card whose identity key would be DEGENERATE — no number and no usable
      name, whether or not the set resolved (see ``_is_degenerate_key``). Such a
      key is shared by every card of its kind, so the row it creates is a bucket
      that the next unreadable card increments rather than adding to.

    Both are reported back as ``skipped``/``skipped_rows`` so the client can say
    what happened instead of silently dropping cells the user pressed save on."""
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
        saved: list[PackCard] = []
        skipped_rows: list[int] = []
        for card in cards:
            numerator = _numerator(card.card_number)
            identity_key = _identity_key(card.set_code, card.set_name, numerator, card.name)
            if _is_degenerate_key(identity_key):
                # Nothing to file it under, and every such cell would file under
                # the same thing. The cell was visibly flagged in review; fixing it
                # there is the path.
                skipped_rows.append(card.row_index)
                continue
            if _is_flagged(card) and not card.confirmed:
                skipped_rows.append(card.row_index)
                continue
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
            saved.append(card)
        await session.commit()

        total_cards = (
            await session.execute(
                select(func.count()).select_from(CollectionCard)
                .where(CollectionCard.trainer_id == trainer.id)
            )
        ).scalar_one()

        try:
            # SAVED cards only. A skipped card is not in the collection, so
            # announcing a wild encounter for it would register a Pokédex moment
            # for a species the trainer does not own — the encounter count reads
            # from the stored rows and would fall back to the batch count.
            encounters = await _collection_encounters(session, trainer.id, saved)
        except Exception:  # the dex moment must never break persistence
            encounters = []

    if skipped_rows:
        log.info("collection.skipped trainer=%s n=%d rows=%s (flagged-unconfirmed "
                 "or no identity)", trainer.id, len(skipped_rows), skipped_rows)
    return CollectionSaveOut(
        added=added, incremented=incremented,
        total_cards=int(total_cards), encounters=encounters,
        skipped=len(skipped_rows), skipped_rows=skipped_rows,
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
