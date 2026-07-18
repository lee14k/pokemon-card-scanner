# Embedding Training Pipeline & Data Management (Sub-project I) — Design

Context: every off-the-shelf recognizer failed the corpus acceptance gate
(CLIP 0/10, geometric variants 0–1/8, SIFT 0–1/8, text fingerprints 1/8,
DINOv2 0/8) because hazy phone photos and clean digital references occupy
disjoint feature spaces. Approved direction: **learn the domain gap** — train a
compact two-tower embedding on synthetically degraded scenes (+ real labeled
data over time), served by the existing matcher container. Code-card reading is
explicitly out of scope (working well).

## Phase 1 — Synthetic training loop (build first)

New top-level `training/` package. PyTorch (+ torchvision) are **dev-only**
dependencies (`training/requirements.txt`), never installed in either deployed
service; the trained model exports to ONNX and replaces the matcher's model
file.

### Scene synthesizer (`training/synth.py`)
- Composes staircase scenes from hires reference images: **K ∈ 1..13 cards**
  (uniform mix of tight fans like the corpus pack, loose protocol fans, and
  single/partial spreads), per-card vertical offset jitter, global rotation
  (±12°), perspective warp, optional curvature.
- Occluders: thumb/finger shapes (skin-tone ellipses with soft edges) placed
  like a holding hand; random partial edge crops.
- Backgrounds: sampled crops from real corpus photos (wrapper, fabric, table)
  plus procedural textures.
- Degradation stack (each sampled independently, wide ranges): defocus + motion
  blur, haze (low-contrast lift), specular glare streaks/blobs, color
  temperature/tint casts, vignette, sensor noise, JPEG quality 50–95, the
  browser downscale chain (full-res → ~2600px → re-encode), EXIF-style
  rotations.
- Output per scene: photo-like image + ground truth (per-card id, band
  geometry). Deterministic from a seed (reproducible datasets).
- Style realism: per-scene histogram/color statistics optionally transferred
  from a randomly chosen real corpus photo.

### Strip harvesting (`training/harvest.py`)
- Runs the **real segmentation** (`find_strips`, both ungrided and guided
  paths) over synthesized scenes; matches detected bands to ground-truth
  geometry (IoU) to label each strip — so training data contains production's
  actual cropping quirks (offset bands, phantom rows, neighbor slivers).
  Unmatched detected strips become explicit negatives ("no card").
- Also emits directly-cropped clean strips (cheap second stream).

### Trainer (`training/train.py`)
- Two-tower shared encoder: pretrained small backbone (convnext-tiny or
  mobilenet_v3_large, decided by first benchmark) + projection head → 256-d
  L2-normalized embedding, 224px letterboxed input.
- Loss: NT-Xent contrastive; batches constructed so **negatives are same-set
  cards** (hard negatives that force fine discrimination).
- Runs on Apple Silicon MPS; checkpoints + metrics to `training/runs/<id>/`.
- Export: `training/export.py` → ONNX (verified against torch outputs), model
  file named `strip-embed-<version>.onnx`.

### Evaluation (`training/eval.py`)
- Held-out synthetic validation split (top-1/top-5 per set size).
- **Corpus gate (never trained on)**: the real-photo harness from H
  (`scripts/measure_matcher.py`, generalized to accept a model path): report
  top-1 and top-3 on every labeled corpus pack. Phase-1 success bar: beat the
  badge-anchor baseline (4/8) on the existing pack; aspiration ≥7/8 top-1 or
  8/8 top-3.

### Model versioning & serving
- Matcher `/index/{set_key}` meta gains `model_version` (read from a version
  file shipped beside the model). `/match` returns 409 when index
  model_version ≠ loaded model version; the app treats 409 like 404 (kick a
  rebuild, degrade to OCR path).
- Deploying a new model = replace the model file in the matcher image (Docker
  ARG), redeploy, indexes rebuild lazily/via admin endpoint.

## Phase 2 — Admin data screens (visibility + intake)

Admin-role-gated dashboard area ("Training Data"), following the stats
dashboard patterns. **Every data population is browsable** — the user's
requirement is high visibility into all of it:

1. **References browser**: per set, grid of catalog reference images (from the
   `card`/`tcgdex_card` tables through the fetcher seam), with the bottom-crop
   overlay shown; flags cards missing images.
2. **Synthetic gallery**: browse generated scenes and harvested strips with
   their labels and generation parameters; regenerate button (admin) for
   spot-checking realism.
3. **Training pools**: labeled and unlabeled uploads, split (train/test),
   source (synthetic / upload / harvest), per-strip thumbnails + labels;
   counts per set/split; delete/relabel.
4. **Evaluation view**: per-model-version scores over time; per-sample
   predictions — for **unlabeled** test uploads this shows the model's top-3
   with scores (the user's cold-robustness check); for labeled ones,
   right/wrong marking.

### Intake flow
- Upload a staircase photo of **any card count ≥1** (training intake bypasses
  the scanner's `min_rows`); server segments and presents detected strips.
- Labeling UI per strip: pick set → pick card (search by number/name against
  the catalog) or "skip row"; or mark the whole upload **unlabeled** → routes
  to the evaluation pool.
- Optional `capture_meta` passthrough when taken via the guided flow.
- Storage: photos + strips on the Railway Volume under a `training/` prefix;
  new tables: `training_photo` (id, path, uploaded_by, labeled bool,
  split, created_at), `training_strip` (id, photo_id, path, row_index,
  set_id nullable, card_key nullable — catalog id, split, source enum
  `synthetic|upload|harvest`).

## Phase 3 — Flywheel + retrain cadence
- Harvester job (admin-triggered; later batch-stage): for review-confirmed
  saved pulls, re-crop strips from stored photos (re-derivation machinery) and
  insert as labeled `harvest` rows keyed to the confirmed cards.
- Retrain: manual for now (run `training/train.py` on the exported pools; an
  admin "export training data" endpoint bundles pools to a tarball). Automation
  deferred until cadence is known.

## Acceptance
- Phase 1: corpus gate ≥ badge-anchor baseline (4/8) required; ≥7/8 top-1 or
  8/8 top-3 aspirational; synth-val top-1 ≥90% same-set. Every result recorded
  in the eval view (phase 2) or run logs (phase 1).
- Phase 2: smoke — upload labeled + unlabeled staircase photos of 3 and 11
  cards, verify pools/galleries/predictions render; repo test suite untouched.
- No automated tests (repo rule); machine care rules apply to all smokes.

## Out of scope
- Code-card OCR (working; user-confirmed).
- Automated retraining/deployment of models; foil-variant discrimination;
  non-staircase photo layouts (binders, spreads).
