"""Stage 1 of the runbook: build a versioned dataset.
Usage: python training/build_dataset.py --version v1 --scenes 1500 [--sets sv6 sv1 swsh9]
Splits: seed%10==0 -> val, else train. Also emits reference bottom crops."""
import argparse, json, random

import cv2

from training.config import DATA, DEFAULT_SETS, REF_BOTTOM_FRAC, REFS_RAW
from training.harvest import harvest
from training.synth import synth_scene


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--scenes", type=int, default=1500)
    ap.add_argument("--sets", nargs="*", default=DEFAULT_SETS)
    args = ap.parse_args()

    root = DATA / args.version
    (root / "strips").mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)
    manifest = open(root / "manifest.jsonl", "w")

    # reference bottom crops (the clean tower side)
    for slug in args.sets:
        for p in sorted((REFS_RAW / slug).glob("*.png")):
            im = cv2.imread(str(p))
            h = im.shape[0]
            crop = im[int(h * (1 - REF_BOTTOM_FRAC)):, :]
            out = root / "refs" / f"{p.stem}.jpg"
            cv2.imwrite(str(out), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])

    n_pos = n_neg = 0
    rng = random.Random(0)
    for i in range(args.scenes):
        slug = rng.choice(args.sets)
        scene, truth = synth_scene(slug, seed=i)
        split = "val" if i % 10 == 0 else "train"
        for j, (strip, key) in enumerate(harvest(scene, truth)):
            path = root / "strips" / f"{i:06d}_{j}.jpg"
            cv2.imwrite(str(path), strip, [cv2.IMWRITE_JPEG_QUALITY, 92])
            manifest.write(json.dumps({
                "path": str(path.relative_to(root)), "card_key": key,
                "set": slug, "split": split, "source": "synthetic",
            }) + "\n")
            n_pos += key is not None
            n_neg += key is None
        if i % 200 == 0:
            print(f"scene {i}/{args.scenes} pos={n_pos} neg={n_neg}")
    manifest.close()
    print(f"dataset {args.version}: {n_pos} labeled strips, {n_neg} negatives")


if __name__ == "__main__":
    main()
