"""Shared merge of one VLM answer into a PackCard.

Both the batch pack pipeline (`pipeline._vlm_fallback`) and the live-scan session
store (`live_session`) hand still-uncertain cards to the RunPod VLM worker and
fold the definitive ID back in the SAME way — number/denominator, set-name → set
resolution via the denominator table, then a keyed re-lookup for name/rarity/
image. One implementation so the two paths can never drift."""
from __future__ import annotations

import logging

from app.cards import cached_lookup_card
from app.pack.matching import card_fields_from_match
from app.pack.set_resolution import DenominatorTable
from app.pokewallet import get_api_key
from app.schemas import PackCard

log = logging.getLogger("pokemon_scanner.pack.vlm_merge")

VLM_ACCEPT = 0.7


async def apply_vlm_answer(card: PackCard, ans: dict, table: DenominatorTable) -> bool:
    """Merge one VLM answer into ``card`` in place (number, set, re-lookup name/
    rarity/image/match_id). Returns True when accepted (confidence >= VLM_ACCEPT —
    also clears needs_review/low_confidence_reason and raises confidence). A
    missing or number-less answer is a no-op returning False, so the card keeps
    its Phase-1 identity."""
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
    if set_id and num.isdigit():
        try:
            m = await cached_lookup_card(set_id, num, api_key=get_api_key())
            if m:
                for k, v in card_fields_from_match(m).items():
                    setattr(card, k, v)
        except Exception as e:
            log.warning("vlm.relookup_failed err=%r", e)
    if float(ans.get("confidence") or 0) >= VLM_ACCEPT:
        card.needs_review = False
        card.low_confidence_reason = None
        card.confidence = max(card.confidence, float(ans["confidence"]))
        return True
    return False
