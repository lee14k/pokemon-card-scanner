"""Env-driven settings for the database/auth layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is required but not set")
    return v


def _require_secret(name: str, min_bytes: int = 32) -> str:
    # HS256 (RFC 7518 §3.2) needs a key >= 32 bytes; fail fast so a weak/placeholder
    # secret can never reach production rather than silently signing with a warning.
    v = _require(name)
    if len(v.encode()) < min_bytes:
        raise RuntimeError(
            f"{name} is {len(v.encode())} bytes; needs >= {min_bytes}. "
            'Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
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
    auth_secret: str = field(default_factory=lambda: _require_secret("AUTH_SECRET"))
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


def database_url() -> str:
    """Just the asyncpg DATABASE_URL — no AUTH_SECRET required. Used by the engine and
    Alembic so migrations/DB access don't depend on the auth secret being set."""
    return _asyncpg_url(_require("DATABASE_URL"))
