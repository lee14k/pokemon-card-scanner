"""Task 8 probe — eager init at lifespan, the RapidOCR init race, OCR-gate scoping.

Not a test (the suite stays as it is); a measurement harness. Every section is a
subcommand so "before" numbers can be taken with the change stashed:

    export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \
      AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \
      PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false

    P=docs/acceptance/probes/task-8-probe.py

    .venv/bin/python $P stall
    .venv/bin/python $P firstscan cold
    .venv/bin/python $P firstscan warm
    .venv/bin/python $P lock
    .venv/bin/python $P health           (lifespan + /health while warm-up runs)
    .venv/bin/python $P audit            (who calls the OCR gate, and with what)
    .venv/bin/python $P gate             (pack scan, who holds a slot)
    .venv/bin/python $P contention       (binder page + live frames)

Each subcommand runs in its OWN process on purpose: everything measured here is a
first-use cost, so a warm import would erase what is being measured.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo
PAGE = REPO / "tests" / "corpus" / "binder" / "real" / "page_3.jpeg"
STAIR = REPO / "tests" / "fixtures" / "e2e" / "staircase.jpg"
STAIR12 = REPO / "tests" / "corpus" / "IMG_7102.heic"     # real 12MP, 9 cards
CODE = REPO / "tests" / "fixtures" / "e2e" / "code.jpg"
REEL = REPO / "tests" / "corpus" / "reel" / "steady_3.png"


# --- log capture --------------------------------------------------------------
class Collect(logging.Handler):
    """Keep every ``timing.*`` / ``warm.*`` line so a section can print exactly the
    lines it cares about instead of the whole scanner's INFO stream."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass

    def matching(self, *prefixes) -> list[str]:
        return [l for l in self.lines if l.startswith(prefixes)]


def _capture() -> Collect:
    h = Collect()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    logging.getLogger("pokemon_scanner").setLevel(logging.INFO)
    return h


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


# --- 1. the stall, component by component -------------------------------------
async def section_stall() -> None:
    """Reproduce the first-scan `pack.constraints` stall Task 3 measured, split
    into the pieces `_apply_constraints`/`_needs_review` actually pay for."""
    print("== cold-start cost inside _apply_constraints (fresh process) ==")
    total = 0.0

    t0 = time.perf_counter()
    from app.pack.constraints import (correct_numerators, modal_denominator,  # noqa: F401
                                      snap_denominators)
    a = ms(t0); total += a
    print(f"  import app.pack.constraints (deferred)            {a:8.1f} ms")

    t0 = time.perf_counter()
    from app.cards import get_set_numerators, normalize_local_id  # noqa: F401
    b = ms(t0); total += b
    print(f"  import app.cards (deferred, pulls the DB layer)    {b:8.1f} ms")

    t0 = time.perf_counter()
    nums = await get_set_numerators("swshp")
    c = ms(t0); total += c
    print(f"  first get_set_numerators (engine+connect+query)    {c:8.1f} ms  n={len(nums)}")

    t0 = time.perf_counter()
    nums2 = await get_set_numerators("swshp")
    d = ms(t0)
    print(f"  second get_set_numerators (warm)                   {d:8.1f} ms  n={len(nums2)}")
    print(f"  {'-' * 54}")
    print(f"  COLD TOTAL (what scan #1 pays)                    {total:8.1f} ms")
    print(f"  WARM TOTAL (what scan #2 pays)                    {d:8.1f} ms")


# --- 2. first scan, warm vs cold ----------------------------------------------
async def section_firstscan(mode: str, which: str = "binder") -> None:
    """First scan of a fresh process, with and without the startup warm task.
    `warm` awaits app.main._warm_start() first (the lifespan runs it in the
    background; awaiting it here is 'the warm task completed'); `cold` is the
    control and simply never runs it.

    ONE scan per process, chosen by ``which`` — running both in one process would
    let the first scan warm the second and hide exactly what is being measured."""
    cap = _capture()
    print(f"== first scan in a fresh process: mode={mode} target={which} ==")

    if mode == "warm":
        from app.main import _warm_start

        t0 = time.perf_counter()
        await _warm_start()
        warm_ms = ms(t0)
        for line in cap.matching("timing.warm.", "warm."):
            print(f"  {line}")
        print(f"  warm task total                                   {warm_ms:8.1f} ms")
    else:
        print("  (control: no warm task)")

    mark = len(cap.lines)
    if which == "binder":
        from app.pack.binder import scan_binder_page

        t0 = time.perf_counter()
        page = await scan_binder_page(PAGE.read_bytes())
        wall = ms(t0)
        head, prefix, n = "binder page_3.jpeg", "timing.binder.", len(page["cards"])
    else:
        from app.pack.pipeline import scan_pack

        t0 = time.perf_counter()
        resp = await scan_pack(STAIR.read_bytes(), CODE.read_bytes(), None)
        wall = ms(t0)
        head, prefix, n = "pack staircase.jpg", "timing.pack.", len(resp.cards)

    print(f"\n  -- {head}, FIRST scan of the process --")
    for line in cap.lines[mark:]:
        if line.startswith(prefix):
            print(f"     {line}")
    print(f"     cards/cells={n}  FIRST-SCAN WALL {wall:10.1f} ms")


# --- 3. the init race and the lock --------------------------------------------
def section_lock() -> None:
    """(a) The real _get() under a concurrent burst: one construction, every
    thread gets the engine. (b) The old algorithm vs the new one, side by side on
    the same fake slow builder, so the race itself is visible rather than inferred."""
    cap = _capture()
    from app.pack import rapidocr_reader as rr

    n = 8
    start = threading.Barrier(n)
    got: list = [None] * n

    def worker(i: int) -> None:
        start.wait()
        got[i] = rr._get()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = ms(t0)

    builds = len([l for l in cap.lines if l.startswith("rapidocr.loaded")])
    nones = sum(1 for g in got if g is None)
    same = len({id(g) for g in got})
    print("== (a) real rapidocr_reader._get(), 8 threads from a barrier ==")
    print(f"   constructions logged (rapidocr.loaded) : {builds}")
    print(f"   threads that got None                  : {nones}")
    print(f"   distinct engine objects returned       : {same}")
    print(f"   wall for the burst                     : {wall:.1f} ms")
    print(f"   VERDICT: {'PASS' if builds == 1 and nones == 0 and same == 1 else 'FAIL'}")

    # (b) the two algorithms on an identical, deliberately slow builder.
    print("\n== (b) old vs new init algorithm, same 200ms builder, 8 threads ==")
    for label, impl in (("OLD (flag set before build)", "old"),
                        ("NEW (lock, flag after build)", "new")):
        state = {"engine": None, "ready": False, "failed": False, "builds": 0}
        lock = threading.Lock()

        def build():
            time.sleep(0.2)          # stands in for the ONNX model load
            state["builds"] += 1
            return object()

        def old_get():
            if state["ready"]:
                return state["engine"]
            state["ready"] = True    # <- the bug: published before the engine exists
            state["engine"] = build()
            return state["engine"]

        def new_get():
            if state["ready"] or state["failed"]:
                return state["engine"]
            with lock:
                if state["ready"] or state["failed"]:
                    return state["engine"]
                engine = build()
                state["engine"] = engine
                state["ready"] = True
            return state["engine"]

        fn = old_get if impl == "old" else new_get
        bar = threading.Barrier(8)
        out: list = [None] * 8

        def w(i: int) -> None:
            bar.wait()
            out[i] = fn()

        ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        nones = sum(1 for o in out if o is None)
        print(f"   {label:30s} builds={state['builds']}  callers that got None={nones}/8"
              f"  -> {'dropped reads' if nones else 'all reads served'}")


# --- gate instrumentation (sections 4 and 5) ----------------------------------
_stage_label: object = None


def _instrument_gate():
    """Record every OCR_GATE acquisition with the timing stage that was open when
    it happened, so 'who is holding an OCR slot' is answerable from the outside.

    The label rides a contextvar set by a patched ``stage`` — asyncio copies the
    context into every task it creates, so a gathered per-card coroutine still
    reports the stage its parent opened."""
    import contextvars
    from contextlib import contextmanager

    import app.timing as timing
    import app.pack.pipeline as pipeline
    import app.pack.binder as binder

    cur = contextvars.ContextVar("probe_stage", default="-")
    real_stage = timing.stage

    @contextmanager
    def patched(flow, name, scan_id=None):
        tok = cur.set(f"{flow}.{name}")
        try:
            with real_stage(flow, name, scan_id):
                yield
        finally:
            cur.reset(tok)

    timing.stage = patched
    for mod in (pipeline, binder):
        mod.stage = patched
    try:
        import app.pack.live_identify as li
        li.stage = patched
    except Exception:
        pass

    gate = pipeline.OCR_GATE
    records: list[dict] = []
    real_acquire = gate.acquire
    real_release = gate.release
    # acquire and release for one slot always happen in the SAME task (async-with
    # or acquire/finally-release), so a per-task stack pairs them exactly.
    held: dict[int, list] = {}

    async def acquire():
        t0 = time.perf_counter()
        await real_acquire()
        rec = {"stage": cur.get(), "wait_ms": ms(t0), "t_acq": time.perf_counter()}
        records.append(rec)
        held.setdefault(id(asyncio.current_task()), []).append(rec)
        return True

    def release():
        stack = held.get(id(asyncio.current_task()))
        if stack:
            rec = stack.pop()
            rec["hold_ms"] = (time.perf_counter() - rec["t_acq"]) * 1000
        return real_release()

    gate.acquire = acquire
    gate.release = release
    return records


def _report(records, title):
    print(f"   {title}")
    by: dict[str, list] = {}
    for r in records:
        by.setdefault(r["stage"], []).append(r)
    print(f"   {'stage holding the slot':28s} {'n':>3s} {'wait ms (max)':>14s} {'hold ms (sum)':>14s}")
    for k in sorted(by):
        rs = by[k]
        print(f"   {k:28s} {len(rs):3d} {max(r['wait_ms'] for r in rs):14.1f} "
              f"{sum(r.get('hold_ms', 0.0) for r in rs):14.1f}")


# --- 4. who holds an OCR slot during a pack scan ------------------------------
async def section_gate() -> None:
    records = _instrument_gate()
    from app.pack.pipeline import scan_pack

    # The real 12MP 9-card photo when it is present — a 3-card synthetic fixture
    # understates every per-card cost.
    stair = STAIR12 if STAIR12.exists() else STAIR
    print(f"   (staircase fixture: {stair.name})")
    await scan_pack(stair.read_bytes(), CODE.read_bytes(), None)   # warm the process
    records.clear()
    t0 = time.perf_counter()
    resp = await scan_pack(stair.read_bytes(), CODE.read_bytes(), None)
    wall = ms(t0)
    print(f"== OCR_GATE acquisitions during ONE warm pack scan (cards={len(resp.cards)}, "
          f"wall={wall:.0f}ms) ==")
    _report(records, "")
    stages = {r["stage"] for r in records}
    print(f"   resolve_set holds an OCR slot: "
          f"{'YES' if any('resolve_set' in s for s in stages) else 'NO'}")


# --- 5. contention: a binder page vs live frames ------------------------------
async def section_contention(ladder_delay_ms: float = 0.0) -> None:
    """A binder page (9 cells, 9 OCR slots wanted) while three live frames arrive
    mid-page. The question is SLOT OCCUPANCY: how long a live frame keeps one of
    the three OCR slots. Before this task live_api held it across the whole
    identify — OCR *and* the DB/ladder/price work; now only the OCR is inside.

    Arrivals are late enough (0.6/0.9/1.2s) that every frame lands while the page
    is already mid-OCR in both variants, so the two runs queue the same way."""
    import inspect

    import cv2

    records = _instrument_gate()
    from app.pack.binder import scan_binder_page
    from app.pack.live_identify import SessionPrior, identify_frame
    from app.pack import rapidocr_reader
    from app.pack.pipeline import OCR_GATE

    # Engine built up front either way, so the numbers are about the gate and not
    # about a model load (the pre-change module has no warmup(), hence the getattr).
    await asyncio.to_thread(getattr(rapidocr_reader, "warmup", rapidocr_reader._get))
    frame = cv2.imread(str(REEL))
    strip = frame[int(frame.shape[0] * 0.75):]
    scoped = "scan_id" in inspect.signature(identify_frame).parameters
    print(f"== binder page + 3 live frames  (identify_frame gate-scoped: {scoped}, "
          f"injected ladder delay: {ladder_delay_ms:.0f} ms) ==")
    totals: list[float] = []

    # Time the identify LADDER (name index + DB + PokéWallet), which is the work
    # that used to sit inside the OCR slot. `ladder_delay_ms` stands in for a
    # PokéWallet round trip, which this box never makes (no API key, warm local
    # DB) but a deployed server does on every cache miss.
    import app.pack.live_identify as li

    real_ladder = li.resolve_identity
    ladder: list[float] = []

    async def timed_ladder(*a, **k):
        t0 = time.perf_counter()
        out = await real_ladder(*a, **k)
        if ladder_delay_ms:
            await asyncio.sleep(ladder_delay_ms / 1000.0)
        ladder.append(ms(t0))
        return out

    li.resolve_identity = timed_ladder

    async def live(i: int) -> None:
        await asyncio.sleep(0.6 + 0.3 * i)      # arrives while the page is mid-OCR
        t0 = time.perf_counter()
        if scoped:                          # AFTER: the gate is inside identify_frame
            await identify_frame(frame, strip, SessionPrior(None, None, "86"))
        else:                               # BEFORE: live_api held it around the call
            import app.timing as timing     # the patched stage -> same label

            with timing.stage("live", "gate_wait", None):
                await OCR_GATE.acquire()
            try:
                await identify_frame(frame, strip, SessionPrior(None, None, "86"))
            finally:
                OCR_GATE.release()
        totals.append(ms(t0))

    t0 = time.perf_counter()
    page_task = asyncio.create_task(scan_binder_page(PAGE.read_bytes()))
    await asyncio.gather(page_task, *(live(i) for i in range(3)))
    page_wall = ms(t0)
    _report(records, "slot holders across the whole run:")
    holds = [r.get("hold_ms", 0.0) for r in records if r["stage"] == "live.gate_wait"]
    waits = [r["wait_ms"] for r in records if r["stage"] == "live.gate_wait"]
    cells = [r.get("hold_ms", 0.0) for r in records if r["stage"] == "binder.ocr_cells"]
    print(f"   binder page wall           : {page_wall:8.0f} ms")
    print(f"   live frame latency  (mean) : {sum(totals) / len(totals):8.1f} ms  "
          f"[{', '.join(f'{t:.0f}' for t in sorted(totals))}]")
    print(f"   live gate wait      (mean) : {sum(waits) / len(waits):8.1f} ms")
    print(f"   live SLOT OCCUPANCY (mean) : {sum(holds) / len(holds):8.1f} ms  "
          f"total={sum(holds):.0f} ms  <- the number this task moves")
    if ladder:
        print(f"   live ladder (DB/lookup)    : {sum(ladder) / len(ladder):8.1f} ms per frame"
              f"  — {'OUTSIDE' if scoped else 'INSIDE'} the slot")
    print(f"   binder cell occupancy (sum): {sum(cells):8.0f} ms over {len(cells)} cells")


# --- 5b. the promo local_id audit, on good and corrupted data -----------------
async def section_audit() -> None:
    """The startup data-trust guard: silent on a healthy catalog, ERROR when the
    denominator table's `local_id_prefixed` flag disagrees with what was ingested
    (simulated by flipping the flag on every promo row)."""
    import dataclasses

    cap = _capture()
    import app.pack.set_resolution as sr
    from app.main import _warm_catalog

    print("== healthy catalog ==")
    mark = len(cap.lines)
    await _warm_catalog()
    for line in cap.lines[mark:]:
        print(f"   {line}")

    table = sr.load_denominator_table()
    flipped = tuple(
        dataclasses.replace(s, local_id_prefixed=not s.local_id_prefixed)
        if s.promo_prefix else s
        for s in table.sets)
    sr._table_cache[None] = dataclasses.replace(table, sets=flipped)
    print("\n== every promo row's local_id_prefixed flag flipped ==")
    mark = len(cap.lines)
    await _warm_catalog()
    for line in cap.lines[mark:]:
        print(f"   {line}")


# --- 6. /health stays fast while the warm task runs ---------------------------
def section_health() -> None:
    """The Railway constraint: the app must serve (and /health must answer) while
    the warm-up is still going. Drives the REAL lifespan through TestClient."""
    from fastapi.testclient import TestClient

    from app.main import app

    t0 = time.perf_counter()
    with TestClient(app) as client:          # runs the real lifespan
        startup = ms(t0)
        t1 = time.perf_counter()
        r = client.get("/health")
        health = ms(t1)
        warm = app.state.warm_task
        still_warming = not warm.done()
        print("\n== lifespan + /health while the warm task runs ==")
        print(f"   lifespan startup -> serving : {startup:8.1f} ms")
        print(f"   GET /health                 : {health:8.1f} ms  "
              f"{r.status_code} {r.json()}")
        print(f"   warm task STILL RUNNING when /health answered: {still_warming}")
        deadline = time.time() + 60
        while not warm.done() and time.time() < deadline:
            client.get("/health")
            time.sleep(0.05)
        print(f"   warm task finished after            {ms(t1):8.1f} ms "
              f"(app served /health throughout)")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stall"
    if cmd == "stall":
        asyncio.run(section_stall())
    elif cmd == "firstscan":
        asyncio.run(section_firstscan(sys.argv[2] if len(sys.argv) > 2 else "cold",
                                      sys.argv[3] if len(sys.argv) > 3 else "binder"))
    elif cmd == "lock":
        section_lock()
    elif cmd == "health":
        section_health()
    elif cmd == "audit":
        asyncio.run(section_audit())
    elif cmd == "gate":
        asyncio.run(section_gate())
    elif cmd == "contention":
        # optional: ms of injected ladder delay, standing in for a PokéWallet call
        asyncio.run(section_contention(float(sys.argv[2]) if len(sys.argv) > 2 else 0.0))
    else:
        raise SystemExit(f"unknown section {cmd}")


if __name__ == "__main__":
    main()
