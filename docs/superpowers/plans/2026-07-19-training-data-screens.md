# Training Data Screens (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Admin "Training Data" area: any-count staircase intake (labeled/unlabeled), label templates + paste-JSON labeling, browsable pools/references/synthetic/eval views.

**Architecture:** New `app/training_data.py` router (admin-gated, mounted like app/admin.py) + migration 0008 + storage under `PHOTO_STORAGE_DIR/training/` + a "Training Data" section in the dashboard frontend following existing dashboard patterns. Spec authority: docs/superpowers/specs/2026-07-18-embedding-training-pipeline-design.md (Phase 2 + label template generator sections).

**Repo rules:** NO automated tests (smokes only, machine-care pkills, fresh ports). Dev env as usual. Follow existing patterns: app/admin.py (admin deps/router style), app/pulls.py (photo storage + owner-gated image routes), app/stats_api.py + frontend/src/dashboard/* (read APIs + screens), app/cards.py (catalog access).

## Task 1: Migration 0008 + models

`alembic/versions/0008_training_data.py` (down_revision "0007_tcgdex_catalog") + models in app/db/models.py, following 0006/0007 style:

- `training_photo`: id UUID pk default uuid4; path TEXT NOT NULL; tier TEXT NOT NULL DEFAULT 'standard' ('standard'|'stress'); split TEXT NOT NULL DEFAULT 'train' ('train'|'test'); labeled BOOLEAN NOT NULL DEFAULT false; set_hint TEXT NULL; uploaded_by UUID FK trainer.id ON DELETE SET NULL, nullable; created_at timestamptz server_default now().
- `training_strip`: id UUID pk; photo_id UUID FK training_photo.id ON DELETE CASCADE, indexed; row_index INT NOT NULL; path TEXT NOT NULL; set_id TEXT NULL; card_key TEXT NULL (catalog id, e.g. "sv6-45" or tcgdex id); source TEXT NOT NULL DEFAULT 'upload' ('upload'|'harvest'); created_at timestamptz server_default now().
- `eval_run`: id UUID pk; model_version TEXT NOT NULL; tier TEXT NOT NULL; top1 INT; top3 INT; total INT; detail JSONB NOT NULL DEFAULT '{}'; created_at timestamptz server_default now().

Verify: `alembic upgrade head` on dev DB. Commit.

## Task 2: Backend — intake + labeling + templates (`app/training_data.py`)

Router `APIRouter(prefix="/admin/training", tags=["training"])`, every route `CurrentAdmin`-gated, mounted in app/main.py after admin_router. Storage helper mirroring app/storage.py: photos → `<PHOTO_STORAGE_DIR>/training/<photo_id>/photo.jpg`, strips → `.../strip_<row>.jpg`.

Endpoints (exact contracts):

1. `POST /photos` — multipart: `photo` (image file, HEIC ok — decode via app.pack.pipeline._decode), form fields `tier` ('standard'|'stress', default 'standard'), `split` ('train'|'test', default 'train'), `set_hint` (optional str). Runs `find_strips(img, None)` directly (NO min_rows constraint — any count ≥1 accepted; if 0 strips → 422 "no rows detected"). Saves photo + per-strip jpgs + DB rows (labeled=false). Returns `{photo_id, rows: [{strip_id, row_index}], tier, split}`.
2. `PATCH /photos/{photo_id}/labels` — body `{"set": "<set_code|set_id|set name>", "rows": ["010", null, ...]}` (rows length must equal strip count; null = skip/no-card). Resolve the set against the denominator table (set_code case-insensitive, set_id exact, name casefold) → set_id + tcgdex mapping via set_id_map when present. Resolve each number: normalized numerator must exist in `tcgdex_card` for the mapped set (compare int-stripped local_id) — card_key = tcgdex card id; if no tcgdex mapping, card_key = "<set_id>-<numerator>". Per-row errors returned as `{row, number, error}` list with HTTP 422 if ANY row invalid (no partial writes). On success sets labeled=true. 
3. `GET /label-template/{query}` — set search (same resolution rules); returns `{set_id, set_code, set_name, tcgdex_set_id, cards: [{number, name, card_key}...]}` from tcgdex_card (ordered by numeric local_id) or 404 with close-match suggestions in detail.
4. `GET /photos?split=&labeled=&tier=` — list with strip counts + label summaries.
5. `GET /photos/{id}/image` and `GET /strips/{id}/image` — FileResponse (admin-gated; pattern: app/pulls.py photo route).
6. `GET /pools/summary` — counts by (source, split, labeled, tier) + per-set labeled counts.
7. `GET /references/{set_query}` — resolved set + cards with image URLs: prefer `card` table rows (PokéWallet image_url), else tcgdex image_base + "/high.png". Returns `{set..., cards: [{card_key, number, name, image_url}]}`.
8. `GET /synthetic` — if env `TRAINING_DATA_DIR` (default "./training/data") exists: list dataset versions (subdirs with manifest.jsonl) with strip counts per split, plus for `?version=X&sample=24` a sample of strip rows (path, card_key) served via `GET /synthetic/image?version=&path=` (path-traversal-guarded: resolved path must be inside the dataset dir). Else `{available: false}`.
9. `GET /eval-runs` — eval_run rows newest-first. `POST /eval-runs` — body `{model_version?: str}`: for each labeled test-split photo, group strips by set, call the matcher (`app/matcher_client.py match_strips`) using the photo's strips' set_id (skip photos with unresolved sets or matcher disabled → 503 when MATCHER_URL unset); compute top1/top3 vs card_key per tier; insert eval_run rows (one per tier) with per-strip detail JSONB; return them.
10. Unlabeled robustness view: `GET /photos/{id}/predictions` — matcher top-3 per strip against `set_hint`-resolved set (503 if matcher off, 409→"index stale", 404→"no index").

Smoke (stub-free, dev DB, fresh port 8175): upload tests/corpus/IMG_7102.heic as stress/test → 11 rows; label it via template flow (`GET /label-template/TWM` → paste rows ["010","126","101","045","143","122","079","066","078","096",null] — expect a per-row 422 for numbers not matching strip count or unknown; then the correct 11-row payload succeeds); pools summary shows it; strip image route 200; unauthorized (non-admin) → 403. pkill after. Commit per logical chunk (storage+intake, labeling+templates, browse+eval).

## Task 3: Frontend — Training Data screens

`frontend/src/dashboard/TrainingData.tsx` (+ small child components in the same folder), a new tab inside the existing Dashboard for admins only (pattern: dashboard/Dashboard.tsx tab switching; role check exists). Views:

1. **Intake & Pools**: upload form (file input incl. HEIC, tier/split/set-hint selectors) → shows detected rows with strip thumbnails; textarea for paste-JSON labels + "Get template for set…" input that fetches `/label-template/{q}` and pre-fills `{"set": CODE, "rows": [null × rowcount]}`; submit PATCH; per-row errors rendered inline. Pools summary table + photo list with labeled badges; unlabeled photos show a "Predictions" button → renders matcher top-3 per row (name + score) via `/photos/{id}/predictions`.
2. **References**: set search box → grid of card images (lazy `<img>`, number + name captions) from `/references/{q}`.
3. **Synthetic**: dataset version list + sample strip grid with labels (or "not available on this deployment").
4. **Eval**: table of eval_run rows (model_version, tier, top1/top3/total, date); "Run evaluation" button → POST /eval-runs with error toast when matcher off.

api.ts additions follow existing fetch patterns (`credentials: "include"`, parse()). Match existing CSS classes (card-rows etc.) — no new styling system. Playwright/E2E NOT required. Smoke: `npm run build` passes; manual: uvicorn + `npm run dev`, admin account walk through all four views (document what you clicked in the report).

## Task 4: Env + docs

`.env.example`: add `TRAINING_DATA_DIR` comment block. Spec's runbook untouched. Commit.

## Self-review checklist for the executor
- Intake must NOT touch app/pack scanning behavior (find_strips imported, not modified).
- All new routes admin-gated; image routes must not leak to non-admins.
- Numbers with letter prefixes (TG12) resolve case-insensitively against tcgdex local_id.
- No file >1MB committed; strip/photo storage under PHOTO_STORAGE_DIR only.
