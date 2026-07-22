"""Filesystem boundary for pull photos (Railway Volume in prod, local dir in dev).

Most paths are built only from server-generated UUIDs. The one exception is
``move_session_frames``, whose ``session_id`` arrives as a client-supplied
``live_session_id`` form field: it is validated as a uuid4 hex string and
containment-checked against the live-sessions root before use, so the
traversal surface is closed by validation rather than by absence of user
input.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from app.db.config import db_settings


def _root() -> Path:
    return Path(db_settings().photo_storage_dir)


def ensure_photo_dir() -> None:
    _root().mkdir(parents=True, exist_ok=True)


def _pull_dir(trainer_id: uuid.UUID, pull_id: uuid.UUID) -> Path:
    return _root() / str(trainer_id) / str(pull_id)


def save_pull_photos(
    trainer_id: uuid.UUID, pull_id: uuid.UUID, staircase: bytes, code: bytes
) -> tuple[str, str]:
    """Write both photos; return (staircase_path, code_path) relative to the storage root."""
    d = _pull_dir(trainer_id, pull_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "staircase.jpg").write_bytes(staircase)
    (d / "code.jpg").write_bytes(code)
    rel = Path(str(trainer_id)) / str(pull_id)
    return str(rel / "staircase.jpg"), str(rel / "code.jpg")


def save_code_photo(trainer_id: uuid.UUID, pull_id: uuid.UUID, code: bytes) -> str:
    """Overwrite the pull's stored code photo (used by the PATCH code-rescue path).
    Returns the storage-root-relative path."""
    d = _pull_dir(trainer_id, pull_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "code.jpg").write_bytes(code)
    return str(Path(str(trainer_id)) / str(pull_id) / "code.jpg")


def move_session_frames(
    session_id: str, trainer_id: uuid.UUID, pull_id: uuid.UUID
) -> int:
    """Move a live session's per-card frames into the saved pull's photo dir.

    Moves every ``live_sessions/<session_id>/frame_*.jpg`` into the pull dir as
    ``frame_NN.jpg`` (preserving numeric order). Returns the number moved. A live
    session can legitimately be gone by save time (TTL sweep), so a missing/empty
    source dir is non-fatal and returns 0.

    ``session_id`` is client-supplied (the caller's ``live_session_id`` form
    field), so it is validated as a uuid4 hex string and containment-checked
    against the live-sessions root before any filesystem access — otherwise
    it would be an arbitrary-path escape (absolute path or ``..`` traversal).
    """
    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        return 0
    base = (_root() / "live_sessions").resolve()
    src_dir = (base / session_id).resolve()
    if base != src_dir.parent:
        return 0
    if not src_dir.is_dir():
        return 0
    frames = [p for p in src_dir.iterdir() if re.fullmatch(r"frame_\d+\.jpg", p.name)]
    if not frames:
        return 0
    frames.sort(key=lambda p: int(re.search(r"\d+", p.name).group()))
    dest_dir = _pull_dir(trainer_id, pull_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in frames:
        shutil.move(str(p), str(dest_dir / p.name))
    return len(frames)


def open_photo(rel_path: str) -> bytes:
    """Read a stored photo by its root-relative path. Raises FileNotFoundError if missing."""
    # rel_path comes from the DB (we wrote it); reject anything escaping the root.
    full = (_root() / rel_path).resolve()
    if _root().resolve() not in full.parents and full != _root().resolve():
        raise FileNotFoundError(rel_path)
    if not full.is_file():  # also turns a dir/missing path into a clean 404
        raise FileNotFoundError(rel_path)
    return full.read_bytes()
