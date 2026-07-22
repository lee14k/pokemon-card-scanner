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
    task = asyncio.ensure_future(
        scan_pack(staircase_bytes, code_bytes, capture_meta, progress=queue.put_nowait)
    )

    try:
        while not task.done():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": hb\n\n"
                continue
            yield _sse("progress", ev)

        # The task may finish (put its final "done" progress event, then
        # return) in the same scheduling slot we last checked task.done() in —
        # drain anything left in the queue before emitting the terminal event
        # so no progress frame is dropped or reordered after the result.
        while not queue.empty():
            yield _sse("progress", queue.get_nowait())

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
