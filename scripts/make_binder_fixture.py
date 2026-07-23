"""Build the synthetic binder-page gate fixture.

Downloads 9 real TCGdex card images spanning >=3 sets, tiles them into a 3x3
grid on a dark-gray canvas, and writes the page JPEG + a truth.json the Task 5
gate diffs against. Offline is not an error: if the assets can't be fetched the
script prints a skip message and exits 0 (the gate then reports BLOCKED — it
needs the fixture).

Usage: PYTHONPATH=. .venv/bin/python scripts/make_binder_fixture.py
"""
from __future__ import annotations

import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

# Exact card list (set_id, local_id), spanning 6 sets across sv / me / swsh.
# Row-major grid order == truth.json order == binder reading-order row_index.
CARDS: list[tuple[str, str]] = [
    ("sv06", "126"),      # Applin
    ("sv06", "101"),      # Nosepass
    ("me05", "004"),      # Lurantis ex
    ("me05", "010"),      # Centiskorch
    ("me02.5", "001"),    # Erika's Oddish
    ("me02.5", "003"),    # Erika's Vileplume ex
    ("sv03.5", "025"),    # Pikachu (151)
    ("swsh12", "040"),    # Milotic
    ("me01", "020"),      # Ninetales
]

CANVAS_W, CANVAS_H = 2400, 3200
GUTTER = 60
COLS = ROWS = 3
CANVAS_BG = (48, 48, 48)
# Cards are pasted centered at a fixed REALISTIC card aspect (63:88 = 600x838), NOT
# scaled to fill the cell. Real card geometry is deliberate: a card's interior
# artwork band is a ~0.5*card_height vertical text gap that exceeds the inter-card
# gutter gap, so a pure y-gap rule would split each card in two. That failure mode
# must stay exercised by the fixture — binder's number-anchored cell clustering
# (break AFTER the bottom-edge collector number) is what handles it, not squat cards.
CARD_W, CARD_H = 600, 838

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "corpus" / "binder"


def _asset_url(set_id: str, local_id: str) -> str:
    # series dir is the leading alphabetic run of the set id (sv06->sv, me05->me,
    # swsh12->swsh); the local id is zero-padded to 3 in the asset path.
    series = re.match(r"^[a-z]+", set_id).group(0)
    return f"https://assets.tcgdex.net/en/{series}/{set_id}/{local_id.zfill(3)}/high.png"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pcs-binder-fixture/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    images: list[Image.Image] = []
    for set_id, local_id in CARDS:
        url = _asset_url(set_id, local_id)
        try:
            data = _fetch(url)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"SKIP: could not fetch {url} ({e}); offline? Fixture not built.")
            return 0
        images.append(Image.open(io.BytesIO(data)).convert("RGB"))

    cell_w = (CANVAS_W - GUTTER * (COLS + 1)) / COLS
    cell_h = (CANVAS_H - GUTTER * (ROWS + 1)) / ROWS
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), CANVAS_BG)
    for i, im in enumerate(images):
        r, c = divmod(i, COLS)
        card = im.resize((CARD_W, CARD_H), Image.LANCZOS)  # fixed size, centered
        cell_x = GUTTER + c * (cell_w + GUTTER)
        cell_y = GUTTER + r * (cell_h + GUTTER)
        px = int(round(cell_x + (cell_w - CARD_W) / 2))
        py = int(round(cell_y + (cell_h - CARD_H) / 2))
        canvas.paste(card, (px, py))

    jpg_path = OUT_DIR / "synthetic_3x3.jpg"
    canvas.save(jpg_path, "JPEG", quality=88)

    truth = {"cards": [{"set": s, "local_id": lid} for s, lid in CARDS]}
    (OUT_DIR / "truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {jpg_path} ({canvas.width}x{canvas.height}) + truth.json ({len(CARDS)} cards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
