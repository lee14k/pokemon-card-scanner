"""Run pipeline stages on photos and print JSON; dump crops to scripts/output/debug/.

Usage:
  .venv/bin/python scripts/debug_scan.py --staircase path.jpg [--code path.jpg]
      [--meta path/truth.json] [--stage segment|number|resolve|full]
For --stage full, set POKEWALLET_API_KEY (or POKEWALLET_BASE_URL to the stub).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

OUT = Path("scripts/output/debug")


def _load_bgr(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staircase", required=True)
    ap.add_argument("--code")
    ap.add_argument("--meta", help="truth.json containing capture_meta")
    ap.add_argument("--stage", default="full", choices=["segment", "number", "resolve", "full"])
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    meta = None
    if args.meta:
        meta = json.loads(Path(args.meta).read_text()).get("capture_meta")

    img = _load_bgr(args.staircase)

    from app.pack.segmentation import find_strips

    seg = find_strips(img, meta)
    print(f"segmentation: {len(seg.strips)} strips warning={seg.warning}")
    for s in seg.strips:
        cv2.imwrite(str(OUT / f"strip_{s.row_index}.png"), s.image)
    if args.stage == "segment":
        return

    from app.pack.ocr import read_card_number

    readings = [read_card_number(s.image) for s in seg.strips]
    for s, r in zip(seg.strips, readings):
        print(f"row {s.row_index}: raw={r.raw!r} num={r.numerator} den={r.denominator} "
              f"prefix={r.prefix} conf={r.confidence:.2f} ok={r.pattern_ok}")
    if args.stage == "number":
        return

    from app.pack.set_resolution import resolve_set

    resolutions = [resolve_set(r, s.image) for r, s in zip(readings, seg.strips)]
    for s, res in zip(seg.strips, resolutions):
        print(f"row {s.row_index}: set={res.set_code or res.set_id} method={res.method} "
              f"candidates={[c for c in res.candidates]} margin={res.margin}")
    if args.stage == "resolve":
        return

    from app.pack.pipeline import scan_pack

    code_bytes = Path(args.code).read_bytes() if args.code else b""
    stair_bytes = Path(args.staircase).read_bytes()
    resp = asyncio.run(scan_pack(stair_bytes, code_bytes, meta))
    print(json.dumps(resp.model_dump(), indent=2))


if __name__ == "__main__":
    main()
