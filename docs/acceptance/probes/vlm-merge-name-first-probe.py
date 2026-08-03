"""Probe: apply_vlm_answer must never display an identity its own guards rejected.

Production case (2026-08-02, prod screenshot == tests/corpus/binder/real/page_1.jpeg
cell 2): the VLM read the printed name "Mega Latias ex" correctly and fabricated
the collector number "130/162". The old merge dropped the exact name hit because
the fabricated denominator contradicted it, resolved 162->Temporal Forces #130
("Skitty"), and left those fields on the card for display even after the name
cross-check refused to accept them.

Four assertions:
  1. Exact-name hit + contradicting denominator -> the NAME wins: card shows
     "Mega Latias ex" / Mega Evolution, stays flagged, the claimed number is not
     merged, and the answer is not accepted (returns False).
  2. Same, but with a claimed numerator that DOES name a real variant of that
     card (163/162 -> me01 #163). A discarded denominator condemns its own
     numerator, so this must still come back display-only and flagged — never
     an accepted identity minted off the rejected number string.
  3. Fuzzy/garbage name + fabricated number -> number-first runs, the name
     cross-check fails, and the merge ROLLS BACK: the card is byte-identical
     to its pre-merge state. Uses denominator 132 rather than the production
     162: offline there is no POKEWALLET_API_KEY and Temporal Forces carries no
     tcgdex_id in the denominator table, so 162 resolves a set but never a
     NAME, leaving the cross-check with nothing to compare and the rollback
     unexercised. 132 -> me01 is nameable from the local catalog alone.
  4. Exact name + correct denominator + variant-picking numerator (181/132)
     still resolves confidently (returns True) — the fix must not break the
     path that works.

The pass-1 card_number is "130/198", which matches no claim any case makes, so
every "the claimed number was not merged" assertion is discriminating.

Needs the local catalog DB (me01 must be ingested). Exit 0 = pass, 2 = BLOCKED.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/vlm-merge-name-first-probe.py
"""
import asyncio
import os
import sys

EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED = 0, 1, 2

PASS1_NUMBER = "130/198"


async def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("BLOCKED: DATABASE_URL unset")
        return EXIT_BLOCKED
    from sqlalchemy import select

    from app.db.models import TcgdexCard
    from app.db.session import async_session_maker
    from app.pack.set_resolution import load_denominator_table
    from app.pack.vlm_merge import apply_vlm_answer
    from app.schemas import PackCard

    try:
        async with async_session_maker() as session:
            row = (await session.execute(
                select(TcgdexCard.name).where(TcgdexCard.set_id == "me01")
                .limit(1))).first()
    except Exception as e:
        print(f"BLOCKED: catalog DB unreachable: {e!r}")
        return EXIT_BLOCKED
    if row is None:
        print("BLOCKED: me01 not ingested (run scripts/ingest_tcgdex.py)")
        return EXIT_BLOCKED

    table = load_denominator_table()
    failures: list[str] = []

    def fresh_card() -> PackCard:
        # The pass-1 state of the production cell: number read, nothing resolved.
        return PackCard(row_index=2, card_number=PASS1_NUMBER, needs_review=True,
                        low_confidence_reason="set_ambiguous", confidence=0.2)

    def check_display_only(case: str, ok: bool, card: PackCard) -> None:
        """The honest name+set, still flagged, pass-1 number untouched."""
        if ok:
            failures.append(f"{case}: accepted an identity built on a "
                            "denominator the merge itself discarded")
        if card.name != "Mega Latias ex":
            failures.append(f"{case}: name={card.name!r}, want 'Mega Latias ex'")
        if (card.set_name or "") != "Mega Evolution":
            failures.append(f"{case}: set_name={card.set_name!r}, want "
                            "'Mega Evolution'")
        if card.card_number != PASS1_NUMBER:
            failures.append(f"{case}: card_number={card.card_number!r} — no part "
                            f"of the claimed number may be merged, want "
                            f"{PASS1_NUMBER!r}")
        if not card.needs_review:
            failures.append(f"{case}: needs_review cleared")

    # 1. Exact name + fabricated denominator: name wins, no Skitty.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "130/162",
               "denominator": 162, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    check_display_only("case1", ok, card)

    # 2. Same, but 163 IS a real me01 printing of this card. The discarded
    #    denominator came off the same glyph run, so the numerator must not be
    #    allowed to pick that variant and mint an accepted identity.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "163/162",
               "denominator": 162, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    check_display_only("case2", ok, card)

    # 3. Garbage name + fabricated number: number-first merge must roll back.
    card = fresh_card()
    before = card.model_dump()
    ok = await apply_vlm_answer(
        card, {"name": "Xyzzy Prime ex", "number": "130/132",
               "denominator": 132, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if ok:
        failures.append("case3: accepted a name-mismatched identity")
    if card.model_dump() != before:
        diff = {k: (before[k], v) for k, v in card.model_dump().items()
                if before[k] != v}
        failures.append(f"case3: guard-rejected merge left fields behind: {diff}")

    # 4. Exact name + true denominator + variant numerator: still resolves.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "181/132",
               "denominator": 132, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if not ok or card.needs_review:
        failures.append("case4: exact name + correct den + variant numerator "
                        f"failed to resolve (ok={ok} flagged={card.needs_review})")
    if card.name != "Mega Latias ex" or card.card_number != "181/132":
        failures.append(f"case4: got {card.name!r} {card.card_number!r}")

    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return EXIT_PASS if not failures else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
