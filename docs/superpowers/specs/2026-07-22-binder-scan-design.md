# Binder Page Scan → Collection — Design (Sub-project M)

**Date:** 2026-07-22 · **Status:** approved (design), spec pending user review

## Goal

Scan multiple cards in one photo — a binder page in sleeves — and save them to
a new per-trainer **Collection**: cards you own, distinct from pack pulls.
Decisions locked with the user: **Collection concept** (not pulls; never
battles/pull-stats; feeds Pokédex + portfolio value) · **fully mixed sets per
page** (no page-level set prior) · **one page at a time** (each photo is its
own scan → review → save; design must not preclude a later multi-page session)
· **Approach A + B's crop idea** (text-cluster segmentation from one
whole-photo OCR pass, plus local contour refinement of each cell crop).

## Data model — migration 0009, table `collection_card`

- `id` uuid PK · `trainer_id` FK → trainer, indexed
- Identity: `tcgdex_card_id` text nullable (e.g. "me05-004") · `set_id` text
  nullable (PokéWallet) · `set_code` text nullable · `set_name` text nullable ·
  `card_number` text nullable (display, e.g. "004/217") · `numerator` text
  nullable · `name` text nullable · `image_url` text nullable · `match_id`
  text nullable
- `identity_key` text NOT NULL — same formula as live dedup:
  `{set_code or set_name or '?'}:{numerator or normalized_name}`
- `qty` int NOT NULL default 1 · `created_at` / `updated_at` timestamptz
- Unique index `(trainer_id, identity_key)` — saves are upserts; re-scanning an
  owned card increments `qty`.

## Page pipeline — `app/pack/binder.py`

1. Decode via existing `_decode` (EXIF/HEIC-safe). Detection copy capped at
   2800px long side (same as staircase).
2. `detect_lines_xy(img, cap)` — NEW in `rapidocr_reader.py`: identical single
   whole-photo RapidOCR pass, returning `(x_center, y_center, text, conf,
   box_w, box_h)` per line (x/box data already exists internally; today it is
   discarded). `detect_lines` becomes a wrapper over it dropping x — zero
   behavior change for existing callers.
3. **Cell clustering** (pure geometry, no ML): sort lines by x; 1-D gap
   clustering into columns (new column when x-gap > 8% of page width); within
   each column, 1-D gap clustering on y into cells (y-gap > 12% of page
   height). A cell = its member lines. Grid dims reported as
   `rows = max cells in any column`, `cols = n columns`. Empty pockets produce
   no cluster and are not errors.
4. **Identity per cell** via a NEW shared core, extracted from live_identify:
   `app/pack/identify_core.py::resolve_identity(name_texts: list[tuple[str,
   float]], reading: NumberReading | None, prior: SessionPrior | None)
   -> IdentityResult` — the exact ladder live mode uses (name index match with
   per-card-denominator set-scoped recovery; name+number agree > unique name >
   number+prior; stage-accurate low_confidence_reason from commit a27e566).
   `live_identify.identify_frame` is refactored to call it (one
   implementation, no drift). Binder passes `prior=None` (mixed sets); each
   cell's own denominator still drives set-scoped recovery. Per-cell number =
   best `pattern_ok` parse among the cell's lines; name candidates = the
   cell's other lines, top-of-cell first.
5. **Cell box + refinement** — NEW `app/pack/card_crop.py`:
   `refine_card_box(img, coarse_bbox) -> bbox`. Coarse box = cluster bbox
   expanded to the grid pitch (median column/row pitch), clamped to image.
   Refinement searches within 1.15× the coarse box: Canny → findContours →
   largest contour with 4-corner approx, aspect in [0.63, 0.80] (card is
   63:88), area ≥ 40% of the search region → minAreaRect box. Any failure →
   coarse box (glare-safe). This is the repo's first quad-detection code —
   deliberately local-scope with a hard fallback.
6. **Lookups & prices:** confident cells → `cached_lookup_card` +
   `card_fields_from_match`; TCGdex name/image fallback for me-era via the
   shared vlm_merge-style path; prices attached from `latest_price_map` by
   match_id (me-era "—").
7. **VLM fallback:** needs_review cells → refined crops → one batched
   `vlm_client.identify` call → `vlm_merge.apply_vlm_answer` per cell —
   synchronous within the request (upload flow; unlike live there is no
   polling channel). All me-era behavior (denominator→set, TCGdex naming,
   identity-gated accept) inherited.

## API

- `POST /scan/binder` — authed (CurrentTrainer), multipart `page` UploadFile →
  `BinderScanResponse { cards: [BinderCard], grid: {rows, cols},
  page_confidence: float }` where `BinderCard = PackCard fields + cell:
  {x, y, w, h} + thumb_b64: str | null` (JPEG ~240px wide, quality 70,
  base64 — stateless, ~15KB × 9).
- `POST /collection` — authed; body `{cards: PackCard-shaped JSON list}` →
  server derives storage identity per card: `identity_key` by the live-dedup
  formula, and `tcgdex_card_id` = `f"{tdx}-{numerator.zfill(3)}"` when the
  card's set resolves to a tcgdex set id via `SetEntry.tcgdex_id or set_code`
  (None otherwise) — clients never send identity keys. Upsert each by
  `(trainer_id, identity_key)`, `qty += 1` per occurrence;
  response `{added: int, incremented: int, total_cards: int, encounters:
  [Encounter]}` (species encounters reuse the pulls helper → Pokédex).
- `GET /collection` — authed → `{cards: [CollectionCardOut(+price_usd_low/
  high)], total_qty: int, estimated_value: float | null, priced_as_of}` via
  `latest_price_map`; sorted set_code then numerator (numeric).
- `PATCH /collection/{id}` body `{qty: int >= 1}` · `DELETE /collection/{id}`
  — owner-only; 404 on foreign/missing.

## Frontend

- Mode chooser (from sub-project L) gains **“Binder page”**.
- `BinderCapture.tsx`: one-shot CameraCapture reuse + upload fallback; copy:
  "Lay the page flat, fill the frame, avoid glare."
- `BinderReview.tsx`: CSS grid `rows × cols` mirroring the page; each cell =
  thumb_b64 image + name/number/set + price + flag state; tap ANY cell →
  existing FixCardForm (preserves row identity); confirm → `POST /collection`
  → summary (added N, +M qty, new dex species, est value added). Cells the
  scan flagged block nothing — same review semantics as live.
- New nav view **“Collection”** (`Collection.tsx`): owned-card grid (image,
  name, qty badge, price), header totals (distinct cards, total qty, estimated
  value). Sort set → number. No search/filter in v1.

## Errors & edge cases

- No text lines at all → 422 `{"detail": "no_cards_found"}`; frontend copy:
  "Couldn't find any cards — reduce glare and fill the frame with the page."
- Irregular cell counts are normal (empty pockets); return what was found.
- Zero silent misidentifications: cells clear the same confidence bar as live
  or flag needs_review; VLM accept still requires set + name.
- Oversized uploads server-downscaled (existing guard pattern); body-size
  middleware unchanged (one photo ≤ existing 2-photo limit).

## Non-goals (v1)

Multi-page binder sessions (the review/save shape is per-page and would not
change; a session would only accumulate) · condition/grading · card backs ·
non-grid layouts (toploader piles) · collection sharing/export · battles or
pull-stats coupling · automated test additions (standing rule — verification
via smokes + the user's binder-photo fixtures).

## Acceptance

1. On the user's real binder-page photos (fixtures in `tests/corpus/binder/`
   once provided; until then a synthetic 3×3 composite built from TCGdex card
   images serves as the interim gate): every fully-visible card correctly
   identified or explicitly flagged — zero silent wrong identities.
2. Grid in review mirrors the physical page (rows/cols/order).
3. Re-scanning a page increments qty (no duplicate rows); PATCH/DELETE work.
4. Prices show where PokéWallet has the set; me-era shows "—" with TCGdex
   name/image.
5. Existing suite `7 passed, 1 skipped`; staircase and live flows byte-
   identical when binder mode unused.
