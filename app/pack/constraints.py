"""Pack-level constraint repair of per-strip number readings.

A pack shares one set, so its cards share a denominator and their numerators
all exist in that set's catalog. These priors correct OCR glyph confusions
(e.g. a misread "045/187" -> "045/167", or "066" -> "068" only when the set has
068 and not 066) far more reliably than any bigger model, because the model has
no such priors. Conservative: only unambiguous corrections are applied.
"""
from __future__ import annotations

import logging
from collections import Counter

log = logging.getLogger("pokemon_scanner.pack.constraints")


def modal_denominator(readings) -> str | None:
    """The pack's dominant numeric denominator, when a clear majority exists."""
    dens = [r.denominator for r in readings
            if r.denominator and r.denominator.isdigit()]
    if not dens:
        return None
    den, count = Counter(dens).most_common(1)[0]
    return den if count >= max(2, (len(dens) + 1) // 2) else None


def snap_denominators(readings, canonical: str) -> int:
    """Set each real (non-promo) number reading's denominator to `canonical`
    when it differs — corrects the frequent last-digit denominator misread.
    Returns the number of corrections."""
    n = 0
    for r in readings:
        if r.prefix:  # promo: no denominator
            continue
        if r.numerator and r.denominator and r.denominator != canonical:
            log.info("constraints.denominator %s/%s -> %s/%s",
                     r.numerator, r.denominator, r.numerator, canonical)
            r.denominator = canonical
            n += 1
    return n


def _unique_edit1(num: str, valid: set[str]) -> str | None:
    """A valid number one same-length digit-substitution from `num`, unique."""
    if num in valid:
        return num
    cands = [v for v in valid
             if len(v) == len(num) and sum(a != b for a, b in zip(v, num)) == 1]
    return cands[0] if len(cands) == 1 else None


def correct_numerators(readings, valid: set[str]) -> int:
    """Snap a pure-numeric numerator to a unique single-digit-off catalog number
    when the OCR'd one isn't in the set. `valid` = normalized (no leading zero)
    numerators of the resolved set. Returns the number of corrections."""
    if not valid:
        return 0
    n = 0
    for r in readings:
        if r.prefix or not r.numerator or not r.numerator.isdigit():
            continue
        num = r.numerator.lstrip("0") or "0"
        fix = _unique_edit1(num, valid)
        if fix and fix != num:
            log.info("constraints.numerator %s -> %s (set catalog)", num, fix)
            r.numerator = fix
            n += 1
    return n
