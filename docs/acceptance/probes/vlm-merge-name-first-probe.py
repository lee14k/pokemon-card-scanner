"""Probe: apply_vlm_answer must never display an identity its own guards rejected.

Production case (2026-08-02, prod screenshot == tests/corpus/binder/real/page_1.jpeg
cell 2): the VLM read the printed name "Mega Latias ex" correctly and fabricated
the collector number "130/162". The old merge dropped the exact name hit because
the fabricated denominator contradicted it, resolved 162->Temporal Forces #130
("Skitty"), and left those fields on the card for display even after the name
cross-check refused to accept them.

Three assertions:
  1. Exact-name hit + contradicting denominator -> the NAME wins: card shows
     "Mega Latias ex" / Mega Evolution, stays flagged, claimed number is not
     merged, and the answer is not accepted (returns False).
  2. Fuzzy/garbage name + fabricated number -> number-first runs, the name
     cross-check fails, and the merge ROLLS BACK: the card is byte-identical
     to its pre-merge state. Uses denominator 132 rather than the production
     162: offline there is no POKEWALLET_API_KEY and Temporal Forces carries no
     tcgdex_id in the denominator table, so 162 resolves a set but never a
     NAME, leaving the cross-check with nothing to compare and the rollback
     unexercised. 132 -> me01 is nameable from the local catalog alone.
  3. Exact name + correct denominator + variant-picking numerator (181/132)
     still resolves confidently (returns True) — the fix must not break the
     path that works.

Needs the local catalog DB (me01 must be ingested). Exit 0 = pass, 2 = BLOCKED.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/vlm-merge-name-first-probe.py
"""
import asyncio
import os
import sys

EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED = 0, 1, 2


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
        return PackCard(row_index=2, card_number="130/162", needs_review=True,
                        low_confidence_reason="set_ambiguous", confidence=0.2)

    # 1. Exact name + fabricated denominator: name wins, no Skitty.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "130/162",
               "denominator": 162, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if ok:
        failures.append("case1: accepted an uncorroborated variant-less identity")
    if card.name != "Mega Latias ex":
        failures.append(f"case1: name={card.name!r}, want 'Mega Latias ex'")
    if (card.set_name or "") != "Mega Evolution":
        failures.append(f"case1: set_name={card.set_name!r}, want 'Mega Evolution'")
    if card.card_number != "130/162":
        failures.append(f"case1: card_number={card.card_number!r} — the claimed "
                        "number must not be merged on a display-only name hit")
    if not card.needs_review:
        failures.append("case1: needs_review cleared")

    # 2. Garbage name + fabricated number: number-first merge must roll back.
    card = fresh_card()
    card.card_number = "130/132"
    before = card.model_dump()
    ok = await apply_vlm_answer(
        card, {"name": "Xyzzy Prime ex", "number": "130/132",
               "denominator": 132, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if ok:
        failures.append("case2: accepted a name-mismatched identity")
    if card.model_dump() != before:
        diff = {k: (before[k], v) for k, v in card.model_dump().items()
                if before[k] != v}
        failures.append(f"case2: guard-rejected merge left fields behind: {diff}")

    # 3. Exact name + true denominator + variant numerator: still resolves.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "181/132",
               "denominator": 132, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if not ok or card.needs_review:
        failures.append("case3: exact name + correct den + variant numerator "
                        f"failed to resolve (ok={ok} flagged={card.needs_review})")
    if card.name != "Mega Latias ex" or card.card_number != "181/132":
        failures.append(f"case3: got {card.name!r} {card.card_number!r}")

    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return EXIT_PASS if not failures else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
