# Visual Card Matching (Sub-project H) — Design

Approved direction: identify pulled cards by matching strip images against
reference card art, demoting number OCR to a tiebreaker. Reference images come
from PokéWallet behind a swappable fetcher seam (B-track/TCGdex converges
later). The matcher runs as a **dedicated service** in its own container.

## Why

Real-photo number OCR is the pipeline's weakest link: digits are ~3mm tall,
glare-prone, and misreads can silently match the wrong real card (`128/167` vs
`126/167`). Set resolution, segmentation, and code reading are all reliable
now. Card art is a far stronger identification signal, and with the set known
the candidate space is only ~200 cards.

## Architecture

Two Railway services, one repo:

- **Main app** (existing): orchestrates. Owns Postgres, PokéWallet access, and
  set enumeration. Calls the matcher over private networking; never depends on
  it (matcher down ⇒ scan degrades to today's OCR-first path).
- **Matcher service** (`matcher/` directory, own Dockerfile, own Volume): owns
  the embedding model and per-set reference indexes. Stateless API + on-disk
  index files; no database.

## Matcher service

- **Model**: CLIP ViT-B/32 *image encoder*, ONNX, run on CPU via
  `onnxruntime`. Multi-stage Docker build: download a pinned pre-exported ONNX
  (HuggingFace, exact URL+sha pinned in the Dockerfile) into the runtime image;
  runtime deps only `fastapi uvicorn onnxruntime numpy pillow httpx`.
  Embedding dim 512, cosine similarity.
- **Endpoints** (all behind `Authorization: Bearer $MATCHER_TOKEN`):
  - `POST /index/{set_key}` — body: `{"cards": [{"id": str, "image_url": str}...]}`.
    Fetches each image (throttled, retried), crops the reference bottom strip,
    embeds, persists `{set_key}.npz` (`ids`, float32 `[N,512]` vectors) plus a
    meta JSON (`built_at`, `count`, `source`) under `INDEX_DIR` (Volume).
    Rebuild = overwrite. Returns counts + failures.
  - `POST /match/{set_key}` — multipart: N strip images in one batched request.
    Returns per strip the top-5 `[{"id", "score"}]` by cosine. 404 if no index.
  - `GET /index/{set_key}` — status (exists, count, built_at).
  - `GET /health`.
- **Reference crop**: bottom 14% of the card image height, full width — the
  region a scan strip shows. Strips and reference crops are letterbox-resized
  to the model's 224×224 input identically on both sides.
- **Config**: `MATCHER_TOKEN`, `INDEX_DIR=/data`, `PORT`.

## Main-app integration

- **Set enumeration** (`app/enumeration.py`): fetch every card of a set from
  PokéWallet via paginated search, upsert into `card` (source `'enumerate'`).
  The exact query form (`q=<set_id>` paginated) must be validated against the
  real API in the first implementation task; fallback if unsupported: iterate
  numerators `1..denominator+40` through the existing keyed lookup, throttled.
  Image URLs come from the **fetcher seam**: `reference_image_url(card_row)` —
  today `pokewallet_image_url(match_id)`; the TCGdex migration swaps this one
  function and rebuilds indexes.
- **Scan flow** (after set resolution, before/alongside PokéWallet lookups):
  1. One batched `POST /match/{set_id}` with all strips (timeout ~8s total).
  2. Per strip, fuse art match with OCR:
     - art top-1 `score ≥ PACK_MATCH_ACCEPT` (default 0.85) **and** margin over
       top-2 `≥ PACK_MATCH_MARGIN` (default 0.02): the matched reference card
       is authoritative — its number/name/match_id populate the row.
       OCR numerator agreement lifts confidence (≈0.95); OCR disagreement sets
       `low_confidence_reason="art_ocr_disagree"` (review-flagged, art kept).
     - below thresholds: today's OCR-first behavior, art top-1 recorded as a
       hint when present.
  3. No index yet (404): fire-and-forget index build (enumerate + POST
     /index), scan completes OCR-only. First scan of a set behaves like today;
     the next is art-matched.
  4. Any matcher error/timeout: log, degrade to OCR path. Never fail a scan.
- **Admin**: `POST /admin/matcher/index/{set_id}` (admin role) to pre-warm or
  rebuild a set index; proxies enumeration + matcher call, returns the report.
- **Config**: `MATCHER_URL` (empty ⇒ feature off, exactly today's behavior),
  `MATCHER_TOKEN`, `PACK_MATCH_ACCEPT`, `PACK_MATCH_MARGIN`.
- **Batch re-derivation** (`app/stats/rederive.py`) picks the art path up
  automatically since it calls `scan_pack`.

## Deployment

- Railway: second service from the same repo, root `matcher/`, Dockerfile
  build, Volume at `/data`, private networking; main app gets
  `MATCHER_URL=http://<matcher>.railway.internal:<port>` + shared token.
- Local dev: `uvicorn matcher.app:app` on a fresh port; smokes use a tiny
  stub image server (pattern: tests/pokewallet_stub.py) for reference fetches.

## Failure modes

| Failure | Behavior |
|---|---|
| Matcher down/slow | scan degrades to OCR-first (log warning) |
| Index missing | OCR-only scan + background index build |
| Reference image fetch fails | card skipped from index, counted in build report |
| Art/OCR disagree | row review-flagged, art result kept |
| Model file missing/corrupt | matcher fails health check; main app treats as down |

## Acceptance (manual, corpus-based — no automated tests per repo rule)

- Corpus pack (11 real strips, TWM): **≥9 of 11 correct top-1 art matches**
  with a locally built TWM index (references via real image URLs), vs 2–3
  usable IDs today. Provisional thresholds tuned on this measurement.
- Fixture regression: synthetic guided scan unchanged with `MATCHER_URL` unset
  and with a local matcher running (stub references).
- Memory: matcher container steady-state < 1GB; main app peak unchanged.

## Out of scope

- TCGdex as reference source (B-track; swaps the fetcher seam later).
- Embedding-model fine-tuning; foil/reverse-holo variant discrimination.
- Client-side changes: the review screen already renders whatever the scan
  returns.
