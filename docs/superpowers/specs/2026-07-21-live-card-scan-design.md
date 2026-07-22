# Live Card Scan + Speed — Design (Sub-project L)

**Date:** 2026-07-21 · **Status:** approved-pending-review

## Goal

A "Live" scan mode: the user points their phone camera at cards one at a time
(as in pack-opening reels) and each card is identified in near-real-time —
captured instantly, identity + price trailing ~1–2s behind — feeding the same
review → save pull → battle flow as the staircase mode. Plus a speed workstream
(both modes) and repair of the RunPod VLM deployment.

Decisions locked with the user: live camera (not video upload) · CPU-first OCR
with VLM fallback · code card shown in-stream · speed focus = live per-card
latency + perceived speed (not staircase wall-time) · manual shutter always
available but optional — auto-fire is the primary UX and keeps pace with
reel-style flipping (~1 card / 1.8s).

## Workstream 1 — Live scan mode

### UX
- Scan screen gains a mode chooser: **One photo** (existing, unchanged) |
  **Live** (new).
- Live screen: fullscreen camera, card-outline guide sized so the card fills
  70–80% of frame height, tray of identified cards (captured-frame thumbnail,
  name, number/set, price or ❓), always-present manual shutter, auto-fire
  toggle. Capture feedback: frame flash + `navigator.vibrate` + optimistic
  tray chip with spinner; `aria-live="polite"` announcements.
- Code card is shown in-stream; QR auto-detected, fills a code slot in the tray.
- **Finish** → ReviewScreen (live cards show their captured thumbnails; every
  card tappable into FixCardForm) → save pull (`capture_path="live"`) → battle.

### Client capture loop (extends CameraCapture)
- `getUserMedia` `{facingMode:"environment", width:{ideal:3840}, height:{ideal:2160}}`,
  1080p fallback; read `track.getSettings()` at start; below ~1080p short side,
  enlarge guide + advise filling the frame (or suggest One-photo mode).
- Screen wake lock on start, re-acquired on `visibilitychange`; track
  `mute`/`ended` → "camera paused — tap to resume" overlay → re-acquire,
  re-read settings, resync tray via GET session.
- Motion: mean abs gray diff on a persistent 160px canvas (~10–15Hz via
  `requestVideoFrameCallback`, rAF fallback). Sharpness: variance of Laplacian
  on the **number-strip region at ~500px** via `getImageData` (blur that kills
  15px digits is invisible at 160px). Card presence: edge density in guide box.
- Fire when stable + strip-sharp ~300ms + card present; re-arm on motion spike
  + cooldown. Firing captures the blob IMMEDIATELY (no card is ever missed at
  flip pace); the POSTs queue FIFO with one in flight per session, drop-stale
  within a hold window. Identities trail the flipping; captures never wait.
  Auto-fire never required: shutter always works.
- Upload per fire (multipart): **card crop** (guide box, scaled to card height
  ~1200–1400px, JPEG q0.8) + **number strip at native stream resolution**.
  Exactly two persistent canvases (metrics canvas with `willReadFrequently`);
  the hold window's 2nd-best frame kept as an encoded Blob, sent once if the
  first is unreadable (routed to VLM instead if the queue is non-empty).
- Client keeps tray state + accepted blobs for the whole session (durable
  source of truth; enables client-side finish if the server session dies).

### Server API (all authed, new module `app/pack/live.py` + router)
```
POST /scan/live/start                → {session_id}
POST /scan/live/{id}/frame           → {event: card|code_card|duplicate_prompt|no_card|unreadable, card?: PackCard, pending_vlm: bool}
GET  /scan/live/{id}                 → session state (poll ~2s only while pending)
GET  /scan/live/{id}/card/{n}/image  → captured crop (thumbnails)
POST /scan/live/{id}/finish          → PackScanResponse-shaped result
```
- One in-flight frame per session (409 on overlap). Global OCR semaphore shared
  with `/scan/pack`. Server-side downscale guard on oversized uploads.
- Sessions in-memory with sliding TTL (~30min idle); frame JPEGs persisted
  under photo storage keyed by session (TTL sweep); on session 404 the client
  recreates or finishes client-side — a Railway restart never loses a pack.

### Identification core (new `app/pack/live_identify.py` + name index)
1. QR check (`cv2.QRCodeDetector`, factored out of `_read_code_via_qr`) →
   code card → existing `read_code_card`.
2. Two small OCR passes: name band (top ~25% of card crop) + number strip
   (native res) — 3–6 lines total, not a whole card's 15–25.
3. **Name index** (new, in-memory, lazy-loaded): all TCGdex cards with
   normalized names (lowercase, NFKD accent-fold, ♀/♂/★ fold, punctuation
   collapse) + rapidfuzz matching. Name candidates hard-filtered to the title
   band; a name that is a substring of another catalog name needs number
   corroboration.
4. Decision ladder: name+number agree → confident · name+session-denominator
   prior unique (verified: name+num/denominator unique for 8,277/8,278 catalog
   pairs) → confident · number-only catalog-valid → confident · else ❓
   needs_review now, VLM later. Session modal set/denominator feeds later
   frames (live analog of `_apply_constraints`).
5. VLM: uncertain cards batch per session into one `runsync` call via a
   background task held in a per-session registry; terminal `vlm_failed` state;
   answers patch session cards in place (reuses `_vlm_fallback` merge logic,
   lifted to a shared function). Never blocks; never spins forever.
6. Duplicates: silent dedup only within the double-fire window (~2s); a later
   same-identity capture → `duplicate_prompt` tray event ("Another copy of X?
   Add / ignore", default add) — also the visible symptom of misresolution.
   "Wrong? re-scan" marks a row replaceable so re-showing overwrites it.
7. Price: `PackCard` gains `price_usd_low/high` (already optional in api.ts),
   filled from `latest_price_map` at identify time.

### Save path & data integrity
- Finish assembles confirmed cards → existing `POST /pulls` with
  `capture_path="live"`. A composite contact sheet satisfies the NOT NULL
  staircase photo column **for display only**: stats re-derivation and training
  harvest MUST skip `capture_path="live"` pulls; derived rows are written
  directly from the session's server-authoritative identifications at save.
- Per-card frames move into the pull's storage dir (`frame_NN.jpg`) — real
  single-card phone frames are future training data.
- Code card at Finish is an explicit choice: "Scan code card (needed to battle
  & count in stats)" vs "Save anyway — this pull can't battle". New
  `PATCH /pulls/{id}/code` (owner-only, unverified pulls) accepts a code photo
  and reruns the existing re-OCR + verified logic — rescues live AND staircase
  pulls.

### Error handling
- `no_card`/`unreadable` are silent client re-arms; VLM failure is terminal
  (card stays needs_review, fixable in review); Finish is never blocked —
  pending cards enter review patchable. Frame POST failures retry once then
  surface a toast; the blob stays client-side so nothing is lost.

## Workstream 2 — Speed (both modes)

- **ORT thread pinning:** RapidOCR constructed with
  `intra_op_num_threads=$OCR_THREADS` (default = cpu quota, env-configurable),
  `inter_op_num_threads=1`; pin `cv2.setNumThreads`/`OMP_NUM_THREADS`. Today
  the engine sizes its thread pool to the HOST's cores against Railway's
  2-vCPU cgroup — likely slowing every scan. Re-baseline on Railway after.
- **Staircase perceived speed:** optional SSE variant of `/scan/pack`
  (StreamingResponse: stage events `decoded` → `cards_found: N` → per-card
  identify progress → final PackScanResponse; comment heartbeats every 15s; no
  compression on the route; existing POST remains the fallback). Frontend
  shows skeleton card rows filling in instead of a spinner. Verify with
  `curl -N` against Railway before building the consumer.
- **Live latency budget (honest):** upload ~0.5s median LTE + band OCR
  0.3–0.6s (pinned, warm) + lookup ~ms → **~1–2s typical, 2–3.5s p90**, masked
  by the optimistic chip. Measured on Railway before UX copy promises anything.

## Workstream 3 — Catalog

- Extend SetIdMap + `set_denominators.json` to the me-era sets (re-run
  `scripts/build_id_maps.py` / `build_denominator_table.py`); PokéWallet gaps
  degrade to identity + TCGdex image with price "—".
- Add `rapidfuzz` to app deps.

## Workstream 4 — RunPod VLM worker redeploy (fix shipped, deploy pending)

Root cause of "All workers are unhealthy" (diagnosed by running the pushed
image locally under amd64 emulation): `CMD ["python", ...]` — runpod/base has
no bare `python` (six pythons; `pip` targets 3.11) → instant
`exec: python: not found` exit loop. Second latent crash: `torchvision`
missing (Qwen2.5-VL image processor imports it). Both fixed in
`runpod_worker/Dockerfile` (commit cd688f4): `CMD python3.11`,
`torch==2.5.1 torchvision==0.20.1` from the cu121 index, `|| true` removed.
Remaining steps: user rebuilds with a **versioned tag**
(`docker buildx build --platform linux/amd64 -t lee14k/pcs-vlm:v2 --push runpod_worker/`),
points the endpoint at `:v2`, then end-to-end verify: a needs_review corpus
photo (the /086 pack) resolved definitively via `VLM_ENDPOINT` against the
live endpoint.

## Non-goals (v1)

Uploaded video files · WebSocket streaming · client-side ML · multi-card
frames · changing verified semantics · automated test-suite additions (per
standing preference; acceptance via phone smokes + reel-frame fixtures).

## Acceptance

1. Live mode on a real phone: a 10-card flip-through at reel pace (~1 card /
   2s) captures every card, ≥8/10 identified without VLM, tray lag ≤ ~2 cards,
   finish → review → saved verified pull → battle-eligible.
2. Reel-frame fixtures (extracted from the user's example video) through the
   frame endpoint: name-based identity resolves for legible steady frames.
3. VLM endpoint healthy on RunPod; /086 corpus cards resolve definitively.
4. Staircase scan on Railway is not slower than before thread pinning, and the
   SSE variant streams stages end-to-end through Railway's proxy.
5. Existing test suite stays green; staircase behavior unchanged when live
   feature unused.
