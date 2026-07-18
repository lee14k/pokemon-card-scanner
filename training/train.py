"""Stage 2: contrastive training. Batches pair degraded strips with their clean
reference crops; all pairs in a batch come from ONE set (hard negatives).
Usage: python training/train.py --dataset v1 [--epochs 8] [--batch 48] [--run-id r1]"""
import argparse, json, random, time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

from training.config import DATA, IMG_SIZE, RUNS
from training.model import StripEncoder


def letterbox(img_bgr: np.ndarray) -> torch.Tensor:
    im = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    w, h = im.size
    s = IMG_SIZE / max(w, h)
    im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BICUBIC)
    canvas = Image.new("RGB", (IMG_SIZE, IMG_SIZE), (128, 128, 128))
    canvas.paste(im, ((IMG_SIZE - im.width) // 2, (IMG_SIZE - im.height) // 2))
    return torch.from_numpy(np.asarray(canvas, np.float32) / 255.0).permute(2, 0, 1)


def load_pairs(root: Path) -> dict[str, list[tuple[Path, Path]]]:
    """set -> [(strip_path, ref_path)] for the train split."""
    by_set: dict[str, list] = defaultdict(list)
    for line in open(root / "manifest.jsonl"):
        row = json.loads(line)
        if row["split"] != "train" or row["card_key"] is None:
            continue
        ref = root / "refs" / f"{row['card_key']}.jpg"
        if ref.exists():
            by_set[row["set"]].append((root / row["path"], ref))
    return by_set


def nt_xent(a: torch.Tensor, b: torch.Tensor, t: float = 0.07) -> torch.Tensor:
    logits = a @ b.T / t
    labels = torch.arange(a.size(0), device=a.device)
    return (F.cross_entropy(logits, labels) + F.cross_entropy(logits.T, labels)) / 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    root = DATA / args.dataset
    by_set = load_pairs(root)
    n = sum(len(v) for v in by_set.values())
    print(f"train pairs: {n} across {len(by_set)} sets; device={device}")

    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(json.dumps(vars(args) | {"pairs": n}))

    model = StripEncoder().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    rng = random.Random(0)
    sets = list(by_set)

    for epoch in range(args.epochs):
        model.train()
        losses = []
        steps = max(1, n // args.batch)
        for step in range(steps):
            slug = rng.choice(sets)
            batch = rng.sample(by_set[slug], min(args.batch, len(by_set[slug])))
            xs = torch.stack([letterbox(cv2.imread(str(s))) for s, _ in batch]).to(device)
            ys = torch.stack([letterbox(cv2.imread(str(r))) for _, r in batch]).to(device)
            loss = nt_xent(model(xs), model(ys))
            opt.zero_grad(); loss.backward(); opt.step()
            losses.append(float(loss))
            if step % 20 == 0:
                print(f"epoch {epoch} step {step}/{steps} loss={np.mean(losses[-20:]):.4f}")
        torch.save(model.state_dict(), run_dir / "model.pt")
        (run_dir / "metrics.json").write_text(json.dumps(
            {"epoch": epoch, "train_loss": float(np.mean(losses))}))
        print(f"epoch {epoch} done loss={np.mean(losses):.4f} -> {run_dir}/model.pt")


if __name__ == "__main__":
    main()
