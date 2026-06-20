"""Env-driven settings for the database/auth layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is required but not set")
    return v


def _asyncpg_url(raw: str) -> str:
    # Railway (and most hosts) provide postgresql:// or postgres://; asyncpg needs
    # the +asyncpg driver segment. Replace only the scheme prefix, once.
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw


@dataclass(frozen=True)
class DbSettings:
    database_url: str = field(default_factory=lambda: _asyncpg_url(_require("DATABASE_URL")))
    auth_secret: str = field(default_factory=lambda: _require("AUTH_SECRET"))
    photo_storage_dir: str = field(
        default_factory=lambda: os.environ.get("PHOTO_STORAGE_DIR", "").strip() or "./var/pulls"
    )
    cookie_secure: bool = field(
        default_factory=lambda: os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"
    )
    session_lifetime_seconds: int = 7 * 24 * 3600  # 7 days


def db_settings() -> DbSettings:
    """Fresh read each call so env changes (dev) take effect without reload."""
    return DbSettings()
