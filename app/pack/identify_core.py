"""Shared identify ladder — the core that both live scan and binder page scan use.

Given candidate title-band name lines plus an optional printed number reading,
resolve a card identity via the decision ladder: name+number agree > unique
name (+denominator prior) > number valid in the session's set > unresolved.
Extracted VERBATIM from ``live_identify.identify_frame`` so the single-card live
flow and the multi-card binder flow share one implementation; the only I/O is
the same name-index / set-map / catalog-lookup the live path already performed."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.cards import cached_lookup_card, get_set_numerators, normalize_local_id
from app.db.models import SetIdMap
from app.db.session import async_session_maker
from app.pack.matching import card_fields_from_match
from app.pack.name_index import get_name_index
from app.pack.ocr import NumberReading
from app.pokewallet import get_api_key

log = logging.getLogger("pokemon_scanner.pack.identify")


@dataclass
class SessionPrior:
    set_id: str | None
    set_name: str | None
    denominator: str | None


@dataclass
class IdentityResult:
    confident: bool
    numerator: str | None
    display_number: str | None
    set_id: str | None
    set_code: str | None
    set_name: str | None
    fields: dict
    low_confidence_reason: str | None
    identity_key: str
    name_match_score: float | None


async def _pw_set_id_for(tcgdex_set_id: str) -> str | None:
    """tcgdex set -> PokéWallet set_id via the set_id_map bridge table (built
    by scripts/build_id_maps.py). me-era sets are self-referential (e.g.
    "me05" -> "me05"); sets that haven't been mapped yet yield None, and the
    card's identity still comes from the name index (price/image stay None)."""
    async with async_session_maker() as session:
        return (await session.execute(
            select(SetIdMap.pokewallet_set_id)
            .where(SetIdMap.tcgdex_set_id == tcgdex_set_id))).scalars().first()


async def resolve_identity(name_texts: list[tuple[str, float]],
                           reading: NumberReading | None,
                           prior: SessionPrior | None) -> IdentityResult:
    """Resolve candidate title-band lines + a number reading into an identity.

    ``name_texts`` is the title-band candidate lines as ``(text, conf)``,
    best-first (the caller does any y/conf ordering). Callers that need the
    live/binder FrameResult kinds (no_card/unreadable) decide those from the
    inputs and this result — the core does not emit them."""
    # name: highest-confidence line in the TITLE band only (hard filter)
    idx = await get_name_index()
    den = reading.denominator if (reading and reading.denominator) \
        else (prior.denominator if prior else None)
    name_match = None
    top_name_text = None
    for text, conf in name_texts:
        if top_name_text is None:
            top_name_text = text
        m = idx.match(text, denominator=den)
        if m is not None:
            name_match = m
            break
    # Fallback for prefixed "Trainer's Pokemon" names (e.g. Ascended Heroes): OCR
    # frequently drops the "Erika's"/"Sabrina's" prefix, so the bare Pokemon name
    # matches a commoner printing in another set ambiguously. If the session already
    # knows the set, or the denominator uniquely identifies one, re-match scoped to
    # that set's cards.
    if (name_match is None or name_match.ambiguous) and top_name_text:
        scoped = idx.match_in_set(
            top_name_text,
            set_id=(prior.set_id if prior and prior.set_id else None),
            denominator=den)
        if scoped is not None and not scoped.ambiguous:
            name_match = scoped

    numerator = None
    if reading is not None and reading.numerator:
        # Tail-normalized so a gallery numerator ("TG22"/"GG7") compares equal to
        # its catalog local_id ("TG22"/"GG07") and validates against the set.
        numerator = normalize_local_id(reading.numerator)

    set_id = set_code = set_name = None
    confident = False

    if name_match and numerator and normalize_local_id(name_match.local_id) == numerator:
        confident = True                      # name + number agree
    elif name_match and not name_match.ambiguous:
        confident = True                      # unique name (+denominator prior)
        numerator = numerator or normalize_local_id(name_match.local_id)
    if name_match and confident:
        set_name = name_match.set_name
        set_code = name_match.tcgdex_set_id
        set_id = await _pw_set_id_for(name_match.tcgdex_set_id)

    if not confident and reading is not None and prior and prior.set_id:
        valid = await get_set_numerators(prior.set_id)
        if numerator and (not valid or numerator in valid):
            confident = True                  # number valid in session's set
            set_id, set_name = prior.set_id, prior.set_name

    fields: dict = {"name": None, "rarity": None, "image_url": None, "match_id": None}
    if set_id and numerator:
        try:
            match = await cached_lookup_card(set_id, numerator,
                                             set_name=set_name, api_key=get_api_key())
            fields = card_fields_from_match(match)
        except Exception as e:
            log.warning("live.lookup_failed err=%r", e)
    if fields.get("name") is None and name_match is not None:
        fields["name"] = name_match.card_name

    display_number = None
    if numerator:
        den = reading.denominator if reading and reading.denominator else \
            (prior.denominator if prior else None)
        display_number = f"{numerator.zfill(3)}/{den}" if den else numerator

    # Reason must name the stage that actually failed — a read number with an
    # unresolved set is NOT "couldn't read the number" (misleading in the tray).
    if confident:
        reason = None
    elif reading is None:
        reason = "number_ambiguous"
    elif set_id is None and set_name is None:
        reason = "set_ambiguous"
    else:
        reason = "no_db_match"

    key = f"{set_code or set_name or '?'}:{numerator or normalize_key(fields.get('name'))}"
    return IdentityResult(
        confident=confident, numerator=numerator, display_number=display_number,
        set_id=set_id, set_code=set_code, set_name=set_name, fields=fields,
        low_confidence_reason=reason, identity_key=key,
        name_match_score=(name_match.score if name_match is not None else None))


def normalize_key(name: str | None) -> str:
    from app.pack.name_index import normalize_name
    return normalize_name(name or "unknown")
