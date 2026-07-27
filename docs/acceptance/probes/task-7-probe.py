"""Probe for Task 7 (A4): numerator-only parsing for promo pages.

Two new number sources, deliberately of very different strength, and the whole
point of this probe is that the difference is enforced rather than described:

  * ``binder._promo_number`` rejoins a promo card's SPLIT "MEP EN 037" into a
    REAL promo reading — ``pattern_ok=True``, ``prefix="MEP"`` — indistinguishable
    from what ``parse_number`` returns for a single "MEP037" token. It is strong
    because the set-naming prefix was actually read off the card.
  * ``ocr.parse_bare_numerator`` returns a lone numerator with ``pattern_ok=False,
    bare=True``. It is weak by construction: no set information at all. It may
    only be confirmed by the card's own NAME agreeing with it, and Task 6's
    page-prior pass must never see one.

Sections:
  A. JOIN      ``_promo_number`` on fixture page_3's REAL line geometry — the
               split shape, the glued "MEPB037" shape, and the near misses it has
               to refuse (the weakness "×2" digits, a digits token to the left of
               the prefix, a line that merely contains a prefix).
  B. LADDER    a joined promo reading through ``resolve_identity``: the mep
               identity, and ``catalog_local_id`` giving the tcgdex id ``mep-038``
               (bare local_ids) rather than ``mep-MEP038``.
  C. BARE      ``parse_bare_numerator`` acceptance and every rejection: 4-digit
               years, HP-shaped lines, "X2", a real reading present, "000".
  D. BARE+     a bare reading through the ladder: confident ONLY where the name
               agrees; flagged with no name; and no denominator/prefix side effect.
  E. PASS 2    a bare reading cannot enter ``_apply_page_prior`` — with the
               counterfactual showing the same cell IS rescued the moment the
               ``bare`` flag is cleared, so the filter is provably load-bearing.
  F. AUDIT     every ``pattern_ok`` consumer, asserted against a bare reading.
  G. PIXELS    the real page_1 cell whose strip contains a "232" (the weakness
               row): the bottom-half restriction must keep it unread — including
               under Task 10's higher-resolution strip retry, which re-reads that
               same row as "232"/"32" and must still drop it. Plus page_3, now
               9/9 confident, with no cell resting on a bare numerator.

Not a pytest test (the suite must stay 7 passed / 1 skipped); a one-shot script.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/task-7-probe.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://pcs:pcs@localhost:5432/pcs")
os.environ.setdefault("AUTH_SECRET", "dev-secret-not-for-prod-pad-0123456789")
os.environ.setdefault("PHOTO_STORAGE_DIR", "./var/pulls")
os.environ.setdefault("COOKIE_SECURE", "false")

import numpy as np  # noqa: E402

from app.cards import normalize_local_id  # noqa: E402
from app.pack.binder import (  # noqa: E402
    BinderCell,
    CellRead,
    _apply_page_prior,
    _identify_quad_cell,
    _pack_card,
    _page_modal,
    _prior_denominator_ok,
    _promo_number,
    scan_binder_page,
)
from app.pack.identify_core import (  # noqa: E402
    SessionPrior,
    _promo_entry,
    promo_set_id,
    resolve_identity,
)
from app.pack.name_index import get_name_index  # noqa: E402
from app.pack.ocr import NumberReading, parse_bare_numerator, parse_number  # noqa: E402
from app.pack.set_resolution import catalog_local_id, entry_for_set_id  # noqa: E402

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo
REAL = REPO / "tests" / "corpus" / "binder" / "real"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL {msg}")


def _tiny():
    return np.full((14, 10, 3), 200, dtype=np.uint8)


def L(text: str, x: float, y: float, conf: float, h: float):
    """A detect_lines_xy line: (x_center, y_center, TEXT, conf, box_w, box_h)."""
    return (x, y, text, conf, h * len(text) * 0.6, h)


# Fixture page_3's ACTUAL per-cell strip detections (dumped from the committed
# photo with detect_lines_xy — the numbers below are measured, not invented).
P3_CELL1 = [                                    # Charmander, the split shape
    L("RETREAT", 760.6, 476.9, 0.99, 27.2),
    L("×2", 265.9, 482.8, 0.77, 43.7),
    L("RESISTANCE", 424.9, 482.8, 0.99, 29.6),
    L("WEAKNESS", 134.2, 486.4, 0.98, 27.2),
    L("ILLUS.SABOTERI", 159.6, 561.4, 0.94, 26.0),
    L("MEPEN", 160.8, 601.0, 0.99, 39.0),
    L("038", 225.2, 608.1, 1.00, 31.9),
    L("2026POKEMON/NINTENDO/CREATURES/GAME FREAK", 583.3, 646.0, 0.95, 31.9),
]
P3_CELL0 = [                                    # Bulbasaur, the GLUED shape
    L("RETREAT", 716.0, 493.3, 0.99, 35.1),
    L("RESISTANCE", 404.4, 515.6, 0.97, 32.8),
    L("82", 262.6, 525.5, 0.64, 43.0),          # the weakness "×2", misread
    L("WEAKNE33", 141.2, 536.3, 0.86, 37.4),
    L("MEPB037", 184.5, 643.8, 0.91, 40.8),
    L("POLEMON/NINTENDO/CREATURES/GAMEFREAK", 573.6, 662.2, 0.93, 49.8),
]
P3_CELL3 = [                                    # Turtwig: a prefix and NO digits
    L("RETREAT", 760.5, 565.8, 0.99, 34.4),
    L("RESISTANCE", 425.7, 572.4, 0.97, 35.5),
    L("WEAKNESS", 144.6, 578.3, 0.95, 30.8),
    L("LLUS.SABOTERI", 165.9, 658.9, 0.90, 33.2),
    L("MEPEN", 163.5, 699.4, 0.98, 40.3),
    L("C2026POKEMON/NINTENDO/CREATURES/GAMEFREAK", 579.8, 746.0, 0.94, 34.4),
]


# ── A. the join ──────────────────────────────────────────────────────────────
def probe_join() -> None:
    print("\n=== A. _promo_number — rejoining a split promo number ===")

    cases = [
        ("page_3 cell 1: 'MEPEN' + '038'          ", P3_CELL1, ("MEP", "38")),
        ("page_3 cell 0: glued 'MEPB037'          ", P3_CELL0, ("MEP", "37")),
        ("page_3 cell 3: prefix, NO digits printed", P3_CELL3, None),
        # (b) the separate-token variant: OCR keeps the language code apart.
        ("'MEP' + 'EN' + '037' as three tokens    ",
         [L("MEP", 160.0, 600.0, 0.98, 39.0), L("EN", 205.0, 601.0, 0.95, 38.0),
          L("037", 250.0, 602.0, 0.99, 38.0)], ("MEP", "37")),
        ("'MEP EN' as one token + '037'           ",
         [L("MEP EN", 170.0, 600.0, 0.98, 39.0), L("037", 250.0, 602.0, 0.99, 38.0)],
         ("MEP", "37")),
        ("SWSH promo, split                       ",
         [L("SWSHEN", 160.0, 600.0, 0.97, 39.0), L("039", 240.0, 603.0, 0.99, 38.0)],
         ("SWSH", "39")),
        ("SVP promo, split                        ",
         [L("SVP", 160.0, 600.0, 0.97, 39.0), L("042", 230.0, 601.0, 0.99, 38.0)],
         ("SVP", "42")),
        # Refusals — each is a real near-miss, not a strawman.
        ("digits on ANOTHER printed row (the ×2)  ",
         [L("MEPEN", 184.5, 643.8, 0.91, 40.8), L("82", 262.6, 525.5, 0.64, 43.0)],
         None),
        ("digits to the LEFT of the prefix        ",
         [L("MEPEN", 400.0, 600.0, 0.98, 39.0), L("037", 200.0, 601.0, 0.99, 38.0)],
         None),
        ("a line that merely CONTAINS a prefix    ",
         [L("MEPHITIC", 160.0, 600.0, 0.9, 39.0), L("037", 250.0, 601.0, 0.99, 38.0)],
         None),
        ("a non-promo language tail ('PREEN')     ",
         [L("PREEN", 219.1, 670.6, 0.98, 35.6), L("166", 321.6, 677.5, 1.00, 41.9)],
         None),
        ("a 4-digit partner (the copyright year)  ",
         [L("MEPEN", 160.0, 600.0, 0.98, 39.0), L("2026", 250.0, 601.0, 0.99, 38.0)],
         None),
        ("no promo line at all                    ",
         [L("WEAKNESS", 100.0, 500.0, 0.99, 30.0), L("037", 250.0, 601.0, 0.99, 38.0)],
         None),
    ]
    for label, lines, expected in cases:
        r = _promo_number(lines)
        got = (r.prefix, r.numerator) if r is not None else None
        print(f"  {label} -> {got}")
        check(got == expected, f"_promo_number {label.strip()}: want {expected}, got {got}")
        if r is not None:
            check(r.pattern_ok and not r.bare,
                  f"_promo_number {label.strip()}: must be a real pattern_ok reading")

    # The joined reading must be INDISTINGUISHABLE from a single-token read.
    joined = _promo_number(P3_CELL1)
    direct = parse_number("MEP038", joined.confidence)
    same = (joined.numerator == direct.numerator and joined.prefix == direct.prefix
            and joined.denominator == direct.denominator
            and joined.pattern_ok == direct.pattern_ok and joined.bare == direct.bare
            and joined.raw == direct.raw and joined.tokens == direct.tokens)
    print(f"  joined 'MEPEN'+'038' == parse_number('MEP038'): {same}  ({joined})")
    check(same, "a joined promo reading must equal parse_number's single-token read")

    # The premise the whole task rests on: neither half parses on its own today.
    for text in ("MEPEN", "038", "MEPB037"):
        check(parse_number(text, 0.99) is None,
              f"premise broken: parse_number({text!r}) now parses on its own")
    print("  premise: parse_number('MEPEN') / ('038') / ('MEPB037') are all None: True")

    # The prefixes come from the table, not a second hardcoded list.
    src = inspect.getsource(_promo_number.__module__ and
                            sys.modules[_promo_number.__module__])
    check("by_promo_prefix" in src.split("def _promo_line_re")[1].split("def ")[0],
          "_promo_line_re must derive its prefixes from the denominator table")
    print("  _promo_line_re derives prefixes from the denominator table: True")


# ── B. the joined reading through the ladder ─────────────────────────────────
async def probe_ladder() -> None:
    print("\n=== B. a joined promo reading through resolve_identity ===")
    reading = _promo_number(P3_CELL1)
    res = await resolve_identity([("CHARMANDER", 0.99)], reading, None)
    print(f"  'CHARMANDER' + MEP038 -> confident={res.confident} num={res.numerator} "
          f"set_code={res.set_code} set_name={res.set_name!r} name={res.fields['name']!r}")
    check(res.confident, "a joined MEP038 + CHARMANDER must resolve confidently")
    check(res.set_code == "mep", f"set must be mep, got {res.set_code}")
    check(res.numerator == "38", f"numerator must be 38, got {res.numerator}")
    check(res.fields["name"] == "Charmander", f"name got {res.fields['name']!r}")

    # (a) the tcgdex card id: mep stores BARE local_ids, so it is mep-038.
    entry = entry_for_set_id("mep")
    local = catalog_local_id(entry, reading.numerator)
    print(f"  catalog_local_id(mep, {reading.numerator!r}) = {local!r} "
          f"-> tcgdex id 'mep-{local}'")
    check(local == "038", f"mep local_id must be '038', got {local!r}")
    check(f"mep-{local}" == "mep-038", "the tcgdex id must be mep-038, not mep-MEP038")
    check(catalog_local_id(entry_for_set_id("swshp"), "123") == "SWSH123",
          "swshp local_ids DO carry the prefix — the two forms must not be conflated")

    # (b) the separate-token variant resolves identically.
    alt = _promo_number([L("MEP EN", 170.0, 600.0, 0.98, 39.0),
                         L("037", 250.0, 602.0, 0.99, 38.0)])
    res_b = await resolve_identity([("BULBASAUR", 0.99)], alt, None)
    print(f"  'BULBASAUR' + 'MEP EN'/'037' -> confident={res_b.confident} "
          f"num={res_b.numerator} set_code={res_b.set_code} name={res_b.fields['name']!r}")
    check(res_b.confident and res_b.set_code == "mep" and res_b.numerator == "37",
          "the separate-token MEP EN variant must resolve to mep-037")

    # The prefix is SCOPING evidence only: it never promotes a cell by itself.
    def promo_of(r):
        return promo_set_id(_promo_entry(r))

    print(f"  promo set named by the MEP reading = {promo_of(reading)!r}")
    check(promo_of(reading) == "mep", "MEP must scope to the mep set")
    check(promo_of(parse_number("012/202", 0.9)) is None,
          "a normal N/N reading must not scope to any promo set")
    check(promo_of(NumberReading(numerator="37", bare=True)) is None,
          "a bare reading has no prefix and must not scope")
    disagree = await resolve_identity([("CHARMANDER", 0.99)],
                                      parse_number("MEP099", 0.99), None)
    print(f"  'CHARMANDER' + MEP099 (name and number DISAGREE) -> "
          f"confident={disagree.confident} reason={disagree.low_confidence_reason}")
    check(not disagree.confident,
          "a promo cell whose name and number disagree must NOT be promoted")


# ── C. parse_bare_numerator ──────────────────────────────────────────────────
def probe_bare_parse() -> None:
    print("\n=== C. parse_bare_numerator — the last-resort numerator ===")
    cases = [
        ("a clean 3-digit numerator      ", [("037", 0.99)], "037"),
        ("a 2-digit numerator            ", [("42", 0.95)], "42"),
        ("highest confidence wins        ", [("11", 0.40), ("045", 0.99)], "045"),
        ("(f) a 4-digit year             ", [("2026", 0.99)], None),
        ("(f) an HP-shaped line          ", [("130 HP", 0.99)], None),
        ("(f) a bare 'HP' label          ", [("HP", 0.99)], None),
        ("the weakness multiplier 'X2'   ", [("X2", 0.80)], None),
        ("a single digit                 ", [("7", 0.99)], None),
        ("zero                           ", [("000", 0.99)], None),
        ("a printed N/N is not bare      ", [("097/088", 0.95)], None),
        ("an N/N anywhere refuses the run", [("097/088", 0.5), ("045", 0.99)], None),
        ("a promo read refuses the run   ", [("MEP037", 0.5), ("045", 0.99)], None),
        ("the copyright line             ",
         [("2026POKEMON/NINTENDO/CREATURES/GAMEFREAK", 0.95)], None),
        ("nothing at all                 ", [], None),
    ]
    for label, lines, expected in cases:
        r = parse_bare_numerator(lines)
        got = r.numerator if r is not None else None
        print(f"  {label} {str(lines)[:44]:46s} -> {got}")
        check(got == expected, f"parse_bare_numerator {label.strip()}: "
                               f"want {expected}, got {got}")
        if r is not None:
            check(r.bare and not r.pattern_ok and r.denominator is None
                  and r.prefix is None,
                  f"{label.strip()}: a bare reading must be bare, not pattern_ok, "
                  "and carry no set evidence")

    # The HP guard is STRUCTURAL: the caller never hands it the title band, which
    # is where HP prints. Asserted on the call site itself.
    body = inspect.getsource(_identify_quad_cell)
    tail, depth, end = body.split("parse_bare_numerator(")[1], 1, 0
    for end, ch in enumerate(tail):          # the call's own argument list only
        depth += (ch == "(") - (ch == ")")
        if depth == 0:
            break
    call = re.sub(r"\s+", " ", tail[:end])
    print(f"  _identify_quad_cell's call site: parse_bare_numerator({call.strip()}")
    check("strip_xy" in call and "strip_mid" in call and "name_xy" not in call,
          "the bare read must come from the strip's lower half only, never the "
          f"name band — call site reads: {call!r}")


# ── D. a bare reading through the ladder ─────────────────────────────────────
async def probe_bare_ladder() -> None:
    print("\n=== D. a bare reading through resolve_identity ===")
    # (c) Tyrogue is printed once in the whole catalog, at me01 071, so the name
    # index's representative printing IS that card: the name AGREES with a bare
    # '071' and the agreement rung — the ONLY rung a bare reading can traverse —
    # fires. (A many-printing name cannot do this; see the report. "TANGELA" was
    # the brief's example but it has five printings, and with no denominator to
    # narrow them the index returns an arbitrary one: measured confident=False.)
    bare = parse_bare_numerator([("071", 0.97)])
    res = await resolve_identity([("TYROGUE", 0.97)], bare, None)
    print(f"  'TYROGUE' + bare '071' -> confident={res.confident} "
          f"num={res.numerator} set_code={res.set_code} name={res.fields['name']!r} "
          f"number={res.display_number}")
    check(res.confident, "bare + an agreeing name must resolve (the agreement rung)")
    check(res.set_code == "me01" and res.fields["name"] == "Tyrogue",
          f"expected me01 Tyrogue, got {res.set_code} {res.fields['name']!r}")
    check(res.display_number == "71",
          f"a bare reading prints no denominator, got {res.display_number!r}")

    tangela = await resolve_identity([("TANGELA", 0.97)],
                                     parse_bare_numerator([("001", 0.97)]), None)
    print(f"  'TANGELA' + bare '001' (correct for sv06, but Tangela has 5 "
          f"printings) -> confident={tangela.confident}")
    check(not tangela.confident,
          "documented limit: with no denominator the index's representative "
          "printing decides, so a many-printing name cannot confirm a bare number")

    # The binding invariant, in its sharpest form: on the UNIQUE-NAME rung — the
    # one rung a bare reading is not allowed to influence — a disagreeing bare
    # numerator must leave the result byte-for-byte identical to no reading at
    # all. (Before the fix it did not: the stray digits were printed as the
    # card's number, so Tyrogue came back as #4 instead of its real #71.)
    def shape(r):
        return (r.confident, r.numerator, r.display_number, r.set_code,
                r.fields["name"], r.low_confidence_reason)

    res_x = await resolve_identity([("TYROGUE", 0.97)],
                                   parse_bare_numerator([("004", 0.97)]), None)
    res_0 = await resolve_identity([("TYROGUE", 0.97)], None, None)
    print(f"  'TYROGUE' + bare '004' (not Tyrogue's number) -> {shape(res_x)}")
    print(f"  'TYROGUE' + NO reading at all              -> {shape(res_0)}")
    check(shape(res_x) == shape(res_0),
          "a disagreeing bare numerator must resolve exactly as reading-absent "
          f"does: {shape(res_x)} != {shape(res_0)}")

    # (d) no name at all -> nothing can confirm the digits.
    res_n = await resolve_identity([], parse_bare_numerator([("037", 0.99)]), None)
    print(f"  no name + bare '037' -> confident={res_n.confident} "
          f"reason={res_n.low_confidence_reason} name={res_n.fields['name']!r}")
    check(not res_n.confident, "a bare numerator with no name must stay flagged")

    # A bare reading must not act like a denominator anywhere: with a prior in
    # hand it behaves exactly as a missing reading does for the NAME path.
    prior = SessionPrior(set_id="23473", set_name="Twilight Masquerade",
                         denominator="167")
    with_bare = await resolve_identity([("APPLIN", 0.9)],
                                       parse_bare_numerator([("126", 0.9)]), prior)
    without = await resolve_identity([("APPLIN", 0.9)], None, prior)
    print(f"  prior + bare '126' -> name={with_bare.fields['name']!r} "
          f"set={with_bare.set_code}; prior + NO reading -> "
          f"name={without.fields['name']!r} set={without.set_code}")
    check(with_bare.fields["name"] == without.fields["name"],
          "a bare reading must not change what the prior's NAME path resolves")


# ── E. pass 2 ────────────────────────────────────────────────────────────────
async def pass1(row: int, name: str | None, reading: NumberReading | None):
    name_texts = [(name, 0.95)] if name else []
    res = await resolve_identity(name_texts, reading, None)
    read = CellRead(box=(0, 0, 10, 14), crop=_tiny(), res=res,
                    texts=[reading.raw if reading else ""],
                    name_texts=name_texts, reading=reading)
    return (BinderCell(cell=read.box, card=_pack_card(row, res), thumb_b64=None,
                       needs_vlm=not res.confident), read)


async def probe_pass2() -> None:
    print("\n=== E. (e) a bare reading can never enter pass 2 ===")
    # A page that genuinely earns a prior: three confident sv06 cells, plus one
    # cell whose only number is a BARE '126'. Its name is readable ("APPLIN") and
    # does not contradict 126 — i.e. it clears every OTHER pass-2 filter, so the
    # bare filter is the only thing between it and a rescue.
    spec = [("TANGELA", parse_number("001/167", 0.95)),
            ("TANGROWTH", parse_number("002/167", 0.95)),
            ("SPINARAK", parse_number("004/167", 0.95)),
            ("APPLIN", parse_bare_numerator([("126", 0.9)]))]
    built = [await pass1(i, n, r) for i, (n, r) in enumerate(spec)]
    cells = [c for c, _ in built]
    reads = [r for _, r in built]
    prior = _page_modal(cells)
    print(f"  elected prior: set={prior.set_name!r} den={prior.denominator!r}")
    check(prior is not None and prior.set_id == "23473", "the page must elect sv06")
    print(f"  the bare cell's veto is vacuous "
          f"(_prior_denominator_ok -> {_prior_denominator_ok(reads[3].reading, prior)}) "
          f"— the bare filter, not the veto, is what stops it")

    rescued = await _apply_page_prior(cells, reads, prior)
    print(f"  pass-1 flagged rows={[c.card.row_index for c in cells if c.needs_vlm]}"
          f"  rescued={rescued}")
    check(rescued == [], f"a bare cell must not be rescued, got {rescued}")
    check(cells[3].card.needs_review, "the bare cell must stay flagged")

    # COUNTERFACTUAL: clear the flag and the very same cell IS rescued, so the
    # filter is load-bearing rather than protecting nothing.
    built2 = [await pass1(i, n, r) for i, (n, r) in enumerate(spec)]
    cells2 = [c for c, _ in built2]
    reads2 = [r for _, r in built2]
    reads2[3].reading.bare = False
    rescued2 = await _apply_page_prior(cells2, reads2, _page_modal(cells2))
    print(f"  counterfactual (same cell, bare flag cleared): rescued={rescued2} "
          f"-> confident={not cells2[3].card.needs_review} "
          f"number={cells2[3].card.card_number} name={cells2[3].card.name!r}")
    check(rescued2 == [3],
          "the counterfactual must reproduce: without the bare filter this cell "
          "is promoted on 'the modal set contains 126' alone")

    # And the filter itself, in the source.
    src = inspect.getsource(_apply_page_prior)
    body = src.split('"""')[2]
    line = next(ln.strip() for ln in body.splitlines() if "bare" in ln)
    print(f"  todo filter carries: {line}")
    check("not r.reading.bare" in body,
          "_apply_page_prior's candidate filter must exclude bare readings")


# ── F. the pattern_ok audit ──────────────────────────────────────────────────
def probe_pattern_ok_audit() -> None:
    print("\n=== F. every pattern_ok consumer, against a bare reading ===")
    bare = parse_bare_numerator([("037", 0.99)])
    check(bare is not None and bare.pattern_ok is False,
          "a bare reading must never be pattern_ok")
    print(f"  bare reading: pattern_ok={bare.pattern_ok} bare={bare.bare} "
          f"den={bare.denominator} prefix={bare.prefix}")

    # Every call site refuses it by the SAME expression it already had.
    from app.pack import confidence as conf_mod
    from app.pack.set_resolution import SetResolution
    score, reason = conf_mod.score_card(bare, SetResolution(), False)
    print(f"  confidence.score_card(bare) -> {score} {reason!r}")
    check(reason == "number_ambiguous" and score == 0.05,
          "score_card must treat a bare reading as no pattern at all")

    from app.pack.vlm_merge import _numerator_corroborated
    src = inspect.getsource(_numerator_corroborated)
    check("parse_number" in src,
          "vlm_merge corroboration re-parses raw OCR text, so it can never see a "
          "bare reading object")
    print("  vlm_merge._numerator_corroborated re-parses raw text via parse_number: "
          "a bare reading object cannot reach it")

    # The producers: only the binder makes one, and only on the quad path.
    #
    # This started as "exactly one call site". Task 10 added the number-strip
    # second pass (``binder._retry_strip_number`` -> ``_strip_number``), which
    # runs the SAME chain over a re-read of the strip and so calls
    # ``parse_bare_numerator`` from a second line. The invariant this check
    # actually protects is not the line count — it is WHICH MODULE may mint a
    # bare reading, and that has not moved: still app/pack/binder.py, still the
    # quad path, still nowhere else. So the assertion is on the module set, and
    # the count is printed rather than pinned. Adding a call from any other
    # module (the whole-photo pipeline, live identify, vlm_merge) still fails.
    root = REPO
    hits = sorted(
        f"{p.relative_to(root)}:{i + 1}"
        for p in root.glob("app/**/*.py")
        for i, ln in enumerate(p.read_text().splitlines())
        if "parse_bare_numerator(" in ln and "def parse_bare_numerator" not in ln)
    print(f"  parse_bare_numerator call sites: {hits}")
    modules = sorted({h.rsplit(":", 1)[0] for h in hits})
    check(modules == ["app/pack/binder.py"],
          f"only the binder may mint a bare reading, got {modules}")
    check(len(hits) == 2,
          f"binder's two bare call sites are pass 1 and the strip retry, got {hits}")


# ── G. real pixels ───────────────────────────────────────────────────────────
async def probe_real_page() -> None:
    print("\n=== G. real pixels: page_1's '232' must stay unread ===")
    p1 = REAL / "page_1.jpeg"
    if not p1.exists():
        print("  SKIP (fixture missing)")
        return
    res = await scan_binder_page(p1.read_bytes())
    c0 = res["cards"][0]
    print(f"  page_1 cell 0: number={c0['card_number']} name={c0['name']!r} "
          f"needs_review={c0['needs_review']} reason={c0['low_confidence_reason']}")
    check(c0["card_number"] is None and c0["low_confidence_reason"] == "number_ambiguous",
          "the weakness row's '232' sits in the strip's UPPER half and must not "
          f"become a numerator — got {c0['card_number']}")

    p3 = REAL / "page_3.jpeg"
    if p3.exists():
        # Task 7 pinned this page at 8/9 — Turtwig ("MEP 040") was the one cell
        # of nine the stacked band pass could not read, and Task 7's job was to
        # prove it stayed unread rather than being rescued by a BARE guess.
        # Task 10's number-strip second pass reads it properly: re-OCR'd alone at
        # 1400px the line comes back as the single token "MEP040", which
        # parse_number accepts as a real promo reading (pattern_ok, prefix "MEP").
        # So the pin moves to 9/9 — and the Task-7 guarantee is asserted DIRECTLY
        # instead of being implied by Turtwig's absence: every call to
        # parse_bare_numerator during this page must return None, i.e. not one of
        # the nine confident cells rests on a bare numerator.
        import app.pack.binder as _binder
        real_bare = _binder.parse_bare_numerator
        bare_results: list[object] = []

        def _recording_bare(lines):
            out = real_bare(lines)
            bare_results.append(out)
            return out

        _binder.parse_bare_numerator = _recording_bare
        try:
            res3 = await scan_binder_page(p3.read_bytes())
        finally:
            _binder.parse_bare_numerator = real_bare
        got = [(c["row_index"], c["card_number"], c["name"], c["needs_review"])
               for c in res3["cards"]]
        for row, num, name, flag in got:
            print(f"    [{row}] {'flagged' if flag else 'confident':9s} "
                  f"{str(num):5s} {name}")
        confident = [g for g in got if not g[3]]
        check(len(confident) == 9,
              f"page_3 must come back 9/9 confident, got {len(confident)}")
        truth = {"37": "Bulbasaur", "38": "Charmander", "39": "Squirtle",
                 "40": "Turtwig", "41": "Chimchar", "42": "Piplup",
                 "43": "Rowlet", "44": "Litten", "45": "Popplio"}
        for _row, num, name, _f in confident:
            check(truth.get(normalize_local_id(num or "")) == name,
                  f"page_3 confident cell {num} named {name!r} — expected "
                  f"{truth.get(normalize_local_id(num or ''))!r}")
        print(f"  every confident page_3 cell matches its printed card: "
              f"{all(truth.get(normalize_local_id(n or '')) == nm for _r, n, nm, _f in confident)}")
        print(f"  parse_bare_numerator was consulted {len(bare_results)}x on this "
              f"page and produced {[r for r in bare_results if r is not None]}")
        check(all(r is None for r in bare_results),
              "no page_3 cell may rest on a BARE numerator — the retry buys "
              f"resolution, not promotion; got {[r for r in bare_results if r]}")


# ── H. fix round 1 ───────────────────────────────────────────────────────────
async def probe_fix_round_1() -> None:
    print("\n=== H. fix round 1 (review verdict F1-F4) ===")

    # F1 — swshp stores PREFIXED local_ids, so the naive rung-1 comparison is
    # structurally dead for all 302 of its cards. First the mechanism, then the
    # behaviour.
    swshp = entry_for_set_id("swshp")
    read4 = parse_number("SWSH004", 0.97)
    print(f"  swshp local_id form: catalog 'SWSH004' vs read numerator "
          f"{read4.numerator!r} -> naive compare "
          f"{normalize_local_id('SWSH004') == normalize_local_id(read4.numerator)}; "
          f"catalog_local_id -> {catalog_local_id(swshp, read4.numerator)!r}")
    check(normalize_local_id("SWSH004") != normalize_local_id(read4.numerator),
          "premise gone: the naive comparison now works, re-derive F1")
    check(catalog_local_id(swshp, read4.numerator) == "SWSH004",
          "catalog_local_id must re-express '4' as swshp's own 'SWSH004'")

    # (swshp names that are unique WITHIN swshp — "Pikachu V" is printed six times
    # in that one set, so no name match can pin it and it correctly stays flagged.)
    for name, num, want_set in [("MEOWTH V", "SWSH004", "swshp"),
                                ("GALARIAN PERRSERKER", "SWSH008", "swshp"),
                                ("CHARMANDER", "MEP038", "mep")]:
        res = await resolve_identity([(name, 0.97)], parse_number(num, 0.97), None)
        print(f"  F1 {name:12s} {num:8s} -> confident={res.confident} "
              f"set={res.set_code} name={res.fields['name']!r}")
        check(res.confident and res.set_code == want_set,
              f"F1 {name} {num}: expected a confident {want_set}, got "
              f"{res.confident}/{res.set_code}")

    # F2 — the prefix must veto rung 1 across sets, not only rung 2.
    for name, num, why in [
            ("BAYLEEF", "MEP009", "the name matches me01, the print says mep"),
            ("LILLIE'S DETERMINATION", "SVP184",
             "a hallucinated svp number beside a true me01 card")]:
        res = await resolve_identity([(name, 0.97)], parse_number(num, 0.97), None)
        print(f"  F2 {name:24s} {num:8s} -> confident={res.confident} "
              f"set={res.set_code} name={res.fields['name']!r}   ({why})")
        check(not res.confident,
              f"F2 {name} {num} must NOT be confident: {why}")
    # ...and the flagged card keeps its OWN name rather than a promo-pool guess.
    lil = await resolve_identity([("LILLIE'S DETERMINATION", 0.97)],
                                 parse_number("SVP184", 0.97), None)
    check(lil.fields["name"] == "Lillie's Determination",
          f"a promo-scoped re-match must not relabel a flagged card: "
          f"{lil.fields['name']!r}")
    # The MECHANISM behind that case, asserted so it cannot rot (fix round 2):
    # the attack is a top-score TIE between DIFFERENT cards in the pool, which is
    # what makes it both wrong and process-dependent — not merely a low score.
    idx = await get_name_index()
    attack = idx.match_in_set("LILLIE'S DETERMINATION", set_id="svp")
    real = idx.match_in_set("CHARMANDER", set_id="mep")
    print(f"  F2 mechanism: svp yields {attack.card_name!r} #{attack.local_id} "
          f"score={attack.score:.1f} ambiguous={attack.ambiguous}; a real promo "
          f"name yields {real.card_name!r} score={real.score:.1f} "
          f"ambiguous={real.ambiguous}")
    check(attack is not None and attack.ambiguous,
          "the attack must be reported AMBIGUOUS (a distinct-key top-score tie)")
    check(real is not None and not real.ambiguous,
          "a real promo name must still resolve unambiguously")
    check(attack.score >= 80,
          "premise gone: the attack no longer clears the default floor, so it is "
          "no longer testing tie-rejection — re-derive the guard")

    # Fix round 2: tie-rejection lives in match_in_set, so it also covers the
    # PRE-EXISTING non-promo hazard the re-review demonstrated — a set-scoped
    # prior with a query that fits a whole "Trainer's Pokemon" cycle equally well.
    klink = idx.match_in_set("KLINK", set_id="sv09")
    print(f"  FR2 sv09-scoped 'KLINK' -> {klink.card_name!r} #{klink.local_id} "
          f"score={klink.score:.1f} ambiguous={klink.ambiguous}")
    check(klink is not None and klink.ambiguous,
          "a scoped tie on the NON-promo path must be ambiguous too "
          "(N's Klink #103 vs N's Klinklang #105)")
    k_res = await resolve_identity(
        [("KLINK", 0.9)], None,
        SessionPrior(set_id="sv09", set_name="Journey Together", denominator=None))
    print(f"  FR2 ladder with an sv09 prior -> confident={k_res.confident} "
          f"set={k_res.set_code} num={k_res.numerator} name={k_res.fields['name']!r}")
    check(not k_res.confident,
          "the KLINK coin-flip must not resolve to a confident identity")
    # Determinism: the ranking input is sorted, so the same query gives the same
    # answer whatever this process's string-hash seed is.
    check(all(idx.match_in_set("KLINK", set_id="sv09").local_id == klink.local_id
              for _ in range(5)),
          "match_in_set must be stable within a process")

    # F3 — the prior rung refuses a bare reading STRUCTURALLY, not via the caller.
    prior = SessionPrior(set_id="23473", set_name="Twilight Masquerade",
                         denominator="167")
    bare = await resolve_identity([], parse_bare_numerator([("126", 0.9)]), prior)
    real = await resolve_identity([], parse_number("126/167", 0.9), prior)
    print(f"  F3 no name + BARE '126' + sv06 prior -> confident={bare.confident} "
          f"num={bare.numerator}   | the same cell reading '126/167' -> "
          f"confident={real.confident}")
    check(not bare.confident,
          "F3: the numerator-in-set rung must refuse a bare reading itself")
    check(real.confident,
          "F3 must not disarm the rung for real readings — control failed")
    rung = inspect.getsource(resolve_identity).split("get_set_numerators")[0]
    check("not reading.bare" in rung.split("if not confident and reading")[-1],
          "F3: the guard must be on the prior rung's own condition")

    # F4 — unconfirmed bare digits never surface.
    print(f"  F4 unconfirmed bare -> numerator={bare.numerator} "
          f"display={bare.display_number} reason={bare.low_confidence_reason} "
          f"key={bare.identity_key!r}")
    check(bare.numerator is None and bare.display_number is None,
          "F4: a flagged cell must not print fabricated bare digits")
    check(bare.low_confidence_reason == "number_ambiguous",
          f"F4: the failing stage is the number, got {bare.low_confidence_reason}")
    ok = await resolve_identity([("TYROGUE", 0.97)],
                                parse_bare_numerator([("071", 0.97)]), None)
    check(ok.confident and ok.display_number == "71",
          "F4 must not suppress a CONFIRMED bare number — control failed")


async def main() -> int:
    probe_join()
    await probe_ladder()
    probe_bare_parse()
    await probe_bare_ladder()
    await probe_pass2()
    probe_pattern_ok_audit()
    await probe_fix_round_1()
    await probe_real_page()

    print("\n=== result ===")
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  ALL PROBE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
