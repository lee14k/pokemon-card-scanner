# Learned Number-Band Detector (Sub-project J) — Design

## Problem

The scanner's `find_strips` locates each card's bottom strip geometrically:
Canny → HoughLinesP → cluster near-horizontal lines → cut fixed-height bands.
On real handheld photos this is fragile against uneven card spacing, tilt,
curved fan edges, and glare — the slices frequently miss the number+set-symbol
region entirely (user-observed on new corpus photos; the guessed-template
alternative also proved brittle in probing: 0–18 anchors across the same set).

The number band is a **learnable visual target**, and our scene synthesizer
already knows exactly where every band is when it composites a scene — giving
unlimited, perfectly-annotated training data at zero labeling cost. Replace the
geometric band-finding with a small learned detector.

## Approach

Frame it as **band-region segmentation**, not general object detection: the
bands are the thing we want, they are roughly-horizontal full-width-ish
rectangles, and there can be any number of them. A compact encoder-decoder
predicts a single-channel "band probability" mask; connected components on the
thresholded mask become the strips (rotated bounding box per component →
deskewed crop, reusing the existing `_extract_strip` deskew). This is robust to
count (peaks, not a fixed grid), spacing (learned), tilt/curve (per-component
rotated rects), and era (synthesize from all sets) — and it is fully learnable
rather than hand-tuned.

### Model
- Compact encoder-decoder (~1–3M params): MobileNetV3-small encoder +
  lightweight upsampling decoder → 1-channel logit mask at ¼ input resolution.
- Input: full scene letterboxed to a fixed size (e.g. 512×512, aspect
  preserved with padding — unlike the strip embedder, whole-scene geometry
  must be preserved). Runs on a downscaled copy; strips still cropped from the
  full-res original (same split the current segmentation already uses).
- Trained in PyTorch/MPS; exported to ONNX; served via onnxruntime.

### Training data (`training/` — reuses phase-1 infrastructure)
- `synth.py` already computes `SceneTruth.band_centers` + `band_height`. Extend
  it to return, per card, the band's **rotated rectangle** (4 corners after the
  same global warp already applied to the scene) so the mask is accurate under
  tilt. The synthesizer's degradation stack is unchanged — the detector learns
  the same real-world variations the embedder trains against.
- `build_band_dataset.py`: synthesize scenes, rasterize each band rect into a
  mask (filled polygon), save (scene.jpg, mask.png) pairs + manifest; parallel
  like `build_dataset.py`. Optionally mix in real training-intake photos once
  their band boxes can be derived (see Real-data note).
- Loss: per-pixel BCE + Dice on the mask; standard segmentation training.

### Post-processing (`app/pack/band_detector.py`, served in-app)
1. Downscale + letterbox scene → ONNX → mask → upscale mask to source coords.
2. Threshold (env-tunable `PACK_BAND_THRESHOLD`), morphological close, connected
   components; drop components below an area/aspect floor.
3. `cv2.minAreaRect` per component → order top→bottom by centroid y → deskew
   crop via the existing `_extract_strip` rotation logic.
4. Return the same `SegmentationResult` shape `find_strips` returns today, so
   nothing downstream changes.

### Serving integration
- `find_strips` gains a learned path, used when the model file is present AND
  `PACK_BAND_DETECTOR=1`; otherwise the current Hough path runs unchanged.
- Never raises: any model-load/inference error logs and falls back to Hough —
  same "never break a scan" philosophy as the matcher.
- Guided-capture path (explicit `capture_meta` guides) stays preferred and
  untouched — it already has ground-truth band positions from the client.
- Requires `onnxruntime` in the app runtime (new dep; ~matcher-sized). The
  band model is small (~a few MB), committed like the embedder exports.

## Real-data note
Synthetic masks are exact and free. Real intake photos have card labels but not
band boxes; deriving band boxes from them needs either (a) a lightweight admin
box-annotation view (future) or (b) bootstrapping: run the trained detector,
have the admin accept/nudge boxes. Phase-1 of J is **synthetic-only** — that
alone should fix the geometric-fragility class of failures. Real band-box
annotation is a later enhancement.

## Acceptance
- **Synthetic:** held-out val — mean IoU of matched bands ≥ 0.7 and band-count
  error (|detected − true|) ≤ 1 on ≥ 90% of scenes.
- **Real (the proxy that matters):** on the corpus photos that fail today,
  measure the **number-readable rate** — fraction of detected strips whose
  crop, run through the existing number OCR, yields a valid `NN/NNN` (or promo)
  read. Bar: beat the current Hough path's readable rate on the same photos,
  targeting a clear majority of true cards localized well enough to read.
- Regression: with `PACK_BAND_DETECTOR` unset, byte-identical current behavior;
  fixture + suite unchanged.

## Out of scope
- Real band-box annotation UI (later; bootstrapped from this model).
- Replacing the embedding matcher or OCR — J only improves *where* strips are
  cut; the downstream identification stack is unchanged.
- Code-card detection (working; unrelated).

## Effort
Sibling of the phase-1 embedding pipeline (~6–8 tasks): synth mask extension,
dataset builder, seg model + trainer, ONNX export, in-app served detector +
fallback wiring, corpus acceptance harness, runbook stage. Training is another
MPS run of similar cost.
