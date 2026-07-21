# Confidence-Gated VLM Fallback — Phase 2 Implementation Plan

> REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Cards the pipeline flags `needs_review` get sent to a Qwen2.5-VL-7B
worker on RunPod (serverless) for definitive identification, behind a swappable
HTTP seam. Off (env unset) ⇒ exact Phase-1 behavior. Never blocks a scan.

**Contract (app ⇄ worker), RunPod serverless runsync:**
- Request: `POST {RUNPOD}/v2/{id}/runsync`  `Authorization: Bearer {key}`
  `{"input": {"cards": [{"row_index", "image_b64", "hint_set", "hint_denominator"}]}}`
- Response: `{"output": {"cards": [{"row_index", "number", "denominator",
  "set_name", "confidence"}]}}`

## File map
- `runpod_worker/handler.py` + `Dockerfile` + `requirements.txt` — the GPU
  worker (user deploys; not run locally). Qwen2.5-VL-7B.
- `app/pack/vlm_client.py` — RunPod runsync client; disabled when `VLM_ENDPOINT`
  unset; every failure → None (never blocks).
- `app/pack/pipeline.py` — MODIFY: `_vlm_fallback(cards, strips, resolutions)`
  post-pass over needs_review cards; re-resolve set + re-lookup.
- `tests/vlm_stub.py` — local FastAPI mimicking runsync (canned IDs) for smokes.
- `.env.example` — MODIFY: VLM_ENDPOINT / VLM_API_KEY / VLM_MODEL.

## Task 1: RunPod worker (Qwen2.5-VL-7B)
- [ ] `runpod_worker/handler.py`: RunPod serverless handler. Load Qwen2.5-VL-7B
  (transformers, bf16, device_map auto) once at cold start. Per card: decode
  image_b64, prompt = "This is the bottom strip of a Pokémon card. Read the
  collector number as printed (e.g. 126/167). If the set symbol is legible, name
  the set. Reply ONLY as JSON {number, denominator, set_name, confidence}."
  (hints injected when present). Parse the JSON from the reply; return per-card.
- [ ] `Dockerfile` (runpod/base CUDA + transformers/accelerate/qwen-vl-utils +
  runpod), `requirements.txt`. Document build+deploy in a header comment.
- [ ] Not runnable locally (no GPU) — verify handler imports + JSON shape with a
  mocked model call.
- [ ] Commit `feat(vlm): RunPod Qwen2.5-VL worker`

## Task 2: app client
- [ ] `app/pack/vlm_client.py`: `enabled()` (VLM_ENDPOINT set); `async
  identify(cards) -> dict[int, dict] | None` — POST runsync, parse output,
  return {row_index: {number, denominator, set_name, confidence}}. Timeout ~60s
  (cold start). Any error/disabled → None. `card` payload includes jpeg→b64 of
  the strip + hints.
- [ ] Commit `feat(vlm): RunPod runsync client (off when unset)`

## Task 3: pipeline integration
- [ ] `_vlm_fallback(cards, strips, resolutions)`: if `vlm_client.enabled()` and
  any `needs_review`, base64 those strips (+ pack hint_set/denominator from the
  resolved majority), call `identify`. For each returned card with a number:
  update card_number; if set_name given, resolve to set_id (denominator table /
  tcgdex) and update set fields; re-lookup via cached_lookup_card for name/
  price/image; clear needs_review when VLM confidence high. Best-effort.
- [ ] Wire into `scan_pack` after the cards list is built. Verify: unset ⇒
  byte-identical; suite green.
- [ ] Commit `feat(vlm): confidence-gated fallback in scan pipeline`

## Task 4: stub + smoke + docs
- [ ] `tests/vlm_stub.py`: FastAPI `POST /v2/{id}/runsync` returning canned
  identifications keyed by row_index (reads the request, echoes plausible
  numbers). Bearer check.
- [ ] Smoke: app + stub, `VLM_ENDPOINT=http://127.0.0.1:PORT/v2/test`; scan a
  photo with unknown-set cards → those cards get VLM numbers/sets merged, others
  untouched. pkill.
- [ ] `.env.example`: VLM_ENDPOINT, VLM_API_KEY, VLM_MODEL + a RunPod deploy note.
- [ ] Commit `feat(vlm): local stub + env + deploy docs`

## Self-review
- Staircase reality: only each card's bottom strip is visible (backs occluded),
  so the worker gets the same strip the OCR did — its edge is better *reading*
  of hard strips + set-symbol recognition, not full-art ID.
- Never blocks: disabled/slow/error all fall through to the Phase-1 result.
