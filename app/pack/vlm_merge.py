"""Shared merge of one VLM answer into a PackCard.

Both the batch pack pipeline (`pipeline._merge_vlm`) and the live-scan session
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
from app.pack.name_index import get_name_index, normalize_name
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
    stays flagged — EXCEPT when the name cross-check refuses a number-first
    identity, which is rolled all the way back, because a self-contradictory
    identity is worse than none. A missing or number-less answer is a no-op
    returning False, so the card keeps its Phase-1 identity.

    ``ocr_texts`` is the cell/card's own OCR'd lines (uppercase). When None (a
    caller that can't supply it) the corroboration check is skipped — behavior is
    unchanged. ``ans`` may carry a "name" (newer worker); when absent the name
    cross-check is skipped."""
    if not ans or not (ans.get("number") or ans.get("name")):
        return False
    num = str(ans.get("number") or "").split("/")[0].strip()
    den = ans.get("denominator")
    vlm_name = str(ans.get("name") or "").strip()

    # NAME-FIRST resolution. Production logs: on full-art cards the VLM reads the
    # large printed NAME near-verbatim but fabricates the tiny gold collector
    # number ('Mega Latias ex' claimed with a number resolving to Skitty). So when
    # the worker supplies a name, identity comes from OUR name index — the VLM
    # denominator narrows, and the CATALOG supplies the true number. The number-first
    # path below remains for nameless (older-worker) answers.
    #
    # When the two claims contradict each other the merge keeps the one this
    # worker does not fabricate: an EXACT normalized-key name hit outranks the
    # claimed denominator (see the polarity rule at the filter below), and a
    # number-first identity that the name cross-check later refuses is rolled
    # back rather than displayed.
    name_resolved = False
    name_display_only = False
    if vlm_name:
        try:
            idx = await get_name_index()
            # Candidates for the claimed name: exact normalized key first, fuzzy
            # (unambiguous only) as fallback for near-miss reads.
            cands = list(idx._entries.get(normalize_name(vlm_name)) or [])
            exact_hit = bool(cands)
            if not cands:
                fz = idx.match(vlm_name, denominator=str(den) if den else None)
                if fz is not None and not fz.ambiguous:
                    cands = list(idx._entries.get(normalize_name(fz.card_name)) or [])
            # Denominator filter — with a POLARITY rule. Keep candidates whose set
            # official count matches the claimed denominator; when the claimed
            # denominator contradicts EVERY candidate, one of the two claims is
            # fabricated, and which one depends on how the name was hit:
            #   * EXACT normalized-key hit: a multi-token printed name read
            #     verbatim is the strong evidence; the denominator is the field
            #     this worker demonstrably fabricates (the Mega Latias ex /
            #     "130/162" -> Skitty production case). Discard the denominator,
            #     keep the name.
            #   * FUZZY hit: the name itself is a guess about a guess — the
            #     original veto stands and the name path is abandoned.
            if cands and den is not None and str(den).isdigit():
                den_i = int(str(den))
                with_den = [c for c in cands if c[4] == den_i]
                if with_den:
                    cands = with_den
                elif all(c[4] is not None and c[4] != den_i for c in cands):
                    if exact_hit:
                        # Discarding the denominator also discards the printings
                        # it was the only thing keeping in play: a Black Star
                        # promo set prints no count at all (official 0, no
                        # denominators in the table), so it can neither
                        # corroborate nor contradict the claim — and leaving it
                        # in splits the same-set variant test below across two
                        # sets, which is exactly how 'Mega Latias ex' (me01 x3 +
                        # mep x1) fell through to the number-first path anyway.
                        cands = [c for c in cands if c[4]] or cands
                        log.info("vlm.den_discarded name=%r den=%s row=%s "
                                 "(exact name hit outweighs claimed denominator)",
                                 vlm_name, den, getattr(card, "row_index", None))
                    else:
                        cands = []
            m = None
            display_only = None
            if len(cands) == 1:
                m = cands[0]
            elif cands and len({c[0] for c in cands}) == 1:
                # Same-set variants (regular vs secret print share the name):
                # the claimed numerator picks the variant — that agreement is
                # real corroboration. No match -> set+name shown, stays flagged.
                claim_n = (num.lstrip("0") or num).upper() if num else ""
                hits = [c for c in cands
                        if (str(c[2]).lstrip("0") or str(c[2])).upper() == claim_n]
                m = hits[0] if len(hits) == 1 else None
                display_only = cands[0] if m is None else None
            if m is None and display_only is not None:
                name_display_only = True
                s_id, s_name, lid, cname, _o = display_only
                entry = next((s for s in table.sets
                              if (s.tcgdex_id or s.set_code) == s_id), None)
                card.set_id = entry.set_id if entry else None
                card.set_code = entry.set_code if entry else s_id
                card.set_name = s_name
                card.name = cname
            if m is not None:
                m_set_id, m_set_name, m_local, m_card_name, _official = m
                entry = next((s for s in table.sets
                              if (s.tcgdex_id or s.set_code) == m_set_id), None)
                card.set_id = entry.set_id if entry else None
                card.set_code = entry.set_code if entry else m_set_id
                card.set_name = m_set_name
                card.name = m_card_name
                num = str(m_local).lstrip("0") or str(m_local)  # catalog truth
                # Display the catalog set's printed denominator, not the
                # (possibly fabricated) claimed one.
                disp_den = (entry.denominators[0] if entry and entry.denominators
                            else (str(den) if den else None))
                card.card_number = f"{m_local}/{disp_den}" if disp_den else str(m_local)
                name_resolved = True
                # Catalog image straight from TCGdex (PokéWallet may not
                # carry the set; the keyed lookup below fills the rest).
                if card.image_url is None:
                    try:
                        async with async_session_maker() as session:
                            row = (await session.execute(
                                select(TcgdexCard.image_base)
                                .where(TcgdexCard.set_id == m_set_id,
                                       TcgdexCard.local_id == str(m_local)))).first()
                        if row and row.image_base:
                            card.image_url = row.image_base + "/high.png"
                    except Exception:
                            pass
        except Exception as e:
            log.warning("vlm.name_first_failed err=%r", e)

    # Number-first merges are provisional: if the worker ALSO read a printed
    # name and that name disagrees with the card the number resolves to, the
    # merge is self-contradictory — displaying it anyway is how a flagged cell
    # showed "Skitty" over a Mega Latias ex. Snapshot here; the cross-check
    # below decides whether the writes stand.
    before = card.model_copy() if not name_resolved else None

    # Everything from here to the re-lookup is number-keyed, so a display-only
    # name hit is TERMINAL for identity fields: its name/set came from the name
    # index and the claimed number is the field under suspicion.
    if not name_resolved:
        if not num:
            return False
        if not name_display_only:
            card.card_number = f"{num}/{den}" if den else num
    set_id = card.set_id
    if not name_resolved and not name_display_only and ans.get("set_name"):
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
    # (Never overrides a name-resolved identity — the claimed den may be fabricated.)
    if not name_resolved and not name_display_only \
            and (set_id is None or card.set_name is None) and den is not None:
        entries = table.by_denominator.get(str(den).lstrip("0") or "0", ())
        if len(entries) == 1:
            e = entries[0]
            card.set_id, card.set_code, card.set_name = e.set_id, e.set_code, e.set_name
            set_id = e.set_id
    if set_id and num.isdigit() and not name_display_only:
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
    # Pixel corroboration applies to NUMBER-claimed identities only. A
    # name-resolved identity is anchored on the catalog name + denominator veto;
    # its number came from the catalog, so OCR misreads must not block it.
    corroborated = True
    if ocr_texts and not name_resolved:
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

    # A refused number-first identity leaves no fingerprints. Name-resolved
    # merges take no snapshot at all, and a display-only merge is cross-checking
    # a name against the catalog name that name itself selected — so the only
    # writes this can undo are the number-keyed ones.
    if before is not None and not name_ok:
        for f in type(card).model_fields:
            setattr(card, f, getattr(before, f))
        log.info("vlm.merge_rolled_back vlm=%r row=%s (number-first identity "
                 "contradicted the worker's own name read)",
                 vlm_name, getattr(card, "row_index", None))
        return False

    if float(ans.get("confidence") or 0) >= VLM_ACCEPT \
            and (card.set_id is not None or (name_resolved and card.set_code)) \
            and card.name is not None and corroborated and name_ok \
            and not name_display_only:
        card.needs_review = False
        card.low_confidence_reason = None
        card.confidence = max(card.confidence, float(ans["confidence"]))
        return True
    return False
