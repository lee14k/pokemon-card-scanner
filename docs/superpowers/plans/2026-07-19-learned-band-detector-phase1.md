# Learned Band Detector — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** A learned segmentation model that locates card number-bands in a staircase photo, replacing geometric band-slicing, served in-app with Hough fallback.

**Architecture:** The scene synthesizer emits band rectangles (it knows them); `build_band_dataset.py` rasterizes masks; a compact MobileNetV3-small encoder + light decoder predicts a band-probability mask (PyTorch/MPS → ONNX); `app/pack/band_detector.py` runs it via onnxruntime, connected-components → deskewed strips; `find_strips` uses it when `PACK_BAND_DETECTOR=1` + model present, else Hough. Never breaks a scan.

**Tech Stack:** torch/torchvision (train only), onnxruntime (app), OpenCV, numpy.

Repo rules: NO automated tests (smokes only). Machine care: one training run at a time; pkill app servers around smokes. Dev env as usual.

## File map
```
training/synth.py           # MODIFY: SceneTruth.band_quads + compute/warp them
training/band_model.py      # encoder-decoder seg net (shared train/export)
training/build_band_dataset.py  # scenes -> (scene.jpg, mask.png) + manifest
training/train_band.py      # BCE+Dice trainer (MPS)
training/export_band.py     # -> app/pack/band_model/model.onnx + version.json
training/eval_band.py       # synthetic IoU/count + real number-readable rate
app/pack/band_detector.py   # onnxruntime inference -> Strip list; never raises
app/pack/segmentation.py    # MODIFY: learned path in find_strips (gated, fallback)
app/pack/config.py          # MODIFY: band_detector settings
requirements.txt            # MODIFY: onnxruntime
app/pack/band_model/model.onnx + version.json  # committed (small); .gitignore allow
docs/training-runbook.md    # MODIFY: band-detector stages
```
Model input: 384×384 letterboxed (aspect preserved — whole-scene geometry). Output: 1-ch logit mask 96×96, upsampled to source coords at serve time.

## Task 1: Synth band rectangles
- [ ] Modify `training/synth.py`: add `band_quads: list` to `SceneTruth`; in `synth_scene`, before the global warp, build each card's pre-warp band rect `[(x0,bottom-gap),(x0+card_w,bottom-gap),(x0+card_w,bottom),(x0,bottom)]`; transform all quads' corners through the global warp `m` (cv2.transform); return them. Clip corners to image bounds at raster time, not here.
- [ ] Verify: `python -c` synthesize seed 1, draw quads on the scene, save, eyeball that green quads sit on the number bands.
- [ ] Commit `feat(band): synth emits per-card band rectangles`

## Task 2: Band dataset builder
- [ ] `training/build_band_dataset.py`: for N scenes (parallel like build_dataset), save `scenes/<i>.jpg` + `masks/<i>.png` (uint8 0/255, band quads filled via `cv2.fillPoly`, same HxW as scene), manifest.jsonl rows `{"scene","mask","split"}` (seed%10==0 → val). CLI `--version --scenes --sets --workers`.
- [ ] Verify: build 20 scenes; confirm mask.png has white band regions; count files.
- [ ] Commit `feat(band): band-mask dataset builder`

## Task 3: Segmentation model
- [ ] `training/band_model.py`: `BandNet(nn.Module)` — mobilenet_v3_small(features) encoder; take an intermediate + final feature; a small FPN-ish decoder upsampling to 96×96, 1 output channel (logits). Normalization baked into forward (ImageNet mean/std) so ONNX takes raw 0..1. `INPUT=384`, `MASK=96` constants.
- [ ] Verify: instantiate, forward a random `(2,3,384,384)` → `(2,1,96,96)`.
- [ ] Commit `feat(band): segmentation model`

## Task 4: Trainer
- [ ] `training/train_band.py`: dataset loads scene (letterbox to 384, raw 0..1) + mask (resize to 96, 0/1 float); loss = BCEWithLogits + Dice; AdamW; MPS; checkpoints to `runs/<id>/band.pt` + metrics. CLI `--dataset --epochs --batch --run-id`. Threaded image load.
- [ ] Verify: 1-epoch sanity on the 20-scene set; loss prints + decreases; band.pt exists.
- [ ] Commit `feat(band): BCE+Dice trainer`

## Task 5: ONNX export + app model dir
- [ ] `training/export_band.py`: load run → ONNX `app/pack/band_model/model.onnx` (dynamic batch), parity check vs torch (<1e-3), write `version.json {model_version, input, mask}`. `.gitignore`: allow `app/pack/band_model/*.onnx` + version.json.
- [ ] `requirements.txt`: add `onnxruntime>=1.17`.
- [ ] Verify: export sanity run; onnxruntime loads it; output shape matches.
- [ ] Commit `feat(band): ONNX export + onnxruntime dep`

## Task 6: In-app served detector
- [ ] `app/pack/band_detector.py`: lazy-load ONNX (module-global session, path `app/pack/band_model/model.onnx`, honor `input`/`mask` from version.json); `detect_bands(img_bgr) -> list[Strip] | None`. Pipeline: letterbox to input → infer → sigmoid → upscale mask to source HxW (undo letterbox padding) → threshold (`PACK_BAND_THRESHOLD`, default 0.5) → morphological close → `connectedComponents` → per component: area/aspect filter, `minAreaRect` → deskew crop (reuse the rotate logic from `_extract_strip`; crop the rotated rect region full-width per band) → order top→bottom by centroid y → `Strip(row_index, image, bbox, angle)`. Return None if session unavailable/no components. NEVER raise (catch all → None).
- [ ] Verify: with a trained model present, run on `tests/corpus/IMG_7102.heic`; print component count + that crops are non-empty.
- [ ] Commit `feat(band): in-app onnxruntime band detector`

## Task 7: Wire into find_strips
- [ ] `app/pack/config.py`: add `band_detector: bool` (`PACK_BAND_DETECTOR`, default False), `band_threshold: float` (`PACK_BAND_THRESHOLD`, 0.5).
- [ ] `app/pack/segmentation.py`: in `find_strips`, ungrided path only (capture_meta None): if `cfg.band_detector`, try `from app.pack.band_detector import detect_bands`; `strips = detect_bands(img)`; if non-empty, wrap in `SegmentationResult(strips, warning=None)` and return; else fall through to current Hough. Any import/inference error → Hough. Guided path unchanged.
- [ ] Verify: `PACK_BAND_DETECTOR` unset → byte-identical current fixture behavior; suite green. With it set + model → scan returns strips.
- [ ] Commit `feat(band): gated learned segmentation path with Hough fallback`

## Task 8: Acceptance harness + runbook
- [ ] `training/eval_band.py`: synthetic val — mean IoU of greedily-matched detected vs true band boxes + count-error stats; real — for each `eval_sets.json` photo, run `detect_bands` and the current Hough `find_strips`, crop strips, run `read_card_number` on each, report **number-readable rate** (valid NN/NNN or promo) for both. CLI `--run`.
- [ ] Runbook: append band-detector stages (build → train → eval → export → serve via `PACK_BAND_DETECTOR=1`).
- [ ] Commit `feat(band): acceptance harness + runbook`

## Task 9: Full dry run (FOREGROUND)
- [ ] Build `bandv1` (≥1500 scenes, all 6 fetched sets), train (~10–15 epochs MPS), eval, export.
- [ ] Acceptance: synthetic IoU ≥0.7 & count-error ≤1 on ≥90%; real number-readable rate beats Hough on the corpus photos. Iterate augmentation/epochs if short.
- [ ] Commit results appendix; the exported model.

## Self-review
- Serving reuses the `Strip` dataclass so nothing downstream changes.
- `PACK_BAND_DETECTOR` unset ⇒ exact current behavior (regression-checked T7).
- onnxruntime already builds on Railway (matcher uses it) — app gaining it is low-risk.
