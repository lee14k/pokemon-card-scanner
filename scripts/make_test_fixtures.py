"""Generate committed test fixtures: e2e photos + stub card DB.

Usage: .venv/bin/python scripts/make_test_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pack.set_resolution import load_denominator_table  # noqa: E402
from tests.fixtures.synth import make_code_card, make_staircase  # noqa: E402

E2E = Path("tests/fixtures/e2e")
CODE_TEXT = "TEST1-CODE2-CARD3"

# (numerator/denominator, set_code printed on card, set_code for truth, fake name)
# SSH and VIV rows print no code text -> exercises denominator-unique resolution.
# SVI row prints "SVI" -> exercises code-text resolution.
ENTRIES = [
    ("012/202", "", "SSH", "Test Mon A"),
    ("045/185", "", "VIV", "Test Mon B"),
    ("101/198", "SVI", "SVI", "Test Mon C"),
]


def main() -> None:
    table = load_denominator_table()
    cards = []
    truth_rows = []
    for i, (number, printed_code, code, name) in enumerate(ENTRIES):
        entry = table.by_code[code]
        cards.append(
            {
                "id": f"test-{code.lower()}-{i}",
                "set_id": entry.set_id,
                "card_info": {
                    "name": name,
                    "set_name": entry.set_name,
                    "card_number": number,
                    "rarity": "Common",
                },
                "tcgplayer": None,
                "cardmarket": None,
            }
        )
        truth_rows.append({"row_index": i, "number": number, "set_id": entry.set_id})

    Path("tests/fixtures/pokewallet_cards.json").write_text(json.dumps(cards, indent=2) + "\n")
    meta = make_staircase([(n, pc) for n, pc, _, _ in ENTRIES], E2E / "staircase.jpg")
    make_code_card(CODE_TEXT, E2E / "code.jpg")
    (E2E / "truth.json").write_text(
        json.dumps({"capture_meta": meta, "cards": truth_rows, "code": CODE_TEXT}, indent=2) + "\n"
    )
    print(f"Wrote {E2E}/ and stub DB ({len(cards)} cards)")


if __name__ == "__main__":
    main()
