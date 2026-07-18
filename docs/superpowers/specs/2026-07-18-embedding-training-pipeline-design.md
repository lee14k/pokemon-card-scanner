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

### Label template generator (low-effort labeling)
- Admin screen: pick any set (search by name/code across the catalog —
  "Twilight Masquerade", "Perfect Order", ...) → instantly get generated JSON:
  - **Set template**: `{set_id, set_code, set_name, cards:[{number, name,
    card_key}...]}` — the full card list prefilled from the catalog, copyable/
    downloadable.
  - **Photo template**: for an uploaded photo, a skeleton prefilled with the
    detected row count — `{set: "TWM", rows:[{row: 0, number: ""}, ...]}` —
    the admin only types card numbers.
- The intake accepts **pasted JSON labels** as a full alternative to the
  click-through picker: `{"set": "TWM", "rows": ["010", "126", ...]}` (numbers
  only; `null` for skip-row). The server resolves numbers → catalog cards and
  rejects numbers not in the set with a clear per-row error. Labeling a photo
  is: generate template, type numbers, paste.
- Optional `capture_meta` passthrough when taken via the guided flow.
- Storage: photos + strips on the Railway Volume under a `training/` prefix;
  new tables: `training_photo` (id, path, uploaded_by, labeled bool,
  split, created_at), `training_strip` (id, photo_id, path, row_index,
  set_id nullable, card_key nullable — catalog id, split, source enum
  `synthetic|upload|harvest`).

## Phase 3 — Flywheel
- Harvester job (admin-triggered; later batch-stage): for review-confirmed
  saved pulls, re-crop strips from stored photos (re-derivation machinery) and
  insert as labeled `harvest` rows keyed to the confirmed cards.
- Admin "export training data" endpoint bundles pools to a tarball for the
  training environment.

## Evaluation tiers & deploy gate

Two labeled test tiers, kept separate from training data forever:

- **Standard tier** — reasonably lit, in-focus staircase photos (protocol-ish
  captures, guided or upload). **Deploy gate: 100% top-1 on this tier.** Any
  miss is triaged (added to training data or shown to be a labeling error)
  before the feature turns on for users. Until the gate passes, the matcher
  service may deploy but `MATCHER_URL` stays unset in production — the feature
  is off.
- **Stress tier** — adversarial captures (the current handheld corpus pack,
  glare/blur/low-light uploads). Tracked and reported on every model, expected
  to climb over time; **non-blocking** for deploy. Baseline to beat: 4/8.

Synthetic validation (held-out synth split, top-1 ≥90% same-set) is the fast
inner-loop metric; the tiers are the truth.

## Training cadence & retraining runbook

Retraining must be an easily-followed pipeline, not archaeology. Phase 1
delivers `docs/training-runbook.md` plus one command per stage:

1. `python training/build_dataset.py` — regenerate synthetic scenes (seeded)
   and pull the current labeled pools from the DB export into
   `training/data/<dataset-version>/`.
2. `python training/train.py --dataset <v>` — train; writes
   `training/runs/<run-id>/` (checkpoints, metrics.json, config).
3. `python training/eval.py --run <run-id>` — prints standard-tier,
   stress-tier, and synth-val scores side-by-side with the currently deployed
   model's scores.
4. `python training/export.py --run <run-id>` — ONNX + version file.
5. Deploy: update the model artifact reference in `matcher/Dockerfile`, push,
   redeploy the matcher service, hit the admin reindex endpoint (indexes are
   model-version-stamped, so stale ones refuse to serve and rebuild).
6. Verify: run the eval harness against the deployed matcher.

**When to retrain** (documented in the runbook):
- New set released → **no retrain needed**: enumerate + index the set (the
  embedding generalizes; indexes are per-set). Retrain only if the eval view
  shows the new set underperforming.
- Standard-tier miss found (via flywheel or admin uploads) → triage, add to
  pools, retrain when a handful accumulate.
- Labeled real pool grows ~2× since last training → retrain (real data is the
  scarcest, highest-value signal).
- Any eval regression after a pipeline change (segmentation, capture).

## Acceptance
- Phase 1: stress tier ≥ badge-anchor baseline (4/8) required, ≥7/8 top-1 or
  8/8 top-3 aspirational; synth-val ≥90%; runbook committed and every stage
  executed end-to-end at least once (a full dry run is part of acceptance).
- Phase 2: smoke — upload labeled + unlabeled staircase photos of 3 and 11
  cards, verify pools/galleries/predictions/eval-tier views render; repo test
  suite untouched.
- Deploy gate (feature-on in prod): 100% top-1 on the standard tier, which
  requires standard-tier photos to exist — supplied via the phase-2 intake.
- No automated tests (repo rule); machine care rules apply to all smokes.

## Out of scope now, planned later (roadmap)
- Automated retraining + model deployment (runbook stays manual until cadence
  is proven).
- Foil / reverse-holo variant discrimination.
- Non-staircase layouts: binders, spreads, singles-on-table.

## Out of scope permanently (this sub-project)
- Code-card OCR (working; user-confirmed).
