"""Stage 1 of the runbook: build a versioned dataset.
Usage: python training/build_dataset.py --version v1 --scenes 1500 \
    [--sets sv6 sv1 swsh9] [--workers 4]
Splits: seed%10==0 -> val, else train. Also emits reference bottom crops.
Scene synthesis + harvesting parallelize across processes (CPU-bound)."""
import argparse
import json
import random
import shutil
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import cv2

from training.config import (DATA, DEFAULT_SETS, REF_BOTTOM_FRAC, REFS_RAW,
                             UPLOADS, tcgdex_card_key_to_ref)


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


def _emit_refs(root, slug) -> int:
    """Write bottom-crop refs for one pokemontcg.io slug; returns count."""
    n = 0
    for p in sorted((REFS_RAW / slug).glob("*.png")):
        im = cv2.imread(str(p))
        h = im.shape[0]
        crop = im[int(h * (1 - REF_BOTTOM_FRAC)):, :]
        cv2.imwrite(str(root / "refs" / f"{p.stem}.jpg"), crop,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        n += 1
    return n


def _merge_uploads(root) -> tuple[list[dict], Counter]:
    """Fold fetched real training strips (training/data/uploads/) into the
    dataset: normalize TCGdex card_keys to pokemontcg.io ref keys, ensure a ref
    crop exists for each set, copy the strip in. Unpairable strips (no ref for
    their set) are skipped and counted, never fatal. Only TRAIN-split uploads
    become training pairs; TEST-split stays for eval.py."""
    manifest_path = UPLOADS / "manifest.json"
    if not manifest_path.exists():
        return [], Counter({"no_uploads": 1})
    strips = json.loads(manifest_path.read_text())["strips"]
    stats: Counter = Counter()
    emitted_slugs: set[str] = set()
    rows: list[dict] = []
    for s in strips:
        if s["split"] != "train":
            stats["skip_not_train"] += 1
            continue
        ref_key = tcgdex_card_key_to_ref(s["card_key"])
        if ref_key is None:
            stats["skip_unmappable_set"] += 1
            continue
        slug = ref_key.rsplit("-", 1)[0]
        if slug not in emitted_slugs:
            if not (REFS_RAW / slug).is_dir():
                stats["skip_no_refs"] += 1
                continue
            _emit_refs(root, slug)
            emitted_slugs.add(slug)
        if not (root / "refs" / f"{ref_key}.jpg").exists():
            stats["skip_no_ref_crop"] += 1
            continue
        src = UPLOADS / s["file"]
        if not src.exists():
            stats["skip_missing_file"] += 1
            continue
        dst_rel = f"strips/upload_{pathlib_stem(s['file'])}.jpg"
        shutil.copy(src, root / dst_rel)
        rows.append({"path": dst_rel, "card_key": ref_key, "set": slug,
                     "split": "train", "source": "upload"})
        stats["merged"] += 1
    return rows, stats


def pathlib_stem(file: str) -> str:
    return file.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--scenes", type=int, default=1500)
    ap.add_argument("--sets", nargs="*", default=DEFAULT_SETS)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--include-uploads", action="store_true",
                    help="fold training/data/uploads/ (real labeled strips) in")
    args = ap.parse_args()

    root = DATA / args.version
    (root / "strips").mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)

    # reference bottom crops (the clean tower side)
    for slug in args.sets:
        _emit_refs(root, slug)

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

        if args.include_uploads:
            upload_rows, stats = _merge_uploads(root)
            for row in upload_rows:
                manifest.write(json.dumps(row) + "\n")
            print(f"uploads merged: {dict(stats)}")

    print(f"dataset {args.version}: {n_pos} labeled strips, {n_neg} negatives")


if __name__ == "__main__":
    main()
