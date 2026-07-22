"""In-memory live-scan session store.

Live scan holds up one card at a time; each frame POST runs Task 4's
``identify_frame`` and lands here. This module accumulates the per-frame results
into a session, dedups the same physical card scanned across consecutive frames,
persists each accepted frame's JPEG to disk, and drains still-uncertain cards to
the RunPod VLM worker in ONE background task per session.

Concurrency shape (all on the single asyncio event loop):
- Module store ``_sessions`` is guarded by ``_store_lock`` for start/get/sweep.
- Each session owns a ``frame_lock`` — Task 6 (the API) holds it per frame so a
  second in-flight frame for the same session gets a 409-busy. The store itself
  never blocks on it for the long VLM call.
- Exactly one VLM drain task per session lives in ``_vlm_tasks`` with a
  done-callback that de-registers it and *logs* (never swallows) any crash — this
  also anchors the task against the weak-ref GC that kills a bare create_task.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import Counter
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4

import cv2
import numpy as np

from app.db.config import db_settings
from app.pack import vlm_client
from app.pack.live_identify import FrameResult, SessionPrior
from app.pack.set_resolution import load_denominator_table
from app.pack.vlm_merge import apply_vlm_answer
from app.schemas import CodeCardResult, PackCard

log = logging.getLogger("pokemon_scanner.pack.live_session")

DUP_WINDOW_S = 2.0        # same identity within this of a card's capture -> same hold-up
SESSION_TTL_S = 1800      # 30 min sliding idle TTL
VLM_ACCEPT = 0.7          # (mirrors vlm_merge.VLM_ACCEPT; used by apply_vlm_answer)
VLM_DEBOUNCE_S = 2.0      # batch consecutive uncertain cards into one identify() call

_State = Literal["ok", "pending_vlm", "vlm_failed", "dup_prompt"]

# Module store + one drain task per session.
_sessions: dict[str, "LiveSession"] = {}
_store_lock = asyncio.Lock()
_vlm_tasks: dict[str, asyncio.Task] = {}


@dataclass
class LiveCard:
    card: PackCard
    identity_key: str
    state: _State
    captured_at: float           # time.monotonic() of the frame that produced this row
    replaceable: bool


@dataclass
class LiveEvent:
    event: Literal["card", "code_card", "duplicate_prompt", "no_card", "unreadable"]
    card: PackCard | None
    pending_vlm: bool


def _live_root() -> Path:
    return Path(db_settings().photo_storage_dir) / "live_sessions"


class LiveSession:
    def __init__(self, session_id: str, trainer_id: str) -> None:
        self.id = session_id
        self.trainer_id = trainer_id
        self.cards: list[LiveCard] = []
        self.code: CodeCardResult | None = None
        self.frame_lock = asyncio.Lock()
        self.expires_at = time.time() + SESSION_TTL_S
        self._pending: list[int] = []   # row_index of cards awaiting the VLM drain

    # --- lifecycle -----------------------------------------------------------
    def touch(self) -> None:
        self.expires_at = time.time() + SESSION_TTL_S

    def _dir(self) -> Path:
        return _live_root() / self.id

    def frame_path(self, row_index: int) -> Path:
        return self._dir() / f"frame_{row_index}.jpg"

    def prior(self) -> SessionPrior:
        """Session context for the next frame: the modal confident set and the
        modal printed denominator seen so far. Feeds identify_frame's ladder and
        the VLM hints."""
        sets: Counter = Counter()
        dens: Counter = Counter()
        for lc in self.cards:
            c = lc.card
            if lc.state == "ok" and c.set_id:
                sets[(c.set_id, c.set_name)] += 1
            if c.card_number and "/" in c.card_number:
                dens[c.card_number.split("/")[1]] += 1
        set_id = set_name = denominator = None
        if sets:
            (set_id, set_name), _ = sets.most_common(1)[0]
        if dens:
            denominator = dens.most_common(1)[0][0]
        return SessionPrior(set_id=set_id, set_name=set_name, denominator=denominator)

    # --- frame ingestion (SYNC: Task 6 calls it while holding frame_lock) -----
    def add_frame_result(self, res: FrameResult, card_jpeg: bytes) -> LiveEvent:
        self.touch()

        if res.kind == "no_card":
            return LiveEvent("no_card", None, False)
        if res.kind == "unreadable":
            # No card row yet — the client retries with its 2nd-best frame. Queue
            # nothing.
            return LiveEvent("unreadable", None, False)
        if res.kind == "code_card":
            # Later read wins only when it is format_ok; otherwise keep the best
            # we already have.
            if res.code is not None and (res.code.format_ok or self.code is None):
                self.code = res.code
            return LiveEvent("code_card", None, False)

        # res.kind == "card"
        card = res.card
        key = res.identity_key or ""
        now = time.monotonic()

        # 1. Dedup against the most-recent NON-replaceable row with this identity.
        dup = None
        for lc in self.cards:
            if not lc.replaceable and lc.identity_key == key:
                if dup is None or lc.captured_at > dup.captured_at:
                    dup = lc
        if dup is not None:
            if now - dup.captured_at <= DUP_WINDOW_S:
                # Same hold-up, refining read -> silently keep the better confidence.
                dup.card.confidence = max(dup.card.confidence, card.confidence)
                return LiveEvent("card", dup.card, False)
            # A later hold-up of a same-identity card -> ask the user (real dup?).
            row = len(self.cards)
            card.row_index = row
            self._persist(row, card_jpeg)
            self.cards.append(LiveCard(card=card, identity_key=key, state="dup_prompt",
                                       captured_at=now, replaceable=False))
            return LiveEvent("duplicate_prompt", card, False)

        # 2. A replaceable row with this identity -> overwrite it in place.
        for lc in self.cards:
            if lc.replaceable and lc.identity_key == key:
                row = lc.card.row_index
                card.row_index = row
                self._persist(row, card_jpeg)
                lc.card = card
                lc.identity_key = key
                lc.captured_at = now
                lc.replaceable = False
                lc.state = "ok"
                pending = self._queue_vlm(lc) if res.needs_vlm else False
                return LiveEvent("card", card, pending)

        # 3. Fresh card -> append a new row.
        row = len(self.cards)
        card.row_index = row
        self._persist(row, card_jpeg)
        lc = LiveCard(card=card, identity_key=key, state="ok", captured_at=now,
                      replaceable=False)
        self.cards.append(lc)
        pending = self._queue_vlm(lc) if res.needs_vlm else False
        return LiveEvent("card", card, pending)

    def _persist(self, row_index: int, card_jpeg: bytes) -> None:
        p = self.frame_path(row_index)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(card_jpeg or b"")

    # --- user resolutions ----------------------------------------------------
    def _at(self, row_index: int) -> LiveCard | None:
        if 0 <= row_index < len(self.cards) and \
                self.cards[row_index].card.row_index == row_index:
            return self.cards[row_index]
        return next((lc for lc in self.cards if lc.card.row_index == row_index), None)

    def resolve_duplicate(self, row_index: int, add: bool) -> None:
        """Answer a ``duplicate_prompt``. add=True keeps the row as a genuine second
        copy (state ok); add=False leaves it ``dup_prompt`` so finish() drops it."""
        lc = self._at(row_index)
        if lc is not None and lc.state == "dup_prompt" and add:
            lc.state = "ok"

    def mark_replaceable(self, row_index: int) -> None:
        lc = self._at(row_index)
        if lc is not None:
            lc.replaceable = True

    def finish(self) -> list[PackCard]:
        """Renumber row_index 0..n-1 in capture order, dropping the dup_prompt rows
        the user never confirmed. Frames stay on disk for Task 7 to move."""
        out: list[PackCard] = []
        for lc in self.cards:
            if lc.state == "dup_prompt":
                continue
            lc.card.row_index = len(out)
            out.append(lc.card)
        return out

    # --- VLM background drain ------------------------------------------------
    def _queue_vlm(self, lc: LiveCard) -> bool:
        """Mark ``lc`` for VLM. With no worker configured the card can never be
        resolved, so it goes straight to the terminal vlm_failed state and nothing
        is queued. Returns True only when a drain is actually pending."""
        if not vlm_client.enabled():
            lc.state = "vlm_failed"
            return False
        lc.state = "pending_vlm"
        self._pending.append(lc.card.row_index)
        self._ensure_drain()
        return True

    def _ensure_drain(self) -> None:
        task = _vlm_tasks.get(self.id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._drain_vlm())
        _vlm_tasks[self.id] = task
        task.add_done_callback(partial(_drain_done, self.id))

    async def _drain_vlm(self) -> None:
        """Single per-session loop: debounce, then send everything still pending in
        ONE identify() batch. Wrapped so one bad batch can't crash the loop or leak
        the task; each card ends terminal (ok or vlm_failed) — never re-queued."""
        try:
            table = load_denominator_table()
            while True:
                await asyncio.sleep(VLM_DEBOUNCE_S)   # batch consecutive uncertain cards
                pending, self._pending = self._pending, []
                if not pending:
                    break
                if not vlm_client.enabled():
                    self._fail(pending)
                    continue
                prior = self.prior()
                payload, row_map = [], {}
                for row in pending:
                    lc = self._at(row)
                    if lc is None or lc.state != "pending_vlm":
                        continue
                    img = self._decode_frame(row)
                    if img is None:
                        self._fail([row])
                        continue
                    payload.append({"row_index": row, "image": img,
                                    "hint_set": prior.set_name,
                                    "hint_denominator": prior.denominator})
                    row_map[row] = lc
                if not payload:
                    continue
                result = await vlm_client.identify(payload, timeout=90)
                for row, lc in row_map.items():
                    if lc.state != "pending_vlm":
                        continue  # resolved/overwritten while identify() was in flight
                    ans = (result or {}).get(row)
                    accepted = await apply_vlm_answer(lc.card, ans, table) if ans else False
                    lc.state = "ok" if accepted else "vlm_failed"
        except Exception as e:
            log.warning("live.vlm_drain_failed session=%s err=%r", self.id, e)

    def _fail(self, rows: list[int]) -> None:
        for row in rows:
            lc = self._at(row)
            if lc is not None and lc.state == "pending_vlm":
                lc.state = "vlm_failed"

    def _decode_frame(self, row_index: int) -> np.ndarray | None:
        try:
            data = self.frame_path(row_index).read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def _drain_done(session_id: str, task: asyncio.Task) -> None:
    if _vlm_tasks.get(session_id) is task:
        _vlm_tasks.pop(session_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("live.vlm_task_crashed session=%s err=%r", session_id, exc)


# --- module API --------------------------------------------------------------
async def start_session(trainer_id: str) -> str:
    await _sweep_expired()
    session_id = uuid4().hex
    session = LiveSession(session_id, trainer_id)
    async with _store_lock:
        _sessions[session_id] = session
    return session_id


async def get_session(session_id: str, trainer_id: str) -> LiveSession | None:
    """Ownership-enforced fetch. Returns None for an unknown session OR a trainer
    who does not own it (never leaks another trainer's in-progress scan)."""
    async with _store_lock:
        session = _sessions.get(session_id)
    if session is None or session.trainer_id != trainer_id:
        return None
    session.touch()
    return session


async def _sweep_expired() -> None:
    """Lazy TTL sweep (called from start_session): drop idle sessions, cancel their
    drain task, and rmtree their frame dir."""
    now = time.time()
    async with _store_lock:
        expired = [sid for sid, s in _sessions.items() if s.expires_at <= now]
        for sid in expired:
            _sessions.pop(sid, None)
    for sid in expired:
        task = _vlm_tasks.pop(sid, None)
        if task is not None and not task.done():
            task.cancel()
        shutil.rmtree(_live_root() / sid, ignore_errors=True)
