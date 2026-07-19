"""Build a band-detector dataset: (scene.jpg, mask.png) pairs where the mask is
the union of card number-band rectangles the synthesizer knows exactly.
Usage: python training/build_band_dataset.py --version bandv1 --scenes 1500 \
    [--sets sv6 sv1 swsh9 ...] [--workers 4]
Splits: seed%10==0 -> val, else train."""
import argparse
import json
import random
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np

from training.config import DATA, DEFAULT_SETS


def _build(job: tuple[str, int, str]) -> dict:
    version, i, slug = job
    from training.synth import synth_scene

    root = DATA / version
    scene, truth = synth_scene(slug, seed=i)
    h, w = scene.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    # band_quads now target the number/set-symbol row directly (synth.py).
    for q in truth.band_quads:
        cv2.fillPoly(mask, [q.astype(np.int32)], 255)
    scene_rel = f"scenes/{i:06d}.jpg"
    mask_rel = f"masks/{i:06d}.png"
    cv2.imwrite(str(root / scene_rel), scene, [cv2.IMWRITE_JPEG_QUALITY, 90])
    cv2.imwrite(str(root / mask_rel), mask)
    return {"scene": scene_rel, "mask": mask_rel,
            "split": "val" if i % 10 == 0 else "train", "bands": len(truth.band_quads)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--scenes", type=int, default=1500)
    ap.add_argument("--sets", nargs="*", default=DEFAULT_SETS)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    root = DATA / args.version
    (root / "scenes").mkdir(parents=True, exist_ok=True)
    (root / "masks").mkdir(parents=True, exist_ok=True)

    rng = random.Random(0)
    jobs = [(args.version, i, rng.choice(args.sets)) for i in range(args.scenes)]
    total_bands = done = 0
    with open(root / "manifest.jsonl", "w") as mf:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for row in ex.map(_build, jobs, chunksize=4):
                mf.write(json.dumps(row) + "\n")
                total_bands += row["bands"]
                done += 1
                if done % 200 == 0:
                    print(f"scene {done}/{args.scenes} bands={total_bands}")
    print(f"band dataset {args.version}: {args.scenes} scenes, {total_bands} bands")


if __name__ == "__main__":
    main()
