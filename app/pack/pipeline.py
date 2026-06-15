"""scan_pack(): staircase + code card bytes → PackScanResponse."""

from __future__ import annotations

import asyncio
import logging

import cv2
import numpy as np

from app.pack.confidence import pack_confidence, score_card
from app.pack.matching import card_fields_from_match, lookup_resolved_cards
from app.pack.ocr import read_card_number, read_code_card
from app.pack.segmentation import find_strips
from app.pack.set_resolution import resolve_set
from app.pokewallet import get_api_key
from app.schemas import CodeCardResult, PackCard, PackScanResponse

log = logging.getLogger("pokemon_scanner.pack.pipeline")


def _decode(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        log.warning("pipeline.decode_failed bytes=%s (non-empty but undecodable)", len(data))
    return img


def _display_number(numerator: str | None, denominator: str | None,
                    prefix: str | None) -> str | None:
    # Invariant from read_card_number: a numerator is only ever set alongside a
    # denominator (NUMBER_RE) or a prefix (PROMO_RE), so no real reading is dropped here.
    if prefix and numerator:
        return f"{prefix}{numerator}"
    if numerator and denominator:
        return f"{numerator}/{denominator}"
    return None


async def scan_pack(
    staircase_bytes: bytes,
    code_bytes: bytes,
    capture_meta: dict | None,
) -> PackScanResponse:
    stair = _decode(staircase_bytes)
    if stair is None:
        raise ValueError("staircase image could not be decoded")

    seg = find_strips(stair, capture_meta)
    # OCR + symbol matching are blocking CPU/subprocess work (~8 tesseract calls per
    # strip); offload to threads so this async path (a FastAPI endpoint) doesn't pin
    # the event loop for the whole pack.
    readings = list(
        await asyncio.gather(*(asyncio.to_thread(read_card_number, s.image) for s in seg.strips))
    )
    resolutions = list(
        await asyncio.gather(
            *(asyncio.to_thread(resolve_set, r, s.image) for r, s in zip(readings, seg.strips))
        )
    )

    matches = await lookup_resolved_cards(
        [(r.numerator, res) for r, res in zip(readings, resolutions)],
        api_key=get_api_key(),
    )

    cards: list[PackCard] = []
    for strip, reading, res, match in zip(seg.strips, readings, resolutions, matches):
        conf, reason = score_card(reading, res, match is not None)
        cards.append(
            PackCard(
                row_index=strip.row_index,
                card_number=_display_number(reading.numerator, reading.denominator,
                                            reading.prefix),
                set_id=res.set_id,
                set_code=res.set_code,
                set_name=res.set_name,
                confidence=conf,
                low_confidence_reason=reason,
                **card_fields_from_match(match),
            )
        )

    code_img = _decode(code_bytes)
    if code_img is None:
        code_result = CodeCardResult(code=None, confidence=0.0, format_ok=False)
    else:
        cr = read_code_card(code_img)
        code_result = CodeCardResult(code=cr.code, confidence=round(cr.confidence, 3),
                                     format_ok=cr.format_ok)

    resp = PackScanResponse(
        cards=cards,
        code_card=code_result,
        pack_confidence=pack_confidence([c.confidence for c in cards]),
        segmentation_warning=seg.warning,
    )
    log.info("pipeline.done rows=%s flagged=%s pack_conf=%.3f code=%s",
             len(cards), sum(1 for c in cards if c.low_confidence_reason),
             resp.pack_confidence, code_result.code)
    return resp
