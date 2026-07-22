"""Live-scan full local E2E acceptance check (spec acceptance #1 proxy, backend).

Requires a running app server (see run_live_scan_e2e.sh, which starts one and
calls this). Flow: login (cookie) -> POST /scan/live/start -> POST each
tests/corpus/reel/*.png as a frame -> POST .../finish -> POST /pulls
(capture_path=live, live_session_id, cards=finish's cards JSON, + a composite
staircase and a code image, reusing a reel frame for both) -> then verifies,
via direct DB/filesystem checks, that:

  1. the pull row has capture_path == 'live'
  2. the pull row has derive_status == 'done'
  3. >=1 pull_card_derived row exists for it
  4. the live session's frames were moved into the pull dir
     (var/pulls/<trainer>/<pull>/frame_*.jpg)
  5. the live_sessions/<sid>/ dir is now gone or empty
  6. rederive_pending() does not pick up this pull — both the isolated
     capture_path!='live' filter clause, and a behavioral check that calling
     it leaves this pull's derive_status/derived_at untouched

Prints one PASS/FAIL line per assertion and exits non-zero on any failure.

Usage: see run_live_scan_e2e.sh in this directory.
"""
from __future__ import annotations

import asyncio
import glob
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

from app.db.models import Pull, PullCardDerived
from app.db.session import async_session_maker
from app.stats.rederive import rederive_pending

BASE = "http://127.0.0.1:8000"
REEL = sorted(glob.glob("tests/corpus/reel/*.png"))
PHOTO_ROOT = Path("var/pulls")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        r = await client.post(
            "/auth/cookie/login",
            data={"username": "tduser@x.io", "password": "trainerpass1"},
        )
        check("login 2xx", r.status_code // 100 == 2, f"status={r.status_code} body={r.text[:200]}")
        if r.status_code // 100 != 2:
            return 1

        r = await client.post("/scan/live/start")
        check("live/start 200", r.status_code == 200, f"status={r.status_code}")
        sid = r.json()["session_id"]
        print(f"session_id = {sid}")

        for path in REEL:
            data = Path(path).read_bytes()
            r = await client.post(
                f"/scan/live/{sid}/frame",
                files={"card": (Path(path).name, data, "image/png")},
            )
            check(f"frame POST {Path(path).name} 200", r.status_code == 200,
                  f"status={r.status_code} body={r.text[:200]}")
            print(f"  {Path(path).name} -> {r.json() if r.status_code == 200 else r.text}")

        r = await client.post(f"/scan/live/{sid}/finish")
        check("finish 200", r.status_code == 200, f"status={r.status_code} body={r.text[:300]}")
        finish_body = r.json()
        cards_json = json.dumps(finish_body["cards"])
        print(f"finish cards: {len(finish_body['cards'])} -> {finish_body['cards']}")

        composite_bytes = Path(REEL[1]).read_bytes()   # stands in for the composite staircase
        code_bytes = Path(REEL[0]).read_bytes()         # stands in for the code image
        r = await client.post(
            "/pulls",
            data={
                "capture_path": "live",
                "live_session_id": sid,
                "cards": cards_json,
                "pack_confidence": str(finish_body.get("pack_confidence", 0.0)),
            },
            files={
                "staircase": ("composite.jpg", composite_bytes, "image/jpeg"),
                "code_card": ("code.jpg", code_bytes, "image/jpeg"),
            },
        )
        check("POST /pulls 201", r.status_code == 201, f"status={r.status_code} body={r.text[:300]}")
        if r.status_code != 201:
            return 1
        pull_out = r.json()
        pull_id = pull_out["id"]
        print(f"pull_id = {pull_id} capture_path={pull_out['capture_path']} verified={pull_out['verified']}")

        r = await client.get("/users/me")
        check("GET /users/me 200", r.status_code == 200, f"status={r.status_code}")
        trainer_id = r.json()["id"]

    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        check("pull row exists", pull is not None, f"pull_id={pull_id}")
        if pull is None:
            return 1

        check("capture_path == 'live'", pull.capture_path == "live", f"actual={pull.capture_path!r}")
        status_str = pull.derive_status.value if hasattr(pull.derive_status, "value") else pull.derive_status
        check("derive_status == 'done'", status_str == "done", f"actual={pull.derive_status!r}")
        derived_at_before = pull.derived_at

        derived_rows = (
            await session.execute(select(PullCardDerived).where(PullCardDerived.pull_id == pull.id))
        ).scalars().all()
        check(">=1 pull_card_derived row", len(derived_rows) >= 1,
              f"count={len(derived_rows)} rows={[(d.row_index, d.name, d.card_number) for d in derived_rows]}")

    pull_dir = PHOTO_ROOT / trainer_id / pull_id
    frame_files = sorted(pull_dir.glob("frame_*.jpg")) if pull_dir.is_dir() else []
    check("pull dir exists", pull_dir.is_dir(), str(pull_dir))
    check(">=1 frame_*.jpg moved into pull dir", len(frame_files) >= 1,
          f"files={[f.name for f in frame_files]}")

    live_sess_dir = PHOTO_ROOT / "live_sessions" / sid
    gone_or_empty = (not live_sess_dir.exists()) or (not any(live_sess_dir.iterdir()))
    check("live_sessions/<sid>/ dir gone or empty", gone_or_empty,
          f"exists={live_sess_dir.exists()} contents={list(live_sess_dir.iterdir()) if live_sess_dir.exists() else None}")

    async with async_session_maker() as session:
        would_match = (
            await session.execute(
                select(Pull.id).where(Pull.id == pull.id, Pull.capture_path != "live")
            )
        ).scalar_one_or_none()
        check("rederive's capture_path!='live' clause excludes this pull",
              would_match is None, f"would_match={would_match} (pull.capture_path={pull.capture_path!r})")

    processed = await rederive_pending(limit=200)
    async with async_session_maker() as session:
        pull_after = await session.get(Pull, pull_id)
        status_after = pull_after.derive_status.value if hasattr(pull_after.derive_status, "value") \
            else pull_after.derive_status
        untouched = (pull_after.derived_at == derived_at_before) and (status_after == "done")
        check("rederive_pending() left this pull's derive_status/derived_at untouched",
              untouched, f"processed_count={processed} derive_status={pull_after.derive_status} "
                         f"derived_at_before={derived_at_before} derived_at_after={pull_after.derived_at}")

    print("\n=== SUMMARY ===")
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"{n_pass} passed, {n_fail} failed, {len(results)} total")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} -- {detail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
