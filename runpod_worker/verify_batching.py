"""Check that the worker's batched generation answers exactly what one-card-at-a-
time generation answers — on a CPU, with no GPU and no real weights.

    python runpod_worker/verify_batching.py <model_dir>

``<model_dir>`` is any Qwen2.5-VL checkpoint the processor and model load from;
a tiny randomly initialized one is the point (``tiny_model.py`` builds one
offline; it loads in a second and runs on a CPU). On a GPU box the real model
works too, with --bf16. The check is token-for-token on the RAW replies, before JSON parsing: a
random model's replies are noise, and noise is what exposes a batching bug —
any padding, image-token or position-id mistake changes it, where a real model
might shrug a small one off and still print the same JSON.

What it runs, on real card images from the repo (all three request shapes the
app sends — binder cells with a strip, live frames, pack strips with hints —
plus a corrupt image and a corrupt strip):

  1. the job at VLM_MAX_BATCH=1, i.e. one generate() per card, the pre-batching
     behaviour, as the reference;
  2. the same job at batch sizes 2, 4 and 8, every reply compared to (1);
  3. a batch whose generate() raises, to show the per-card fallback reproduces (1);
  4. with --old <file>, an earlier handler.py (e.g. the pre-batching one:
     `git show <rev>:runpod_worker/handler.py > old_handler.py`) on the same
     weights, its raw replies compared to (1).

fp32 by default so "identical" can mean identical; --bf16 runs the worker's real
dtype and reports agreement instead (bf16 batched matmuls are allowed to round
differently, so the occasional flipped near-tie is expected there, not a bug).

Exit 0 when every comparison held.
"""
import argparse
import base64
import importlib.util
import io
import os
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))       # handler.py does `from prompts import ...`

ap = argparse.ArgumentParser()
ap.add_argument("model_dir")
ap.add_argument("--bf16", action="store_true")
ap.add_argument("--old", help="an earlier handler.py to compare against at batch 1")
args = ap.parse_args()
os.environ["VLM_MODEL"] = args.model_dir


def b64(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def job_cards() -> list[dict]:
    """Every request shape the app sends, from real photos in the repo."""
    page = Image.open(REPO / "tests/corpus/binder/synthetic_3x3.jpg")
    w, h = page.size
    cells = [page.crop((c * w // 3, r * h // 3, (c + 1) * w // 3, (r + 1) * h // 3))
             for r in range(3) for c in range(3)]
    cards = []
    for i, cell in enumerate(cells[:4]):                  # binder: full card + strip
        cw, ch = cell.size
        strip = cell.crop((0, int(ch * 0.8), cw, ch))
        cards.append({"row_index": i, "image_b64": b64(cell), "strip_b64": b64(strip),
                      "kind": "full_card", "hint_set": "Twilight Masquerade",
                      "hint_denominator": "167"})
    for i, p in enumerate(sorted((REPO / "tests/corpus/reel").glob("steady_*.png"))[:2]):
        cards.append({"row_index": 10 + i, "image_b64": b64(Image.open(p)),
                      "kind": "full_card", "hint_set": None, "hint_denominator": None})
    stair = Image.open(REPO / "tests/fixtures/e2e/staircase.jpg")
    sw, sh = stair.size
    for i in range(2):                                    # pack: bottom strips, hinted
        band = stair.crop((0, int(sh * (0.55 + 0.15 * i)), sw, int(sh * (0.62 + 0.15 * i))))
        cards.append({"row_index": 20 + i, "image_b64": b64(band), "kind": "strip",
                      "hint_set": "Twilight Masquerade", "hint_denominator": "167"})
    cards.append({"row_index": 30, "image_b64": "not-an-image", "kind": "full_card"})
    cards.append({"row_index": 31, "image_b64": b64(cells[5]),
                  "strip_b64": "also-not-an-image", "kind": "full_card"})
    cards.append({"row_index": 32, "image_b64": b64(cells[6])})      # no kind: old app
    return cards


def load(path: Path, name: str):
    # The pre-change handler starts the RunPod loop at import; make that a no-op.
    import runpod
    runpod.serverless.start = lambda *a, **k: None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    import torch

    failures = []
    h = load(HERE / "handler.py", "handler")
    dtype = torch.bfloat16 if args.bf16 else torch.float32
    h._DTYPE = dtype
    _model, processor = h._load()
    # Every reply any handler decodes, in order — recorded at the processor so the
    # pre-batching handler (--old) is measured by exactly the same instrument.
    replies: list[str] = []
    real_decode = processor.batch_decode

    def recording(*a, **k):
        out = real_decode(*a, **k)
        replies.extend(out)
        return out
    processor.batch_decode = recording
    cards = job_cards()
    rows = [c["row_index"] for c in cards]

    def run(mod, max_batch: int = 1) -> tuple[list[dict], list[str]]:
        mod.MAX_BATCH = max_batch
        replies.clear()
        res = mod.handler({"input": {"cards": cards}})["cards"]
        return res, list(replies)

    ref, ref_replies = run(h, 1)
    print(f"cards={len(cards)} decodable={len(ref_replies)} dtype={dtype} "
          f"model={args.model_dir}")
    errs = [c["row_index"] for c in ref if c.get("error")]
    ok = errs == [30] and [c["row_index"] for c in ref] == rows
    print(f"  {'ok  ' if ok else 'FAIL'} order kept, only the corrupt image errored "
          f"(errors={errs})")
    if not ok:
        failures.append("order/errors")
    print(f"  (sample reply: {ref_replies[0][:60]!r})")

    for mb in (2, 4, 8):
        res, rep = run(h, mb)
        same = rep == ref_replies
        agree = sum(a == b for a, b in zip(rep, ref_replies))
        print(f"  {'ok  ' if same else ('FAIL' if not args.bf16 else 'diff')} batch={mb}: "
              f"{agree}/{len(ref_replies)} replies identical to one-at-a-time")
        if not same and not args.bf16:
            failures.append(f"batch={mb}")
        if [c["row_index"] for c in res] != rows:
            failures.append(f"order batch={mb}")
            print(f"  FAIL batch={mb} reordered rows")

    real_generate = h._generate

    def exploding(model, processor, convs):
        if len(convs) > 1:
            raise torch.cuda.OutOfMemoryError("probe: CUDA out of memory")
        return real_generate(model, processor, convs)
    h._generate = exploding
    res, rep = run(h, 4)
    h._generate = real_generate
    same = rep == ref_replies and [c["row_index"] for c in res] == rows
    print(f"  {'ok  ' if same else 'FAIL'} a batch that runs out of memory falls back "
          f"to one at a time and answers the same")
    if not same:
        failures.append("fallback")

    if args.old:
        old = load(Path(args.old), "old_handler")
        old._load = h._load                    # same weights, processor and dtype
        res, rep = run(old)
        same = rep == ref_replies and [c["row_index"] for c in res] == rows
        print(f"  {'ok  ' if same else 'FAIL'} the pre-batching handler's raw replies "
              f"match ({sum(a == b for a, b in zip(rep, ref_replies))}/{len(ref_replies)})")
        if not same:
            failures.append("old handler")

    print("ALL CHECKS HELD" if not failures else f"FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
