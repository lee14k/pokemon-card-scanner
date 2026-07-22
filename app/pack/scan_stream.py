"""SSE wrapper around scan_pack(): streams progress events, then the final
result (or an error), for the progressive /scan/pack/stream endpoint.

This is purely additive — POST /scan/pack keeps calling scan_pack() directly
with no progress callback (default None), so its behavior is untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from app.pack.pipeline import scan_pack

log = logging.getLogger("pokemon_scanner.pack.scan_stream")

# How long the generator waits on an empty queue before emitting a heartbeat
# comment. Keeps intermediate proxies (and browsers) from timing out an idle
# connection during a long segmentation/OCR stretch between progress events.
_HEARTBEAT_SECONDS = 15.0


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Sentinel enqueued (in a `finally`) once scan_pack finishes or raises, so the
# consumer loop below — which otherwise only wakes on queue.get() — is
# guaranteed a prompt wakeup instead of waiting for the next heartbeat.
_SENTINEL = object()


async def scan_pack_sse(
    staircase_bytes: bytes,
    code_bytes: bytes,
    capture_meta: dict | None,
) -> AsyncIterator[str]:
    """Runs scan_pack() as a background task with progress=queue.put_nowait,
    yielding each queued progress dict as an `event: progress` SSE frame as
    soon as it's queued (real streaming, not buffered until the scan ends).
    Emits `: hb\\n\\n` comment heartbeats on idle gaps, a terminal
    `event: result` with the full PackScanResponse JSON on success, or a
    terminal `event: error` on failure. Either terminal event ends the stream.
    """
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def _runner():
        try:
            return await scan_pack(
                staircase_bytes, code_bytes, capture_meta, progress=queue.put_nowait
            )
        finally:
            # Always runs — whether scan_pack returned or raised (including
            # before its first progress event) — so the consumer never blocks
            # past this point waiting on queue.get().
            queue.put_nowait(_SENTINEL)

    task = asyncio.ensure_future(_runner())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": hb\n\n"
                continue
            if item is _SENTINEL:
                break
            yield _sse("progress", item)

        # _runner's finally already ran (it put the sentinel), so task is done.
        try:
            resp = task.result()
        except Exception as e:  # scan_pack failed — report, don't raise into ASGI
            log.warning("scan_stream.scan_failed err=%r", e)
            yield _sse("error", {"message": str(e)})
            return

        yield _sse("result", resp.model_dump(mode="json"))
    finally:
        if not task.done():
            task.cancel()
