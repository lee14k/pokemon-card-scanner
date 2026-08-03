"""Stub-driven probe for Task 5: warm-up ping, per-kind prompt, binder hints.

Extends the Task-4 probe shape (in-process tests/vlm_stub.py + TestClient, whose
live event loop actually runs the background tasks) with the three Task-5 claims:

  A. WARM-UP  vlm_client.kick() fires ONE ping per debounce window across four
     scan entry points, the ping carries an empty `cards` list (which the worker's
     unconditional _load() answers by loading the model), and the window is
     honored / released as designed.
  B. KIND      the stub receives kind="strip" for a pack scan and
     kind="full_card" for a binder page — the flow's real image shape.
  C. HINTS     a binder page with >=2 confident same-set cells sends that set (and
     its unambiguous denominator) as hint_set/hint_denominator; a page without
     sends None. _page_modal is also exercised directly on hand-built cells.

  D. WORKER    runpod_worker.prompts.build_prompt (import-safe: no runpod/torch)
     is exercised with kind present/absent, and the strip prompt is asserted
     BYTE-IDENTICAL to the string the worker built before this change.

Not a pytest test (the suite must stay VLM-free); a one-shot script.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/task-5-probe.py
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
os.environ["VLM_API_KEY"] = "x"

import uvicorn  # noqa: E402

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL {msg}")


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


def poll(client, url, label, budget=60.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < budget:
        r = client.get(url)
        if r.status_code != 200:
            raise AssertionError(f"{label}: poll {r.status_code}")
        body = r.json()
        if body["done"]:
            return body
        time.sleep(0.3)
    raise AssertionError(f"{label}: follow-up never finished within {budget}s")


def identify_calls(reqs):
    """Stub calls that are real identify batches (non-empty cards)."""
    return [r for r in reqs if r["cards"]]


def warmup_calls(reqs):
    """Stub calls that are warm-up pings: empty `cards` (vlm_client.warmup)."""
    return [r for r in reqs if not r["cards"]]


# ── D. worker prompt builder (no GPU, no runpod/torch import) ────────────────
def probe_prompts() -> None:
    print("\n=== D. runpod_worker.prompts.build_prompt (import-safe) ===")
    from runpod_worker.prompts import build_prompt

    # The EXACT string handler.py built before this change, reconstructed from the
    # pre-change literal (`_PROMPT.format(hint=...)`, so {{ }} collapse to { }).
    OLD = (
        "This image is the bottom strip of a Pokemon trading card. Read the collector "
        "number exactly as printed (formats like 126/167, 12/198, or TG12/TG30). "
        "If the set symbol or name is legible, identify the set. "
        "If the card's printed name is legible, read it. "
        'Reply with ONLY a JSON object: '
        '{"number": "<numerator>", "denominator": "<denominator or null>", '
        '"set_name": "<set or null>", "name": "<the card\'s printed name or null>", '
        '"confidence": <0..1>}. '
    )
    OLD_HINTED = OLD + "Context: this pack is likely the set 'Twilight Masquerade'. " \
                       "denominator 167. "

    strip = build_prompt("strip")
    absent = build_prompt(None)
    unknown = build_prompt("something_new")
    full = build_prompt("full_card")

    print(f"  kind=strip      : {strip[:78]}...")
    print(f"  kind=full_card  : {full[:78]}...")
    print(f"  kind absent == strip prompt      : {absent == strip}")
    print(f"  unknown kind == strip prompt     : {unknown == strip}")
    print(f"  strip == pre-change worker string : {strip == OLD}")
    print(f"  strip+hints == pre-change hinted  : "
          f"{build_prompt('strip', 'Twilight Masquerade', '167') == OLD_HINTED}")
    check(absent == strip, "build_prompt(None) != the strip prompt (old app compat)")
    check(unknown == strip, "an unknown kind did not fall back to the strip prompt")
    check(strip == OLD, "the strip prompt is no longer byte-identical to today's")
    check(build_prompt("strip", "Twilight Masquerade", "167") == OLD_HINTED,
          "the hinted strip prompt changed")
    check("whole Pokemon trading card" in full,
          "the full_card prompt does not describe a whole card")
    check("bottom strip of a Pokemon" not in full,
          "the full_card prompt still claims the image is a bottom strip")
    for frag in ("126/167", "TG12/TG30", "SWSH123", "MEP 037"):
        check(frag in full, f"the full_card prompt lost the {frag!r} number format")
    check("printed name" in full, "the full_card prompt does not ask for the name")
    check("identify the set" in full, "the full_card prompt does not ask for the set")
    # Same reply contract for every kind (worker output shape is kind-independent).
    reply = 'Reply with ONLY a JSON object:'
    check(reply in strip and reply in full, "a kind lost the JSON reply contract")
    hinted_full = build_prompt("full_card", "Twilight Masquerade", "167")
    print(f"  full_card hint tail: ...{hinted_full[-88:]}")
    check("this card is likely from" in hinted_full,
          "the full_card hint still calls the image a pack")


# ── C(2). _page_modal, exercised directly on hand-built cells ───────────────
def probe_page_modal() -> None:
    print("\n=== C1. binder._page_modal (direct, hand-built cells) ===")
    from app.pack.binder import BinderCell, _page_modal
    from app.schemas import PackCard

    def cell(row, set_id, confident):
        return BinderCell(
            cell=(0, 0, 1, 1),
            card=PackCard(row_index=row, set_id=set_id,
                          confidence=0.9 if confident else 0.3,
                          needs_review=not confident),
            thumb_b64=None, needs_vlm=not confident)

    # set_id is the denominator-table id space (PokéWallet ids, self-mapped for
    # me-era sets) — that IS what resolve_identity puts on a binder cell:
    #   '23473' = Twilight Masquerade (denominators ('167',))
    #   '3170'  = Silver Tempest      (denominators ('195',))
    TWM, SIT = "23473", "3170"
    cases = [
        ("empty page", [], (None, None)),
        ("one confident cell (below the >=2 bar)",
         [cell(0, TWM, True), cell(1, TWM, False)], (None, None)),
        ("two confident cells, same set",
         [cell(0, TWM, True), cell(1, TWM, True), cell(2, None, False)],
         ("Twilight Masquerade", "167")),
        ("two confident cells, DIFFERENT sets (no set reaches 2)",
         [cell(0, TWM, True), cell(1, SIT, True)], (None, None)),
        ("flagged cells only (their sets must not vote)",
         [cell(0, TWM, False), cell(1, TWM, False)], (None, None)),
        ("modal wins over a minority set",
         [cell(0, TWM, True), cell(1, TWM, True), cell(2, SIT, True)],
         ("Twilight Masquerade", "167")),
        ("unknown set_id (not in the denominator table)",
         [cell(0, "zz99", True), cell(1, "zz99", True)], (None, None)),
        # STRICT maximum: a tie is not a modal set. Both orderings, because the
        # bug this replaces was Counter insertion order picking a winner.
        ("2-2 TIE (must not pick an insertion-order winner)",
         [cell(0, TWM, True), cell(1, TWM, True),
          cell(2, SIT, True), cell(3, SIT, True)], (None, None)),
        ("2-2 TIE, other insertion order",
         [cell(0, SIT, True), cell(1, SIT, True),
          cell(2, TWM, True), cell(3, TWM, True)], (None, None)),
        ("the synthetic 3x3 page's real vote (me05 x2, me02.5 x2, 3 singletons)",
         [cell(0, "23473", True), cell(1, "me05", True), cell(2, "me05", True),
          cell(3, "me02.5", True), cell(4, "me02.5", True), cell(5, "3170", True),
          cell(6, "me01", True)], (None, None)),
        ("a strict 3-2 majority still hints",
         [cell(0, TWM, True), cell(1, TWM, True), cell(2, TWM, True),
          cell(3, SIT, True), cell(4, SIT, True)],
         ("Twilight Masquerade", "167")),
    ]
    for label, cells, expected in cases:
        p = _page_modal(cells)
        got = (p.set_name, p.denominator) if p else (None, None)
        print(f"  {label:52s} -> {got}")
        check(got == expected, f"_page_modal {label}: expected {expected}, got {got}")

    # A set whose catalog row has no single denominator must offer none (pack rule).
    from app.pack.set_resolution import load_denominator_table
    for want, label in ((lambda n: n > 1, "multi-denominator"),
                        (lambda n: n == 0, "no recorded denominator")):
        s = next((s for s in load_denominator_table().sets
                  if want(len(s.denominators))), None)
        if s is None:
            print(f"  SKIP no {label} set in the table")
            continue
        p = _page_modal([cell(0, s.set_id, True), cell(1, s.set_id, True)])
        print(f"  {label} set {s.set_id} ({s.denominators}) -> "
              f"set={p and p.set_name!r} den={p and p.denominator!r}")
        check(p is not None and p.set_name == s.set_name and p.denominator is None,
              f"_page_modal offered a denominator for {label} set {s.set_id}")

    # Purity: the helper must not touch the cells it is handed.
    cells = [cell(0, TWM, True), cell(1, TWM, True)]
    before = [c.card.model_dump() for c in cells] + [c.needs_vlm for c in cells]
    _page_modal(cells)
    after = [c.card.model_dump() for c in cells] + [c.needs_vlm for c in cells]
    print(f"  cells unchanged after the call: {before == after}")
    check(before == after, "_page_modal mutated the cells it was given")
    p1, p2 = _page_modal(cells), _page_modal(cells)
    check((p1.set_id, p1.set_name, p1.denominator) == (p2.set_id, p2.set_name,
                                                       p2.denominator),
          "_page_modal is not deterministic on the same input")


# ── A. warm-up debounce, in isolation ───────────────────────────────────────
def probe_warmup_unit(endpoint: str) -> None:
    print("\n=== A1. vlm_client.warmup debounce (direct) ===")
    import asyncio

    from app.pack import vlm_client
    from tests.vlm_stub import REQUESTS

    async def run() -> None:
        vlm_client._last_warmup = None
        REQUESTS.clear()
        os.environ["VLM_ENDPOINT"] = endpoint
        await asyncio.gather(*(vlm_client.warmup() for _ in range(5)))
        n = len(warmup_calls(REQUESTS))
        print(f"  5 concurrent warmup() calls -> {n} ping(s) at the worker")
        check(n == 1, f"5 concurrent warmups sent {n} pings, expected 1")
        await vlm_client.warmup()
        n2 = len(warmup_calls(REQUESTS))
        print(f"  + a 6th call inside the {vlm_client.WARMUP_INTERVAL_S:.0f}s window "
              f"-> {n2} ping(s) total")
        check(n2 == 1, f"a call inside the debounce window sent a {n2}th ping")

        # Window expiry: rewind the timestamp past the interval.
        vlm_client._last_warmup -= vlm_client.WARMUP_INTERVAL_S + 1
        await vlm_client.warmup()
        n3 = len(warmup_calls(REQUESTS))
        print(f"  + a call after the window elapsed -> {n3} ping(s) total")
        check(n3 == 2, f"a call after the window sent {n3} pings, expected 2")
        payload = warmup_calls(REQUESTS)[0]
        print(f"  ping payload cards={payload['cards']}  (worker _load() runs "
              f"unconditionally, so an empty batch warms the model)")
        check(payload["cards"] == [], "the warm-up ping was not an empty card batch")

        # Disabled endpoint: no ping, and the debounce state is not consumed.
        os.environ.pop("VLM_ENDPOINT")
        vlm_client._last_warmup = None
        await vlm_client.warmup()
        vlm_client.kick()
        n4 = len(warmup_calls(REQUESTS))
        print(f"  VLM_ENDPOINT unset: warmup()+kick() -> {n4} ping(s) total "
              f"(unchanged), _last_warmup={vlm_client._last_warmup}")
        check(n4 == 2, "a disabled endpoint still sent a warm-up ping")
        check(vlm_client._last_warmup is None,
              "a disabled warmup() consumed the debounce window")

        # A broken endpoint must be swallowed, not raised.
        os.environ["VLM_ENDPOINT"] = f"http://127.0.0.1:{free_port()}/v2/dead"
        vlm_client._last_warmup = None
        vlm_client.WARMUP_TIMEOUT_S, saved = 2.0, vlm_client.WARMUP_TIMEOUT_S
        try:
            await vlm_client.warmup()
            print("  unreachable endpoint: warmup() returned normally (error logged)")
        except Exception as e:
            check(False, f"warmup() raised on an unreachable endpoint: {e!r}")
        finally:
            vlm_client.WARMUP_TIMEOUT_S = saved

        # A 4xx must NOT read as "warmup ok status=401": this ping is the only
        # thing that validates VLM_ENDPOINT/VLM_API_KEY before a card needs them.
        import logging
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, rec): records.append(rec)

        cap = _Cap()
        logging.getLogger("pokemon_scanner.pack.vlm").addHandler(cap)
        try:
            # The stub serves /v2/{id}/runsync; an extra path segment 404s.
            os.environ["VLM_ENDPOINT"] = endpoint + "/nope"
            vlm_client._last_warmup = None
            await vlm_client.warmup()
        finally:
            logging.getLogger("pokemon_scanner.pack.vlm").removeHandler(cap)
        rejected = [r for r in records if r.getMessage().startswith("vlm.warmup_rejected")]
        oks = [r for r in records if r.getMessage().startswith("vlm.warmup ok")]
        for r in records:
            print(f"  4xx ping logged: {logging.getLevelName(r.levelno)} "
                  f"{r.getMessage()}")
        check(len(rejected) == 1 and rejected[0].levelno >= logging.ERROR,
              "a >=400 warm-up response did not log an error-level rejection")
        check(not oks, "a >=400 warm-up response was logged as 'ok'")
        os.environ["VLM_ENDPOINT"] = endpoint

    asyncio.run(run())


def main() -> int:
    endpoint = start_stub()
    print(f"vlm stub at {endpoint}")

    probe_prompts()
    probe_warmup_unit(endpoint)
    probe_page_modal()

    os.environ["VLM_ENDPOINT"] = endpoint
    from fastapi.testclient import TestClient

    import app.main as main_mod
    from app.db.users import current_active_user
    from app.pack import vlm_client
    from tests.vlm_stub import REQUESTS

    class _T:
        id = uuid.uuid4()
    main_mod.app.dependency_overrides[current_active_user] = lambda: _T()

    with TestClient(main_mod.app) as client:
        # ── A2. one ping across four warming entry points ────────────────────
        print("\n=== A2. warm-up from the real endpoints (one per window) ===")
        REQUESTS.clear()
        vlm_client._last_warmup = None
        page = (REPO / "tests" / "corpus" / "binder" / "synthetic_3x3.jpg").read_bytes()

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
        pack_files = {"staircase": ("s.jpg", stair, "image/jpeg"),
                      "code_card": ("c.jpg", code, "image/jpeg")}

        # A REJECTED unauthenticated pack upload must not spend a billable GPU
        # ping: /scan/pack and /scan/pack/stream kick only after validation.
        for path in ("/scan/pack", "/scan/pack/stream"):
            r = client.post(path, files={
                "staircase": ("s.txt", b"not-an-image", "text/plain"),
                "code_card": ("c.jpg", b"", "image/jpeg")})
            print(f"  POST {path:22s} (junk upload) -> {r.status_code}")
            check(r.status_code >= 400,
                  f"{path} accepted a non-image upload ({r.status_code})")
        time.sleep(0.3)
        check(not warmup_calls(REQUESTS),
              f"a rejected unauthenticated upload fired {len(warmup_calls(REQUESTS))} "
              f"billable warm-up ping(s)")
        print(f"  warm-up pings from rejected uploads: {len(warmup_calls(REQUESTS))}")

        r = client.post("/scan/live/start")
        print(f"  POST /scan/live/start          -> {r.status_code}")
        check(r.status_code == 200, f"/scan/live/start returned {r.status_code}")
        binder_first = client.post(
            "/scan/binder", files={"page": ("page.jpg", page, "image/jpeg")})
        check(binder_first.status_code == 200, "POST /scan/binder failed")
        print(f"  POST /scan/binder              -> {binder_first.status_code}")
        r = client.post("/scan/pack", files=pack_files,
                        data={"capture_meta": json.dumps(pack_meta)})
        pack_body = r.json()
        print(f"  POST /scan/pack                -> {r.status_code}")
        r = client.post("/scan/pack/stream", files=pack_files,
                        data={"capture_meta": json.dumps(pack_meta)})
        print(f"  POST /scan/pack/stream         -> {r.status_code}")

        # Give the anchored warm-up task room to land (it is off the response path).
        deadline = time.time() + 15
        while not warmup_calls(REQUESTS) and time.time() < deadline:
            client.get("/health")
            time.sleep(0.2)
        pings = warmup_calls(REQUESTS)
        print(f"  warm-up pings at the worker after 4 warming requests: {len(pings)} "
              f"(payload {pings[0]['cards'] if pings else '-'})")
        check(len(pings) == 1,
              f"4 scan entry points produced {len(pings)} pings, expected 1")

        # ── B/C. kind + hints per flow ──────────────────────────────────────
        print("\n=== B/C. per-card kind + binder hints at the worker ===")
        binder_sid = binder_first.json().get("scan_id")
        check(bool(binder_sid), "binder response carried no scan_id")
        if binder_sid:
            poll(client, f"/scan/binder/{binder_sid}", "binder")
        pack_sid = pack_body.get("scan_id")
        check(bool(pack_sid), "pack response carried no scan_id")
        if pack_sid:
            poll(client, f"/scan/pack/{pack_sid}", "pack")

        batches = identify_calls(REQUESTS)
        for b in batches:
            print(f"  batch cards={len(b['cards'])}: "
                  + ", ".join(f"row={c['row_index']} kind={c['kind']} "
                              f"hint_set={c['hint_set']!r} "
                              f"hint_den={c['hint_denominator']!r}"
                              for c in b["cards"]))
        kinds = {c["kind"] for b in batches for c in b["cards"]}
        print(f"  kinds seen across all batches: {sorted(kinds)}")
        check(kinds == {"strip", "full_card"},
              f"expected both kinds across pack+binder, saw {sorted(kinds)}")

        full = [c for b in batches for c in b["cards"] if c["kind"] == "full_card"]
        strips = [c for b in batches for c in b["cards"] if c["kind"] == "strip"]
        check(bool(full), "the binder page sent no full_card cards")
        check(bool(strips), "the pack scan sent no strip cards")

        # The optional SECOND image is half of the kind contract, so it is
        # asserted in both directions: a binder cell must carry a magnified
        # bottom strip (the worker's prompt promises the model one), and a pack
        # strip must NOT — that card already IS the strip, and a second copy
        # would be paid-for tokens pointing at nothing new.
        print(f"  binder (full_card) strip_bytes: "
              f"{sorted({c['strip_bytes'] > 0 for c in full})} "
              f"min={min((c['strip_bytes'] for c in full), default=0)}")
        check(all(c["strip_bytes"] > 0 for c in full),
              "a full_card binder cell reached the worker with no strip_b64 — "
              "the prompt tells the model to read the number from a second "
              "image that was never sent")
        print(f"  pack (strip) strip_bytes: "
              f"{sorted({c['strip_bytes'] for c in strips})}")
        check(all(c["strip_bytes"] == 0 for c in strips),
              "a pack strip card carried a redundant second image")
        # The synthetic page's 7 confident cells are me05 x2, me02.5 x2 and three
        # singletons — a 2-2 TIE, so the page has no dominant set and must send NO
        # hint. (Before the strict-maximum rule this shipped 'Pitch Black'/'84',
        # which is the set of NEITHER flagged cell.) The positive hint case is
        # covered by the hand-built cells in C1.
        print(f"  binder (full_card) hints: "
              f"{sorted({(c['hint_set'], c['hint_denominator']) for c in full})}")
        check(all(c["hint_set"] is None and c["hint_denominator"] is None
                  for c in full),
              "the synthetic page's 2-2 tie still produced a hint — the modal set "
              "must be a STRICT maximum")
        print(f"  pack (strip) hints: "
              f"{sorted({(c['hint_set'], c['hint_denominator']) for c in strips})}")

        # ── C3. a page with no >=2-supporter modal set sends no hints ────────
        print("\n=== C3. a page WITHOUT a modal set sends hint_set=None ===")
        real = REPO / "tests" / "corpus" / "binder" / "real" / "page_4.jpeg"
        if real.exists():
            REQUESTS.clear()
            r = client.post("/scan/binder",
                            files={"page": ("page.jpg", real.read_bytes(),
                                            "image/jpeg")})
            body = r.json()
            confident = [c for c in body["cards"] if not c.get("needs_review")]
            print(f"  {real.name}: cells={len(body['cards'])} "
                  f"confident={len(confident)} (no set can reach 2 supporters)")
            sid = body.get("scan_id")
            if sid:
                poll(client, f"/scan/binder/{sid}", "page_4")
            cards = [c for b in identify_calls(REQUESTS) for c in b["cards"]]
            print(f"  flagged cells sent={len(cards)}  hints="
                  f"{sorted({(c['hint_set'], c['hint_denominator']) for c in cards})}")
            print(f"  kinds={sorted({c['kind'] for c in cards})}")
            check(bool(cards), "page_4 sent no cards to the VLM")
            check(all(c["hint_set"] is None and c["hint_denominator"] is None
                      for c in cards),
                  "a page with no >=2-supporter modal set still sent a hint")
            check(all(c["kind"] == "full_card" for c in cards),
                  "a binder cell went out with a kind other than full_card")
        else:
            print(f"  SKIP {real} not present")

    print("\n=== result ===")
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  ALL PROBE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
