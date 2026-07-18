"""Stage 3: evaluate a run (or exported ONNX) on synth-val + real tiers.
Usage: python training/eval.py --run <run-id> --dataset v1
Registry: training/eval_sets.json — tier "stress" | "standard"; rows list a
card_key or null per detected row (row order = find_strips ungrided output
order for that photo)."""
import argparse, json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch

from training.config import DATA, REF_BOTTOM_FRAC, REFS_RAW, ROOT, RUNS
from training.model import StripEncoder
from training.train import letterbox


def embed_many(model, imgs, device, batch=32):
    out = []
    with torch.no_grad():
        for i in range(0, len(imgs), batch):
            x = torch.stack([letterbox(im) for im in imgs[i:i + batch]]).to(device)
            out.append(model(x).cpu().numpy())
    return np.concatenate(out) if out else np.zeros((0, 256), np.float32)


def ref_index(model, slug, device):
    ids, imgs = [], []
    for p in sorted((REFS_RAW / slug).glob("*.png")):
        im = cv2.imread(str(p))
        h = im.shape[0]
        imgs.append(im[int(h * (1 - REF_BOTTOM_FRAC)):, :])
        ids.append(p.stem)
    return ids, embed_many(model, imgs, device)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--dataset", required=True)
    args = ap.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = StripEncoder().to(device).eval()
    model.load_state_dict(torch.load(RUNS / args.run / "model.pt", map_location=device))

    # synth val
    root = DATA / args.dataset
    by_set = defaultdict(list)
    for line in open(root / "manifest.jsonl"):
        row = json.loads(line)
        if row["split"] == "val" and row["card_key"]:
            by_set[row["set"]].append(row)
    top1 = top3 = total = 0
    for slug, rows in by_set.items():
        ids, refv = ref_index(model, slug, device)
        strips = [cv2.imread(str(root / r["path"])) for r in rows]
        sv = embed_many(model, strips, device)
        sims = sv @ refv.T
        for r, s in zip(rows, sims):
            order = np.argsort(-s)
            top1 += ids[order[0]] == r["card_key"]
            top3 += r["card_key"] in [ids[o] for o in order[:3]]
            total += 1
    print(f"synth-val: top1={top1}/{total} ({top1/max(total,1):.1%}) "
          f"top3={top3}/{total}")

    # real tiers
    import sys
    sys.path.insert(0, str(ROOT.parent))
    from app.pack.pipeline import _decode
    from app.pack.segmentation import find_strips
    tiers = defaultdict(lambda: [0, 0, 0])  # correct1, correct3, total
    for entry in json.loads((ROOT / "eval_sets.json").read_text()):
        img = _decode(Path(entry["photo"]).read_bytes())
        seg = find_strips(img, None)
        ids, refv = ref_index(model, entry["set_slug"], device)
        sv = embed_many(model, [s.image for s in seg.strips], device)
        sims = sv @ refv.T
        for i, s in enumerate(sims):
            want = entry["rows"][i] if i < len(entry["rows"]) else None
            if want is None:
                continue
            order = np.argsort(-s)
            t = tiers[entry["tier"]]
            t[0] += ids[order[0]] == want
            t[1] += want in [ids[o] for o in order[:3]]
            t[2] += 1
            print(f"  [{entry['tier']}] row{i}: want={want} "
                  f"top1={ids[order[0]]}@{s[order[0]]:.3f}")
    for tier, (c1, c3, tot) in tiers.items():
        print(f"{tier}-tier: top1={c1}/{tot} top3={c3}/{tot}")
    if "standard" not in tiers:
        print("standard-tier: no photos yet (deploy gate unmeasurable)")


if __name__ == "__main__":
    main()
