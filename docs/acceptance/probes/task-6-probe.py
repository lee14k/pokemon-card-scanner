"""Probe for Task 6 (A3): the binder page-level set prior, two-pass identify.

A3 is the highest-stakes accuracy change in the plan: ``SessionPrior`` is
identity-DECIDING inside ``resolve_identity`` (its own rung promotes a cell to
``confident`` with no VLM-style corroboration), so a prior handed to the wrong
cell mints a confident-WRONG identity directly. The binder gate can't show any of
this — every committed fixture is a page that never earns a prior — so this probe
drives the REAL scan-level functions on cell sets that do:

  A. GUARD      ``_prior_denominator_ok`` — the per-cell veto, case by case:
                absent / agreeing / contradicting denominators, the alpha gallery
                denominators, and the promo prefixes ``parse_number`` emits
                INSTEAD of a denominator.
  B. PASS 2     ``_apply_page_prior`` on pages built from REAL pass-1 results
                (``resolve_identity`` with prior=None, cards via ``_pack_card``):
                (a) a matching-denominator cell is rescued, (b) the same cell
                reading a CONTRADICTING denominator is not — and the same input
                fed to the ladder WITHOUT the guard is shown to come back
                confident-wrong, so the guard is provably load-bearing, (c) a tie
                page elects no prior and pass 2 is a byte-for-byte no-op.
                Plus: pass-1 confident cells are the SAME objects afterwards, and
                ``_improves`` refuses every non-forward move.
  C. VLM        ``_vlm_payload`` — a cell the prior rescued is not sent to the
                worker, and the hint is the prior that was ELECTED, not a
                recount of the post-rescue cells.
  F. REAL PAGES the committed binder photographs, scanned with the page prior
                FORCED to a set they could plausibly elect. The gate cannot see
                any of this (A3 never fires on them), so this is the only place
                pass 2 is exercised against real pixels: the two cells that were
                promoted nameless must stay flagged, no confident cell may lack a
                card name, and the legitimate rescues must survive.
  D. END-TO-END a generated SINGLE-SET sv06 page through ``scan_binder_page``.
                Needs the network (tcgdex card assets, cached under --cache);
                SKIPs cleanly offline, like scripts/make_binder_fixture.py does.
                The generated page is NOT committed.

Not a pytest test (the suite must stay 7 passed / 1 skipped); a one-shot script.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/task-6-probe.py
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://pcs:pcs@localhost:5432/pcs")
os.environ.setdefault("AUTH_SECRET", "dev-secret-not-for-prod-pad-0123456789")
os.environ.setdefault("PHOTO_STORAGE_DIR", "./var/pulls")
os.environ.setdefault("COOKIE_SECURE", "false")

import numpy as np  # noqa: E402

from app.pack.binder import (  # noqa: E402
    BinderCell,
    CellRead,
    _apply_page_prior,
    _denominator_owners,
    _improves,
    _name_contradicts_numerator,
    _pack_card,
    _page_modal,
    _prior_denominator_ok,
    _vlm_payload,
)
from app.pack.identify_core import IdentityResult, SessionPrior, resolve_identity  # noqa: E402
from app.pack.ocr import parse_number  # noqa: E402

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo

failures: list[str] = []

# The two id spaces a binder cell juggles (see task-5 report): card.set_id is the
# denominator-table / PokeWallet space, set_code is the tcgdex id.
TWM = "23473"        # Twilight Masquerade, tcgdex sv06, denominators ('167',)
PAR = "23286"        # Paradox Rift,        tcgdex sv04, denominators ('182',)  SHARED
DRI = "24269"        # Destined Rivals,                  denominators ('182',)  SHARED
SIT_TG = "17674"     # Silver Tempest Trainer Gallery,   denominators ('TG30',) SHARED x4
ME02 = "me02"        # Phantasmal Flames,                denominators ('94',)   private
ME04 = "me04"        # Chaos Rising,                     denominators ('86',)   SHARED x3
SWSHP = "swshp"      # SWSH Black Star Promos, denominators () , prefix SWSH

# Real sv06 / sv04 cards whose NAME+NUMBER agree, so pass 1 resolves them
# confidently with no prior at all (verified against the local catalog).
SV06_CONFIDENT = [("TANGELA", "001/167"), ("TANGROWTH", "002/167"),
                  ("SPINARAK", "004/167")]
SV04_CONFIDENT = [("SURSKIT", "001/182"), ("MASQUERAIN", "002/182"),
                  ("FROSLASS EX", "003/182")]


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL {msg}")


def _tiny():
    """A stand-in cell crop: _vlm_payload only JPEG-encodes it."""
    return np.full((14, 10, 3), 200, dtype=np.uint8)


async def pass1(row: int, name: str | None, number: str | None
                ) -> tuple[BinderCell, CellRead]:
    """One cell's genuine PASS-1 state: the real ladder with prior=None, and the
    real ``_pack_card`` builder — exactly what ``_finish`` produces."""
    reading = parse_number(number, 0.95) if number else None
    name_texts = [(name, 0.95)] if name else []
    res = await resolve_identity(name_texts, reading, None)
    read = CellRead(box=(0, 0, 10, 14), crop=_tiny(), res=res,
                    texts=[number or ""], name_texts=name_texts, reading=reading)
    return (BinderCell(cell=read.box, card=_pack_card(row, res), thumb_b64=None,
                       needs_vlm=not res.confident),
            read)


async def build_page(spec: list[tuple[str | None, str | None]]):
    out = [await pass1(i, n, num) for i, (n, num) in enumerate(spec)]
    return [c for c, _r in out], [r for _c, r in out]


def snapshot(cells: list[BinderCell]) -> list[tuple]:
    return [(c.card.model_dump_json(), c.needs_vlm, id(c.card)) for c in cells]


# ── A. the per-cell guard ────────────────────────────────────────────────────
def probe_guard() -> None:
    print("\n=== A. _prior_denominator_ok (the per-cell veto) ===")
    twm = SessionPrior(set_id=TWM, set_name="Twilight Masquerade", denominator="167")
    par = SessionPrior(set_id=PAR, set_name="Paradox Rift", denominator="182")
    dri = SessionPrior(set_id=DRI, set_name="Destined Rivals", denominator="182")
    tg = SessionPrior(set_id=SIT_TG, set_name="Silver Tempest Trainer Gallery",
                      denominator="TG30")
    me02 = SessionPrior(set_id=ME02, set_name="Phantasmal Flames", denominator="94")
    me04 = SessionPrior(set_id=ME04, set_name="Chaos Rising", denominator="86")
    promo = SessionPrior(set_id=SWSHP, set_name="SWSH Black Star Promos",
                         denominator=None)

    cases = [
        # (label, prior, printed number the cell read, expected)
        ("no reading at all (OCR blind)             ", twm, None, True),
        ("agreeing, PRIVATE denominator 126/167     ", twm, "126/167", True),
        ("CONTRADICTING denominator 126/165         ", twm, "126/165", False),
        ("contradicting denominator 012/202         ", twm, "012/202", False),
        # F2: printed denominators are zero-padded, the table stores the bare
        # count. Before the fix "094" never matched me02's "94" and this branch
        # could not fire at all for 12 of the 53 rows.
        ("ZERO-PADDED 096/094 vs its own me02 set   ", me02, "096/094", True),
        ("zero-padded 096/094 vs a DIFFERENT set    ", twm, "096/094", False),
        # F1: "the modal set prints it" is not enough — it must print it ALONE.
        ("SHARED denominator 005/182 vs Paradox Rift", par, "005/182", False),
        ("SHARED denominator 005/182 vs Dest. Rivals", dri, "005/182", False),
        ("SHARED 005/086 (padded) vs Chaos Rising   ", me04, "005/086", False),
        ("SHARED alpha TG12/TG30 vs its OWN gallery ", tg, "TG12/TG30", False),
        ("alpha gallery denominator vs a normal set ", twm, "TG12/TG30", False),
        ("plain denominator vs a gallery modal set  ", tg, "126/167", False),
        ("promo prefix MEP 037 vs a normal modal set", twm, "MEP 037", False),
        ("promo prefix SWSH123 vs a normal modal set", twm, "SWSH123", False),
        ("promo prefix SWSH123 vs the SWSH promo set", promo, "SWSH123", True),
        ("promo prefix SVP 042 vs the SWSH promo set", promo, "SVP 042", False),
        ("a denominator vs a set that records NONE  ", promo, "126/167", False),
    ]
    for label, prior, number, expected in cases:
        reading = parse_number(number, 0.9) if number else None
        got = _prior_denominator_ok(reading, prior)
        den = reading.denominator if reading else None
        pre = reading.prefix if reading else None
        print(f"  {label} read(den={str(den):6s} prefix={str(pre):5s}) -> {got}")
        check(got is expected,
              f"_prior_denominator_ok {label.strip()}: expected {expected}, got {got}")

    # The two table facts the guard is calibrated against, asserted rather than
    # assumed: if a catalog update changes them, this probe says so.
    from app.pack.set_resolution import load_denominator_table
    tbl = load_denominator_table()
    shared = {d: v for d, v in tbl.by_denominator.items() if len(v) > 1}
    n_shared_rows = sum(len(v) for v in shared.values())
    print(f"  table: {len(tbl.sets)} sets, {len(shared)} SHARED denominators "
          f"covering {n_shared_rows} rows -> those rows can never take a prior")
    for d, v in sorted(shared.items()):
        print(f"    {d:5s} x{len(v)}: " + ", ".join(s.set_name for s in v))
    check(n_shared_rows > 0, "no shared denominators found — re-derive F1's premise")
    check(len(_denominator_owners("182")) == 2
          and len(_denominator_owners("167")) == 1,
          "_denominator_owners no longer distinguishes shared from private")
    # Padding normalization is symmetric and does not invent matches.
    check(_denominator_owners("094") == _denominator_owners("94")
          and _denominator_owners("94") != (),
          "_denominator_owners does not normalize zero-padding")
    check(_denominator_owners("TG30") == _denominator_owners("tg30"),
          "_denominator_owners is case-sensitive on alpha denominators")

    # parse_number really does emit prefix INSTEAD of denominator — the premise
    # the promo branch rests on. If that ever changes the branch is dead code.
    for promo_txt in ("MEP 037", "SWSH123", "SVP 042"):
        r = parse_number(promo_txt, 0.9)
        check(r is not None and r.prefix is not None and r.denominator is None,
              f"parse_number({promo_txt!r}) no longer yields prefix-without-denominator")
    print("  parse_number promo reads carry prefix and NO denominator: True")


# ── B. pass 2 ────────────────────────────────────────────────────────────────
async def probe_apply() -> None:
    print("\n=== B. _apply_page_prior (real pass-1 cells) ===")

    # (a) 3 confident sv06 cells + 1 unresolved cell whose printed denominator
    #     AGREES with the page -> rescued by the prior. The cell reads a name
    #     ("Applin" is ambiguous across printings, so pass 1 cannot place it) and
    #     a number; the prior supplies the set. A rescue must be NAMED — see (a2).
    cells, reads = await build_page(SV06_CONFIDENT + [("APPLIN", "126/167")])
    prior = _page_modal(cells)
    print(f"  (a) page = 3x sv06 confident + 1 unplaced cell reading APPLIN 126/167")
    print(f"      elected prior: set={prior and prior.set_name!r} "
          f"den={prior and prior.denominator!r} set_id={prior and prior.set_id!r}")
    check(prior is not None and prior.set_id == TWM,
          "3 confident sv06 cells did not elect Twilight Masquerade")
    before = snapshot(cells)
    check(cells[3].card.needs_review, "(a) the 4th cell was already confident in pass 1")
    rescued = await _apply_page_prior(cells, reads, prior)
    print(f"      pass-1 flagged rows={[i for i, s in enumerate(before) if '\"needs_review\":true' in s[0]]}"
          f"  rescued rows={rescued}")
    print(f"      rescued cell -> review={cells[3].card.needs_review} "
          f"number={cells[3].card.card_number} set_id={cells[3].card.set_id} "
          f"set={cells[3].card.set_name!r} name={cells[3].card.name!r} "
          f"needs_vlm={cells[3].needs_vlm}")
    check(rescued == [3], f"(a) expected row 3 rescued, got {rescued}")
    check(not cells[3].card.needs_review, "(a) the rescued cell still needs review")
    check(cells[3].card.set_id == TWM,
          f"(a) rescued into set_id {cells[3].card.set_id!r}, expected {TWM!r}")
    check(cells[3].needs_vlm is False, "(a) the rescued cell is still marked needs_vlm")
    check(bool(cells[3].card.name), "(a) the rescued cell has no card name")
    # Confident cells: not merely equal — the SAME PackCard objects.
    check([s[2] for s in before[:3]] == [id(c.card) for c in cells[:3]],
          "(a) a pass-1 confident cell's PackCard was REPLACED")
    check(before[:3] == snapshot(cells)[:3],
          "(a) a pass-1 confident cell changed")
    print(f"      pass-1 confident cells 0-2 are the same objects, unchanged: True")

    # (a2) FIX ROUND 3: the same cell with an UNREADABLE name band. The prior rung
    #      would promote it on "126 exists in sv06" alone, and with nothing able
    #      to name the card the result is a confident row carrying a set and a
    #      number but no identity. Refused; the cell stays flagged for the VLM.
    cells, reads = await build_page(SV06_CONFIDENT + [(None, "126/167")])
    prior = _page_modal(cells)
    before = snapshot(cells)
    rescued = await _apply_page_prior(cells, reads, prior)
    nameless = await resolve_identity(reads[3].name_texts, reads[3].reading, prior)
    print(f"  (a2) same page, the 4th cell's NAME BAND is unreadable "
          f"-> rescued={rescued} review={cells[3].card.needs_review}")
    print(f"       counterfactual (no name requirement): confident="
          f"{nameless.confident} name={nameless.fields.get('name')!r} "
          f"set={nameless.set_name!r} number={nameless.display_number}"
          f"  <- confident with NO card name")
    check(rescued == [] and before == snapshot(cells),
          "(a2) a rescue that cannot name the card was accepted")
    check(cells[3].card.needs_review, "(a2) the nameless cell lost its review flag")
    check(nameless.confident and not nameless.fields.get("name"),
          "(a2) the counterfactual no longer demonstrates the nameless promotion "
          "— re-derive fix round 3's justification before trusting this probe")

    # (b) the SAME cell reading a CONTRADICTING denominator -> no prior for it.
    cells, reads = await build_page(SV06_CONFIDENT + [(None, "126/165")])
    prior = _page_modal(cells)
    before = snapshot(cells)
    rescued = await _apply_page_prior(cells, reads, prior)
    print(f"  (b) same page, the 4th cell reads 126/165 (contradicts /167)")
    print(f"      elected prior: set={prior and prior.set_name!r}  rescued rows={rescued}")
    print(f"      cell 3 -> review={cells[3].card.needs_review} "
          f"set_id={cells[3].card.set_id} reason={cells[3].card.low_confidence_reason}")
    check(prior is not None and prior.set_id == TWM,
          "(b) the page still elects a prior (the veto is PER-CELL, not per-page)")
    check(rescued == [], f"(b) a contradicting-denominator cell was rescued: {rescued}")
    check(before == snapshot(cells), "(b) pass 2 mutated a cell it must not touch")
    check(cells[3].card.needs_review, "(b) the vetoed cell lost its review flag")

    # ...and the guard is what stopped it. Feed the SAME inputs to the ladder
    # directly, with the prior and no veto: it comes back CONFIDENT — in the
    # wrong set, because /165 is the 151 set and 126 happens to exist in sv06.
    unguarded = await resolve_identity(reads[3].name_texts, reads[3].reading, prior)
    print(f"      counterfactual (same inputs, prior applied WITHOUT the guard): "
          f"confident={unguarded.confident} set_id={unguarded.set_id} "
          f"number={unguarded.display_number}")
    check(unguarded.confident and unguarded.set_id == TWM,
          "(b) the counterfactual no longer demonstrates the hazard — re-derive the "
          "guard's justification before trusting this probe")
    print("      => the veto, not the ladder, is what prevents this confident-wrong")

    # (c) a tie page elects nothing, so pass 2 cannot run anywhere.
    cells, reads = await build_page(
        SV06_CONFIDENT + SV04_CONFIDENT + [(None, "126/167")])
    prior = _page_modal(cells)
    before = snapshot(cells)
    rescued = await _apply_page_prior(cells, reads, prior)
    votes = sorted({c.card.set_id for c in cells if not c.card.needs_review})
    print(f"  (c) tie page = 3x sv06 confident + 3x sv04 confident + 1 blind cell")
    print(f"      confident set_ids={votes}  elected prior={prior}  rescued={rescued}")
    check(prior is None, f"(c) a 3-3 tie elected a prior: {prior}")
    check(rescued == [], f"(c) pass 2 ran on a page with no prior: {rescued}")
    check(before == snapshot(cells), "(c) pass 2 mutated cells on a no-prior page")
    check(cells[6].card.needs_review, "(c) the blind cell was silently promoted")

    # A page below the support bar is the same no-op (1 confident cell).
    cells, reads = await build_page(SV06_CONFIDENT[:1] + [(None, "126/167")])
    prior = _page_modal(cells)
    before = snapshot(cells)
    rescued = await _apply_page_prior(cells, reads, prior)
    print(f"  (c2) 1 confident cell (below _MODAL_MIN_SUPPORT) -> prior={prior} "
          f"rescued={rescued}")
    check(prior is None and rescued == [] and before == snapshot(cells),
          "(c2) a single confident cell was allowed to carry the page")

    # prior=None is explicitly a no-op even if a caller hands it one.
    cells, reads = await build_page(SV06_CONFIDENT + [(None, "126/167")])
    before = snapshot(cells)
    check(await _apply_page_prior(cells, reads, None) == []
          and before == snapshot(cells),
          "_apply_page_prior(prior=None) was not a no-op")
    print("  (c3) _apply_page_prior(..., None) is a no-op: True")

    # (e) F7 containment: a NUMBER-BLIND cell is never touched, however
    #     confident the page is. Without this the prior reaches the cell through
    #     the NAME path (match_in_set scoped to the prior's set + the prior's
    #     denominator narrowing idx.match), which the denominator guard cannot
    #     protect because there is no denominator to check.
    for label, name in (("a garbled title 'KYUREM EX'", "KYUREM EX"),
                        ("a bare 'ENERGY'            ", "ENERGY"),
                        ("no name and no number      ", None)):
        cells, reads = await build_page(SV06_CONFIDENT + [(name, None)])
        prior = _page_modal(cells)
        before = snapshot(cells)
        rescued = await _apply_page_prior(cells, reads, prior)
        print(f"  (e) number-blind cell, {label} -> rescued={rescued} "
              f"review={cells[3].card.needs_review}")
        check(rescued == [] and before == snapshot(cells),
              f"(e) a number-blind cell ({label.strip()}) was touched by pass 2")
        check(cells[3].card.needs_review,
              f"(e) a number-blind cell ({label.strip()}) was promoted")

    # (f) F5: a cell whose NAME names no card at its own printed numerator
    #     disagrees with itself -> no rescue. Note every one of these name
    #     matches reports ambiguous=True (the substring hazard fires for common
    #     Pokemon names), which is exactly why the test is "is there a printing
    #     at this number" and not "ambiguous?" / "local_id == numerator".
    twm = SessionPrior(set_id=TWM, set_name="Twilight Masquerade", denominator="167")
    probes = [
        # (label, name read, printed number, expected "contradicts?")
        ("name and number agree             ", "TANGELA", "001/167", False),
        ("name names a DIFFERENT card       ", "TANGELA", "004/167", True),
        ("name names a DIFFERENT card (005) ", "TANGELA", "005/167", True),
        ("no name read at all               ", None, "126/167", False),
        ("several printings, number is one  ", "PINSIR", "168/167", False),
        ("several printings, number is none ", "PINSIR", "005/167", True),
        # The honest sv06 cells the veto must NOT touch (these are exactly the
        # three rescues section D depends on, plus three ordinary cards).
        ("honest sv06 168 (illustration rare)", "PINSIR", "168/167", False),
        ("honest sv06 170 (illustration rare)", "DIPPLIN", "170/167", False),
        ("honest sv06 173 (illustration rare)", "INFERNAPE", "173/167", False),
        ("honest sv06 004                    ", "SPINARAK", "004/167", False),
        ("honest sv06 007                    ", "SUNFLORA", "007/167", False),
        # REGRESSION (fix round 2). The name has NO printing in ANY 167-card set,
        # which is the STRONGEST contradiction — but an earlier version widened
        # the empty narrowing back to every printing in the catalog, found the
        # name at that number in some unrelated set, and let the rescue through.
        # "Twilight Masquerade 017" is Applin, not Pikachu.
        ("ATTACK Pikachu at 017 (mfb has #17)", "PIKACHU", "017/167", True),
        ("ATTACK Pikachu at 025 (151 has #25)", "PIKACHU", "025/167", True),
        ("ATTACK Lapras at 131 (151 has #131)", "LAPRAS", "131/167", True),
    ]
    for label, name, number, expected in probes:
        _c, r = await pass1(0, name, number)
        got = await _name_contradicts_numerator(r, twm)
        print(f"  (f) {label} {str(number):10s} -> contradicts={got}")
        check(got is expected,
              f"(f) _name_contradicts_numerator {label.strip()}: "
              f"expected {expected}, got {got}")

    # An empty narrowing must stay a contradiction — assert the mechanism, not
    # just the three symptoms, so a re-introduced fallback is caught whatever
    # name it is exercised with.
    from app.pack.name_index import get_name_index, normalize_name
    _idx = await get_name_index()
    pika = _idx._entries.get(normalize_name("Pikachu"), ())
    print(f"  (f) 'Pikachu' printings in a 167-card set: "
          f"{[e[2] for e in pika if e[4] == 167]} (empty == the name is not in "
          f"that set at all)")
    check(not [e for e in pika if e[4] == 167],
          "'Pikachu' now HAS a 167-card printing — the attack cases above no "
          "longer test the empty-narrowing path; pick another name")

    # (g) ALPHA denominators (fix round 2): a gallery denominator scopes through
    #     _alpha_den_to_sets, not card_count_official, so the same
    #     name-at-number-in-scope question is actually asked. GG70 (Crown Zenith
    #     Galarian Gallery) is one of the two gallery rows F1 still admits.
    gg = SessionPrior(set_id="17689", set_name="Crown Zenith Galarian Gallery",
                      denominator="GG70")
    for label, name, number, expected in (
            ("Pikachu IS GG30            ", "PIKACHU", "GG30/GG70", False),
            ("Pikachu is NOT GG05 (Lapras)", "PIKACHU", "GG05/GG70", True)):
        _c, r = await pass1(0, name, number)
        got = await _name_contradicts_numerator(r, gg)
        print(f"  (g) {label} {number:10s} -> contradicts={got}")
        check(got is expected,
              f"(g) alpha-denominator scope {label.strip()}: "
              f"expected {expected}, got {got}")
    # ...and it actually blocks the rescue end to end. The counterfactual shows
    # what it blocked: the cell goes CONFIDENT while its own name and its own
    # number name two different cards (Tangela is sv06 001; 005 is Ariados).
    # The second harm F5 covers — the prior rung re-keying the catalog lookup by
    # the read numerator and RENAMING the card — is not asserted here because it
    # only materializes when that card is in the local catalog cache; in this
    # environment the lookup misses and the name falls back to the OCR match.
    cells, reads = await build_page(SV06_CONFIDENT + [("TANGELA", "005/167")])
    prior = _page_modal(cells)
    before = snapshot(cells)
    pass1_name = cells[3].card.name
    rescued = await _apply_page_prior(cells, reads, prior)
    unguarded = await resolve_identity(reads[3].name_texts, reads[3].reading, prior)
    print(f"  (f) page cell reading name 'TANGELA' but number 005/167 "
          f"-> rescued={rescued} (pass-1 name={pass1_name!r})")
    print(f"      counterfactual (no F5 veto): confident={unguarded.confident} "
          f"name={unguarded.fields.get('name')!r} number={unguarded.display_number}"
          f"  <- confident, and 005 is Ariados, not Tangela")
    check(rescued == [] and before == snapshot(cells),
          "(f) a cell whose name contradicts its number was rescued anyway")
    check(unguarded.confident,
          "(f) the counterfactual no longer demonstrates the harm — without the "
          "F5 veto this cell must come back CONFIDENT despite its name and "
          "number naming different cards; re-derive F5's justification")


def probe_improves() -> None:
    print("\n=== B2. _improves — pass 2 may only move a cell FORWARD ===")

    def res(confident, set_id=None, set_name=None, name="Applin"):
        return IdentityResult(
            confident=confident, numerator="126", display_number="126/167",
            set_id=set_id, set_code=None, set_name=set_name,
            fields={"name": name},
            low_confidence_reason=None, identity_key="k", name_match_score=None)

    cases = [
        ("unresolved -> confident + named          ", res(False), res(True, TWM), True),
        ("unresolved -> unresolved (no gain)       ", res(False), res(False), False),
        ("unresolved -> unresolved + a set         ", res(False),
         res(False, TWM, "Twilight Masquerade"), True),
        ("confident  -> confident (never re-run)   ", res(True, TWM), res(True, PAR),
         False),
        ("confident  -> unresolved (a DOWNGRADE)   ", res(True, TWM), res(False), False),
        ("had a set  -> unresolved without one     ",
         res(False, TWM, "Twilight Masquerade"), res(False), False),
        # FIX ROUND 3: a confident result that cannot name the card is a set and a
        # number, not an identity — not an improvement at any strength.
        ("unresolved -> confident but NAMELESS     ", res(False),
         res(True, TWM, "Twilight Masquerade", name=None), False),
        ("unresolved -> confident, empty name      ", res(False),
         res(True, TWM, "Twilight Masquerade", name=""), False),
    ]
    for label, old, new, expected in cases:
        got = _improves(old, new)
        print(f"  {label} -> {got}")
        check(got is expected, f"_improves {label.strip()}: expected {expected}, got {got}")


# ── C. the VLM payload ───────────────────────────────────────────────────────
async def probe_vlm() -> None:
    print("\n=== C. _vlm_payload after pass 2 ===")
    cells, reads = await build_page(
        SV06_CONFIDENT + [("APPLIN", "126/167"), (None, "126/165")])
    prior = _page_modal(cells)
    flagged_1 = sorted(c.card.row_index for c in cells if c.needs_vlm)
    rescued = await _apply_page_prior(cells, reads, prior)
    payload = _vlm_payload(cells, reads, prior)
    sent = sorted(p["row_index"] for p in payload)
    print(f"  pass-1 flagged rows={flagged_1}  rescued={rescued}  sent to VLM={sent}")
    for p in payload:
        print(f"    row={p['row_index']} kind={p['kind']} "
              f"hint_set={p['hint_set']!r} hint_den={p['hint_denominator']!r}")
    check(flagged_1 == [3, 4], f"expected rows 3,4 flagged in pass 1, got {flagged_1}")
    check(rescued == [3], f"expected row 3 rescued, got {rescued}")
    check(sent == [4], f"the rescued cell was still sent to the VLM (sent={sent})")
    check(all(p["kind"] == "full_card" for p in payload),
          "a binder cell went out with a kind other than full_card")
    # F6: row 4 is here BECAUSE its printed /165 contradicts the page. Handing it
    # the modal set as context would push the model toward the one set its own
    # pixels rule out, and an echoed answer can carry an identity through the
    # merge guards — so a vetoed cell must be sent hint-FREE.
    row4 = next(p for p in payload if p["row_index"] == 4)
    check(row4["hint_set"] is None and row4["hint_denominator"] is None,
          "a cell vetoed for contradicting the modal denominator still got the hint")
    print("  the denominator-vetoed cell was sent hint-free: True")

    # ...and a cell that does NOT contradict still gets the hint. Row 4 here is
    # number-blind: pass 2 won't act on it (F7), but it contradicts nothing, so
    # the worker still gets the page context it has always been meant to have.
    cells2, reads2 = await build_page(
        SV06_CONFIDENT + [("APPLIN", "126/167"), ("KYUREM EX", None)])
    prior2 = _page_modal(cells2)
    await _apply_page_prior(cells2, reads2, prior2)
    payload2 = _vlm_payload(cells2, reads2, prior2)
    blind = next(p for p in payload2 if p["row_index"] == 4)
    print(f"  number-blind (non-contradicting) cell hint: "
          f"{blind['hint_set']!r}/{blind['hint_denominator']!r}")
    check(blind["hint_set"] == "Twilight Masquerade"
          and blind["hint_denominator"] == "167",
          "a non-contradicting cell lost the page hint")

    # The hint must be the ELECTED prior, not a recount of the post-pass-2 cells.
    # Here they agree; the point is that _vlm_payload no longer votes at all, so
    # it cannot elect a set off cells the prior itself promoted.
    import inspect

    from app.pack import binder as binder_mod
    src = inspect.getsource(binder_mod._vlm_payload)
    check("_page_modal(" not in src,
          "_vlm_payload still recomputes _page_modal instead of reusing the prior")
    print("  _vlm_payload does not recompute _page_modal: True")

    # A page with no prior still sends its flagged cells, hint-free (today's shape).
    cells, reads = await build_page(
        SV06_CONFIDENT + SV04_CONFIDENT + [(None, "126/167")])
    prior = _page_modal(cells)
    await _apply_page_prior(cells, reads, prior)
    payload = _vlm_payload(cells, reads, prior)
    print(f"  tie page: sent={sorted(p['row_index'] for p in payload)} hints="
          f"{sorted({(p['hint_set'], p['hint_denominator']) for p in payload})}")
    check([p["row_index"] for p in payload] == [6],
          "the tie page sent the wrong cells to the VLM")
    check(payload[0]["hint_set"] is None and payload[0]["hint_denominator"] is None,
          "a page with no elected prior still sent a hint")


# ── D. end to end over a generated single-set page ───────────────────────────
CANVAS_W, CANVAS_H, GUTTER, GRID = 2400, 3200, 60, 3
CARD_W, CARD_H = 600, 838
# A SINGLE-SET sv06 page: six ordinary cards the band OCR resolves on its own,
# plus three illustration rares (168/170/173 — printed past the 167 official
# count) whose full-art faces defeat the set resolution in pass 1. Same tiling
# geometry and constants as scripts/make_binder_fixture.py.
E2E_CARDS = ["001", "002", "004", "005", "006", "007", "168", "170", "173"]
E2E_TRUTH = {"001": "Tangela", "002": "Tangrowth", "004": "Spinarak",
             "005": "Ariados", "006": "Sunkern", "007": "Sunflora",
             "168": "Pinsir", "170": "Dipplin", "173": "Infernape"}


def build_single_set_page(out: Path, cache: Path) -> Path | None:
    """Tile the sv06 cards into a 3x3 page. None when the assets can't be
    fetched (offline) — the same non-error scripts/make_binder_fixture.py uses."""
    from PIL import Image
    cache.mkdir(parents=True, exist_ok=True)
    imgs = []
    for lid in E2E_CARDS:
        p = cache / f"sv06_{lid}.png"
        if not p.exists():
            url = f"https://assets.tcgdex.net/en/sv/sv06/{lid}/high.png"
            req = urllib.request.Request(
                url, headers={"User-Agent": "pcs-binder-fixture/1"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    p.write_bytes(r.read())
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                print(f"  SKIP could not fetch {url} ({e}); offline?")
                return None
        imgs.append(Image.open(io.BytesIO(p.read_bytes())).convert("RGB"))
    cw = (CANVAS_W - GUTTER * (GRID + 1)) / GRID
    ch = (CANVAS_H - GUTTER * (GRID + 1)) / GRID
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (48, 48, 48))
    for i, im in enumerate(imgs):
        r, c = divmod(i, GRID)
        canvas.paste(im.resize((CARD_W, CARD_H), Image.LANCZOS),
                     (int(round(GUTTER + c * (cw + GUTTER) + (cw - CARD_W) / 2)),
                      int(round(GUTTER + r * (ch + GUTTER) + (ch - CARD_H) / 2))))
    canvas.save(out, "JPEG", quality=88)
    return out


async def probe_end_to_end(cache: Path) -> None:
    print("\n=== D. end-to-end: a generated SINGLE-SET sv06 page ===")
    import logging

    from app.pack.binder import scan_binder_page

    with tempfile.TemporaryDirectory() as td:
        page = build_single_set_page(Path(td) / "sv06_single_set.jpg", cache)
        if page is None:
            print("  SKIP (no network) — section D not run")
            return
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, rec): records.append(rec)

        cap = _Cap()
        lg = logging.getLogger("pokemon_scanner.pack.binder")
        lvl = lg.level
        lg.addHandler(cap)
        lg.setLevel(logging.INFO)
        try:
            res = await scan_binder_page(page.read_bytes())
        finally:
            lg.removeHandler(cap)
            lg.setLevel(lvl)

    line = next((r.getMessage() for r in records
                 if r.getMessage().startswith("binder.page_prior")), None)
    print(f"  {line}")
    for c in res["cards"]:
        print(f"    [{c['row_index']}] review={str(c['needs_review']):5s} "
              f"{str(c['card_number']):10s} set_id={str(c['set_id']):8s} "
              f"set_code={str(c['set_code']):6s} {c['name']}")
    check(line is not None and "set=Twilight Masquerade den=167" in line,
          f"the single-set page did not elect Twilight Masquerade ({line})")
    check(line is not None and "rescued=3" in line,
          f"expected 3 rescued cells on the single-set page ({line})")
    confident = [c for c in res["cards"] if not c["needs_review"]]
    check(len(confident) == 9,
          f"expected all 9 cells confident after the prior pass, got {len(confident)}")
    # And every one of them is the RIGHT card — a rescue that mints a wrong
    # identity is worse than no rescue at all.
    names = {c["card_number"]: c["name"] for c in res["cards"]}
    bad = [(n, names.get(f"{lid}/167")) for lid, n in E2E_TRUTH.items()
           if names.get(f"{lid}/167") != n]
    print(f"  every cell matches its printed card: {not bad}"
          + (f"  MISMATCHES {bad}" if bad else ""))
    check(not bad, f"the single-set page produced wrong identities: {bad}")
    check(all(c["set_id"] == TWM for c in res["cards"]),
          "a cell on the single-set page did not land in Twilight Masquerade")
    print("  (the generated page is written to a TemporaryDirectory and is NOT "
          "committed; tests/corpus/binder is untouched)")


# ── F. forced priors over the committed REAL pages ──────────────────────────
# A3 never fires on the committed fixtures, so the gate cannot see any of this.
# Forcing the prior asks what pass 2 WOULD do to real photographs. Priors are
# the catalog rows that uniquely own a denominator those pages actually read —
# i.e. exactly what a page could elect once pass-1 accuracy improves.
#
# (page, denominator to force, [rows that must stay flagged], min named rescues)
FORCED_CASES = [
    # FIX ROUND 3 regressions: both of these promoted a NAMELESS cell to
    # confident before the name requirement, and both scored confident-WRONG.
    ("page_2.jpeg", "191", [1], 0),   # Surging Sparks  -> row 1 "248/191", no name
    ("page_4.jpeg", "94", [5], 0),    # Phantasmal Flames -> row 5 "096/094", no name
    # The legitimate rescues, which the name requirement must NOT touch.
    ("page_2.jpeg", "131", [], 1),    # Prismatic Evolutions -> Teal Mask Ogerpon ex
    ("page_4.jpeg", "217", [], 4),    # Ascended Heroes -> Dustox/Snorunt/Scorbunny/Budew
    ("page_5.jpeg", "217", [], 2),    # Ascended Heroes -> Misdreavus/Banette
]


async def probe_forced_real_pages() -> None:
    print("\n=== F. forced priors over the committed REAL binder pages ===")
    from app.pack import binder as binder_mod
    from app.pack.binder import scan_binder_page

    real = REPO / "tests" / "corpus" / "binder" / "real"
    if not real.exists():
        print(f"  SKIP {real} not present")
        return

    async def scan(name, prior):
        orig = binder_mod._page_modal
        binder_mod._page_modal = lambda cells: prior
        try:
            return await scan_binder_page((real / name).read_bytes())
        finally:
            binder_mod._page_modal = orig

    baselines: dict[str, set[int]] = {}
    for page, den, must_flag, min_rescues in FORCED_CASES:
        if not (real / page).exists():
            print(f"  SKIP {page} not present")
            continue
        if page not in baselines:
            base = await scan(page, None)
            baselines[page] = {c["row_index"] for c in base["cards"]
                               if c["needs_review"]}
        owners = _denominator_owners(den)
        check(len(owners) == 1,
              f"F: /{den} is no longer uniquely owned ({len(owners)} rows) — this "
              f"prior could not be elected, so the case is moot")
        if len(owners) != 1:
            continue
        entry = owners[0]
        prior = SessionPrior(set_id=entry.set_id, set_name=entry.set_name,
                             denominator=entry.denominators[0])
        res = await scan(page, prior)
        flagged_now = {c["row_index"] for c in res["cards"] if c["needs_review"]}
        rescued = sorted(baselines[page] - flagged_now)
        # THE CONTRACT (fix round 3): no confident cell may lack a card name.
        nameless = [c["row_index"] for c in res["cards"]
                    if not c["needs_review"] and not c["name"]]
        print(f"  {page} + forced {entry.set_name!r}/{prior.denominator}: "
              f"rescued={rescued} nameless_confident={nameless}")
        check(not nameless,
              f"F: {page} under {entry.set_name!r} produced confident cell(s) with "
              f"NO card name: rows {nameless}")
        for row in must_flag:
            still = row in flagged_now
            print(f"      row {row} (was a nameless confident-WRONG) stays "
                  f"flagged: {still}")
            check(still,
                  f"F: {page} row {row} was rescued under {entry.set_name!r} — the "
                  f"nameless-rescue regression is back")
        if min_rescues:
            named = [c for c in res["cards"]
                     if c["row_index"] in rescued and c["name"]]
            print(f"      legitimate named rescues: "
                  f"{[(c['row_index'], c['card_number'], c['name']) for c in named]}")
            check(len(named) >= min_rescues,
                  f"F: {page} under {entry.set_name!r} lost legitimate rescues: "
                  f"expected >={min_rescues} named, got {len(named)}")


# ── grep-proof: a pass-1 confident cell cannot be written to ─────────────────
def probe_structure() -> None:
    print("\n=== E. structural proof: pass-1 confident cells are unreachable ===")
    import inspect

    from app.pack import binder as binder_mod
    src = inspect.getsource(binder_mod._apply_page_prior)
    body = src.split('"""', 2)[-1]
    writes = [ln.strip() for ln in body.splitlines()
              if re.search(r"^\s*(cells|reads)\[", ln)]
    print("  every write in _apply_page_prior's body:")
    for w in writes:
        print(f"    {w}")
    check(all(w.startswith(("cells[i]", "reads[i]")) for w in writes),
          f"_apply_page_prior writes through an index other than `i`: {writes}")
    check("for i, new in zip(todo, results):" in body,
          "_apply_page_prior no longer iterates `todo` — the guarantee is gone")
    check("todo = [i for i, bad in zip(candidates, contradicts) if not bad]" in body,
          "`todo` is no longer a subset of `candidates` — re-verify the guarantee")
    for frag in ("if not r.res.confident",
                 "and r.reading is not None and r.reading.numerator",
                 "and _prior_denominator_ok(r.reading, prior)"):
        check(frag in body,
              f"_apply_page_prior's candidate filter lost {frag!r} — re-verify")
    print("  -> `i` only ever comes from `todo`, `todo` is a subset of "
          "`candidates`, and\n     `candidates` is filtered on "
          "`not r.res.confident` (plus a read numerator and the\n     denominator "
          "veto), so no confident cell's index is ever produced. Writes\n     go "
          "through _name_contradicts_numerator and then `_improves` first.")


async def main() -> int:
    cache = Path(os.environ.get("TASK6_ASSET_CACHE")
                 or Path(tempfile.gettempdir()) / "pcs-task6-assets")
    probe_guard()
    await probe_apply()
    probe_improves()
    await probe_vlm()
    probe_structure()
    await probe_forced_real_pages()
    await probe_end_to_end(cache)

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
