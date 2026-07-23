"""Single-card identification for the live scan mode.

Two SMALL OCR passes (name band of the card crop + the native-res number
strip) instead of whole-card OCR — that is the latency win. The decision ladder
itself (name+number agree > name+denominator-unique > number+catalog-valid >
VLM) lives in ``identify_core.resolve_identity`` so the binder page flow can
share it; this module owns the live-only framing (QR gate, the two band passes,
FrameResult kinds)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.pack.identify_core import SessionPrior, resolve_identity
from app.pack.ocr import CodeReading, is_code_card, parse_number, read_code_card
from app.pack.rapidocr_reader import detect_lines
from app.schemas import CodeCardResult, PackCard

# Re-exported so app.pack.live_api / app.pack.live_session keep importing
# SessionPrior from here unchanged (it now lives in identify_core).
__all__ = ["SessionPrior", "FrameResult", "identify_frame"]

log = logging.getLogger("pokemon_scanner.pack.live")


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

    # name candidates: title-band lines highest-confidence first (the y sort stays
    # here; resolve_identity consumes them as (text, conf) best-first).
    name_texts = [(t, c) for _y, t, c in sorted(name_lines, key=lambda t: -t[2])]
    res = await resolve_identity(name_texts, reading, prior)

    # Unreadable: nothing to go on — no number read AND no name match (name_match
    # is None iff its score is None). Same short-circuit as before, mapped from the
    # core's result rather than returned by it.
    if not res.confident and reading is None and res.name_match_score is None:
        return FrameResult("unreadable", None, None, None, needs_vlm=True)

    card = PackCard(
        row_index=-1,  # assigned by the session store
        card_number=res.display_number, set_id=res.set_id, set_code=res.set_code,
        set_name=res.set_name, confidence=0.9 if res.confident else 0.3,
        low_confidence_reason=res.low_confidence_reason,
        needs_review=not res.confident, **res.fields)
    return FrameResult("card", card, None, res.identity_key,
                       needs_vlm=not res.confident)
