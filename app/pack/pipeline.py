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
from app.pack import scan_followup
from app.pack.confidence import pack_confidence, score_card
from app.pack.matching import card_fields_from_match, lookup_resolved_cards
from app.pack.ocr import cached_read_code_card, read_card_number
from app.pack.segmentation import find_strips
from app.pack.set_resolution import catalog_local_id, entry_for_set_id, resolve_set
from app.pokewallet import get_api_key
from app.schemas import CodeCardResult, PackCard, PackScanResponse
from app.timing import new_scan_id, stage

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


async def _apply_constraints(readings, resolutions):
    """Snap denominators to the pack's canonical value and correct numerators
    against the resolved set's catalog. Best-effort: any failure is a no-op.
    Returns ``(valid_numerators, modal_set_entry)`` — the entry is needed to
    compare a numerator in that set's own local_id form (see _needs_review)."""
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
            from app.cards import get_set_numerators

            # A set we hold no numerator catalog for (not in set_id_map, or not
            # ingested — a promo set before scripts/build_id_maps.py has run) must
            # NOT win this vote: an empty result silently disables numerator
            # correction AND the _needs_review catalog check for EVERY card in the
            # pack, so one such cell would un-review the whole page. Walk the
            # candidates in count order and take the first DOMINANT set that
            # actually has a catalog. most_common() is descending, so once a
            # candidate misses the dominance bar nothing after it can clear it.
            # The bar is still measured against ALL votes on purpose: it is what
            # stops correct_numerators from snapping a mixed pack's readings onto a
            # minority set's catalog. If no dominant set has a catalog we validate
            # nothing — the same outcome as before, never a wrong correction.
            for candidate, n in Counter(set_ids).most_common():
                if n < max(2, (len(set_ids) + 1) // 2):
                    break
                valid = await get_set_numerators(candidate)
                if valid:
                    correct_numerators(readings, valid)
                    return valid, entry_for_set_id(candidate)
                log.warning("pipeline.modal_set_no_catalog set=%s cards=%s "
                            "(no numerator catalog — skipped for the vote)",
                            candidate, n)
    except Exception as e:
        log.warning("pipeline.constraints_failed err=%r", e)
    return set(), None


def _needs_review(reading, res, valid_nums: set[str], modal_entry) -> bool:
    """A card is confidently identified when its number reads cleanly, its set
    resolves, and (when we have the set catalog) its numerator is a real card in
    that set. Independent of the DB lookup, which only adds name/price — a clean
    number IS the identity. ``valid_nums``/``modal_entry`` come from
    _apply_constraints; an empty ``valid_nums`` means "no catalog to check
    against", which is why the modal-set vote refuses to elect such a set."""
    if not reading.pattern_ok or reading.blank or not res.set_id:
        return True
    if valid_nums and reading.numerator and reading.numerator.isdigit():
        # Compare in the modal set's OWN local_id form. A promo number is read as a
        # prefix plus digits, and swshp's catalog keeps the prefix ("SWSH123") while
        # svp/mep drop it ("037") — comparing bare digits against swshp's catalog
        # would reject every genuine swshp card. Only convert when the read prefix
        # and the modal set agree: a promo read in a different set's pack (or a
        # normal read in a promo pack) must NOT be re-shaped into a form that
        # accidentally matches, so it keeps the plain numerator and fails as before.
        from app.cards import normalize_local_id   # deferred: pulls in the DB layer

        read_prefix = (reading.prefix or "").upper()
        modal_prefix = (modal_entry.promo_prefix or "").upper() if modal_entry else ""
        entry = modal_entry if read_prefix == modal_prefix else None
        lid = catalog_local_id(entry, f"{read_prefix}{reading.numerator}")
        return normalize_local_id(lid) not in valid_nums
    return False


def _vlm_payload(cards, strips, resolutions) -> list[dict]:
    """The VLM request body for the still-uncertain cards, encoded UP FRONT.

    Blocking (cv2 JPEG encode per flagged strip) — callers run it in a thread.
    Encoding here rather than inside ``identify`` is what lets the background
    follow-up retain a few KB of base64 per flagged row instead of pinning
    full-width strips of a 12MP staircase photo for the whole RunPod round trip."""
    from app.pack import vlm_client
    from app.pack.set_resolution import load_denominator_table

    idx = [i for i, c in enumerate(cards) if c.needs_review]
    if not idx:
        return []
    table = load_denominator_table()
    set_ids = [r.set_id for r in resolutions if r.set_id]
    hint_set = hint_den = None
    if set_ids:
        modal = Counter(set_ids).most_common(1)[0][0]
        e = next((s for s in table.sets if s.set_id == modal), None)
        if e:
            hint_set = e.set_name
            hint_den = e.denominators[0] if len(e.denominators) == 1 else None
    payload = []
    for i in idx:
        b64 = vlm_client.jpeg_b64(strips[i].image)
        if b64 is None:
            continue
        # kind="strip": these ARE bottom number-row bands (segmentation.find_strips
        # / detect_first crop a band around the printed number), which is what the
        # worker's default prompt describes.
        payload.append({"row_index": cards[i].row_index, "image_b64": b64,
                        "hint_set": hint_set, "hint_denominator": hint_den,
                        "kind": "strip"})
    return payload


async def _merge_vlm(cards, payload, texts_by_row) -> None:
    """Send the prebuilt batch to the RunPod worker and merge definitive IDs back
    into ``cards`` (number, set, re-lookup name/price). Best-effort — any failure
    leaves the Phase-1 cards untouched.

    ``cards`` are the follow-up task's OWN copies (see ``_start_vlm_followup``):
    the objects in the HTTP response must never be mutated behind the client's
    back, and FastAPI may still be serializing them when this runs."""
    from app.pack import vlm_client
    try:
        from app.pack.set_resolution import load_denominator_table

        table = load_denominator_table()
        result = await vlm_client.identify(payload)
        if not result:
            return

        from app.pack.vlm_merge import apply_vlm_answer, collapse_duplicate_answers

        # Zero out 3+ identical (number, den) claims before merge — a page-wide
        # hallucination signature (e.g. "126/167" answered for unrelated crops).
        result = collapse_duplicate_answers(result)

        for card in cards:
            # Corroborate the claimed number against this strip's own OCR text
            # (NumberReading.raw). Empty when OCR read nothing -> pass None so the
            # corroboration check is skipped for that card (unchanged behavior).
            await apply_vlm_answer(card, result.get(card.row_index) or {}, table,
                                   ocr_texts=texts_by_row.get(card.row_index))
        log.info("vlm.fallback applied cards=%s", len(cards))
    except Exception as e:
        log.warning("pipeline.vlm_fallback_failed err=%r", e)


async def _vlm_followup(followup_id: str, cards: list[PackCard], payload: list[dict],
                        texts_by_row: dict[int, list[str] | None],
                        scan_id: str | None) -> None:
    """The background half of a pack scan: the VLM batch that used to be awaited
    inside the request. Every card it owns ends TERMINAL — ``ok`` or
    ``vlm_failed`` — and ``finish`` runs in a ``finally`` so a crash or a 90s
    timeout still stops the client's poll instead of spinning it forever."""
    try:
        with stage("pack", "vlm", scan_id):
            await _merge_vlm(cards, payload, texts_by_row)
    finally:
        resolved = 0
        # finish() is itself in a finally: a patch that raises (a card that won't
        # serialize, say) must not be able to strand the client's poll on a
        # forever-pending row.
        try:
            for card in cards:
                card.state = "vlm_failed" if card.needs_review else "ok"
                resolved += card.state == "ok"
                scan_followup.patch(followup_id, card.row_index, card.model_dump())
        finally:
            scan_followup.finish(followup_id)
            log.info("pipeline.followup_done id=%s scan=%s cards=%s resolved=%s",
                     followup_id, scan_id, len(cards), resolved)


async def _start_vlm_followup(cards: list[PackCard], strips, resolutions, readings,
                              scan_id: str | None) -> str | None:
    """Hand the still-uncertain cards to a background VLM drain; return the
    follow-up id the client polls, or None for "nothing to follow up" — every case
    the old awaited call would itself have no-op'd on (no worker configured, no
    flagged card, nothing encodable). None means the response is byte-identical to
    the pre-follow-up one, states and all.

    Sets ``state`` on the response cards (``pending_vlm`` on the flagged rows,
    ``ok`` on the rest) and seeds the follow-up entry from that same snapshot, so
    the first poll already agrees with the response the client is holding."""
    from app.pack import vlm_client
    if not vlm_client.enabled() or not any(c.needs_review for c in cards):
        return None
    # Threaded: N cv2 JPEG encodes is real CPU, and it is the one piece of VLM
    # work that stays on the request path.
    payload = await asyncio.to_thread(_vlm_payload, cards, strips, resolutions)
    if not payload:
        return None
    pending_rows = {p["row_index"] for p in payload}
    for c in cards:
        c.state = "pending_vlm" if c.row_index in pending_rows else "ok"
    # The task gets deep COPIES so its merge can never mutate the response's own
    # cards (FastAPI serializes those after scan_pack returns), and the OCR texts
    # it needs for the corroboration guard, keyed by row rather than by position.
    mine = [c.model_copy(deep=True) for c in cards if c.row_index in pending_rows]
    texts_by_row = {c.row_index: ([readings[i].raw] if readings[i].raw else None)
                    for i, c in enumerate(cards) if c.row_index in pending_rows}
    followup_id = scan_followup.create("pack", [c.model_dump() for c in cards])
    scan_followup.spawn(
        followup_id, _vlm_followup(followup_id, mine, payload, texts_by_row, scan_id))
    log.info("pipeline.followup id=%s scan=%s cards=%s", followup_id, scan_id,
             len(payload))
    return followup_id


async def scan_pack(
    staircase_bytes: bytes,
    code_bytes: bytes,
    capture_meta: dict | None,
    *,
    progress: Callable[[dict], None] | None = None,
) -> PackScanResponse:
    def _emit(ev: dict) -> None:
        # Fire-and-forget: defensive against any put error (the callback must
        # never break or block a scan). Default None skips this entirely, so
        # the no-callback path is byte-identical to before this param existed.
        if progress is None:
            return
        try:
            progress(ev)
        except Exception as e:
            log.warning("pipeline.progress_callback_failed err=%r", e)

    # One correlation id per scan, threaded through this function's `stage` calls
    # only — the shared helpers stay un-signature-changed so the live/binder flows
    # that reuse them are untouched.
    scan_id = new_scan_id()

    with stage("pack", "total", scan_id):
        with stage("pack", "decode", scan_id):
            # A 12MP HEIC decode is 100s of ms of pure CPU; on the loop it stalls
            # every other request in the process (see the thread offload rationale
            # below, which applies just as much to decoding as to OCR).
            stair = await asyncio.to_thread(_decode, staircase_bytes)
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
            with stage("pack", "detect_first", scan_id):
                df = await asyncio.to_thread(detect_first, stair)
            if df is not None:
                strips, readings = df
        if strips is None:
            with stage("pack", "find_strips", scan_id):
                seg = await asyncio.to_thread(find_strips, stair, capture_meta)
            strips = seg.strips
            seg_warning = seg.warning
            with stage("pack", "read_numbers", scan_id):
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

        with stage("pack", "resolve_set", scan_id):
            resolutions = list(
                await asyncio.gather(
                    *(_resolve_with_progress(r, s) for r, s in zip(readings, strips))
                )
            )

        # Pack-level constraint repair: cards in one pack share a denominator and
        # their numerators exist in the set catalog — priors that fix OCR glyph
        # confusions the reader can't. Corrects readings in place before lookup.
        with stage("pack", "constraints", scan_id):
            valid_nums, modal_entry = await _apply_constraints(readings, resolutions)

        with stage("pack", "art_match", scan_id):
            art = await _match_art(strips, resolutions)  # None when disabled/unavailable
        art_ids = [a["id"] for a in (art or []) if a]
        from app.cards import get_cached_by_match_ids
        art_payloads = await get_cached_by_match_ids(art_ids) if art_ids else {}

        with stage("pack", "lookup", scan_id):
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
                needs_review=_needs_review(reading, res, valid_nums, modal_entry),
                **card_fields_from_match(match),
            ))

        # Confidence-gated VLM fallback: only the still-uncertain cards go to the
        # RunPod worker for definitive ID. Off when VLM_ENDPOINT unset. NON-BLOCKING
        # since the follow-up store landed: awaiting it here put a serverless cold
        # start (90s timeout) inside the HTTP response for exactly the packs that
        # read worst. The response now ships the Phase-1 cards with
        # state="pending_vlm" on the flagged rows plus a scan_id, and one background
        # task patches the follow-up entry the client polls. The `vlm` timing stage
        # moved with the work — it now measures the background call.
        with stage("pack", "vlm_dispatch", scan_id):
            followup_id = await _start_vlm_followup(cards, strips, resolutions,
                                                    readings, scan_id)

        with stage("pack", "code_ocr", scan_id):
            # The single heaviest blocking call in a scan (QR pass + up to ~6 serial
            # Tesseract subprocesses), so it belongs in a thread — and behind the same
            # OCR_GATE as every other Tesseract call, or N concurrent scans could each
            # fork ~6 subprocesses and blow the small container's memory. Like the
            # other _bounded stages (read_numbers, resolve_set), this stage's timing
            # therefore includes any gate queueing. Reading through the memo also lets
            # the save path (POST /pulls) reuse this exact reading instead of OCR'ing
            # the same photo a second time.
            code_img = await asyncio.to_thread(_decode, code_bytes)
            if code_img is None:
                code_result = CodeCardResult(code=None, confidence=0.0, format_ok=False)
            else:
                cr = await _bounded(cached_read_code_card, code_bytes, code_img)
                code_result = CodeCardResult(code=cr.code, confidence=round(cr.confidence, 3),
                                             format_ok=cr.format_ok)

        resp = PackScanResponse(
            cards=cards,
            code_card=code_result,
            # Phase-1 confidence: a background VLM merge can only raise a card's
            # confidence, so this is the floor, not the final word, when a
            # follow-up is running (the poll carries the per-card truth).
            pack_confidence=pack_confidence([c.confidence for c in cards]),
            segmentation_warning=seg_warning,
            scan_id=followup_id,
        )
        log.info("pipeline.done rows=%s flagged=%s pack_conf=%.3f code=%s followup=%s",
                 len(cards), sum(1 for c in cards if c.low_confidence_reason),
                 resp.pack_confidence, code_result.code, followup_id or "-")
        _emit({"stage": "done"})
        return resp
