# Embedding Training Pipeline — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Train a two-tower strip-embedding on synthetic staircase scenes and serve it from the existing matcher container, with a repeatable six-stage runbook.

**Architecture:** New dev-only `training/` package (PyTorch/MPS, never deployed): scene synthesizer → strip harvesting through the real segmentation → NT-Xent contrastive training (same-set hard negatives) → ONNX export with normalization baked in → tiered evaluation (stress/standard/synth). The matcher's preprocessing simplifies to letterbox+scale (model-agnostic), and indexes become model-version-stamped.

**Tech Stack:** torch + torchvision (training only), existing onnxruntime matcher, Pillow/OpenCV/numpy.

**Repo rules:** NO automated tests — verification is the smokes/dry-run written into tasks. Machine care: pkill matcher/app servers around smokes; training runs are intentionally compute-heavy — run ONE training process, never concurrently with other heavy work.

Dev env for app-side commands: `DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789`.

## File map

```
training/requirements.txt      # torch, torchvision (dev-only)
training/__init__.py           # empty
training/config.py             # paths + dataset/training constants
training/fetch_refs.py         # hires reference downloader (per set slug)
training/synth.py              # staircase scene synthesizer + degradations
training/harvest.py            # real-segmentation strip harvesting (IoU->labels)
training/build_dataset.py      # stage 1: scenes -> harvested dataset + manifest
training/model.py              # encoder definition (shared by train/export)
training/train.py              # stage 2: contrastive training on MPS
training/eval.py               # stage 3: stress/standard/synth tiers
training/export.py             # stage 4: ONNX + version.json + parity check
training/eval_sets.json        # eval-tier registry (seeded with corpus pack)
docs/training-runbook.md       # the six-stage runbook
matcher/model.py               # MODIFY: raw 0..1 preprocessing (norm baked in ONNX)
matcher/app.py                 # MODIFY: model_version stamping + 409 on stale index
matcher/config.py              # MODIFY: version file path
matcher/Dockerfile             # MODIFY: COPY committed model artifact (no download)
app/matcher_client.py          # MODIFY: treat 409 like 404
.gitignore                     # MODIFY: track matcher/model/*.onnx exports (small), keep big caches out
```

Identity space: training/eval use catalog keys `"<slug>-<number>"` (e.g. `sv6-45`).
The model is id-agnostic (it's an embedding); production indexes keep PokéWallet ids.

---

### Task 1: Scaffolding, config, reference fetcher

**Files:** Create `training/__init__.py` (empty), `training/requirements.txt`, `training/config.py`, `training/fetch_refs.py`.

- [ ] **Step 1:**

```
# training/requirements.txt  (dev-only; NEVER added to app or matcher images)
torch>=2.3
torchvision>=0.18
```

```python
# training/config.py
"""Paths and constants for the training pipeline. All data lives under
training/data/ (gitignored) except exported models (committed via matcher/)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
REFS_RAW = DATA / "refs_raw"        # hires reference card images per set slug
BACKGROUNDS = DATA / "backgrounds"  # real-photo background crops
RUNS = ROOT / "runs"

EMBED_DIM = 256
IMG_SIZE = 224
REF_BOTTOM_FRAC = 0.14   # must match matcher/app.py REF_BOTTOM_FRAC
DEFAULT_SETS = ["sv6", "sv1", "swsh9"]  # phase-1 training sets (~600 cards)
```

- [ ] **Step 2:**

```python
# training/fetch_refs.py
"""Download hires reference images for set slugs (pokemontcg.io public CDN).
Usage: python training/fetch_refs.py sv6 sv1 swsh9"""
import sys, time
import httpx
from training.config import REFS_RAW

def fetch_set(slug: str) -> int:
    out = REFS_RAW / slug
    out.mkdir(parents=True, exist_ok=True)
    n, got = 1, 0
    with httpx.Client(timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": "pokemon-card-scanner-training/1.0"}) as c:
        misses = 0
        while misses < 3:  # sets end where consecutive 404s begin
            p = out / f"{slug}-{n}.png"
            if p.exists():
                got += 1; n += 1; continue
            r = c.get(f"https://images.pokemontcg.io/{slug}/{n}_hires.png")
            if r.status_code == 404:
                misses += 1; n += 1; continue
            r.raise_for_status()
            p.write_bytes(r.content)
            got += 1; misses = 0; n += 1
            time.sleep(0.05)
    print(f"{slug}: {got} images")
    return got

if __name__ == "__main__":
    for slug in sys.argv[1:] or ["sv6"]:
        fetch_set(slug)
```

- [ ] **Step 3: verify** — `.venv/bin/pip install -r training/requirements.txt` then `.venv/bin/python -c "import torch; print(torch.backends.mps.is_available())"` → `True`. `.venv/bin/python training/fetch_refs.py sv6` → `sv6: 226 images` (idempotent on rerun). Add `training/data/` and `training/runs/` to `.gitignore`.

- [ ] **Step 4: commit** `feat(training): scaffolding + hires reference fetcher`

### Task 2: Scene synthesizer

**Files:** Create `training/synth.py`. Also create `training/data/backgrounds/` content: `python -c` snippet below extracts crops from corpus photos.

- [ ] **Step 1:**

```python
# training/synth.py
"""Synthesize staircase-scene photos with per-card ground truth bands.
Deterministic per seed. Cards: hires reference images; look: degradation stack
approximating real user photos (see spec)."""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from training.config import BACKGROUNDS, REFS_RAW


@dataclass
class SceneTruth:
    card_keys: list[str]        # front (fully visible) first
    band_centers: list[float]   # y of each card's visible band center, post-warp
    band_height: float


def _load_refs(slug: str) -> list[Path]:
    return sorted((REFS_RAW / slug).glob("*.png"))


def _rand_background(rng: random.Random, w: int, h: int) -> np.ndarray:
    bgs = sorted(BACKGROUNDS.glob("*.jpg"))
    if bgs and rng.random() < 0.7:
        bg = cv2.imread(str(rng.choice(bgs)))
        return cv2.resize(bg, (w, h))
    base = np.full((h, w, 3), rng.randint(30, 220), np.uint8)
    noise = rng.randint(5, 25)
    return cv2.add(base, cv2.randn(np.zeros((h, w, 3), np.int16), 0, noise).astype(np.uint8))


def _degrade(img: np.ndarray, rng: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    # defocus / motion blur
    if rng.random() < 0.8:
        k = rng.choice([3, 5, 7, 9])
        img = cv2.GaussianBlur(img, (k, k), 0)
    if rng.random() < 0.3:
        k = rng.choice([5, 9, 13])
        kern = np.zeros((k, k), np.float32); kern[k // 2, :] = 1.0 / k
        ang = rng.uniform(0, 180)
        m = cv2.getRotationMatrix2D((k / 2, k / 2), ang, 1.0)
        img = cv2.filter2D(img, -1, cv2.warpAffine(kern, m, (k, k)))
    # haze (contrast lift)
    if rng.random() < 0.6:
        a = rng.uniform(0.05, 0.35)
        img = cv2.addWeighted(img, 1 - a, np.full_like(img, 230), a, 0)
    # glare streaks/blobs
    for _ in range(rng.randint(0, 3)):
        overlay = np.zeros_like(img)
        cx, cy = rng.randint(0, w), rng.randint(0, h)
        ax, ay = rng.randint(w // 8, w // 2), rng.randint(10, h // 6)
        cv2.ellipse(overlay, (cx, cy), (ax, ay), rng.uniform(0, 180), 0, 360,
                    (255, 255, 255), -1)
        overlay = cv2.GaussianBlur(overlay, (0, 0), rng.uniform(15, 60))
        img = cv2.add(img, (overlay * rng.uniform(0.15, 0.5)).astype(np.uint8))
    # color cast + vignette
    if rng.random() < 0.7:
        cast = np.array([rng.uniform(0.85, 1.15) for _ in range(3)])
        img = np.clip(img.astype(np.float32) * cast, 0, 255).astype(np.uint8)
    if rng.random() < 0.5:
        ys, xs = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xs - w / 2) / (w / 2)) ** 2 + ((ys - h / 2) / (h / 2)) ** 2)
        vig = 1 - rng.uniform(0.1, 0.35) * np.clip(d - 0.5, 0, 1)
        img = np.clip(img.astype(np.float32) * vig[..., None], 0, 255).astype(np.uint8)
    # sensor noise
    if rng.random() < 0.7:
        img = cv2.add(img, cv2.randn(np.zeros_like(img, np.int16), 0,
                                     rng.randint(2, 10)).astype(np.uint8))
    # resolution chain + jpeg
    if rng.random() < 0.6:
        s = rng.uniform(0.45, 0.85)
        img = cv2.resize(cv2.resize(img, None, fx=s, fy=s), (w, h))
    q = rng.randint(50, 95)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return cv2.imdecode(enc, cv2.IMREAD_COLOR)


def _finger(img: np.ndarray, rng: random.Random) -> None:
    h, w = img.shape[:2]
    cx, cy = rng.randint(w // 3, w - 1), rng.randint(0, h // 2)
    ax, ay = rng.randint(w // 10, w // 4), rng.randint(h // 8, h // 3)
    tone = (rng.randint(120, 190), rng.randint(140, 200), rng.randint(180, 230))
    overlay = img.copy()
    cv2.ellipse(overlay, (cx, cy), (ax, ay), rng.uniform(-30, 30), 0, 360, tone, -1)
    a = rng.uniform(0.85, 1.0)
    cv2.addWeighted(overlay, a, img, 1 - a, 0, dst=img)


def synth_scene(slug: str, seed: int, k: int | None = None
                ) -> tuple[np.ndarray, SceneTruth]:
    rng = random.Random(seed)
    refs = _load_refs(slug)
    k = k or rng.choice([1, 2, 3, 5, 8, 10, 11, 12])
    picks = rng.sample(refs, min(k, len(refs)))
    card_keys = [p.stem for p in picks]

    card_w = rng.randint(900, 1600)
    card0 = cv2.imread(str(picks[0]))
    ch = int(card0.shape[0] * card_w / card0.shape[1])
    gap = int(ch * rng.uniform(0.07, 0.14))

    W = int(card_w * rng.uniform(1.15, 1.6))
    H = int(ch + gap * (len(picks) - 1) + ch * rng.uniform(0.2, 0.5))
    canvas = _rand_background(rng, W, H)
    x0 = (W - card_w) // 2 + rng.randint(-card_w // 10, card_w // 10)
    y0 = int(ch * rng.uniform(0.05, 0.2))

    band_centers = []
    # draw back-to-front: card i sits i*gap lower; front card (index 0) on top
    for i in reversed(range(len(picks))):
        card = cv2.resize(cv2.imread(str(picks[i])), (card_w, ch))
        y = y0 + i * gap
        ang = rng.uniform(-2.5, 2.5)
        m = cv2.getRotationMatrix2D((card_w / 2, ch / 2), ang, 1.0)
        card = cv2.warpAffine(card, m, (card_w, ch), borderMode=cv2.BORDER_REPLICATE)
        ys, ye = max(0, y), min(H, y + ch)
        xs, xe = max(0, x0), min(W, x0 + card_w)
        canvas[ys:ye, xs:xe] = card[ys - y:ye - y, xs - x0:xe - x0]
    for i in range(len(picks)):
        bottom = y0 + i * gap + ch
        band_centers.append(bottom - gap / 2)

    if rng.random() < 0.5:
        _finger(canvas, rng)

    # global perspective/rotation
    ang = rng.uniform(-8, 8)
    m = cv2.getRotationMatrix2D((W / 2, H / 2), ang, rng.uniform(0.92, 1.0))
    canvas = cv2.warpAffine(canvas, m, (W, H), borderMode=cv2.BORDER_REPLICATE)
    pts = np.array([[[W / 2, y] for y in band_centers]], np.float32)
    band_centers = [float(p[1]) for p in cv2.transform(pts, m)[0]]

    canvas = _degrade(canvas, rng)
    return canvas, SceneTruth(card_keys, band_centers, float(gap))
```

- [ ] **Step 2: backgrounds** — extract crops from the real corpus photos:

```bash
.venv/bin/python - <<'EOF'
import cv2, pathlib
from app.pack.pipeline import _decode
from training.config import BACKGROUNDS
BACKGROUNDS.mkdir(parents=True, exist_ok=True)
for i, p in enumerate(sorted(pathlib.Path("tests/corpus").glob("*.heic"))):
    img = _decode(p.read_bytes())
    h, w = img.shape[:2]
    for j, (y0, x0) in enumerate([(0, 0), (0, w//2), (h//2, 0), (h//2, w//2)]):
        cv2.imwrite(str(BACKGROUNDS / f"bg{i}{j}.jpg"), img[y0:y0+h//2, x0:x0+w//2])
print("backgrounds:", len(list(BACKGROUNDS.glob("*.jpg"))))
EOF
```

- [ ] **Step 3: verify** — generate and eyeball three scenes:

```bash
.venv/bin/python -c "
import cv2
from training.synth import synth_scene
for s in (1, 2, 3):
    img, t = synth_scene('sv6', seed=s)
    print(s, img.shape, t.card_keys[:3], [int(y) for y in t.band_centers[:3]])
    cv2.imwrite(f'training/data/scene{s}.jpg', img)"
```
Open the three jpgs (Read tool / Finder) — they must look like plausible phone photos of fans with visible bottom bands.

- [ ] **Step 4: commit** `feat(training): staircase scene synthesizer with degradation stack`

### Task 3: Harvesting + dataset build

**Files:** Create `training/harvest.py`, `training/build_dataset.py`.

- [ ] **Step 1:**

```python
# training/harvest.py
"""Run the real segmentation over a synthetic scene; label detected strips by
matching row centers to ground-truth bands."""
from __future__ import annotations

import numpy as np

from app.pack.segmentation import find_strips
from training.synth import SceneTruth


def harvest(scene: np.ndarray, truth: SceneTruth) -> list[tuple[np.ndarray, str | None]]:
    """[(strip_bgr, card_key_or_None)] — None = no matching band (negative)."""
    seg = find_strips(scene, None)
    out = []
    for s in seg.strips:
        _, y0, _, h = s.bbox
        center = y0 + h / 2
        dists = [abs(center - c) for c in truth.band_centers]
        j = int(np.argmin(dists)) if dists else -1
        if j >= 0 and dists[j] <= truth.band_height * 0.6:
            out.append((s.image, truth.card_keys[j]))
        else:
            out.append((s.image, None))
    return out
```

```python
# training/build_dataset.py
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
```

- [ ] **Step 2: verify** — `.venv/bin/python training/build_dataset.py --version smoke --scenes 30 --sets sv6` → completes; `wc -l training/data/smoke/manifest.jsonl` ≥ 60; spot-open two strip jpgs and confirm their labels look right against the scene generator (`synth_scene('sv6', seed=<i>)` card order).

- [ ] **Step 3: commit** `feat(training): harvest through real segmentation + dataset builder`

### Task 4: Model + trainer

**Files:** Create `training/model.py`, `training/train.py`.

- [ ] **Step 1:**

```python
# training/model.py
"""Strip encoder: mobilenet_v3_large backbone + projection head -> 256-d.
Normalization is part of forward() so the exported ONNX takes raw 0..1 input."""
import torch
import torch.nn as nn
from torchvision.models import MobileNet_V3_Large_Weights, mobilenet_v3_large

from training.config import EMBED_DIM

_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
_STD = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


class StripEncoder(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        backbone = mobilenet_v3_large(weights=MobileNet_V3_Large_Weights.DEFAULT)
        self.features = backbone.features
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Sequential(
            nn.Linear(960, 512), nn.GELU(), nn.Linear(512, EMBED_DIM),
        )
        self.register_buffer("mean", _MEAN)
        self.register_buffer("std", _STD)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # x: [N,3,224,224] in 0..1
        x = (x - self.mean) / self.std
        x = self.pool(self.features(x)).flatten(1)
        x = self.head(x)
        return nn.functional.normalize(x, dim=1)
```

```python
# training/train.py
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
```

- [ ] **Step 2: verify** — 2-minute sanity run on the smoke dataset:
`.venv/bin/python training/train.py --dataset smoke --epochs 1 --batch 16 --run-id sanity` → loss prints and decreases within the epoch; `training/runs/sanity/model.pt` exists.

- [ ] **Step 3: commit** `feat(training): strip encoder + contrastive trainer (MPS)`

### Task 5: Export + matcher versioning

**Files:** Create `training/export.py`. Modify `matcher/model.py`, `matcher/config.py`, `matcher/app.py`, `matcher/Dockerfile`, `app/matcher_client.py`, `.gitignore`.

- [ ] **Step 1:**

```python
# training/export.py
"""Stage 4: export a run to ONNX + version file, verify parity vs torch.
Usage: python training/export.py --run <run-id>
Writes matcher/model/strip-embed-<run-id>.onnx + matcher/model/version.json
and updates matcher/model/model.onnx (the served symlink-equivalent copy)."""
import argparse, json, pathlib, shutil

import numpy as np
import torch

from training.config import RUNS
from training.model import StripEncoder

MATCHER_MODEL_DIR = pathlib.Path(__file__).resolve().parent.parent / "matcher" / "model"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    run_dir = RUNS / args.run

    model = StripEncoder()
    model.load_state_dict(torch.load(run_dir / "model.pt", map_location="cpu"))
    model.eval()

    MATCHER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = MATCHER_MODEL_DIR / f"strip-embed-{args.run}.onnx"
    dummy = torch.rand(2, 3, 224, 224)
    torch.onnx.export(model, dummy, out, input_names=["pixel_values"],
                      output_names=["embedding"],
                      dynamic_axes={"pixel_values": {0: "batch"},
                                    "embedding": {0: "batch"}})
    # parity check
    import onnxruntime as ort
    sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    with torch.no_grad():
        ref = model(dummy).numpy()
    (got,) = sess.run(None, {"pixel_values": dummy.numpy()})
    err = float(np.abs(ref - got).max())
    assert err < 1e-3, f"ONNX parity failed: {err}"
    shutil.copy(out, MATCHER_MODEL_DIR / "model.onnx")
    (MATCHER_MODEL_DIR / "version.json").write_text(json.dumps(
        {"model_version": args.run, "embed_dim": ref.shape[1]}))
    print(f"exported {out.name} (parity max err {err:.2e}); "
          f"model.onnx + version.json updated")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: matcher preprocessing + versioning.** In `matcher/model.py`: delete the CLIP `_MEAN/_STD` constants and the `(arr - _MEAN) / _STD` line in `_letterbox` (normalization now lives inside the ONNX graph; input is raw 0..1); add:

```python
import json, os

def version() -> str:
    vf = os.path.join(os.path.dirname(config_model_path()), "version.json")
    try:
        return json.loads(open(vf).read()).get("model_version", "unknown")
    except OSError:
        return "unknown"
```
(where `config_model_path` is `matcher.config.model_path` imported at top). In `matcher/app.py`: `index.save(...)` meta gains `"model_version": model.version()`; in `match()`, after loading the index, read its meta via `index.status(...)` and if `meta and meta.get("model_version") != model.version()`: `raise HTTPException(409, "index built with different model")`. In `app/matcher_client.py` `match_strips`: `if r.status_code in (404, 409): return None`.

- [ ] **Step 3: Dockerfile + git tracking.** Replace `matcher/Dockerfile`'s model download stage with a straight copy of the committed artifact:

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY matcher/requirements.txt matcher/requirements.txt
RUN pip install --no-cache-dir -r matcher/requirements.txt
COPY matcher/ matcher/
ENV INDEX_DIR=/data MODEL_PATH=matcher/model/model.onnx
CMD ["sh", "-c", "uvicorn matcher.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

`.gitignore`: replace the blanket `matcher/model/` ignore with rules that track exports but never the old 340MB CLIP file:

```
matcher/model/*
!matcher/model/model.onnx
!matcher/model/strip-embed-*.onnx
!matcher/model/version.json
```
(Exports are ~20MB — committable. Delete the CLIP `model.onnx` before the first export lands so it is never committed; `scripts/fetch_matcher_model.py` is now obsolete — delete it.)

- [ ] **Step 4: verify** — after Task 4's sanity run: `.venv/bin/python training/export.py --run sanity` → parity passes; start the matcher (`MATCHER_TOKEN=t INDEX_DIR=./var/matcher-index .venv/bin/uvicorn matcher.app:app --port 8183`), `/health` ok, build a tiny index, `GET /index/<key>` shows `"model_version": "sanity"`; overwrite `matcher/model/version.json` with a different version and confirm `/match` → 409; restore. pkill matcher.

- [ ] **Step 5: commit** `feat(training): ONNX export + model-version-stamped matcher`

### Task 6: Evaluation tiers

**Files:** Create `training/eval.py`, `training/eval_sets.json`.

- [ ] **Step 1:**

```json
// training/eval_sets.json — registry of real-photo eval packs.
// tier: "stress" | "standard". rows: card_key or null per detected row
// (row order = find_strips ungrided output order for that photo).
[
  {
    "photo": "tests/corpus/IMG_7102.heic",
    "set_slug": "sv6",
    "tier": "stress",
    "rows": [null, null, "sv6-126", "sv6-101", "sv6-45", "sv6-143",
             "sv6-79", "sv6-66", "sv6-78", "sv6-96", null]
  }
]
```

```python
# training/eval.py
"""Stage 3: evaluate a run (or exported ONNX) on synth-val + real tiers.
Usage: python training/eval.py --run <run-id> --dataset v1"""
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
```

- [ ] **Step 2: verify** — `.venv/bin/python training/eval.py --run sanity --dataset smoke` runs end-to-end and prints synth-val + stress-tier numbers (they will be poor for the sanity run — that's expected; the harness working is what's verified).

- [ ] **Step 3: commit** `feat(training): tiered evaluation harness (synth/stress/standard)`

### Task 7: Runbook

**Files:** Create `docs/training-runbook.md`.

- [ ] **Step 1:** write the runbook with the six stages, exact commands (as implemented in Tasks 1–6), the retrain triggers from the spec verbatim, the deploy-gate rule (100% standard-tier top-1 before `MATCHER_URL` is set in prod), the model/index-version behavior (409 ⇒ rebuild via `POST /admin/matcher/index/{set_id}`), and a "machine load" note (training occupies the GPU/CPU for its duration; run one at a time).

- [ ] **Step 2: commit** `docs(training): six-stage retraining runbook`

### Task 8: Full dry run (FOREGROUND — acceptance)

- [ ] Stage 1: `python training/fetch_refs.py sv6 sv1 swsh9` then `python training/build_dataset.py --version v1 --scenes 1500`
- [ ] Stage 2: `python training/train.py --dataset v1 --epochs 8 --batch 48 --run-id v1a` (expect ~1–3h on MPS; machine is busy — coordinate with the user)
- [ ] Stage 3: `python training/eval.py --run v1a --dataset v1` — record all tiers. Acceptance: stress-tier top-1 ≥ 4/8 (beat badge-anchor baseline); synth-val ≥ 90%. If short: iterate documented levers (augmentation strengths, epochs, tile preprocessing, more scenes) before concluding.
- [ ] Stage 4: `python training/export.py --run v1a`
- [ ] Stage 5–6: local "deploy": restart matcher with the new model, rebuild the sv6 index via the API, run `scripts/measure_matcher.py` against it — scores must match eval.py's stress numbers (serving parity).
- [ ] Commit results (runbook gets a "dry run v1a results" appendix; exported model committed).

## Self-review notes

- Spec coverage: synthesizer/harvest (T2–3), trainer/same-set negatives (T4), export+version stamping+409 (T5), tiers incl. standard-empty case (T6), runbook (T7), dry-run acceptance incl. serving parity (T8). Phase-2/3 items (admin screens, flywheel, label templates) are explicitly NOT in this plan.
- Type consistency: `synth_scene -> (ndarray, SceneTruth)`; `harvest -> [(ndarray, str|None)]`; manifest rows `{path, card_key, set, split, source}` consumed identically by train.py and eval.py; `letterbox` shared via import.
- Known risks pinned: MPS training speed (batch 48 may need lowering), synth realism gap (levers listed in T8), eval row-order dependence on `find_strips` (eval_sets.json documents it).
