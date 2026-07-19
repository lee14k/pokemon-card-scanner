"""Evaluate the band detector: synthetic mask IoU + band-count error, and the
real-photo number-readable rate (learned vs Hough) on eval_sets.json photos.
Usage: python training/eval_band.py --run <run-id> --dataset bandv1"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import torch

from training.band_model import MASK, BandNet
from training.config import DATA, ROOT, RUNS
from training.train_band import letterbox_mask, letterbox_scene


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--dataset", required=True)
    args = ap.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = BandNet().to(device).eval()
    model.load_state_dict(torch.load(RUNS / args.run / "band.pt", map_location=device))

    # synthetic: pixel IoU + band-count error on val
    root = DATA / args.dataset
    val = [json.loads(x) for x in open(root / "manifest.jsonl") if json.loads(x)["split"] == "val"]
    ious, count_ok, total = [], 0, 0
    with torch.no_grad():
        for r in val:
            x = letterbox_scene(root / r["scene"])[None].to(device)
            pred = (torch.sigmoid(model(x))[0, 0].cpu().numpy() >= 0.5).astype(np.uint8)
            true = (letterbox_mask(root / r["mask"])[0].numpy() >= 0.5).astype(np.uint8)
            inter = (pred & true).sum()
            union = (pred | true).sum()
            ious.append(inter / union if union else 1.0)
            nd = cv2.connectedComponents(pred)[0] - 1
            count_ok += abs(nd - r["bands"]) <= 1
            total += 1
    print(f"synth-val: mean IoU {np.mean(ious):.3f}, count-error<=1 on "
          f"{count_ok}/{total} ({count_ok/max(total,1):.0%})")

    # real: number-readable rate, learned vs Hough
    import sys
    sys.path.insert(0, str(ROOT.parent))
    from app.pack.ocr import read_card_number
    from app.pack.pipeline import _decode
    from app.pack.segmentation import find_strips

    export_dir = ROOT.parent / "app" / "pack" / "band_model"
    have_onnx = (export_dir / "model.onnx").exists()
    if have_onnx:
        from app.pack import band_detector as bd

    def readable(strips):
        return sum(1 for s in strips if read_card_number(s.image).pattern_ok)

    for entry in json.loads((ROOT / "eval_sets.json").read_text()):
        img = _decode(Path(entry["photo"]).read_bytes())
        hough = find_strips(img, None).strips
        h_read = readable(hough)
        line = f"{Path(entry['photo']).name[:14]}: Hough {h_read}/{len(hough)} readable"
        if have_onnx:
            learned = bd.detect_bands(img) or []
            line += f" | band {readable(learned)}/{len(learned)} readable"
        print(line)


if __name__ == "__main__":
    main()
