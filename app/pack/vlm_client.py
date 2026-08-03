"""Client for the RunPod-hosted Qwen2.5-VL card-identification worker.

VLM_ENDPOINT unset ⇒ feature off entirely. Every failure returns None — the VLM
is a fallback, never load-bearing: a disabled/slow/errored worker leaves the
Phase-1 result untouched."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time

import cv2
import httpx

log = logging.getLogger("pokemon_scanner.pack.vlm")

# --- warm-up ping ------------------------------------------------------------
WARMUP_INTERVAL_S = 300.0   # at most one attempt per 5 min, process-wide
WARMUP_TIMEOUT_S = 120.0    # must OUTLAST a cold model load, not race it
_last_warmup: float | None = None      # time.monotonic() of the last attempt
_warmup_task: asyncio.Task | None = None   # anchors the in-flight ping (see kick)


def _endpoint() -> str | None:
    # e.g. https://api.runpod.ai/v2/<endpoint-id>
    return os.environ.get("VLM_ENDPOINT", "").strip().rstrip("/") or None


def enabled() -> bool:
    return _endpoint() is not None


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('VLM_API_KEY', '')}"}


async def warmup() -> None:
    """Nudge the serverless endpoint into loading its model, so the first flagged
    card of a scan doesn't pay the ~16GB cold start (up to 90s) inside its own
    identify call.

    The payload is ``{"cards": []}``: the worker's ``handler`` calls ``_load()``
    unconditionally, BEFORE it iterates the cards (runpod_worker/handler.py), so
    an empty batch loads the model and generates nothing. If that ever changes,
    this has to start sending a 1x1 card instead.

    A short timeout would be exactly wrong here — the point is to let the worker
    finish loading, and abandoning the request at, say, 5s tells RunPod to stop.
    So the ping gets its OWN httpx client with a load-sized timeout and holds
    nothing else; it runs off the request path (see ``kick``).

    Debounced process-wide to one attempt per ``WARMUP_INTERVAL_S``: a binder
    page and three pack scans in a row must not mint four cold-start requests
    (each one is a billable serverless job). The window is claimed BEFORE the
    request goes out, so concurrent callers can't slip past the check together —
    and a failed ping therefore also waits out the window rather than retrying on
    every upload against a broken endpoint."""
    global _last_warmup
    base = _endpoint()
    if base is None:
        return
    now = time.monotonic()
    if _last_warmup is not None and now - _last_warmup < WARMUP_INTERVAL_S:
        return
    _last_warmup = now
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=WARMUP_TIMEOUT_S) as client:
            r = await client.post(f"{base}/runsync", json={"input": {"cards": []}},
                                  headers=_auth())
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code >= 400:
            # This ping is the ONLY thing that validates VLM_ENDPOINT/VLM_API_KEY
            # before a card depends on them, so a 401/404 must read as a
            # misconfiguration and not as "warmup ok status=401". identify() logs
            # the same 401 case, but only once a scan has already flagged a card.
            log.error("vlm.warmup_rejected status=%s ms=%.0f "
                      "(check VLM_ENDPOINT / VLM_API_KEY)", r.status_code, ms)
        else:
            log.info("vlm.warmup ok status=%s ms=%.0f", r.status_code, ms)
    except Exception as e:
        # Never load-bearing: a warm-up that fails costs nothing but a log line.
        log.warning("vlm.warmup err=%r ms=%.0f", e, (time.perf_counter() - t0) * 1000)


def kick() -> None:
    """Fire-and-forget ``warmup()`` from a request handler — synchronous, returns
    immediately, and safe to call on every upload (``warmup`` debounces itself).

    The task is anchored in a module global with a done-callback that logs a
    crash, the same shape as ``live_session._ensure_drain`` and
    ``scan_followup.spawn``: a bare ``create_task`` reference can be
    garbage-collected mid-flight, and an unretrieved exception would otherwise
    surface only as asyncio noise at interpreter shutdown. There is at most ONE
    such task, so it needs no key — an in-flight ping means the next caller has
    nothing to do."""
    global _warmup_task
    if not enabled():
        return
    if _warmup_task is not None and not _warmup_task.done():
        return
    try:
        _warmup_task = asyncio.get_running_loop().create_task(warmup())
    except RuntimeError:      # no running loop (sync caller) — nothing to warm
        return
    _warmup_task.add_done_callback(_warmup_done)


def _warmup_done(task: asyncio.Task) -> None:
    global _warmup_task
    if _warmup_task is task:
        _warmup_task = None
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:   # warmup() swallows its own errors, so this is a bug
        log.error("vlm.warmup_task_crashed err=%r", exc)


def jpeg_b64(img) -> str | None:
    """The exact payload encoding ``identify`` sends. Public so a caller that
    hands the request off to a background task can encode UP FRONT and retain
    only these few KB per card instead of pinning whole-page BGR arrays for the
    life of the task (see scan_followup)."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


async def identify(cards: list[dict], timeout: float = 90.0) -> dict[int, dict] | None:
    """cards: [{"row_index": int, "image": bgr ndarray, "hint_set": str|None,
    "hint_denominator": str|None, "kind": "strip"|"full_card",
    "strip_b64": str|None (optional — a magnified bottom-strip crop, already
    ``jpeg_b64``-encoded, sent as a SECOND image; see binder._vlm_payload)}]. Returns
    {row_index: {number, denominator, set_name, confidence}} or None (disabled /
    no cards / error). Timeout is generous for serverless cold start.

    ``"image_b64"`` may be supplied instead of ``"image"`` (already
    ``jpeg_b64``-encoded) — the background follow-up path does that so the crop
    is encoded once, on the request path, and the ndarray can be released.

    ``kind`` tells the worker WHAT IT IS LOOKING AT so it can prompt honestly:
    ``strip`` (the default) is a pack scan's number-row band, ``full_card`` is a
    whole card photo (binder cell / live frame). An omitted kind sends ``strip``,
    which is the prompt every worker built before the field existed — so an old
    worker that ignores the field behaves exactly as it does today."""
    base = _endpoint()
    if base is None or not cards:
        return None
    payload_cards = []
    for c in cards:
        b = c.get("image_b64") or jpeg_b64(c["image"])
        if b is None:
            continue
        payload_cards.append({
            "row_index": c["row_index"], "image_b64": b,
            "hint_set": c.get("hint_set"), "hint_denominator": c.get("hint_denominator"),
            "kind": c.get("kind") or "strip",
            "strip_b64": c.get("strip_b64"),
        })
    if not payload_cards:
        return None
    # Round-trip cost of the remote worker, with the request weight that drives it
    # (serverless cold start + b64 upload dominate this call). Logged in a `finally`
    # so a timeout/error records its wall time too — the slow cases matter most.
    payload_bytes = sum(len(c["image_b64"]) + len(c.get("strip_b64") or "")
                        for c in payload_cards)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/runsync", json={"input": {"cards": payload_cards}},
                headers=_auth())
        if r.status_code == 401:
            log.warning("vlm.unauthorized (check VLM_API_KEY)")
            return None
        r.raise_for_status()
        out = (r.json().get("output") or {}).get("cards") or []
        result = {c["row_index"]: c for c in out if c.get("row_index") is not None}
        log.info("vlm.identify cards=%s answered=%s", len(payload_cards), len(result))
        return result or None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("vlm.identify_failed err=%r", e)
        return None
    finally:
        log.info("timing.vlm.identify cards=%s bytes=%s ms=%.1f",
                 len(payload_cards), payload_bytes, (time.perf_counter() - t0) * 1000)
