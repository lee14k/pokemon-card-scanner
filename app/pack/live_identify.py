"""Single-card identification for the live scan mode.

Two SMALL OCR passes (name band of the card crop + the native-res number
strip) instead of whole-card OCR — that is the latency win. Decision ladder:
name+number agree > name+denominator-unique > number+catalog-valid > VLM."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np
from sqlalchemy import select

from app.cards import cached_lookup_card, get_set_numerators
from app.db.models import SetIdMap
from app.db.session import async_session_maker
from app.pack.matching import card_fields_from_match
from app.pack.name_index import get_name_index
from app.pack.ocr import CodeReading, is_code_card, parse_number, read_code_card
from app.pack.rapidocr_reader import detect_lines
from app.pokewallet import get_api_key
from app.schemas import CodeCardResult, PackCard

log = logging.getLogger("pokemon_scanner.pack.live")


@dataclass
class SessionPrior:
    set_id: str | None
    set_name: str | None
    denominator: str | None


@dataclass
class FrameResult:
    kind: Literal["card", "code_card", "no_card", "unreadable"]
    card: PackCard | None
    code: CodeCardResult | None
    identity_key: str | None
    needs_vlm: bool


def _name_band(card_bgr: np.ndarray) -> np.ndarray:
    h = card_bgr.shape[0]
    return card_bgr[: max(1, int(h * 0.25))]


async def _pw_set_id_for(tcgdex_set_id: str) -> str | None:
    """tcgdex set -> PokéWallet set_id via the set_id_map bridge table (built
    by scripts/build_id_maps.py). me-era sets are self-referential (e.g.
    "me05" -> "me05"); sets that haven't been mapped yet yield None, and the
    card's identity still comes from the name index (price/image stay None)."""
    async with async_session_maker() as session:
        return (await session.execute(
            select(SetIdMap.pokewallet_set_id)
            .where(SetIdMap.tcgdex_set_id == tcgdex_set_id))).scalars().first()


async def identify_frame(card_bgr: np.ndarray, strip_bgr: np.ndarray | None,
                         prior: SessionPrior | None) -> FrameResult:
    if await asyncio.to_thread(is_code_card, card_bgr):
        cr: CodeReading = await asyncio.to_thread(read_code_card, card_bgr)
        return FrameResult(
            "code_card", None,
            CodeCardResult(code=cr.code, confidence=round(cr.confidence, 3),
                           format_ok=cr.format_ok),
            None, needs_vlm=False)

    # Run the name-band and number-strip OCR passes CONCURRENTLY rather than back
    # to back — for a single scanner (the common case) this overlaps them across
    # the CPUs and roughly halves per-card OCR wall-time. Concurrent RapidOCR use
    # is already exercised by the pack pipeline (OCR_GATE lets several run at once),
    # so the shared engine handles it. Cross-request load stays bounded by OCR_GATE.
    name_task = asyncio.to_thread(detect_lines, _name_band(card_bgr), cap=1400)
    if strip_bgr is not None:
        name_lines, strip_lines = await asyncio.gather(
            name_task, asyncio.to_thread(detect_lines, strip_bgr, cap=1600))
    else:
        name_lines, strip_lines = await name_task, []
    if not name_lines and not strip_lines:
        return FrameResult("no_card", None, None, None, needs_vlm=False)

    # number: best pattern_ok parse from the strip (fall back to name-band lines)
    reading = None
    for _y, text, conf in sorted(strip_lines + name_lines, key=lambda t: -t[2]):
        r = parse_number(text, conf)
        if r is not None and r.pattern_ok:
            reading = r
            break

    # name: highest-confidence line in the TITLE band only (hard filter)
    idx = await get_name_index()
    name_match = None
    for _y, text, conf in sorted(name_lines, key=lambda t: -t[2]):
        den = reading.denominator if reading else (prior.denominator if prior else None)
        m = idx.match(text, denominator=den)
        if m is not None:
            name_match = m
            break

    numerator = None
    if reading is not None and reading.numerator:
        numerator = reading.numerator.lstrip("0") or "0"

    set_id = set_code = set_name = None
    confident = False

    if name_match and numerator and name_match.local_id.lstrip("0") == numerator:
        confident = True                      # name + number agree
    elif name_match and not name_match.ambiguous:
        confident = True                      # unique name (+denominator prior)
        numerator = numerator or (name_match.local_id.lstrip("0") or "0")
    if name_match and confident:
        set_name = name_match.set_name
        set_code = name_match.tcgdex_set_id
        set_id = await _pw_set_id_for(name_match.tcgdex_set_id)

    if not confident and reading is not None and prior and prior.set_id:
        valid = await get_set_numerators(prior.set_id)
        if numerator and (not valid or numerator in valid):
            confident = True                  # number valid in session's set
            set_id, set_name = prior.set_id, prior.set_name

    if not confident and (reading is None and name_match is None):
        return FrameResult("unreadable", None, None, None, needs_vlm=True)

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

    card = PackCard(
        row_index=-1,  # assigned by the session store
        card_number=display_number, set_id=set_id, set_code=set_code,
        set_name=set_name, confidence=0.9 if confident else 0.3,
        low_confidence_reason=None if confident else "number_ambiguous",
        needs_review=not confident, **fields)
    key = f"{set_code or set_name or '?'}:{numerator or normalize_key(fields.get('name'))}"
    return FrameResult("card", card, None, key, needs_vlm=not confident)


def normalize_key(name: str | None) -> str:
    from app.pack.name_index import normalize_name
    return normalize_name(name or "unknown")
