# Detection-First Scanning + Confidence-Gated VLM Fallback (Sub-project K) — Design

## Vision

Card identification should be automatic and seamless — the user snaps a photo
and confirms, correcting as little as possible. Architecture:

1. **Local pass (fast, tuned):** PP-OCR's real-photo-trained detector finds and
   reads every card it can, in one pass (~5s), scoring each with a confidence.
2. **The gate:** if every card is confidently identified and the set is certain,
   we're done — no VLM, instant, zero cost.
3. **VLM fallback (definitive):** cards that aren't confident — or a shaky
   set/count — escalate to a self-hosted VLM (RunPod) that resolves them
   authoritatively, and recovers any card the local pass missed.

This gives union-primary accuracy *and* completeness (the VLM is the safety net
for illegible cards), with the VLM firing only on hard packs (cost stays low),
and the system escalating *itself* rather than asking the user to fix things.

Decisions (confirmed): drop guided capture for one-snap automatic capture;
escalate aggressively (any doubt → VLM).

## Phase 1 — Local detection-first pipeline (buildable now, no GPU)

### Card finding: detection replaces the strip grid
- `detect_lines` (whole-photo PP-OCR, already built) is now the PRIMARY card
  finder. Each number-shaped box = one card, ordered by vertical position.
- Per card, crop a band around the box (full width, ~1 number-row tall +
  margin) — this image carries the number + set symbol for resolution, and is
  what the review screen / save / future matcher use.
- `find_strips` (Hough) becomes a FALLBACK: used only when detection finds too
  few cards (< a floor) — robustness for odd photos.
- Guided path (`capture_meta`) is removed from the API contract (see frontend).

### Per-card resolution + confidence
- For each detected card: `parse_number` → numerator/denominator; `resolve_set`
  on the crop (denominator + symbol pHash); catalog lookup.
- Constraint layer (denominator snap + set-catalog numerator correction) applies
  pack-wide as today.
- **Card confidence** ∈ [0,1] = combination of: OCR box confidence, set resolved
  (yes/ambiguous), numerator exists in the resolved set's catalog, denominator
  matches the set. A card scoring below `PACK_CARD_CONFIDENCE` (default 0.85) is
  "uncertain".
- **Pack confidence** = min card confidence, penalized if the detected count
  looks wrong (e.g. far from a plausible pack size) or the set is unresolved.

### Capture UX (frontend)
- Drop the guide overlay + declared-count from `StaircaseCapture`: the user
  takes one photo of the fanned cards (plus the code-card photo, unchanged).
- `CameraCapture` keeps photo capture; remove the guide-drawing overlay path.
- `scan/pack` no longer needs `capture_meta`; keep the field accepted-but-
  ignored for one release so old clients don't break.

### Speed
- One detection pass over the whole photo instead of the per-strip OCR loop —
  faster. Per-strip OCR retained only for the Hough fallback.
- Tune detection: input cap, `limit_side_len`, det thresholds, and per-crop
  upscale for the recognizer, measured on the corpus.

## Phase 2 — Confidence-gated VLM fallback (needs RunPod GPU)

### Service
- Standalone container (matcher pattern), own repo dir `vlm/`, deployed on a
  RunPod GPU pod (or serverless). Runs a self-hostable VLM — candidates:
  Qwen2.5-VL-3B/7B, GOT-OCR2.0, PaddleOCR-VL.
- `POST /identify`: whole staircase photo + optional set hint (from the code
  card) → JSON list `[{number, set?, confidence, y}]` — definitive card list.
- Bearer-token auth; runs behind RunPod's HTTPS endpoint.

### Gate + reconciliation (in the app)
- After the local pass, if pack confidence < `VLM_GATE` (aggressive: any
  uncertain card, unresolved set, or suspect count triggers it) AND `VLM_URL`
  is set → call the VLM with the whole photo.
- Reconcile: VLM output is authoritative for the uncertain cards and adds any
  card the local pass missed (completeness). Confident local cards are kept.
- Seam: `VLM_URL` unset ⇒ local-only (exact Phase-1 behavior). Any VLM
  error/timeout ⇒ fall back to the local result. Never blocks a scan.

### Validation-first
- A corpus benchmark harness runs the VLM endpoint against the corpus and
  reports accuracy vs the local pass BEFORE wiring it into scans — no repeat of
  "great in theory, unverified on real photos".

## Data / API changes
- `PackScanResponse.cards`: each card now carries `confidence` (already has it)
  sourced from the new model; `row_index` = detection order. Shapes unchanged.
- New: `PackScanResponse.pack_confidence` already exists; semantics updated.
  Add per-card `needs_review: bool` (confidence < threshold) so the frontend can
  highlight only the cards worth a glance.
- Save/review/stats-rederive all consume `scan_pack` output unchanged.
- Config: `PACK_CARD_CONFIDENCE`, `VLM_URL`, `VLM_TOKEN`, `VLM_GATE`.

## Acceptance
- Phase 1: on the corpus, detection-first identifies ≥ the current hybrid's
  distinct correct numbers, faster, with per-card confidence that correctly
  flags the wrong/missing ones as `needs_review` (so escalation/attention lands
  on the right cards). Fixtures updated for the no-guides contract; suite green.
- Phase 2: VLM benchmark beats the local pass on the corpus before integration;
  with `VLM_URL` unset, behavior is exactly Phase 1.
- Seamlessness check: on a clean, reasonably-lit pack, zero user corrections
  needed; on a hard pack, only genuinely-ambiguous cards are flagged.

## Out of scope
- Replacing the code-card capture (it's the anti-fraud "pull is real" signal).
- The visual art matcher (separate; still consumes the per-card crops).
- Training a custom detector (PP-OCR detection is sufficient and real-trained).
