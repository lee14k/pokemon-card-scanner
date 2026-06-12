# Pack Scanner Retool — Design Spec

**Date:** 2026-06-11
**Status:** Approved pending user review
**Sub-project:** A (first of six; B = auth + Postgres foundation follows)

## Product context

The app is being reconfigured from a single-card whole-photo scanner into a **pack-pull logging platform**. The full vision decomposes into six sub-projects:

1. **Pack scanner retool** (this spec) — OCR only the bottom strip of each card from a staircase photo of a full pack, plus the pack's code card.
2. **Foundation** — Better Auth accounts + Postgres on Railway.
3. **Crowd-sourced pull rates** — validated pulls feed set-level statistics; optional scraped priors.
4. **Batched pricing + anomaly tracking** — weekly/monthly price snapshots.
5. **Pack battles** — real-pull vs real-pull comparisons between trainers.
6. **Pokédex** — "times seen" layer over personal pull history.

Sub-project A deliberately has **no persistence and no accounts**. Its job is to de-risk the core bet: that bottom-strip OCR on a staircase photo can identify a full pack of cards reliably.

## Scope

### In scope

- New scanning pipeline for a **staircase photo**: cards stacked vertically, each shifted down so all bottom strips are visible in a column.
- **Code card** captured as a separate second photo; OCR the code, format-check it. (Duplicate-code rejection requires the DB — sub-project B.)
- Card identification = **card number + set**; **rarity is derived** from the PokéWallet record, never read from the photo.
- Set coverage: **SWSH + SV English** (~30 sets, ≈2020+).
- Two capture paths: **guided** (camera overlay, primary) and **plain upload** (fallback).
- Review screen with **low-confidence flagging**: high-confidence cards pass through; flagged cards require user fix or retake.
- Single endpoint `POST /scan/pack` (both photos required).
- Labeled test corpus + calibration harness; acceptance gate on the success criteria below.

### Out of scope (explicitly)

Accounts/auth, persistence, duplicate-code rejection, pull-rate stats, pricing batching, battles, Pokédex, Japanese/non-English cards, loose-card or binder scanning, vision-LLM OCR (documented fallback only — see Risks).

## Success criteria

Measured against the labeled corpus via the integration-test harness:

| Metric | Bar |
|---|---|
| Precision of high-confidence card identifications | ≥ 99% |
| Recall of mistakes via low-confidence flagging | ≥ 90% |
| Code card OCR success (well-framed photo) | ≥ 99% |

Rationale: a scanner that is almost never silently wrong, and knows when it's unsure, is what keeps downstream crowd-sourced stats clean. Raw accuracy alone is not the bar.

## Architecture

### Codebase strategy: greenfield pipeline, deprecate old

- New package `app/pack/` containing `segmentation.py`, `ocr.py`, `set_resolution.py`, `matching.py`, `confidence.py`.
- **Deleted:** `app/ocr_extract.py`, `app/card_signals.py`, current `app/matching.py`, single-card endpoints in `app/main.py`. Git history retains them.
- **Reused:** `app/pokewallet.py` (API client), `app/set_symbol_index.py` (perceptual-hash symbol index — re-seeded to cover all ~30 SWSH+SV sets), base types in `app/schemas.py`.
- Stack unchanged: FastAPI + OpenCV + Tesseract backend; Vite + React + TypeScript frontend; single Railway service serving frontend statically.

### Data flow

```
Mobile browser
  ├─ staircase photo (guided overlay → guide metadata, or plain upload → none)
  ├─ code card photo (framed close-up, or plain upload)
  └─ POST /scan/pack (multipart: staircase, code_card, metadata?)
        │
        ▼
FastAPI app/pack/
  1. Segmentation   → N bottom-strip ROIs
  2. Per-strip OCR  → card number (numerator + denominator)
  3. Set resolution → denominator table narrows → symbol hash tiebreaks
  4. Matching       → keyed PokéWallet lookup per (set_id, number)
  5. Code card OCR  → code string + format check
  6. Confidence     → per-card score + low_confidence_reason
        │
        ▼
Response → frontend review screen → user fixes flagged rows → summary
```

### API

`POST /scan/pack` — multipart:

- `staircase`: image (required)
- `code_card`: image (required)
- `capture_meta`: JSON string (optional; guided path only): `{ guide_positions: [y...], image_dims: [w,h], declared_count: int }`

Response:

```json
{
  "cards": [
    {
      "row_index": 0,
      "card_number": "123/198",
      "set_id": "sv1",
      "name": "…",
      "rarity": "…",
      "image_url": "…",
      "confidence": 0.97,
      "low_confidence_reason": null
    }
  ],
  "code_card": { "code": "…", "confidence": 0.99 },
  "pack_confidence": 0.94,
  "segmentation_warning": null
}
```

- Rows are **never silently dropped**: an unreadable strip still appears with `confidence: 0` and a reason. Row-count integrity matters for future pull-rate stats.
- `low_confidence_reason` ∈ `unreadable_strip | number_ambiguous | set_ambiguous | no_db_match`.
- `segmentation_warning` set when detected row count ≠ declared count (or < 5 / > 13 ungrided).

`GET /cards/lookup?set=X&number=Y` — thin PokéWallet proxy used by the review screen's manual-fix flow to show card art for a hand-entered correction.

## Segmentation

Shared OpenCV core; the guided path adds priors.

1. **Preprocess** — grayscale, adaptive threshold, light denoise. Assume 1080p+ phone photos.
2. **Edge detection** — Canny + probabilistic Hough constrained to near-horizontal (±10°); each card's bottom edge is a strong wide line.
3. **Row hypothesis** — cluster lines by Y; fit a roughly-uniform spacing model; reject outliers (shadows, table edges).
   - *Guided:* snap clusters to `guide_positions`; reject non-corresponding lines.
   - *Ungrided:* accept best self-consistent model with 5–13 rows (pack contents vary; card count is never hardcoded).
4. **Strip extraction** — crop above each bottom edge: full card width × ~8% card height (the SWSH/SV bottom info zone).
5. **Per-strip deskew** — small affine rotation from each strip's own edge angle.
6. **ROI** — left ~40% of strip is the primary OCR target (number + set symbol cluster); full strip retained as fallback for shifted SV layouts.

Failures surface as data, not errors: missing rows → `segmentation_warning` + review-screen retake prompt; unreadable strips → zero-confidence rows.

## OCR and set resolution

### Card number (Tesseract)

- Strip ROI upscaled 2–3×, contrast-normalized, binarized.
- `--psm 7` (single line), whitelist: digits, `/`, `A–Z` (covers `TG12/TG30`, `GG44/GG70`, `SWSH123`, `SVP 067`).
- Pattern: `(\w{0,3}\d{1,3})\s*/\s*(\w{0,3}\d{1,3})` plus denominator-less promo formats.
- Per-character confidences retained for the confidence model.

### Set resolution: denominator first, symbol second

1. **Denominator table** (static, checked into repo, built from PokéWallet/TCG set data): printed denominator → candidate set(s). Usually 1–4 candidates.
   - Secret rares (`201/198`): denominator still printed, still identifies the set.
   - Promos: prefix (`SWSH`/`SVP`) identifies the set; no denominator needed.
2. **Symbol hash tiebreak** — only when candidates > 1 or denominator OCR confidence is low: perceptual-hash match of the symbol crop against **candidate sets only** (small-pool discrimination, not the global match the old pipeline attempted).

### Matching

- `(set_id, number)` → **keyed PokéWallet lookup**. No fuzzy name search; old query-building/scoring code is deleted.
- Match supplies canonical name, **rarity**, set name, and card-art `image_url` (powers visual confirmation in review).
- PokéWallet failure → card returned with number+set, `match: null`, flagged `no_db_match`.

### Code card

- High-contrast close-up; Tesseract with alphanumeric whitelist; expect TCG Live code format (era-dependent, with hyphens).
- v1 validation = format + confidence only.

## Confidence model

```
card_confidence = f( ocr_char_confidence,      # Tesseract per-char scores
                     pattern_match_quality,    # clean regex match?
                     set_resolution_margin,    # unique denominator? hash distance gap?
                     pokewallet_match_found )
```

Threshold **T** separates pass-through from flagged. T is **tuned, not guessed**: swept over corpus results to satisfy the precision/recall bars; the chosen value and sweep data are committed. T lives in config (env/file), changeable without code edits.

## Frontend

### Capture (mobile-first, 3 steps)

1. **Staircase** — `getUserMedia` rear-camera live view; overlay renders N horizontal guide lines + card-bottom silhouette; count stepper (default 10) sets N. Capture → photo + `capture_meta`. "Upload instead" link = fallback path, no metadata.
2. **Code card** — second camera step with a rectangular frame guide; same upload fallback.
3. **Submit** — single multipart POST; staged progress text.

### Review

- Row per card: art thumbnail, name, number, set icon, rarity.
- Flagged rows highlighted with plain-language reason + two affordances: **fix manually** (number entry + short set picker, art preview via `GET /cards/lookup`) or **retake**.
- Count banner on mismatch: "Found 9, you said 10 — retake or continue?"
- "Looks good" → summary screen (sub-project B turns this into the persistence point).

### Structure & constraints

- Split `frontend/src/App.tsx` into `capture/`, `review/`, shared `api.ts`.
- Camera requires HTTPS even in dev on a phone: use Vite HTTPS dev-server (self-signed) or test against Railway. Documented here so it doesn't bite mid-build.

## Testing & calibration

**Integration and end-to-end tests only — no unit tests** (explicit project decision).

1. **Corpus** — ~20–30 labeled staircase photos from the user's own packs (several sets, B–C availability): mixed lighting, both capture paths, stress shots (foil glare, rotation, cramped spacing, one blurry control). Ground truth = JSON per photo (per-row number/set + code-card text). Lives in repo or repo-adjacent storage (decided at implementation by size).
2. **Pipeline integration tests** — corpus photo → FastAPI test client → `/scan/pack` → assert vs ground truth. The suite doubles as the **calibration harness**, emitting a metrics report (precision, flag-recall, code success).
3. **Acceptance gate** — success-criteria table asserted as a test; v1 is done when it passes.
4. **E2E smoke** — Playwright driving real frontend + backend with fixture photos: upload-fallback capture → review renders → fix flow → summary. A handful of journeys.

## Deployment

- Single Railway service, unchanged pattern: FastAPI serves built frontend statically.
- Verify `railpack.json` covers pinned `tesseract-ocr` + OpenCV system deps.
- Config surface: threshold T, denominator-table path, segmentation tolerances — env/file only.
- No DB, no server-side persistence in this sub-project.

## Risks & fallbacks

| Risk | Mitigation |
|---|---|
| Tesseract accuracy insufficient on strips even after calibration | **Documented fallback:** swap per-strip read to a vision LLM (e.g., Claude Haiku) returning `{number, set_guess}`; ~1–2¢/pack, +1–3s latency. Pipeline seams (per-strip reader interface) designed so this is a module swap, not a rewrite. |
| Ungrided segmentation too unreliable | Guided path is primary; fallback path can ship with a lower expectations banner ("works best with the camera guide"). |
| Set-symbol index gaps for newer SV sets | Re-seed via existing `scripts/` pipeline as a v1 task; denominator table reduces symbol dependence to tiebreaks. |
| PokéWallet rate limits/outages during batch lookups (≈11/pack) | Batch + cache set lists locally; graceful `no_db_match` degradation already designed in. |
| Code-card format drift across eras | Format table per era (TCGO vs TCG Live); format check is advisory, not blocking. |

## Decisions log

| Decision | Choice |
|---|---|
| Photo arrangement | Staircase (vertical cascade), not fan/grid |
| Code card | Separate second photo; "validate" = OCR + (in B) duplicate rejection — no Pokémon-server verification |
| Set scope | SWSH + SV English (~30 sets) |
| Rarity | Derived from PokéWallet record, not OCR'd |
| Capture | Guided overlay primary; plain upload fallback |
| Review | Auto-flag low-confidence only (threshold T, tuned) |
| OCR stack | Tesseract + OpenCV + perceptual hash; vision LLM documented as fallback only |
| API | Single `POST /scan/pack`, both photos required; pack-only scope |
| Codebase | Greenfield `app/pack/`; old whole-card pipeline deleted |
| Success bar | Calibration target (99% precision / 90% flag-recall / 99% code) over raw accuracy |
| Testing | Integration + E2E only; no unit tests |
