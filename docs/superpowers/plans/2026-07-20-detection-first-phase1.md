# Detection-First Scanning — Phase 1 Implementation Plan

> REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Make PP-OCR whole-photo detection the primary card-finder (drop the
strip grid + guided capture to fallbacks), with a per-card confidence + review
flag. Local only — no GPU.

**Tech:** existing FastAPI/RapidOCR/OpenCV stack. No new deps.

Repo rules: NO automated tests (smokes only). Dev env as usual.

## File map
- `app/pack/pipeline.py` — MODIFY: `detect_first()` produces (strips, readings)
  from whole-photo detection; `scan_pack` uses it, Hough fallback when sparse.
- `app/pack/confidence.py` — MODIFY: catalog-aware card confidence + review flag.
- `app/schemas.py` — MODIFY: `PackCard.needs_review: bool`.
- `frontend/src/capture/StaircaseCapture.tsx` — MODIFY: one photo, no guides.
- `frontend/src/api.ts` + `App.tsx` — MODIFY: drop capture_meta from scan; show
  needs_review.
- `frontend/src/review/ReviewScreen.tsx` — MODIFY: highlight needs_review cards.

## Task 1: detect_first in the pipeline
- [ ] Add `detect_first(img) -> tuple[list[Strip], list[NumberReading]] | None`:
  run `detect_lines`, `parse_number` each, keep `pattern_ok`, sort by y; if
  fewer than `PACK_MIN_ROWS-ish` floor (use 3) return None. Median y-gap → band
  height; crop a full-width band around each number (`[y-0.6g, y+0.4g]`) as the
  Strip image; readings = the parsed numbers. Dedup numerators (keep higher conf).
- [ ] In `scan_pack`: `df = detect_first(stair)` (upload path). If df: use its
  strips+readings (skip per-strip OCR). Else current find_strips + per-strip.
  Guided `capture_meta` still routes to find_strips guided path (kept, not
  removed server-side yet — frontend stops sending it).
- [ ] Verify: corpus scan — distinct numbers ≥ current 30, faster; fixtures
  (guided) unaffected; suite green.
- [ ] Commit `feat(scan): detection-first card finding (Hough fallback)`

## Task 2: confidence + review flag
- [ ] `app/schemas.py`: add `needs_review: bool = False` to PackCard.
- [ ] `app/pack/confidence.py`: card confidence factors in catalog hit (pass the
  set's valid numerators in) + match found; `needs_review = conf < threshold`
  (`PACK_CARD_CONFIDENCE`, default 0.85). Pack confidence = penalized min.
- [ ] `pipeline.py`: thread valid-numerators (already fetched for constraints)
  into scoring; set needs_review per card.
- [ ] Verify: corpus — confident correct cards NOT flagged; wrong/missing ones
  flagged. Suite green.
- [ ] Commit `feat(scan): catalog-aware card confidence + needs_review`

## Task 3: frontend one-snap capture
- [ ] `StaircaseCapture.tsx`: remove guide overlay + declared-count; a single
  "photo of your fanned pack" capture (reuse CameraCapture without drawOverlay
  guides). `onDone(photo)` with no meta.
- [ ] `App.tsx` + `api.ts`: `scanPack(staircase, code)` — drop capture_meta arg
  and the guided step. Summary/review unchanged.
- [ ] `ReviewScreen.tsx`: cards with `needs_review` get a visible highlight
  ("check this one"); others are quietly confirmed.
- [ ] Verify: `npm run build`; browser walkthrough of the simplified flow.
- [ ] Commit `feat(scan): one-snap capture + review highlights only uncertain cards`

## Task 4: acceptance
- [ ] Corpus: detection-first distinct-correct ≥ hybrid, per-card confidence
  flags the right cards, timing improved. Update `.env.example`
  (PACK_CARD_CONFIDENCE). Fixtures pass.
- [ ] Commit `feat(scan): phase-1 acceptance + env`

## Self-review
- `detect_first` returns the same (Strip, NumberReading) shapes downstream
  already consumes — resolve_set/lookup/build unchanged.
- capture_meta stays accepted server-side (old clients) but frontend stops
  sending it — no hard break.
- VLM gate (Phase 2) reads pack/card confidence this phase produces.
