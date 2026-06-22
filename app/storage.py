"""Filesystem boundary for pull photos (Railway Volume in prod, local dir in dev).

Paths are built only from server-generated UUIDs — no user-controlled segments,
so there is no path-traversal surface.
"""

from __future__ import annotations

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


def open_photo(rel_path: str) -> bytes:
    """Read a stored photo by its root-relative path. Raises FileNotFoundError if missing."""
    # rel_path comes from the DB (we wrote it); reject anything escaping the root.
    full = (_root() / rel_path).resolve()
    if _root().resolve() not in full.parents and full != _root().resolve():
        raise FileNotFoundError(rel_path)
    if not full.is_file():  # also turns a dir/missing path into a clean 404
        raise FileNotFoundError(rel_path)
    return full.read_bytes()
