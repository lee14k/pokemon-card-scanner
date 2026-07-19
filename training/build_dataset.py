"""Stage 1 of the runbook: build a versioned dataset.
Usage: python training/build_dataset.py --version v1 --scenes 1500 \
    [--sets sv6 sv1 swsh9] [--workers 4]
Splits: seed%10==0 -> val, else train. Also emits reference bottom crops.
Scene synthesis + harvesting parallelize across processes (CPU-bound)."""
import argparse
import json
import random
from concurrent.futures import ProcessPoolExecutor

import cv2

from training.config import DATA, DEFAULT_SETS, REF_BOTTOM_FRAC, REFS_RAW


def _build_scene(job: tuple[str, int, int, str]) -> tuple[int, list[dict]]:
    """Worker: synthesize one scene, harvest strips, write jpgs, return
    manifest rows. Module-level for pickling; heavy imports stay in-worker."""
    version, i, _, slug = job
    from training.harvest import harvest
    from training.synth import synth_scene

    root = DATA / version
    scene, truth = synth_scene(slug, seed=i)
    split = "val" if i % 10 == 0 else "train"
    rows = []
    for j, (strip, key) in enumerate(harvest(scene, truth)):
        path = root / "strips" / f"{i:06d}_{j}.jpg"
        cv2.imwrite(str(path), strip, [cv2.IMWRITE_JPEG_QUALITY, 92])
        rows.append({
            "path": str(path.relative_to(root)), "card_key": key,
            "set": slug, "split": split, "source": "synthetic",
        })
    return i, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--scenes", type=int, default=1500)
    ap.add_argument("--sets", nargs="*", default=DEFAULT_SETS)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    root = DATA / args.version
    (root / "strips").mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)

    # reference bottom crops (the clean tower side)
    for slug in args.sets:
        for p in sorted((REFS_RAW / slug).glob("*.png")):
            im = cv2.imread(str(p))
            h = im.shape[0]
            crop = im[int(h * (1 - REF_BOTTOM_FRAC)):, :]
            cv2.imwrite(str(root / "refs" / f"{p.stem}.jpg"), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])

    rng = random.Random(0)
    jobs = [(args.version, i, 0, rng.choice(args.sets)) for i in range(args.scenes)]
    n_pos = n_neg = done = 0
    with open(root / "manifest.jsonl", "w") as manifest:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for i, rows in ex.map(_build_scene, jobs, chunksize=4):
                for row in rows:
                    manifest.write(json.dumps(row) + "\n")
                    n_pos += row["card_key"] is not None
                    n_neg += row["card_key"] is None
                done += 1
                if done % 200 == 0:
                    print(f"scene {done}/{args.scenes} pos={n_pos} neg={n_neg}")
    print(f"dataset {args.version}: {n_pos} labeled strips, {n_neg} negatives")


if __name__ == "__main__":
    main()
