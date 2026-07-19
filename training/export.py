"""Stage 4: export a run to ONNX + version file, verify parity vs torch.
Usage: python training/export.py --run <run-id>
Writes matcher/model/strip-embed-<run-id>.onnx + matcher/model/version.json
and updates matcher/model/model.onnx (the served symlink-equivalent copy)."""
import argparse, json, pathlib, shutil

import numpy as np
import torch

from training.config import IMG_H, IMG_W

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
    dummy = torch.rand(2, 3, IMG_H, IMG_W)
    torch.onnx.export(model, dummy, out, input_names=["pixel_values"],
                      output_names=["embedding"],
                      dynamic_axes={"pixel_values": {0: "batch"},
                                    "embedding": {0: "batch"}},
                      external_data=False)  # single self-contained file (committed)
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
        {"model_version": args.run, "embed_dim": ref.shape[1],
         "input_hw": [IMG_H, IMG_W]}))
    print(f"exported {out.name} (parity max err {err:.2e}); "
          f"model.onnx + version.json updated")


if __name__ == "__main__":
    main()
