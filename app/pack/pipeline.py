"""scan_pack(): staircase + code card bytes → PackScanResponse."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from collections import Counter

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF support for direct-from-iPhone uploads.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - dependency ships in requirements
    pass

from app.matcher_client import enabled as matcher_enabled, kick_index_build, match_strips
from app.pack.confidence import pack_confidence, score_card
from app.pack.matching import card_fields_from_match, lookup_resolved_cards
from app.pack.ocr import read_card_number, read_code_card
from app.pack.segmentation import find_strips
from app.pack.set_resolution import resolve_set
from app.pokewallet import get_api_key
from app.schemas import CodeCardResult, PackCard, PackScanResponse

log = logging.getLogger("pokemon_scanner.pack.pipeline")

_MATCH_ACCEPT = float(os.environ.get("PACK_MATCH_ACCEPT", "0.85"))
_MATCH_MARGIN = float(os.environ.get("PACK_MATCH_MARGIN", "0.02"))


async def _match_art(seg, resolutions) -> list[dict | None] | None:
    """Batched art match for all strips against the pack's modal set.
    Returns per-strip accepted {'id','score'} or None; None overall when the
    matcher is disabled, unindexed (build kicked), or errored."""
    if not matcher_enabled():
        return None
    set_ids = [r.set_id for r in resolutions if r.set_id]
    if not set_ids:
        return None
    modal_set = Counter(set_ids).most_common(1)[0][0]
    jpegs = []
    for s in seg.strips:
        ok, buf = cv2.imencode(".jpg", s.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        jpegs.append(buf.tobytes() if ok else b"")
    results = await match_strips(str(modal_set), jpegs)
    if results is None:
        kick_index_build(str(modal_set))
        return None
    out: list[dict | None] = []
    for ranked in results:
        if (ranked and ranked[0]["score"] >= _MATCH_ACCEPT
                and (len(ranked) < 2 or ranked[0]["score"] - ranked[1]["score"] >= _MATCH_MARGIN)):
            out.append(ranked[0])
        else:
            out.append(None)
    log.info("pipeline.art_match set=%s accepted=%s/%s", modal_set,
             sum(1 for a in out if a), len(out))
    return out


def _decode(data: bytes) -> np.ndarray | None:
    if not data:
        return None
    # Pillow first: the upload path sends raw camera files, so orientation lives
    # in EXIF (which cv2.imdecode ignores — a sideways image breaks segmentation
    # and OCR) and iPhone uploads may be HEIC (which cv2 can't parse at all).
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            return cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    except (UnidentifiedImageError, OSError, ValueError):
        pass
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        log.warning("pipeline.decode_failed bytes=%s (non-empty but undecodable)", len(data))
    return img


def _lookup_numerator(reading) -> str | None:
    """Numerator for the keyed PokéWallet lookup. Promo cards (SWSH/SVP) are stored
    prefixed ("SWSH123") with no denominator; normal cards by bare numerator ("012")."""
    if reading.prefix and reading.numerator:
        return f"{reading.prefix}{reading.numerator}"
    return reading.numerator


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

    # Segmentation (fastNlMeansDenoising + Hough), OCR, and symbol matching are all
    # blocking CPU/subprocess work; offload to threads so this async path (a FastAPI
    # endpoint) doesn't pin the event loop for the whole pack.
    seg = await asyncio.to_thread(find_strips, stair, capture_meta)
    # Bound OCR concurrency: each strip read spawns a Tesseract subprocess over
    # 3x-upscaled variants; a 12-card pack running unbounded peaks near 1GB in a
    # small container. Two or three in flight saturate a cloud vCPU anyway.
    ocr_sem = asyncio.Semaphore(3)

    async def _bounded(fn, *args):
        async with ocr_sem:
            return await asyncio.to_thread(fn, *args)

    readings = list(
        await asyncio.gather(*(_bounded(read_card_number, s.image) for s in seg.strips))
    )
    resolutions = list(
        await asyncio.gather(
            *(_bounded(resolve_set, r, s.image) for r, s in zip(readings, seg.strips))
        )
    )

    art = await _match_art(seg, resolutions)  # None when disabled/unavailable
    art_ids = [a["id"] for a in (art or []) if a]
    from app.cards import get_cached_by_match_ids
    art_payloads = await get_cached_by_match_ids(art_ids) if art_ids else {}

    matches = await lookup_resolved_cards(
        # The keyed lookup wants the card's numerator as the DB stores it. For promo
        # cards (SWSH/SVP) that's the prefixed form "SWSH123" (the DB has no separate
        # denominator); for normal cards it's the bare numerator "012" (NOT "012/202").
        [(_lookup_numerator(r), res) for r, res in zip(readings, resolutions)],
        api_key=get_api_key(),
    )

    cards: list[PackCard] = []
    for i, (strip, reading, res, match) in enumerate(zip(seg.strips, readings, resolutions, matches)):
        art_hit = art[i] if art else None
        payload = art_payloads.get(art_hit["id"]) if art_hit else None
        if art_hit and payload:
            info = payload.get("card_info") or {}
            art_num = str(info.get("card_number") or "")
            ocr_num = _display_number(reading.numerator, reading.denominator, reading.prefix)
            agrees = bool(ocr_num) and ocr_num.split("/")[0].lstrip("0") == art_num.split("/")[0].lstrip("0")
            conf = 0.97 if agrees else max(0.9 * art_hit["score"], 0.75)
            reason = None if agrees or not ocr_num else "art_ocr_disagree"
            cards.append(PackCard(
                row_index=strip.row_index,
                card_number=art_num or ocr_num,
                set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
                confidence=round(conf, 3), low_confidence_reason=reason,
                **card_fields_from_match(payload),
            ))
            continue
        conf, reason = score_card(reading, res, match is not None)
        cards.append(PackCard(  # unchanged OCR-first path
            row_index=strip.row_index,
            card_number=_display_number(reading.numerator, reading.denominator,
                                        reading.prefix),
            set_id=res.set_id,
            set_code=res.set_code,
            set_name=res.set_name,
            confidence=conf,
            low_confidence_reason=reason,
            **card_fields_from_match(match),
        ))

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
