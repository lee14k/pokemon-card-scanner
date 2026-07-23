"""Shared merge of one VLM answer into a PackCard.

Both the batch pack pipeline (`pipeline._vlm_fallback`) and the live-scan session
store (`live_session`) hand still-uncertain cards to the RunPod VLM worker and
fold the definitive ID back in the SAME way — number/denominator, set-name → set
resolution via the denominator table, then a keyed re-lookup for name/rarity/
image. One implementation so the two paths can never drift."""
from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz
from sqlalchemy import select

from app.cards import cached_lookup_card
from app.db.models import TcgdexCard
from app.db.session import async_session_maker
from app.pack.matching import card_fields_from_match
from app.pack.name_index import normalize_name
from app.pack.set_resolution import DenominatorTable
from app.pokewallet import get_api_key
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.pack.vlm_merge")

VLM_ACCEPT = 0.7
NAME_MATCH_MIN = 75  # rapidfuzz WRatio floor for VLM-name vs catalog-name agreement


def _num_den_key(ans: dict) -> tuple[str, str | None] | None:
    """(numerator, denominator) identity of one answer, or None when it has no
    number. Numerator is stripped of any "/den" tail exactly like apply's merge."""
    if not ans or not ans.get("number"):
        return None
    num = str(ans["number"]).split("/")[0].strip()
    den = ans.get("denominator")
    return (num, str(den) if den is not None else None)


def collapse_duplicate_answers(answers: dict[int, dict]) -> dict[int, dict]:
    """Hallucination guard applied to one VLM batch BEFORE merge: when the SAME
    (number, denominator) pair is claimed for 3+ distinct rows, the VLM is almost
    certainly repeating one plausible number across unrelated crops (a real binder
    page rarely holds 3+ identical cards). Zero the ``confidence`` on every one of
    those answers so the confidence gate in ``apply_vlm_answer`` refuses to clear
    needs_review. Mutates and returns the same dict; a batch with no 3+ duplicate
    is returned untouched."""
    counts: dict[tuple[str, str | None], int] = {}
    for ans in answers.values():
        k = _num_den_key(ans)
        if k is not None:
            counts[k] = counts.get(k, 0) + 1
    dup = {k for k, n in counts.items() if n >= 3}
    if dup:
        for ans in answers.values():
            if _num_den_key(ans) in dup:
                ans["confidence"] = 0.0
    return answers


def _numerator_corroborated(num: str, ocr_texts: list[str]) -> bool:
    """CONTRADICTION test, not a presence test. The VLM exists to read numbers
    OCR could not, so silence in the cell's OCR must never block an answer —
    demanding presence rejected correct reads on every foil/full-art cell
    (production evidence). The claim is refused only when the cell's own OCR
    READ a collector-number pattern (N/N) and the claimed numerator matches
    none of them — that is real pixel evidence against the VLM (the garbage
    fragments in the original hallucination case read "12/198" against a
    claimed "126/167", so they still die here). A claim appearing verbatim in
    any line also passes, covering partial strip reads."""
    from app.pack.ocr import parse_number

    claim = re.sub(r"[^A-Za-z0-9]", "", num or "").upper()
    if not claim:
        return False
    seen_numerators: set[str] = set()
    for t in ocr_texts:
        flat = re.sub(r"[^A-Za-z0-9]", "", str(t or "")).upper()
        if claim in flat:
            return True
        r = parse_number(str(t or ""), 0.9)
        if r is not None and r.pattern_ok and r.numerator:
            seen_numerators.add(r.numerator.upper().lstrip("0") or "0")
    if not seen_numerators:
        return True                       # OCR was blind here: no contradiction
    return (claim.lstrip("0") or "0") in seen_numerators


async def apply_vlm_answer(card: PackCard, ans: dict, table: DenominatorTable,
                           *, ocr_texts: list[str] | None = None) -> bool:
    """Merge one VLM answer into ``card`` in place (number, set, re-lookup name/
    rarity/image/match_id). Returns True only when an identity was actually
    produced AND survived the corroboration guards — confidence >= VLM_ACCEPT,
    both set_id and name populated, the claimed numerator corroborated by the
    cell's own OCR text (when ``ocr_texts`` is supplied and non-empty), and the
    VLM's printed name (when present) agreeing with the resolved catalog name —
    in which case it also clears needs_review/low_confidence_reason and raises
    confidence. Otherwise it returns False WITHOUT touching needs_review: the
    best-effort number/set/name is still merged for review display, but the card
    stays flagged. A missing or number-less answer is a no-op returning False, so
    the card keeps its Phase-1 identity.

    ``ocr_texts`` is the cell/card's own OCR'd lines (uppercase). When None (a
    caller that can't supply it) the corroboration check is skipped — behavior is
    unchanged. ``ans`` may carry a "name" (newer worker); when absent the name
    cross-check is skipped."""
    if not ans or not ans.get("number"):
        return False
    num = str(ans["number"]).split("/")[0].strip()
    den = ans.get("denominator")
    card.card_number = f"{num}/{den}" if den else num
    set_id = card.set_id
    if ans.get("set_name"):
        sn = str(ans["set_name"]).casefold()
        match = next((s for s in table.sets if s.set_name.casefold() == sn), None) or \
            next((s for s in table.sets
                  if sn in s.set_name.casefold() or s.set_name.casefold() in sn), None)
        if match:
            card.set_id, card.set_code, card.set_name = \
                match.set_id, match.set_code, match.set_name
            set_id = match.set_id
    # The VLM can't name sets released after its training cutoff (set_name=null),
    # so fall back to a unique denominator to pin the set. Keys are stored stripped.
    if (set_id is None or card.set_name is None) and den is not None:
        entries = table.by_denominator.get(str(den).lstrip("0") or "0", ())
        if len(entries) == 1:
            e = entries[0]
            card.set_id, card.set_code, card.set_name = e.set_id, e.set_code, e.set_name
            set_id = e.set_id
    if set_id and num.isdigit():
        try:
            m = await cached_lookup_card(set_id, num, api_key=get_api_key())
            if m:
                for k, v in card_fields_from_match(m).items():
                    setattr(card, k, v)
        except Exception as e:
            log.warning("vlm.relookup_failed err=%r", e)
    # me-era sets aren't in PokéWallet, so the re-lookup above finds nothing; pull
    # name/image straight from the TCGdex catalog keyed by the resolved set.
    if card.name is None and set_id is not None and num.isdigit():
        entry = next((s for s in table.sets if s.set_id == set_id), None)
        tdx = (entry.tcgdex_id or entry.set_code) if entry else None
        if tdx:
            try:
                async with async_session_maker() as session:
                    row = (await session.execute(
                        select(TcgdexCard.name, TcgdexCard.image_base)
                        .where(TcgdexCard.set_id == tdx,
                               TcgdexCard.local_id.in_((num, num.zfill(3)))))).first()
                if row and row.name:
                    card.name = row.name
                    if card.image_url is None and row.image_base:
                        card.image_url = row.image_base + "/high.png"
            except Exception as e:
                log.warning("vlm.tcgdex_fallback_failed err=%r", e)
    # Pixel corroboration: the claimed numerator must actually appear in the
    # cell's own OCR text. Only enforced when the caller supplies OCR lines.
    corroborated = True
    if ocr_texts:
        corroborated = _numerator_corroborated(num, ocr_texts)
        if not corroborated:
            log.info("vlm.uncorroborated num=%s row=%s (kept flagged)",
                     num, getattr(card, "row_index", None))

    # Name cross-check: when the worker returns a printed name and we resolved a
    # catalog name, they must agree (fuzzy). Old workers omit "name" -> skipped.
    name_ok = True
    vlm_name = str(ans.get("name") or "").strip()
    if vlm_name and card.name:
        name_ok = fuzz.WRatio(normalize_name(vlm_name),
                              normalize_name(card.name)) >= NAME_MATCH_MIN
        if not name_ok:
            log.info("vlm.name_mismatch vlm=%r catalog=%r row=%s (kept flagged)",
                     vlm_name, card.name, getattr(card, "row_index", None))

    if float(ans.get("confidence") or 0) >= VLM_ACCEPT \
            and card.set_id is not None and card.name is not None \
            and corroborated and name_ok:
        card.needs_review = False
        card.low_confidence_reason = None
        card.confidence = max(card.confidence, float(ans["confidence"]))
        return True
    return False
