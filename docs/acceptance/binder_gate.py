"""Binder-page scan gate — the project's committed accuracy + latency baseline.

Runs ``app.pack.binder.scan_binder_page`` directly (no server) over every
committed binder fixture and scores each page against its ground truth:

  tests/corpus/binder/synthetic_3x3.jpg   + tests/corpus/binder/truth.json
      Synthetic 3x3 sheet of 9 real card images spanning 6 sets. Truth is
      ``{"cards": [{"set": <tcgdex set id>, "local_id": <printed numerator>}]}``
      — and ``set_code`` on a resolved cell IS the tcgdex set id (see
      identify_core.resolve_identity), so the comparison is direct.

  tests/corpus/binder/real/page_1..5.jpeg + tests/corpus/binder/real/truth.json
      Photos of the user's own binder pages. Truth is per-file
      ``{"grid": [r, c], "cards": [{"name": str, "number": str|null}]}`` in
      reading order; ``number: null`` means it wasn't legible to the human either.

Scoring per cell: a cell is FLAGGED when the scan set ``needs_review``; otherwise
it is confident and is CORRECT when it matches a not-yet-consumed truth entry,
else confident-WRONG. Real pages additionally separate NUMDIFF — right card name,
wrong printed numerator (see ``_score_real``) — which is a real defect but not an
identity contradiction, so it is gated against a pinned per-page baseline rather
than against zero (see ``NUMDIFF_BASELINE``). Matching is against the page's truth
as a multiset rather than positionally: the gate's claim is "never confidently
wrong", which must not turn red merely because two cells swapped reading order.

Gate (non-zero exit):
  * any confident-WRONG cell on a real page,
  * a real page's cell count != its truth card count,
  * a real page's NUMDIFF count ABOVE its pinned baseline (a decrease is an
    improvement — it is printed and passes),
  * synthetic confident-correct < 7 of 9.

Exit codes: 0 = PASS, 1 = gate failure, 2 = BLOCKED (env/DB not usable — a
distinct code so CI can tell "not measured" from "measured and bad").

Usage:
    export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \\
      AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \\
      PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false
    .venv/bin/python docs/acceptance/binder_gate.py
    .venv/bin/python docs/acceptance/binder_gate.py --only page_3 --timing

``--only <name>`` runs the subset whose fixture name contains <name>.
``--timing`` turns the scanner's INFO logging on so the ``timing.binder.*``
stage lines print alongside the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SYNTH_IMG = REPO / "tests" / "corpus" / "binder" / "synthetic_3x3.jpg"
SYNTH_TRUTH = REPO / "tests" / "corpus" / "binder" / "truth.json"
REAL_DIR = REPO / "tests" / "corpus" / "binder" / "real"
REAL_TRUTH = REAL_DIR / "truth.json"

SYNTH_MIN_CORRECT = 7        # of 9 — the binder plan's shipped gate

# Pinned per-page NUMDIFF baseline (right card, wrong printed numerator). A numdiff
# is a genuine defect, so it cannot be gated against zero without failing the gate on
# the day it was written — but leaving it ungated would let a regression silently turn
# good numerators into malformed ones. So the count is RATCHETED: above the pin fails,
# at the pin passes, below the pin passes AND is reported as an improvement.
#
# Baseline measured 2026-07-26 (Phase 0). The single entry is page_2 cell 0, where OCR
# reads "EGG10/GG70" for Mew's printed "GG10/GG70" — the right card in the right set
# with a stray leading "E" on the numerator.
#
# WHEN YOU IMPROVE THE SCANNER: lower the pin to the new count in the same commit.
# The gate prints the exact line to change.
NUMDIFF_BASELINE: dict[str, int] = {
    "page_1.jpeg": 0,
    "page_2.jpeg": 1,
    "page_3.jpeg": 0,
    "page_4.jpeg": 0,
    "page_5.jpeg": 0,
}
_NUMDIFF_DEFAULT = 0         # a page with no pin must have zero numdiffs

EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED = 0, 1, 2

_ENV_HINT = (
    "  export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \\\n"
    "    AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \\\n"
    "    PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false"
)


def _blocked(msg: str) -> None:
    """Print a BLOCKED diagnosis and exit 2 — never confusable with a real failure."""
    print(f"\nBLOCKED: {msg}")
    print("This gate needs the local catalog DB (name index + set map). Expected env:")
    print(_ENV_HINT)
    sys.exit(EXIT_BLOCKED)


# ── truth comparison ─────────────────────────────────────────────────────────
def _numerator(display: str | None) -> str | None:
    """Numerator of a printed number ("240/191" -> "240", "GG10/GG70" -> "GG10"),
    normalized the same way the catalog comparison does it."""
    from app.cards import normalize_local_id
    if not display:
        return None
    return normalize_local_id(display.split("/")[0])


def _score(cards: list[dict], expected: list[tuple], keyer) -> tuple[int, int, int, list[str]]:
    """Score a page's cells against ``expected`` (a multiset of truth keys).

    ``keyer(card)`` yields the card's comparison key; a confident cell consumes the
    first equal remaining truth entry. Returns (correct, wrong, flagged, per-cell
    report lines)."""
    remaining = list(expected)
    correct = wrong = flagged = 0
    lines: list[str] = []
    for c in cards:
        key = keyer(c)
        if c.get("needs_review"):
            flagged += 1
            verdict = "flagged"
        elif key in remaining:
            remaining.remove(key)
            correct += 1
            verdict = "correct"
        else:
            wrong += 1
            verdict = "WRONG"
        lines.append(
            f"    [{c.get('row_index')}] {verdict:20s} {str(c.get('card_number')):12s} "
            f"{str(c.get('set_code')):10s} {str(c.get('name'))}"
            + (f"   reason={c['low_confidence_reason']}" if c.get("low_confidence_reason") else "")
        )
    if remaining:
        lines.append(f"    unmatched truth entries: {remaining}")
    return correct, wrong, flagged, lines


def _synthetic_keys(truth: dict) -> list[tuple]:
    from app.cards import normalize_local_id
    return [(c["set"], normalize_local_id(c["local_id"])) for c in truth["cards"]]


def _synthetic_key(card: dict) -> tuple:
    return (card.get("set_code"), _numerator(card.get("card_number")))


def _real_keys(page_truth: dict) -> list[tuple]:
    from app.cards import normalize_local_id
    from app.pack.name_index import normalize_name
    out = []
    for c in page_truth["cards"]:
        num = c.get("number")
        out.append((normalize_name(c["name"]),
                    normalize_local_id(num.split("/")[0]) if num else None))
    return out


def _real_key(card: dict) -> tuple:
    from app.pack.name_index import normalize_name
    return (normalize_name(card.get("name") or ""), _numerator(card.get("card_number")))


def _score_real(cards: list[dict], page_truth: dict) -> tuple[int, int, int, int, list[str]]:
    """Real pages -> (correct, numdiff, wrong, flagged, report lines).

    On a real page the NAME is the identity — that is what the truth file asserts
    ("every listed card identified-or-flagged, ZERO confident-wrong") — so the
    three confident outcomes are kept apart:

      correct  the cell's name matches a truth entry AND its numerator agrees
               (a ``number: null`` truth entry matches on name alone — the human
               couldn't read that number either),
      numdiff  the name matches a truth entry but the printed numerator does not,
               e.g. OCR reading "EGG10/GG70" for "GG10/GG70". The card the user
               gets IS the right card, so this is NOT an identity contradiction —
               it is a printed-number defect, counted separately and RATCHETED
               against ``NUMDIFF_BASELINE`` so it can neither regress unnoticed nor
               fail the gate at its already-measured level,
      WRONG    the cell confidently names a card that is not on the page at all.
               This is the gate: a confident-wrong identity is the one failure the
               binder flow must never produce.
    """
    remaining = _real_keys(page_truth)
    correct = numdiff = wrong = flagged = 0
    lines: list[str] = []
    for c in cards:
        got_name, got_num = _real_key(c)
        if c.get("needs_review"):
            flagged += 1
            verdict = "flagged"
        else:
            # Prefer a truth entry that agrees on the number; fall back to a
            # name-only hit so the same entry can't be scored twice.
            hit = next((e for e in remaining
                        if e[0] == got_name and (e[1] is None or e[1] == got_num)), None)
            if hit is not None:
                remaining.remove(hit)
                correct += 1
                verdict = "correct"
            elif (hit := next((e for e in remaining if e[0] == got_name), None)) is not None:
                remaining.remove(hit)
                numdiff += 1
                verdict = f"numdiff(truth {hit[1]})"
            else:
                wrong += 1
                verdict = "WRONG"
        lines.append(
            f"    [{c.get('row_index')}] {verdict:20s} {str(c.get('card_number')):12s} "
            f"{str(c.get('set_code')):10s} {str(c.get('name'))}"
            + (f"   reason={c['low_confidence_reason']}" if c.get("low_confidence_reason") else "")
        )
    if remaining:
        lines.append(f"    truth entries not confidently identified: {remaining}")
    return correct, numdiff, wrong, flagged, lines


# ── runner ───────────────────────────────────────────────────────────────────
async def _probe_catalog() -> int:
    """Rows in the tcgdex card catalog — doubles as the DB reachability probe
    (the name index and set map both read from it)."""
    from sqlalchemy import func, select

    from app.db.models import TcgdexCard
    from app.db.session import async_session_maker
    async with async_session_maker() as session:
        return int((await session.execute(
            select(func.count()).select_from(TcgdexCard))).scalar_one())


async def _scan(path: Path) -> tuple[dict | None, float, str | None]:
    """(result, wall_ms, error). Only ValueError("no_cards_found") is returned as an
    error — a DB/driver failure is an env problem, so it blocks instead."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.pack.binder import scan_binder_page
    data = path.read_bytes()
    t0 = time.perf_counter()
    try:
        res = await scan_binder_page(data)
    except ValueError as e:
        return None, (time.perf_counter() - t0) * 1000, str(e)
    except (SQLAlchemyError, OSError) as e:
        _blocked(f"scanning {path.name} failed against the DB: {e!r}")
    return res, (time.perf_counter() - t0) * 1000, None


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="run only fixtures whose name contains this")
    ap.add_argument("--timing", action="store_true",
                    help="enable INFO logging so timing.binder.* lines print")
    args = ap.parse_args(argv)

    if not os.environ.get("DATABASE_URL"):
        _blocked("DATABASE_URL is unset")

    warnings.filterwarnings("ignore")
    if args.timing:
        os.environ.setdefault("LOG_LEVEL", "INFO")
        from app.logging_config import configure_logging
        configure_logging()
    else:
        logging.getLogger("pokemon_scanner").setLevel(logging.ERROR)

    try:
        n_cards = await _probe_catalog()
    except Exception as e:  # any driver/connection/schema problem is an env problem
        _blocked(f"catalog DB unreachable: {e!r}")
    if n_cards == 0:
        _blocked("catalog DB has zero tcgdex_card rows — the name index would be "
                 "empty and every cell would flag (run scripts/ingest_tcgdex.py)")
    print(f"catalog: {n_cards} tcgdex_card rows")

    # (name, path, kind) — synthetic first, then the real pages in page order.
    fixtures: list[tuple[str, Path, str]] = []
    if SYNTH_IMG.exists() and SYNTH_TRUTH.exists():
        fixtures.append((SYNTH_IMG.name, SYNTH_IMG, "synthetic"))
    real_truth: dict = json.loads(REAL_TRUTH.read_text()) if REAL_TRUTH.exists() else {}
    for name in sorted(k for k in real_truth if not k.startswith("_")):
        p = REAL_DIR / name
        if p.exists():
            fixtures.append((name, p, "real"))
    if args.only:
        fixtures = [f for f in fixtures if args.only in f[0]]
    if not fixtures:
        _blocked(f"no fixtures to run (--only={args.only!r} matched nothing)")

    synth_truth: dict = json.loads(SYNTH_TRUTH.read_text()) if SYNTH_TRUTH.exists() else {}
    failures: list[str] = []
    improvements: list[str] = []
    summaries: list[str] = []
    tot_correct = tot_numdiff = tot_pin = tot_wrong = tot_flagged = 0
    tot_ms = 0.0

    for name, path, kind in fixtures:
        print(f"\n=== {name} ({kind}) ===")
        res, wall_ms, err = await _scan(path)
        tot_ms += wall_ms
        if res is None:
            summaries.append(f"FAIL {name:18s} scan raised {err!r}  wall={wall_ms:8.0f}ms")
            failures.append(f"{name}: scan raised {err!r}")
            continue

        cards = res["cards"]
        grid = res["grid"]
        if kind == "synthetic":
            expected_n = len(synth_truth["cards"])
            # Synthetic truth is set + local_id (no names), so its match is the
            # strict (set_code, numerator) pair — no numdiff bucket applies.
            correct, wrong, flagged, lines = _score(
                cards, _synthetic_keys(synth_truth), _synthetic_key)
            numdiff, pin = 0, None
            page_fail = [] if correct >= SYNTH_MIN_CORRECT else [
                f"{name}: confident-correct {correct}/{expected_n} "
                f"< required {SYNTH_MIN_CORRECT}"]
        else:
            page_truth = real_truth[name]
            expected_n = len(page_truth["cards"])
            correct, numdiff, wrong, flagged, lines = _score_real(cards, page_truth)
            page_fail = []
            if wrong:
                page_fail.append(f"{name}: {wrong} confident-WRONG cell(s)")
            if len(cards) != expected_n:
                page_fail.append(
                    f"{name}: found {len(cards)} cells, truth has {expected_n}")
            # Ratchet the numdiff count against its pin (see NUMDIFF_BASELINE).
            pin = NUMDIFF_BASELINE.get(name, _NUMDIFF_DEFAULT)
            if numdiff > pin:
                page_fail.append(
                    f"{name}: {numdiff} numdiff cell(s) exceeds the pinned baseline "
                    f"{pin} — a printed numerator regressed")
            elif numdiff < pin:
                improvements.append(
                    f"{name}: numdiff {pin} -> {numdiff}; lower its pin in "
                    f"NUMDIFF_BASELINE (docs/acceptance/binder_gate.py)")

        print(f"  grid={grid['rows']}x{grid['cols']} cells={len(cards)}/{expected_n} "
              f"page_conf={res['page_confidence']:.3f} wall={wall_ms:.0f}ms")
        for line in lines:
            print(line)
        failures += page_fail
        tot_correct += correct
        tot_numdiff += numdiff
        tot_pin += pin or 0
        tot_wrong += wrong
        tot_flagged += flagged
        summaries.append(
            f"{'PASS' if not page_fail else 'FAIL'} {name:18s} "
            f"cells={len(cards)}/{expected_n} correct={correct} "
            f"numdiff={numdiff}/{'-' if pin is None else pin} "
            f"wrong={wrong} flagged={flagged} wall={wall_ms:8.0f}ms")

    print("\n=== summary ===")
    print("  (numdiff=<found>/<pinned baseline>; above the pin fails, below improves)")
    for s in summaries:
        print("  " + s)
    print(f"  TOTAL correct={tot_correct} numdiff={tot_numdiff}/{tot_pin} "
          f"confident-wrong={tot_wrong} flagged={tot_flagged} wall={tot_ms:.0f}ms "
          f"({tot_ms / max(1, len(fixtures)):.0f}ms/page avg)")
    if tot_numdiff:
        print(f"  NOTE {tot_numdiff} numdiff cell(s) at/below the pinned baseline: "
              f"right card, wrong printed numerator — a tracked defect held flat by "
              f"the ratchet, not yet fixed.")
    if improvements:
        print("\nIMPROVED (passes — pin is now stale):")
        for i in improvements:
            print(f"  - {i}")
    if failures:
        print("\nGATE FAIL:")
        for f in failures:
            print(f"  - {f}")
        return EXIT_FAIL
    print("\nGATE PASS")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
