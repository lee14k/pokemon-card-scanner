"""Shared identify ladder — the core that both live scan and binder page scan use.

Given candidate title-band name lines plus an optional printed number reading,
resolve a card identity via the decision ladder: name+number agree > unique
name (+denominator prior) > number valid in the session's set > unresolved.
Extracted VERBATIM from ``live_identify.identify_frame`` so the single-card live
flow and the multi-card binder flow share one implementation; the only I/O is
the same name-index / set-map / catalog-lookup the live path already performed."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.cards import cached_lookup_card, get_set_numerators, normalize_local_id
from app.db.models import SetIdMap
from app.db.session import async_session_maker
from app.pack.matching import card_fields_from_match
from app.pack.name_index import get_name_index
from app.pack.ocr import NumberReading
from app.pack.set_resolution import SetEntry, catalog_local_id, load_denominator_table
from app.pokewallet import get_api_key

log = logging.getLogger("pokemon_scanner.pack.identify")


@dataclass
class SessionPrior:
    set_id: str | None
    set_name: str | None
    denominator: str | None


@dataclass
class IdentityResult:
    confident: bool
    numerator: str | None
    display_number: str | None
    set_id: str | None
    set_code: str | None
    set_name: str | None
    fields: dict
    low_confidence_reason: str | None
    identity_key: str
    name_match_score: float | None


async def _pw_set_id_for(tcgdex_set_id: str) -> str | None:
    """tcgdex set -> PokéWallet set_id via the set_id_map bridge table (built
    by scripts/build_id_maps.py). me-era sets are self-referential (e.g.
    "me05" -> "me05"); sets that haven't been mapped yet yield None, and the
    card's identity still comes from the name index (price/image stay None)."""
    async with async_session_maker() as session:
        return (await session.execute(
            select(SetIdMap.pokewallet_set_id)
            .where(SetIdMap.tcgdex_set_id == tcgdex_set_id))).scalars().first()


def _promo_entry(reading: NumberReading | None) -> SetEntry | None:
    """The denominator-table row a printed PROMO PREFIX names, or None.

    A promo card prints "MEP EN 037" / "SWSH039": a set-naming prefix and NO
    denominator. ``parse_number`` reflects that faithfully (``prefix`` set,
    ``denominator=None``), but every set-narrowing in this ladder keys off the
    denominator — so the cards carrying the most explicit set evidence there is
    were arriving with none of it usable, and their names matched against the
    whole catalog. Measured on binder fixture page_3 (nine MEP promos, names read
    perfectly): five of nine resolved to the wrong set's printing of the name and
    stayed unconfident, purely because ``NameIndex.match`` returns an arbitrary
    representative printing when a name has several.

    ``by_promo_prefix`` is one row per prefix by construction, so a matched prefix
    identifies exactly one set — the same fact ``binder._prior_denominator_ok``
    already leans on.

    The row (not just its id) is what callers need: the printed number has to be
    re-expressed in the set's OWN local_id form before it can be compared with
    the catalog, and only the row knows that form (``catalog_local_id``).
    ``promo_set_id`` below is the TCGdex id, which is the space
    ``NameIndex._by_set`` and ``NameMatch.tcgdex_set_id`` are keyed in (the three
    promo rows are self-referential: ``set_id == tcgdex_id``).

    ``bare`` readings have no prefix and never reach this."""
    if reading is None or not reading.prefix or not reading.pattern_ok:
        return None
    return load_denominator_table().by_promo_prefix.get(reading.prefix.upper())


def promo_set_id(entry: SetEntry | None) -> str | None:
    """``_promo_entry``'s row as a tcgdex set id."""
    return (entry.tcgdex_id or entry.set_id) if entry is not None else None


def _number_agrees(name_match, reading: NumberReading, numerator: str,
                   promo_entry: SetEntry | None) -> bool:
    """Rung 1's question: is the card this NAME names printed at the number this
    cell READ? Both sides normalized, and for a promo read both sides expressed
    in the set's own local_id form.

    Two things a plain ``normalize_local_id(local_id) == numerator`` gets wrong on
    a promo card, both measured:

      * **swshp stores its local_ids WITH the prefix** ("SWSH004"), while the read
        numerator is the digits alone ("4"). The direct comparison can therefore
        NEVER be true for that set — a perfectly read "SWSH004" next to a
        perfectly read "Meowth V" failed rung 1 structurally, for all 302 cards.
        ``catalog_local_id`` (the project's one converter for exactly this, added
        in Task 2) re-expresses the read number in the set's form: "SWSH004" for
        swshp, "037" for svp/mep, so both sets compare correctly.
      * **the prefix names the set, so a name matched in a DIFFERENT set does not
        agree at all** — it contradicts. Without this a hallucinated/misread
        prefix could pair with a name from elsewhere and mint a confident-wrong
        (measured: a true me01 "Lillie's Determination" cell whose number read as
        "SVP184" came back as a confident svp "Hop's Snorlax", because svp's #184
        happens to exist and the name fuzzy-matched inside svp). A promo read must
        agree with the promo set or not at all."""
    printed = normalize_local_id(name_match.local_id)
    if promo_entry is None:
        return printed == numerator
    if name_match.tcgdex_set_id != promo_set_id(promo_entry):
        return False
    catalog = catalog_local_id(promo_entry, reading.numerator)
    return catalog is not None and normalize_local_id(catalog) == printed


async def resolve_identity(name_texts: list[tuple[str, float]],
                           reading: NumberReading | None,
                           prior: SessionPrior | None) -> IdentityResult:
    """Resolve candidate title-band lines + a number reading into an identity.

    ``name_texts`` is the title-band candidate lines as ``(text, conf)``,
    best-first (the caller does any y/conf ordering). Callers that need the
    live/binder FrameResult kinds (no_card/unreadable) decide those from the
    inputs and this result — the core does not emit them.

    A ``bare`` reading (numerator only — ``ocr.parse_bare_numerator``) traverses
    exactly ONE rung: name+number agreement. It has no denominator, so ``den``
    falls through to the prior's exactly as a missing reading would and no
    narrowing or veto keys off it; it has no prefix, so it gets no promo scoping;
    and the numerator-in-set rung refuses it outright, here rather than at the
    caller. Its only power is to let a card's own printed name confirm its own
    printed number — and when that confirmation does not happen the digits are
    DISCARDED, so an unconfirmed bare reading leaves no trace on the result at
    all (no numerator, no display number) and is indistinguishable from having
    read no number."""
    # name: highest-confidence line in the TITLE band only (hard filter)
    idx = await get_name_index()
    den = reading.denominator if (reading and reading.denominator) \
        else (prior.denominator if prior else None)
    # A promo prefix is set-naming evidence with no denominator to express it in;
    # see _promo_entry. None for every non-promo and every bare reading.
    promo_entry = _promo_entry(reading)
    promo_set = promo_set_id(promo_entry)
    numerator = None
    if reading is not None and reading.numerator:
        # Tail-normalized so a gallery numerator ("TG22"/"GG7") compares equal to
        # its catalog local_id ("TG22"/"GG07") and validates against the set.
        numerator = normalize_local_id(reading.numerator)
    name_match = None
    top_name_text = None
    for text, conf in name_texts:
        if top_name_text is None:
            top_name_text = text
        m = idx.match(text, denominator=den)
        if m is not None:
            name_match = m
            break
    # Fallback for prefixed "Trainer's Pokemon" names (e.g. Ascended Heroes): OCR
    # frequently drops the "Erika's"/"Sabrina's" prefix, so the bare Pokemon name
    # matches a commoner printing in another set ambiguously. If the session already
    # knows the set, the card printed a promo prefix, or the denominator uniquely
    # identifies one, re-match scoped to that set's cards.
    if (name_match is None or name_match.ambiguous) and top_name_text:
        by_promo = promo_set is not None and not (prior and prior.set_id)
        # No special score floor for the promo scope: the hazard it was standing
        # in for is a top-score TIE between different cards in the pool, which
        # match_in_set now reports as ambiguous for every scoping (see its
        # docstring). A floor separates on the wrong axis — it also refuses the
        # honest recoveries this scoping exists for, whose scores are legitimately
        # well under 100 (a dropped "Hop's" possessive scores 90.0).
        scoped = idx.match_in_set(
            top_name_text,
            set_id=(prior.set_id if prior and prior.set_id else promo_set),
            denominator=den)
        if scoped is not None and not scoped.ambiguous and (
                # A PROMO-scoped re-match is adopted only when it makes the card's
                # name and its printed number AGREE. The promo prefix exists here
                # to feed rung 1, not to relabel a card: absent agreement the
                # scoped hit is just the best fuzzy match inside a 300-card promo
                # pool, and adopting it would replace a correctly read title with
                # that guess in the review tray (measured: a flagged
                # "Lillie's Determination" was being displayed as "N's Zorua").
                # Scoping from a prior or a denominator is unchanged.
                not by_promo
                or (numerator and _number_agrees(scoped, reading, numerator,
                                                 promo_entry))):
            name_match = scoped

    set_id = set_code = set_name = None
    confident = False

    if name_match and numerator and _number_agrees(name_match, reading, numerator,
                                                   promo_entry):
        confident = True                      # name + number agree
    elif name_match and not name_match.ambiguous:
        # Veto: a unique-name fuzzy match must not defeat the card's own printed
        # denominator — when that denominator is known to the catalog and names a
        # DIFFERENT set than the match, the "unique" name was OCR garbage
        # (production: "Pikachu on the Ball"/Futsal beat a /131 Ogerpon cell).
        den_veto = False
        if reading and reading.denominator and reading.denominator.isdigit():
            sets_for_den = idx._official_to_sets.get(int(reading.denominator))
            if sets_for_den and name_match.tcgdex_set_id not in sets_for_den:
                den_veto = True
        if promo_set is not None:
            # Same veto in the promo shape: the printed prefix names the set, so a
            # unique-name match in a DIFFERENT set is contradicted by the card's
            # own pixels. And a promo read always carries a numerator too
            # (PROMO_RE needs the digits), so the name had something concrete to
            # agree with — reaching here means it did NOT, i.e. this cell
            # disagrees with itself. This rung is for cells whose name is the only
            # evidence, which a promo read is not, so it never promotes one:
            # a promo cell goes confident through name+number agreement or not at
            # all. That is only a safe trade because rung 1 can now actually FIRE
            # for every promo set — see _number_agrees, where swshp's prefixed
            # local_ids made it structurally impossible.
            den_veto = True
        if not den_veto:
            confident = True                  # unique name (+denominator prior)
            if reading is not None and reading.bare and numerator \
                    and not _number_agrees(name_match, reading, numerator, None):
                # This rung promotes on the NAME alone, and a bare numerator is
                # not evidence — it is the digits left over when no pattern was
                # found. Letting it ride along would print a number no rung
                # believes onto a card the name resolved (measured: a unique
                # "TYROGUE" with a stray "004" came back as Tyrogue #4 instead of
                # its real #71). A bare reading must behave here exactly as no
                # reading at all does, so the disagreeing digits are dropped and
                # the matched printing's own local_id is used, as it is for a
                # cell that read no number.
                numerator = None
            numerator = numerator or normalize_local_id(name_match.local_id)
    if name_match and confident:
        set_name = name_match.set_name
        set_code = name_match.tcgdex_set_id
        set_id = await _pw_set_id_for(name_match.tcgdex_set_id)

    if not confident and reading is not None and not reading.bare \
            and prior and prior.set_id:
        # A BARE reading is excluded here STRUCTURALLY, not by the caller. This
        # rung promotes on "the numerator exists in the prior's set" and nothing
        # else — precisely the evidence a bare reading does not have — so a lone
        # 2-3 digit smudge would become a confident identity in whatever set the
        # session or page happens to be in. The binder's page-prior pass filters
        # bare candidates out before it ever calls here, but that is a caller's
        # policy; this is the ladder's own rule, and it holds for every caller.
        #
        # get_set_numerators returns an EMPTY set for BOTH a DB failure and a set
        # that isn't mapped/ingested (app/cards.py). Empty therefore means "we
        # don't know this set's numerators" — never "every numerator is valid".
        # This rung's whole job is to check the number against the set, so with no
        # catalog to check against it must validate NOTHING (otherwise any garbage
        # the OCR produced became a confident identity in the session's set). And
        # it must say so: a repeatedly empty result is a catalog gap, not noise.
        valid = await get_set_numerators(prior.set_id)
        if numerator and not valid:
            log.warning("identify.set_numerators_empty set=%s numerator=%s "
                        "(unmapped/uningested set or DB failure) — rung skipped",
                        prior.set_id, numerator)
        if numerator and valid and numerator in valid:
            confident = True                  # number valid in session's set
            set_id, set_name = prior.set_id, prior.set_name

    if not confident and reading is not None and reading.bare:
        # UNCONFIRMED BARE DIGITS ARE NOT A NUMBER THE APP MAY SHOW. Nothing above
        # accepted them: no rung promoted this cell, so the only thing that could
        # have vouched for the digits (the card's own name agreeing with them)
        # did not. Surfacing them anyway would print a fabricated collector number
        # into the review tray on a flagged card — a number no rung believes, next
        # to a card the user is being asked to check. Cleared here rather than at
        # the display layer so `numerator`, `display_number` and `identity_key`
        # cannot disagree, and so an unconfirmed bare reading is what it always
        # should be: exactly as if no number had been read.
        numerator = None

    fields: dict = {"name": None, "rarity": None, "image_url": None, "match_id": None}
    if set_id and numerator:
        try:
            match = await cached_lookup_card(set_id, numerator,
                                             set_name=set_name, api_key=get_api_key())
            fields = card_fields_from_match(match)
        except Exception as e:
            log.warning("live.lookup_failed err=%r", e)
    if fields.get("name") is None and name_match is not None:
        fields["name"] = name_match.card_name

    display_number = None
    if numerator:
        den = reading.denominator if reading and reading.denominator else \
            (prior.denominator if prior else None)
        display_number = f"{numerator.zfill(3)}/{den}" if den else numerator

    # Reason must name the stage that actually failed — a read number with an
    # unresolved set is NOT "couldn't read the number" (misleading in the tray).
    if confident:
        reason = None
    elif reading is None or (reading.bare and numerator is None):
        # An unconfirmed bare reading failed at the NUMBER stage — its digits were
        # just discarded — so it reports that stage, exactly as reading-absent does.
        reason = "number_ambiguous"
    elif set_id is None and set_name is None:
        reason = "set_ambiguous"
    else:
        reason = "no_db_match"

    key = f"{set_code or set_name or '?'}:{numerator or normalize_key(fields.get('name'))}"
    return IdentityResult(
        confident=confident, numerator=numerator, display_number=display_number,
        set_id=set_id, set_code=set_code, set_name=set_name, fields=fields,
        low_confidence_reason=reason, identity_key=key,
        name_match_score=(name_match.score if name_match is not None else None))


def normalize_key(name: str | None) -> str:
    from app.pack.name_index import normalize_name
    return normalize_name(name or "unknown")
