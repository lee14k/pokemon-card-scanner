"""Stub-driven end-to-end probe for Task 4: is the VLM really off the response path?

Runs tests/vlm_stub.py with VLM_STUB_DELAY_S=5, then drives the REAL HTTP
endpoints through TestClient (which keeps a live event loop, so the background
drain task actually runs) for both the binder and the pack flow:

  1. POST the scan -> must return in ~scan-time, NOT scan-time + 5s,
     carrying a top-level scan_id and state="pending_vlm" on the flagged rows.
  2. GET the follow-up -> any_pending true at first, then patched cards + done.

Not a pytest test (the suite must stay VLM-free); a one-shot script.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/task-4-probe.py
"""
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://pcs:pcs@localhost:5432/pcs")
os.environ.setdefault("AUTH_SECRET", "dev-secret-not-for-prod-pad-0123456789")
os.environ.setdefault("PHOTO_STORAGE_DIR", "./var/pulls")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ["VLM_STUB_DELAY_S"] = "5"
os.environ["VLM_API_KEY"] = "x"

import uvicorn  # noqa: E402

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_stub() -> str:
    from tests.vlm_stub import app as stub
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(stub, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("vlm stub did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/v2/test"


def poll(client, url, label, budget=40.0):
    t0 = time.perf_counter()
    first = None
    while time.perf_counter() - t0 < budget:
        r = client.get(url)
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        if first is None:
            first = body
            print(f"  {label} first poll: any_pending={body['any_pending']} "
                  f"done={body['done']} at t+{(time.perf_counter()-t0)*1000:.0f}ms")
        if body["done"]:
            print(f"  {label} done at t+{(time.perf_counter()-t0):.1f}s "
                  f"any_pending={body['any_pending']}")
            return first, body
        time.sleep(0.5)
    raise AssertionError(f"{label}: follow-up never finished within {budget}s")


def main() -> int:
    endpoint = start_stub()
    os.environ["VLM_ENDPOINT"] = endpoint
    print(f"vlm stub at {endpoint} (delay 5s)\n")

    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.db.users import current_active_user

    # Auth bypass: /scan/binder only needs *a* trainer, and registering one would
    # drag user-table state into a latency probe.
    class _T:
        id = uuid.uuid4()
    main_mod.app.dependency_overrides[current_active_user] = lambda: _T()

    failures = []
    with TestClient(main_mod.app) as client:
        # ── binder ───────────────────────────────────────────────────────────
        print("=== POST /scan/binder (synthetic_3x3.jpg) ===")
        page = (REPO / "tests" / "corpus" / "binder" / "synthetic_3x3.jpg").read_bytes()
        t0 = time.perf_counter()
        r = client.post("/scan/binder",
                        files={"page": ("page.jpg", page, "image/jpeg")})
        binder_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body.get("scan_id")
        states = [c.get("state") for c in body["cards"]]
        print(f"  response in {binder_ms:.0f}ms  scan_id={sid}")
        print(f"  states={states}")
        if not sid:
            failures.append("binder: no scan_id in response")
        if "pending_vlm" not in states:
            failures.append("binder: no pending_vlm state in response")
        if binder_ms >= 5000:
            failures.append(f"binder: response took {binder_ms:.0f}ms >= the 5s "
                            f"stub delay — the VLM is still on the response path")
        before = {c["row_index"]: c.get("card_number") for c in body["cards"]}
        first, final = poll(client, f"/scan/binder/{sid}", "binder")
        if not first["any_pending"]:
            failures.append("binder: first poll already had nothing pending")
        after = {c["row_index"]: c.get("card_number") for c in final["cards"]}
        changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        print(f"  patched card_numbers: {changed}")
        print(f"  terminal states: {[c.get('state') for c in final['cards']]}")
        if final["any_pending"]:
            failures.append("binder: a card stayed pending_vlm after done")
        if not changed:
            failures.append("binder: no card was patched by the follow-up")
        if client.get(f"/scan/pack/{sid}").status_code != 404:
            failures.append("binder: its scan_id resolved through the PACK route")
        if client.get("/scan/binder/" + "0" * 32).status_code != 404:
            failures.append("binder: unknown scan_id did not 404")

        # ── pack ─────────────────────────────────────────────────────────────
        # The committed e2e staircase reads 3/3 cleanly, so it never flags and
        # never needs a follow-up. Use the same blurred synthetic staircase
        # test_blurry_row_is_flagged_not_dropped builds, which flags row 1.
        print("\n=== POST /scan/pack (synthetic staircase, row 1 blurred) ===")
        import tempfile
        from pathlib import Path

        from tests.fixtures.synth import make_code_card, make_staircase
        tmp = Path(tempfile.mkdtemp())
        pack_meta = make_staircase(
            [("012/202", ""), ("045/185", ""), ("101/198", "SVI")],
            tmp / "stair.jpg", blur_rows={1})
        make_code_card("TEST1-CODE2-CARD3", tmp / "code.jpg")
        stair = (tmp / "stair.jpg").read_bytes()
        code = (tmp / "code.jpg").read_bytes()
        t0 = time.perf_counter()
        r = client.post("/scan/pack",
                        files={"staircase": ("s.jpg", stair, "image/jpeg"),
                               "code_card": ("c.jpg", code, "image/jpeg")},
                        data={"capture_meta": json.dumps(pack_meta)})
        pack_ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, r.text
        body = r.json()
        sid = body.get("scan_id")
        states = [c.get("state") for c in body["cards"]]
        print(f"  response in {pack_ms:.0f}ms  scan_id={sid}")
        print(f"  states={states}")
        if not sid:
            failures.append("pack: no scan_id in response")
        elif "pending_vlm" not in states:
            failures.append("pack: no pending_vlm state in response")
        if pack_ms >= 5000:
            failures.append(f"pack: response took {pack_ms:.0f}ms >= the 5s stub "
                            f"delay — the VLM is still on the response path")
        if sid:
            before = {c["row_index"]: c.get("card_number") for c in body["cards"]}
            first, final = poll(client, f"/scan/pack/{sid}", "pack")
            after = {c["row_index"]: c.get("card_number") for c in final["cards"]}
            changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            print(f"  patched card_numbers: {changed}")
            print(f"  terminal states: {[c.get('state') for c in final['cards']]}")
            if final["any_pending"]:
                failures.append("pack: a card stayed pending_vlm after done")
            if client.get(f"/scan/binder/{sid}").status_code != 404:
                failures.append("pack: its scan_id resolved through the BINDER route")

        # ── SSE variant: same non-blocking behavior, scan_id on `result` ─────
        print("\n=== POST /scan/pack/stream ===")
        t0 = time.perf_counter()
        r = client.post("/scan/pack/stream",
                        files={"staircase": ("s.jpg", stair, "image/jpeg"),
                               "code_card": ("c.jpg", code, "image/jpeg")},
                        data={"capture_meta": json.dumps(pack_meta)})
        sse_ms = (time.perf_counter() - t0) * 1000
        frames = [f for f in r.text.split("\n\n") if f.strip()]
        events = [f.split("\n")[0] for f in frames if f.startswith("event:")]
        result = next(f for f in frames if f.startswith("event: result"))
        payload = json.loads(result.split("data: ", 1)[1])
        print(f"  stream complete in {sse_ms:.0f}ms  events={events}")
        print(f"  result scan_id={payload.get('scan_id')}  "
              f"states={[c.get('state') for c in payload['cards']]}")
        if sse_ms >= 5000:
            failures.append(f"stream: took {sse_ms:.0f}ms >= the 5s stub delay")
        if not payload.get("scan_id"):
            failures.append("stream: result frame carried no scan_id")
        if payload.get("scan_id"):
            poll(client, f"/scan/pack/{payload['scan_id']}", "stream")

        # ── VLM off: no scan_id, no states ───────────────────────────────────
        print("\n=== VLM disabled: response must be unchanged ===")
        os.environ.pop("VLM_ENDPOINT")
        r = client.post("/scan/binder",
                        files={"page": ("page.jpg", page, "image/jpeg")})
        body = r.json()
        keys = sorted(body.keys())
        card_keys = sorted(body["cards"][0].keys())
        print(f"  binder top-level keys: {keys}")
        print(f"  binder card keys: {card_keys}")
        if "scan_id" in keys or "state" in card_keys:
            failures.append("VLM off: binder response leaked scan_id/state")
        r = client.post("/scan/pack",
                        files={"staircase": ("s.jpg", stair, "image/jpeg"),
                               "code_card": ("c.jpg", code, "image/jpeg")},
                        data={"capture_meta": json.dumps(pack_meta)})
        body = r.json()
        print(f"  pack top-level keys: {sorted(body.keys())}")
        print(f"  pack card keys: {sorted(body['cards'][0].keys())}")
        if "scan_id" in body:
            failures.append("VLM off: pack response carried a scan_id key")
        if any("state" in c for c in body["cards"]):
            failures.append("VLM off: pack cards carried a state")

    print("\n=== result ===")
    for f in failures:
        print(f"  FAIL {f}")
    if failures:
        return 1
    print("  ALL PROBE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
