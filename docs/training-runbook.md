# Embedding Training Runbook

Six stages, one command each. Retraining is a pipeline, not archaeology.

All commands run from the repo root with the dev venv. The `training/` package
is dev-only (`pip install -r training/requirements.txt` into `.venv`; torch is
never installed in the app or matcher images). Scripts import the `training`
package, so prefix commands with `PYTHONPATH=.`.

**Machine load:** training occupies the GPU/CPU for its whole duration
(~1–3h for a full run on MPS). Run ONE training process at a time, never
concurrently with other heavy work. pkill matcher/app servers around smokes.

## Stage 1 — Build the dataset

Fetch hires references for the training sets (idempotent; skips files already
on disk), then synthesize scenes and harvest strips through the real
segmentation:

```bash
PYTHONPATH=. .venv/bin/python training/fetch_refs.py sv6 sv1 swsh9
PYTHONPATH=. .venv/bin/python training/build_dataset.py --version v1 --scenes 1500
```

Output: `training/data/<version>/` — `strips/` (degraded, production-cropped),
`refs/` (clean bottom crops), `manifest.jsonl` (`{path, card_key, set, split,
source}`; `card_key: null` rows are explicit negatives; seed%10==0 → val).
Datasets are versioned and deterministic per seed — regenerating `v1` gives
the same scenes. As the labeled real pool grows (phase 2), this stage also
pulls those pools into the dataset directory.

## Stage 2 — Train

```bash
PYTHONPATH=. .venv/bin/python training/train.py --dataset v1 --epochs 8 --batch 48 --run-id v1a
```

Two-tower shared `StripEncoder` (mobilenet_v3_large + head → 256-d,
L2-normalized; ImageNet normalization inside `forward()` so the export takes
raw 0..1 input). NT-Xent loss; every batch is single-set so negatives are
same-set hard negatives. Writes `training/runs/<run-id>/` (`model.pt`,
`metrics.json`, `config.json`). Lower `--batch` if MPS memory complains.

## Stage 3 — Evaluate

```bash
PYTHONPATH=. .venv/bin/python training/eval.py --run v1a --dataset v1
```

Prints three scores:

- **synth-val** — held-out synthetic split, the fast inner-loop metric.
  Target: top-1 ≥ 90% same-set.
- **stress tier** — adversarial real captures (handheld corpus pack,
  glare/blur/low-light). Tracked on every model, expected to climb over time;
  **non-blocking** for deploy. Baseline to beat: 4/8 (badge-anchor).
- **standard tier** — reasonably lit, in-focus staircase photos. **Deploy
  gate: 100% top-1.** Until standard-tier photos exist (phase-2 intake), the
  harness prints `standard-tier: no photos yet (deploy gate unmeasurable)`.

Real photos are registered in `training/eval_sets.json`
(`{photo, set_slug, tier, rows}`; `rows` lists a card_key or null per detected
row, in `find_strips` ungrided output order). Both tiers are kept separate
from training data forever.

## Stage 4 — Export

```bash
PYTHONPATH=. .venv/bin/python training/export.py --run v1a
```

Exports a single self-contained ONNX with a torch-parity check (max abs err
< 1e-3), writing `matcher/model/strip-embed-<run-id>.onnx`, updating the
served copy `matcher/model/model.onnx`, and stamping
`matcher/model/version.json` (`{"model_version": "<run-id>", "embed_dim":
256}`). These artifacts are ~15MB and **committed** — the matcher image copies
them at build time (no download stage).

## Stage 5 — Deploy

Commit the exported artifacts (`matcher/model/model.onnx`,
`strip-embed-<run-id>.onnx`, `version.json`), push, and redeploy the matcher
service. Indexes are model-version-stamped:

- `POST /index/{set_key}` stamps the index meta with the loaded
  `model_version`.
- `/match/{set_key}` returns **409** when the index's model_version differs
  from the loaded model; the app treats 409 like 404 (result degrades to the
  OCR path and a rebuild is kicked).
- Rebuild indexes explicitly via the admin endpoint:
  `POST /admin/matcher/index/{set_id}` (enumerates the set and rebuilds), or
  let them rebuild lazily on the 409/404 path.

**Deploy gate (feature-on in prod):** 100% top-1 on the standard tier. Any
miss is triaged (added to training data or shown to be a labeling error)
before the feature turns on for users. Until the gate passes, the matcher
service may deploy but `MATCHER_URL` stays **unset** in production — the
feature is off.

## Stage 6 — Verify serving

Run the eval harness against the deployed matcher and confirm scores match
stage 3 (serving parity):

```bash
pkill -f "uvicorn matcher.app" || true
MATCHER_TOKEN=t INDEX_DIR=./var/matcher-index .venv/bin/uvicorn matcher.app:app --port 8183 &
# build the index for the eval set, then:
.venv/bin/python scripts/measure_matcher.py   # scores must match eval.py's stress numbers
pkill -f "uvicorn matcher.app"
```

## When to retrain

- New set released → **no retrain needed**: enumerate + index the set (the
  embedding generalizes; indexes are per-set). Retrain only if the eval view
  shows the new set underperforming.
- Standard-tier miss found (via flywheel or admin uploads) → triage, add to
  pools, retrain when a handful accumulate.
- Labeled real pool grows ~2× since last training → retrain (real data is the
  scarcest, highest-value signal).
- Any eval regression after a pipeline change (segmentation, capture).

## Dry-run results (phase-1 acceptance, 2026-07-18/19)

| Run | Data | Preprocessing | Synth-val top-1 | Stress top-1 (real pack) |
|---|---|---|---|---|
| v1a | v1: 1.5k scenes, 3 sets, 8 ep | aspect-preserving letterbox | 21.4% | 0/8 |
| v1b | v1, 8 ep | **stretch-to-fill** | 46.8% | **3/8** |
| v1c | v2: 4k scenes, 6 sets, 12 ep | stretch | 66.2% | 2/8 (top-3 3/8) |

- Stretch preprocessing was decisive (v1a→v1b: one change, doubled synth-val,
  first real-photo hits of any learned or off-the-shelf model).
- Scaling data/epochs kept improving the synthetic domain but NOT the real
  photo — the sim-to-real gap is the binding constraint. Highest-leverage next
  input: real labeled strips (phase-2 intake + flywheel), plus augmentation
  realism tuned against real captures.
- Serving parity (stage 6) caught two deployment-class bugs: matcher
  preprocessing still letterboxing after training switched to stretch, and the
  harness indexing 245px small references vs training's hires. After both
  fixes, service-side stress = 3/8, identical to direct eval. Deployed model:
  v1b.
- **Acceptance verdict: pipeline + runbook proven end-to-end; accuracy gates
  not yet met** (stress 3/8 vs ≥4/8 required; synth-val 66% vs ≥90%). The
  matcher feature stays OFF in production (`MATCHER_URL` unset) per the deploy
  gate. Next: phase 2, so real labeled data starts flowing.

## Pulling real labeled uploads into training (export connector)

Uploads labeled in the app (Training Data → Intake & Pools) live in the app DB
+ volume. To fold them into a local training run:

1. `PYTHONPATH=. python training/fetch_uploads.py --base <app-url> --email <admin> --password <pw>`
   — downloads every labeled strip into `training/data/uploads/` (train + test).
2. `python training/build_dataset.py --version <v> --scenes <n> --sets ... --include-uploads`
   — merges TRAIN-split upload strips as `source=upload` pairs (oversampled 3×
   in training); TEST-split strips are held for eval. Unpairable strips (set
   with no local refs) are skipped and counted, never fatal.
3. Train/eval as usual. `eval.py` automatically folds TEST-split uploads into
   the standard/stress tiers alongside `eval_sets.json`.

Card-key note: uploads use TCGdex ids (`sv06-045`); the merge normalizes to the
pokemontcg.io ref key (`sv6-45`) via `training/config.tcgdex_card_key_to_ref`.
Subset slugs that break the mechanical rule live in `config._SLUG_OVERRIDES`.
