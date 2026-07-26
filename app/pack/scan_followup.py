"""In-memory follow-up store for the NON-BLOCKING VLM pass (pack + binder).

Both batch scans used to await the RunPod round trip (90s timeout) before
answering the HTTP request, so a single cold worker turned a ~2s scan into a
~30s one. They now answer immediately with the Phase-1 cards, park the flagged
rows here, and drain the VLM in ONE background task that patches this store; the
client polls ``GET /scan/{pack,binder}/{scan_id}`` until ``done``.

This is the batch-flow twin of ``live_session``'s per-session drain and copies
its concurrency shape:

- Single process only (the Procfile runs ONE uvicorn worker — the same
  constraint live sessions already carry). A second worker would serve a poll
  that never resolves, so this must never be paired with ``--workers``.
- Store access is plain synchronous code on the single asyncio event loop: every
  function here returns without awaiting, so no lock is needed (unlike
  live_session, which must hold ``_store_lock`` across awaits).
- Exactly one drain task per entry lives in ``_tasks`` with a done-callback that
  de-registers it and *logs* (never swallows) a crash — that callback reference
  also anchors the task against the weak-ref GC that kills a bare create_task.
- TTL is swept lazily on ``create``, mirroring live_session's ``_sweep_expired``.

Entries are keyed by a 128-bit ``scan_id``: the pack poll endpoint is anonymous
(same as ``POST /scan/pack``), so the key IS the capability. ``kind`` is checked
on every read so a binder entry — created behind auth — can never be read
through the anonymous pack route.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from functools import partial
from typing import Coroutine, Literal

log = logging.getLogger("pokemon_scanner.pack.scan_followup")

TTL_S = 1800          # 30 min — a review screen left open still resolves
_ID_BYTES = 16        # 128-bit unguessable key (the pack route has no auth)

Kind = Literal["pack", "binder"]
PENDING = "pending_vlm"

_entries: dict[str, "FollowupEntry"] = {}
_tasks: dict[str, asyncio.Task] = {}


@dataclass
class FollowupEntry:
    kind: Kind
    cards: list[dict]     # full card dicts, one per row, each carrying "state"
    created: float
    expires_at: float
    done: bool = False


# --- store -------------------------------------------------------------------
def create(kind: Kind, cards: list[dict]) -> str:
    """Register a follow-up for one scan and return its ``scan_id``.

    ``cards`` are COPIED shallowly: the caller's dicts are (or become) part of an
    HTTP response body that FastAPI is about to serialize across awaits, and the
    drain task must not be able to mutate them mid-serialization."""
    _sweep()
    scan_id = secrets.token_hex(_ID_BYTES)
    now = time.time()
    _entries[scan_id] = FollowupEntry(
        kind=kind, cards=[dict(c) for c in cards], created=now,
        expires_at=now + TTL_S)
    return scan_id


def patch(scan_id: str, row_index: int, card: dict) -> None:
    """REPLACE one row's card dict (never mutate it in place — ``get`` hands out
    the same dict objects, and a poll response may be mid-serialization). An
    unknown scan_id/row is a no-op: the entry may have been swept while the VLM
    call was in flight, which must not crash the drain task."""
    entry = _entries.get(scan_id)
    if entry is None:
        return
    for i, existing in enumerate(entry.cards):
        if existing.get("row_index") == row_index:
            entry.cards[i] = dict(card)
            return
    log.warning("followup.patch_unknown_row scan=%s row=%s", scan_id, row_index)


def finish(scan_id: str) -> None:
    """Mark the follow-up terminal so the client's poll stops. Called from the
    drain task's ``finally`` — a crashed or timed-out VLM pass must still end the
    poll, or the review screen spins forever."""
    entry = _entries.get(scan_id)
    if entry is not None:
        entry.done = True


def get(scan_id: str, kind: Kind) -> dict | None:
    """``{cards, any_pending, done}`` for a live entry of THIS kind, else None
    (unknown id, expired, or a kind mismatch — all collapse to one 404 so a probe
    can't distinguish them). The card list is a fresh list of the stored dicts:
    ``patch`` replaces entries rather than editing them, so this snapshot stays
    coherent even if the drain task lands while the response is serializing."""
    entry = _entries.get(scan_id)
    if entry is None or entry.kind != kind or entry.expires_at <= time.time():
        return None
    cards = list(entry.cards)
    return {
        "cards": cards,
        "any_pending": any(c.get("state") == PENDING for c in cards),
        "done": entry.done,
    }


# --- the one drain task per entry --------------------------------------------
def spawn(scan_id: str, coro: Coroutine) -> None:
    """Run ``coro`` as THE background drain for ``scan_id``, anchored in
    ``_tasks`` (a bare create_task can be garbage-collected mid-flight) with a
    done-callback that logs any crash instead of letting asyncio swallow it into
    a never-retrieved exception."""
    task = asyncio.create_task(coro)
    _tasks[scan_id] = task
    task.add_done_callback(partial(_drain_done, scan_id))


def _drain_done(scan_id: str, task: asyncio.Task) -> None:
    if _tasks.get(scan_id) is task:
        _tasks.pop(scan_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # The drain's own `finally` already called finish(), so the client is not
        # stranded — this line exists so the crash is diagnosable.
        log.error("followup.drain_crashed scan=%s err=%r", scan_id, exc)


def _sweep() -> None:
    """Lazy TTL sweep (from ``create``): drop expired entries and cancel any
    drain still attached to them. A drain outlives its entry only if the VLM call
    hangs past the TTL — 30 min against a 90s client timeout — but cancelling
    costs nothing and keeps the task dict from leaking."""
    now = time.time()
    expired = [sid for sid, e in _entries.items() if e.expires_at <= now]
    for sid in expired:
        _entries.pop(sid, None)
        task = _tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()
    if expired:
        log.info("followup.swept entries=%s remaining=%s", len(expired), len(_entries))
