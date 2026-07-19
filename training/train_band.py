"""Stage: train the band detector (BCE + Dice) on synthetic (scene, mask) pairs.
Usage: python training/train_band.py --dataset bandv1 [--epochs 12] [--batch 24]
    [--run-id bandv1a]"""
import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from training.band_model import INPUT, MASK, BandNet
from training.config import DATA, RUNS

_loader = ThreadPoolExecutor(max_workers=8)


def letterbox_scene(path: Path) -> torch.Tensor:
    im = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
    h, w = im.shape[:2]
    s = INPUT / max(h, w)
    nh, nw = max(1, round(h * s)), max(1, round(w * s))
    im = cv2.resize(im, (nw, nh))
    canvas = np.full((INPUT, INPUT, 3), 128, np.uint8)
    canvas[(INPUT - nh) // 2:(INPUT - nh) // 2 + nh,
           (INPUT - nw) // 2:(INPUT - nw) // 2 + nw] = im
    return torch.from_numpy(canvas.astype(np.float32) / 255.0).permute(2, 0, 1)


def letterbox_mask(path: Path) -> torch.Tensor:
    m = cv2.imread(str(path), 0)
    h, w = m.shape
    s = INPUT / max(h, w)
    nh, nw = max(1, round(h * s)), max(1, round(w * s))
    m = cv2.resize(m, (nw, nh), interpolation=cv2.INTER_NEAREST)
    canvas = np.zeros((INPUT, INPUT), np.uint8)
    canvas[(INPUT - nh) // 2:(INPUT - nh) // 2 + nh,
           (INPUT - nw) // 2:(INPUT - nw) // 2 + nw] = m
    small = cv2.resize(canvas, (MASK, MASK), interpolation=cv2.INTER_AREA)
    return torch.from_numpy((small > 127).astype(np.float32))[None]


def _load(row_root):
    row, root = row_root
    return letterbox_scene(root / row["scene"]), letterbox_mask(root / row["mask"])


def dice_loss(logits, target, eps=1.0):
    p = torch.sigmoid(logits)
    num = 2 * (p * target).sum((2, 3)) + eps
    den = p.sum((2, 3)) + target.sum((2, 3)) + eps
    return (1 - num / den).mean()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    root = DATA / args.dataset
    rows = [json.loads(x) for x in open(root / "manifest.jsonl")]
    train = [r for r in rows if r["split"] == "train"]
    print(f"train scenes: {len(train)}; device={device}")

    run_id = args.run_id or time.strftime("band-%Y%m%d-%H%M%S")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(json.dumps(vars(args) | {"n": len(train)}))

    model = BandNet().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    rng = random.Random(0)
    for epoch in range(args.epochs):
        model.train()
        rng.shuffle(train)
        losses = []
        for i in range(0, len(train), args.batch):
            batch = train[i:i + args.batch]
            pairs = list(_loader.map(_load, [(r, root) for r in batch]))
            xs = torch.stack([a for a, _ in pairs]).to(device)
            ys = torch.stack([b for _, b in pairs]).to(device)
            logits = model(xs)
            loss = F.binary_cross_entropy_with_logits(logits, ys) + dice_loss(logits, ys)
            opt.zero_grad(); loss.backward(); opt.step()
            losses.append(float(loss))
            if (i // args.batch) % 20 == 0:
                print(f"epoch {epoch} step {i//args.batch}/{len(train)//args.batch} "
                      f"loss={np.mean(losses[-20:]):.4f}")
        torch.save(model.state_dict(), run_dir / "band.pt")
        (run_dir / "metrics.json").write_text(json.dumps(
            {"epoch": epoch, "loss": float(np.mean(losses))}))
        print(f"epoch {epoch} done loss={np.mean(losses):.4f}")


if __name__ == "__main__":
    main()
