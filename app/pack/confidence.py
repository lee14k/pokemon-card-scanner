"""Per-card confidence + low-confidence reasons. Threshold T comes from config."""

from __future__ import annotations

import logging

from app.pack.config import settings
from app.pack.ocr import NumberReading
from app.pack.set_resolution import SetResolution

log = logging.getLogger("pokemon_scanner.pack.confidence")

# Reasons (spec contract): unreadable_strip | number_ambiguous | set_ambiguous | no_db_match

_SET_METHOD_SCORE = {
    "promo_prefix": 1.0,
    "code_text": 1.0,
    "denominator_unique": 0.95,
    "unresolved": 0.0,
}


def _set_score(res: SetResolution) -> float:
    if res.method == "symbol_tiebreak":
        # Margin-scaled: margin >= 8 bits is decisive, 0 is a coin flip.
        m = res.margin if res.margin is not None else 0
        return 0.5 + 0.4 * min(1.0, m / 8.0)
    return _SET_METHOD_SCORE.get(res.method, 0.0)


def score_card(
    number: NumberReading, set_res: SetResolution, match_found: bool
) -> tuple[float, str | None]:
    """Returns (confidence 0..1, low_confidence_reason or None)."""
    if number.blank:
        return 0.0, "unreadable_strip"
    if not number.pattern_ok:
        return 0.05, "number_ambiguous"

    sset = _set_score(set_res)
    conf = 0.5 * number.confidence + 0.3 * sset + 0.2 * (1.0 if match_found else 0.0)
    conf = round(min(1.0, max(0.0, conf)), 3)

    reason: str | None = None
    if conf < settings().confidence_threshold:
        if set_res.method == "unresolved":
            reason = "set_ambiguous"
        elif not match_found:
            reason = "no_db_match"
        elif number.confidence < 0.6:
            reason = "number_ambiguous"
        else:
            reason = "set_ambiguous"
    log.info("confidence.card conf=%.3f reason=%s (ocr=%.2f set=%.2f match=%s)",
             conf, reason, number.confidence, sset, match_found)
    return conf, reason


def pack_confidence(card_confidences: list[float]) -> float:
    if not card_confidences:
        return 0.0
    return round(sum(card_confidences) / len(card_confidences), 3)
