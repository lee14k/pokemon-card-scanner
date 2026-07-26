"""Stage timing instrumentation — one grep-able INFO line per measured stage.

Every line is exactly ``timing.<flow>.<stage> scan=<id> ms=<float>``, so a run's
latency profile falls out of the logs with a single grep:

    grep -o 'timing\\.[a-z_]*\\.[a-z_]* .* ms=[0-9.]*' server.log

Purely observational: ``stage`` never alters control flow (it logs on the way out
whether the body raised or not, and re-raises untouched). Stdlib only.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("pokemon_scanner.timing")


def new_scan_id() -> str:
    """Short correlation id (8 hex chars) tying one flow's stage lines together."""
    return secrets.token_hex(4)


@contextmanager
def stage(flow: str, name: str, scan_id: str | None = None) -> Iterator[None]:
    """Time the block and log ``timing.<flow>.<name> scan=<id> ms=<elapsed>``.

    Sync context manager, but safe around ``await`` (it only brackets enter/exit).
    Logs on exception too — the timing of a failed stage is exactly as interesting
    as a successful one — and never swallows it."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log.info("timing.%s.%s scan=%s ms=%.1f", flow, name,
                 scan_id or "-", (time.perf_counter() - t0) * 1000)
