"""Acceptance: corpus pack vs a locally built TWM reference index.
Usage: MATCHER_TOKEN=... python scripts/measure_matcher.py http://127.0.0.1:8181
Requires: matcher running, tests/corpus/IMG_7102.heic present.

Reference images come from images.pokemontcg.io (public, acceptance-only —
the app path builds indexes through the PokéWallet seam instead)."""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

# Corpus pack ground truth, top row → bottom row (read off the photo).
# None = the SVE energy card, which is not part of the sv6 index.
# Per DETECTED row (find_strips ungrided order), verified against the strip
# contact sheet: rows 0-1 are mid-card slices of the top card (no number band),
# row 10 is the SVE energy card — all None.
TRUTH = [None, None, "126", "101", "45", "143", "79", "66", "78", "96", None]


async def main(base: str) -> None:
    import cv2
    from app.pack.pipeline import _decode
    from app.pack.segmentation import find_strips

    headers = {"Authorization": f"Bearer {os.environ['MATCHER_TOKEN']}"}
    async with httpx.AsyncClient(timeout=900.0) as client:
        # _hires: reference embeddings must match training's hires crops — the
        # 245px small variants embed differently and break parity.
        cards = [{"id": f"sv6-{n}", "image_url": f"https://images.pokemontcg.io/sv6/{n}_hires.png"}
                 for n in range(1, 227)]
        r = await client.post(f"{base}/index/sv6", json={"cards": cards}, headers=headers)
        print("index build:", r.status_code, r.text[:200])

        img = _decode(open("tests/corpus/IMG_7102.heic", "rb").read())
        seg = find_strips(img, None)
        print(f"strips: {len(seg.strips)}")
        files = []
        for i, s in enumerate(seg.strips):
            ok, buf = cv2.imencode(".jpg", s.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            files.append(("strips", (f"s{i}.jpg", buf.tobytes(), "image/jpeg")))
        r = await client.post(f"{base}/match/sv6", files=files, headers=headers)
        r.raise_for_status()

        correct = 0
        for i, ranked in enumerate(r.json()):
            want = TRUTH[i] if i < len(TRUTH) else None
            got = ranked[0]["id"].split("-")[1] if ranked else "?"
            hit = want is not None and got == want
            correct += hit
            print(f"row {i}: want={want} top1={ranked[0]['id']}@{ranked[0]['score']}"
                  f" top2={ranked[1]['id']}@{ranked[1]['score']} {'✓' if hit else '✗'}")
        print(f"top-1 accuracy: {correct}/{sum(1 for t in TRUTH if t)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
