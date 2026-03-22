"""Logging setup for OCR / search diagnostics (Railway captures stdout)."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    raw = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, raw, logging.INFO)
    fmt = "%(levelname)s [%(name)s] %(message)s"
    log = logging.getLogger("pokemon_scanner")
    log.setLevel(level)
    log.propagate = False
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt))
        log.addHandler(handler)


def preview_text(s: str, limit: int = 800) -> str:
    """Single-line safe preview for logs."""
    t = (s or "").replace("\r", "").replace("\n", " \\n ")
    if len(t) <= limit:
        return t
    return t[: limit - 3] + "..."
