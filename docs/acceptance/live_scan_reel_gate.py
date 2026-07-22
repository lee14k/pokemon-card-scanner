"""Live-scan reel-fixture identify gate (spec acceptance #2).

Re-runs Task 4's fixture smoke against `tests/corpus/reel/*.png` through
`identify_frame`, verbatim call pattern: whole frame as `card_bgr`, its
bottom-25% as `strip_bgr`, `SessionPrior(None, None, "86")`.

Gate: every SHARP fixture that resolves comes back `kind=card` with a
plausible name identity; blurred/no-text frames come back
`unreadable`/`no_card` (never a confident WRONG identity). In particular,
`steady_1.png` (car-dashboard glyphs, no real card text in its top-25% band)
must NOT resolve to a confident card — see `NameIndex.match()`'s
short/garbled-OCR guards in app/pack/name_index.py, added specifically
because this fixture used to false-positive.

Usage:
    export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \\
      AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls \\
      COOKIE_SECURE=false
    .venv/bin/python docs/acceptance/live_scan_reel_gate.py

No server needed — this calls identify_frame directly (only needs DB access
for the name/set index lookups).
"""
import asyncio
import glob

import cv2

from app.pack.live_identify import SessionPrior, identify_frame


async def main():
    for path in sorted(glob.glob("tests/corpus/reel/*.png")):
        img = cv2.imread(path)
        h = img.shape[0]
        strip = img[int(h * 0.75):]
        res = await identify_frame(img, strip, SessionPrior(None, None, "86"))
        card = res.card
        card_tuple = None
        if card is not None:
            card_tuple = (card.card_number, card.set_name, card.name, card.needs_review)
        print(f"{path.split('/')[-1]:16s} -> {res.kind:10s} {str(card_tuple):55s} vlm: {res.needs_vlm}")


if __name__ == "__main__":
    asyncio.run(main())
