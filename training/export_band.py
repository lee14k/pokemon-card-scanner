"""Export a band-detector run to ONNX for in-app serving + parity check.
Usage: python training/export_band.py --run <run-id>
Writes app/pack/band_model/model.onnx + version.json (served by app)."""
import argparse
import json
import pathlib

import numpy as np
import torch

from training.band_model import INPUT, MASK, BandNet
from training.config import RUNS

APP_MODEL_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "pack" / "band_model"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()

    model = BandNet()
    model.load_state_dict(torch.load(RUNS / args.run / "band.pt", map_location="cpu"))
    model.eval()

    APP_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = APP_MODEL_DIR / "model.onnx"
    dummy = torch.rand(1, 3, INPUT, INPUT)
    torch.onnx.export(model, dummy, out, input_names=["scene"], output_names=["mask"],
                      dynamic_axes={"scene": {0: "batch"}, "mask": {0: "batch"}},
                      external_data=False)

    import onnxruntime as ort
    sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    with torch.no_grad():
        ref = model(dummy).numpy()
    (got,) = sess.run(None, {"scene": dummy.numpy()})
    err = float(np.abs(ref - got).max())
    assert err < 1e-3, f"ONNX parity failed: {err}"
    (APP_MODEL_DIR / "version.json").write_text(json.dumps(
        {"model_version": args.run, "input": INPUT, "mask": MASK}))
    print(f"exported band model.onnx (parity {err:.2e}); version={args.run}")


if __name__ == "__main__":
    main()
