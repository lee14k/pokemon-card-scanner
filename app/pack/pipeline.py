"""scan_pack(): staircase + code card bytes → PackScanResponse."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from collections import Counter
from typing import Callable

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

# Global OCR admission gate — shared by pack scans AND live-frame OCR so
# concurrent scanners can't oversubscribe the (small) Railway CPU.
OCR_GATE = asyncio.Semaphore(int(os.environ.get("OCR_CONCURRENCY", "3")))


async def _match_art(strips, resolutions) -> list[dict | None] | None:
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
    for s in strips:
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


def detect_first(img):
    """Detection-first card finding: PP-OCR's real-photo-trained detector
    localizes and reads every card number across the whole photo; each becomes a
    card with a cropped number-row band (for set resolution / review / save).
    Returns (strips, readings) top->bottom, or None when it finds too few cards
    (caller falls back to Hough segmentation)."""
    from app.pack.ocr import parse_number
    from app.pack.segmentation import Strip
    try:
        from app.pack.rapidocr_reader import detect_lines

        lines = detect_lines(img)
    except Exception as e:
        log.warning("pipeline.detect_first_failed err=%r", e)
        return None

    parsed = [(y, r) for y, text, conf in lines
              if (r := parse_number(text, conf)) is not None and r.pattern_ok]
    # dedup numerators, keeping the highest-confidence read
    by_num: dict[str, tuple[float, object]] = {}
    for y, r in parsed:
        cur = by_num.get(r.numerator)
        if cur is None or r.confidence > cur[1].confidence:
            by_num[r.numerator] = (y, r)
    parsed = sorted(by_num.values(), key=lambda t: t[0])
    if len(parsed) < 3:
        return None

    h, w = img.shape[:2]
    ys = [y for y, _ in parsed]
    gap = float(np.median(np.diff(ys))) if len(ys) > 1 else h * 0.08
    band = max(30, int(gap * 0.95))
    strips, readings = [], []
    for i, (y, r) in enumerate(parsed):
        y0, y1 = max(0, int(y - band * 0.55)), min(h, int(y + band * 0.45))
        if y1 - y0 < 8:
            continue
        strips.append(Strip(row_index=len(strips), image=img[y0:y1, :].copy(),
                            bbox=(0, y0, w, y1 - y0), angle=0.0))
        readings.append(r)
    log.info("pipeline.detect_first cards=%s", len(strips))
    return (strips, readings) if len(strips) >= 3 else None


async def _read_numbers(img, strips, bounded, use_wholephoto: bool):
    """Per-strip OCR for every strip, then (upload path only) fill the strips it
    failed on with PP-OCR's whole-photo number detections — its real-photo-
    trained detector localizes number rows geometric cropping misses. Union
    beats either source; per-strip stays primary so precise crops win."""
    from app.pack.ocr import NumberReading, parse_number

    readings = list(await asyncio.gather(
        *(bounded(read_card_number, s.image) for s in strips)))
    if not use_wholephoto:
        return readings

    boxes: list = []
    try:
        from app.pack.rapidocr_reader import detect_lines

        for y, text, conf in await asyncio.to_thread(detect_lines, img):
            r = parse_number(text, conf)
            if r is not None and r.pattern_ok:
                boxes.append((y, r))
    except Exception as e:
        log.warning("pipeline.wholephoto_failed err=%r", e)
        return readings

    # Fill only the strips per-strip OCR failed on, claiming the nearest unused
    # box (within ~1.5 strip-heights, to tolerate the strip/number misalignment
    # that weak segmentation causes) whose number isn't already read elsewhere.
    have = {r.numerator for r in readings if r.pattern_ok and r.numerator}
    used = set()
    filled = 0
    for i, s in enumerate(strips):
        if readings[i].pattern_ok:
            continue
        _, sy, _, sh = s.bbox
        cy = sy + sh / 2
        best = best_d = None
        for j, (y, r) in enumerate(boxes):
            if j in used or r.numerator in have:
                continue
            d = abs(y - cy)
            if d <= 1.5 * sh and (best_d is None or d < best_d):
                best, best_d = j, d
        if best is not None:
            readings[i] = boxes[best][1]
            used.add(best)
            have.add(boxes[best][1].numerator)
            filled += 1

    # A pack's card numbers are unique: if two strips ended with the same
    # numerator (an OCR false positive), keep the higher-confidence one.
    by_num: dict[str, int] = {}
    for i, r in enumerate(readings):
        if not (r.pattern_ok and r.numerator):
            continue
        j = by_num.get(r.numerator)
        if j is None:
            by_num[r.numerator] = i
        elif readings[i].confidence > readings[j].confidence:
            readings[j] = NumberReading(blank=True); by_num[r.numerator] = i
        else:
            readings[i] = NumberReading(blank=True)

    log.info("pipeline.numbers wholephoto_filled=%s of %s strips", filled, len(strips))
    return readings


async def _apply_constraints(readings, resolutions) -> None:
    """Snap denominators to the pack's canonical value and correct numerators
    against the resolved set's catalog. Best-effort: any failure is a no-op."""
    from collections import Counter

    from app.pack.constraints import (correct_numerators, modal_denominator,
                                      snap_denominators)
    try:
        # Snap only on a real denominator majority — a genuine single-set pack
        # always has one; a mixed-set image (no shared denominator) does not, so
        # it is correctly left alone.
        canonical = modal_denominator(readings)
        if canonical:
            snap_denominators(readings, canonical)
        # Numerator catalog correction against the pack's modal (dominant) set.
        set_ids = [r.set_id for r in resolutions if r.set_id]
        if set_ids:
            modal_set, n = Counter(set_ids).most_common(1)[0]
            if n >= max(2, (len(set_ids) + 1) // 2):  # dominant set only
                from app.cards import get_set_numerators

                valid = await get_set_numerators(modal_set)
                correct_numerators(readings, valid)
                return valid
    except Exception as e:
        log.warning("pipeline.constraints_failed err=%r", e)
    return set()


async def _vlm_fallback(cards, strips, resolutions) -> None:
    """Send still-uncertain cards to the RunPod VLM worker; merge definitive IDs
    back in (number, set, re-lookup name/price). Best-effort — any failure or a
    disabled worker leaves the Phase-1 cards untouched."""
    from app.pack import vlm_client
    if not vlm_client.enabled():
        return
    idx = [i for i, c in enumerate(cards) if c.needs_review]
    if not idx:
        return
    try:
        from app.pack.set_resolution import load_denominator_table

        table = load_denominator_table()
        set_ids = [r.set_id for r in resolutions if r.set_id]
        hint_set = hint_den = None
        if set_ids:
            modal = Counter(set_ids).most_common(1)[0][0]
            e = next((s for s in table.sets if s.set_id == modal), None)
            if e:
                hint_set = e.set_name
                hint_den = e.denominators[0] if len(e.denominators) == 1 else None
        payload = [{"row_index": cards[i].row_index, "image": strips[i].image,
                    "hint_set": hint_set, "hint_denominator": hint_den} for i in idx]
        result = await vlm_client.identify(payload)
        if not result:
            return

        from app.pack.vlm_merge import apply_vlm_answer

        for i in idx:
            card = cards[i]
            await apply_vlm_answer(card, result.get(card.row_index) or {}, table)
        log.info("vlm.fallback applied cards=%s", len(idx))
    except Exception as e:
        log.warning("pipeline.vlm_fallback_failed err=%r", e)


async def scan_pack(
    staircase_bytes: bytes,
    code_bytes: bytes,
    capture_meta: dict | None,
    *,
    progress: Callable[[dict], None] | None = None,
) -> PackScanResponse:
    def _emit(ev: dict) -> None:
        # Fire-and-forget: a broken/slow callback (e.g. a full SSE queue) must
        # never break or block a scan. Default None skips this entirely, so the
        # no-callback path is byte-identical to before this param existed.
        if progress is None:
            return
        try:
            progress(ev)
        except Exception as e:
            log.warning("pipeline.progress_callback_failed err=%r", e)

    stair = _decode(staircase_bytes)
    if stair is None:
        raise ValueError("staircase image could not be decoded")
    _emit({"stage": "decoded"})

    # Segmentation (fastNlMeansDenoising + Hough), OCR, and symbol matching are all
    # blocking CPU/subprocess work; offload to threads so this async path (a FastAPI
    # endpoint) doesn't pin the event loop for the whole pack.
    # Bound OCR concurrency: each strip read spawns a Tesseract subprocess over
    # 3x-upscaled variants; a 12-card pack running unbounded peaks near 1GB in a
    # small container. Two or three in flight saturate a cloud vCPU anyway.

    async def _bounded(fn, *args):
        async with OCR_GATE:
            return await asyncio.to_thread(fn, *args)

    # Detection-first (upload path): PP-OCR detection finds+reads the cards
    # directly. Falls back to Hough segmentation + per-strip OCR for the guided
    # path or when detection finds too few cards.
    strips = readings = None
    seg_warning = None
    if capture_meta is None:
        df = await asyncio.to_thread(detect_first, stair)
        if df is not None:
            strips, readings = df
    if strips is None:
        seg = await asyncio.to_thread(find_strips, stair, capture_meta)
        strips = seg.strips
        seg_warning = seg.warning
        readings = await _read_numbers(stair, strips, _bounded,
                                       use_wholephoto=capture_meta is None)
    _emit({"stage": "cards_found", "count": len(strips)})

    # Per-card "identifying" progress: resolve_set is the natural per-card unit
    # of work remaining after strip/number detection, bounded by OCR_GATE so
    # cards genuinely finish at staggered times (not all at once). asyncio.gather
    # preserves input order in `resolutions` regardless of completion order, so
    # this changes nothing about the result — only adds a side-effect callback.
    _done = 0

    async def _resolve_with_progress(r, s):
        nonlocal _done
        res = await _bounded(resolve_set, r, s.image)
        _done += 1
        _emit({"stage": "identifying", "done": _done, "total": len(strips)})
        return res

    resolutions = list(
        await asyncio.gather(
            *(_resolve_with_progress(r, s) for r, s in zip(readings, strips))
        )
    )

    # Pack-level constraint repair: cards in one pack share a denominator and
    # their numerators exist in the set catalog — priors that fix OCR glyph
    # confusions the reader can't. Corrects readings in place before lookup.
    valid_nums = await _apply_constraints(readings, resolutions)

    def _needs_review(reading, res) -> bool:
        # A card is confidently identified when its number reads cleanly, its set
        # resolves, and (when we have the set catalog) its numerator is a real
        # card in that set. Independent of the DB lookup, which only adds name/
        # price — a clean number IS the identity.
        if not reading.pattern_ok or reading.blank or not res.set_id:
            return True
        if valid_nums and reading.numerator and reading.numerator.isdigit():
            return (reading.numerator.lstrip("0") or "0") not in valid_nums
        return False

    art = await _match_art(strips, resolutions)  # None when disabled/unavailable
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
    for i, (strip, reading, res, match) in enumerate(zip(strips, readings, resolutions, matches)):
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
                needs_review=reason is not None,
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
            needs_review=_needs_review(reading, res),
            **card_fields_from_match(match),
        ))

    # Confidence-gated VLM fallback: send only the still-uncertain cards to the
    # RunPod worker for definitive ID. Off when VLM_ENDPOINT unset; never blocks.
    await _vlm_fallback(cards, strips, resolutions)

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
        segmentation_warning=seg_warning,
    )
    log.info("pipeline.done rows=%s flagged=%s pack_conf=%.3f code=%s",
             len(cards), sum(1 for c in cards if c.low_confidence_reason),
             resp.pack_confidence, code_result.code)
    _emit({"stage": "done"})
    return resp
