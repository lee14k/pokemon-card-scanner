"""Probe for Task 9 (S5 caches + S6 fast name index + threaded matching).

Not a pytest test (the suite stays 7 passed / 1 skipped); a measurement and
behaviour-identity harness. Every section is a subcommand:

    export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \
      AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \
      PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false

    P=docs/acceptance/probes/task-9-probe.py

    .venv/bin/python $P dump <out.json>
        The 500-query corpus through NameIndex.match / match_in_set, serialized.
        Deliberately depends on NOTHING this task added, so it runs unmodified on
        the PRE-CHANGE tree — that is the whole point of it.

    .venv/bin/python $P identity [base_rev]
        Checks `base_rev` (default bf212ed, Task 8's HEAD) out into a scratch
        worktree, runs `dump` there and here under four PYTHONHASHSEEDs, and
        asserts every field of every result is identical.

    .venv/bin/python $P equivalence
        The precomputed structures against a brute-force recomputation of the
        code they replaced, over the real catalog. Belt to identity's braces:
        identity samples 500 queries, this checks all ~2.4k keys and all 53 sets.

    .venv/bin/python $P latency
        Per-match latency, cold and warm, for match() and match_in_set().

    .venv/bin/python $P concurrency
        Event-loop tick lateness during a match storm, inline vs to_thread.

    .venv/bin/python $P caches
        The four caches: numerator TTL + empty-result policy, set_id_map preload
        and its fallback, price-map staleness window, shared HTTP client reuse
        and shutdown.

    .venv/bin/python $P page
        SQL statements issued per real binder page — the exact thing the three DB
        memos remove, counted rather than timed (page wall time is 87% OCR).

    .venv/bin/python $P loops
        Every cache driven concurrently from TWO event loops in one process —
        the case that rules out a module-level asyncio.Lock around a cache miss.

Exit code 0 = every assertion in the section held.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

SELF = Path(__file__).resolve()
_p = SELF.parents
REPO = _p[3] if len(_p) > 3 and (_p[3] / "app").is_dir() else Path.cwd()
TMP = Path(os.environ.get("TMPDIR", "/tmp"))
BASE_REV = "bf212ed"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


def finish(section: str) -> None:
    print(f"\n{section}: {'PASS' if not FAILURES else 'FAIL ' + repr(FAILURES)}")
    sys.exit(0 if not FAILURES else 1)


# --- the query corpus ----------------------------------------------------------
def _old_is_token_subsequence(short: str, long: str) -> bool:
    """VERBATIM copy of the predicate `match` used to run per call, kept here so
    both trees compute the identical corpus and so `equivalence` has an oracle
    that owes nothing to the code under test."""
    a, b = short.split(), long.split()
    if not a or len(a) >= len(b):
        return False
    for i in range(len(b) - len(a) + 1):
        if b[i:i + len(a)] == a:
            return True
    return False


def _garble(rng: random.Random, s: str) -> str:
    """OCR-ish damage: dropped/confused characters, dropped prefix, trailing junk."""
    kind = rng.randrange(5)
    if kind == 0 and len(s) > 4:
        i = rng.randrange(len(s))
        return s[:i] + s[i + 1:]
    if kind == 1 and len(s) > 3:
        i = rng.randrange(len(s))
        sub = {"o": "0", "i": "1", "l": "1", "s": "5", "e": "c", "n": "m",
               "a": "4", "b": "6", "g": "9", "u": "v"}.get(s[i].lower(), "x")
        return s[:i] + sub + s[i + 1:]
    if kind == 2:
        return s.upper()
    if kind == 3 and " " in s:
        return s.split(" ", 1)[1]
    return s + rng.choice(["", " ", " v", "-", " ex"])


def build_corpus(idx, n: int = 500) -> list[dict]:
    """500 diverse queries — deterministic given the catalog, and computed the
    same way on both trees. Each is {text, denominator, set_id}."""
    rng = random.Random(20260726)
    names = sorted(idx._entries.keys())
    sets = sorted(idx._by_set.keys())
    out: list[dict] = []

    # Hand-picked hazards first, so a regression in one can never be sampled away:
    # the Task-7 tie cases, the possessive recoveries, the promo reads, the
    # substring-hazard pairs, the gallery denominators and the degenerate inputs.
    for text, den, sid in [
        ("KLINK", None, "sv09"), ("KLINKLANG", None, "sv09"),
        ("N'S KLINK", None, "sv09"), ("N'S KLINKLANG", None, "sv09"),
        ("LILLIE'S DETERMINATION", "184", "svp"),
        ("SNORLAX", None, "svp"), ("DARMANITAN", "181", "svp"),
        ("ODDISH", None, "sv10"), ("ERIKA'S ODDISH", None, "sv10"),
        ("PIKACHU", None, None), ("SURFING PIKACHU", None, None),
        ("PIKACHU ON THE BALL", "131", None),
        ("HATTERENE V", None, None), ("HATTERENE VMAX", None, None),
        ("BLISSEY V", "TG30", "swsh12.5tg"), ("MEOWTH V", None, "swshp"),
        ("ROWLET", None, "mep"), ("LITTEN", None, "mep"), ("POPPLIO", None, "mep"),
        ("TYROGUE", "4", None), ("OGERPON", "131", None),
        ("WOOLOO", "170", None), ("HOP'S WOOLOO", "170", None),
        ("TOGEDEMARU", "94", None), ("DUSTOX", "217", None),
        ("", None, None), ("  ", None, None), ("ab", None, None),
        ("123", None, None), ("♀♂★", None, None),
        ("PIKACHU", "TG30", None), ("PIKACHU", "GG70", None),
        ("PIKACHU", "notanumber", None), ("PIKACHU", "", None),
    ]:
        out.append({"text": text, "denominator": den, "set_id": sid})

    # Every substring-hazard name is a case: that flag is exactly what the
    # precompute now answers, so the corpus must be dense in it.
    hazards = sorted(k for k in names
                     if any(o != k and _old_is_token_subsequence(k, o) for o in names))
    for k in hazards[:60]:
        out.append({"text": k.upper(), "denominator": None, "set_id": None})

    while len(out) < n:
        name = rng.choice(names)
        text = name if rng.random() < 0.35 else _garble(rng, name)
        den = None
        if rng.random() < 0.45:
            entry = idx._entries[name][0]
            den = (str(entry[4]) if entry[4] is not None and rng.random() < 0.6
                   else rng.choice([str(rng.randrange(1, 300)), "TG30", "GG70",
                                    "SWSH", "", "0"]))
        sid = rng.choice(sets) if rng.random() < 0.4 else None
        out.append({"text": text, "denominator": den, "set_id": sid})
    return out[:n]


def _ser(m) -> dict | None:
    if m is None:
        return None
    return {"set": m.tcgdex_set_id, "set_name": m.set_name, "local_id": m.local_id,
            "name": m.card_name, "score": round(float(m.score), 10),
            "ambiguous": bool(m.ambiguous)}


async def section_dump(out_path: str) -> None:
    from app.pack.name_index import get_name_index
    idx = await get_name_index()
    corpus = build_corpus(idx)
    results = []
    for q in corpus:
        results.append({
            "q": q,
            "match": _ser(idx.match(q["text"], denominator=q["denominator"])),
            "match_in_set": _ser(idx.match_in_set(
                q["text"], set_id=q["set_id"], denominator=q["denominator"])),
        })
    Path(out_path).write_text(json.dumps(results, sort_keys=True, indent=0))
    print(f"dump: {len(results)} queries -> {out_path}")


# --- identity: this tree vs the pre-change tree --------------------------------
def section_identity(base_rev: str = BASE_REV) -> None:
    scratch = TMP / "task9-base"
    if not (scratch / "app").is_dir():
        subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                        str(scratch), base_rev], check=True)
    probe_there = scratch / "task-9-probe.py"
    probe_there.write_text(SELF.read_text())    # base_rev predates this file

    seeds = ["0", "1", "12345", "99991"]
    per_seed = []
    for seed in seeds:
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "."}
        new_json, old_json = TMP / f"t9-new-{seed}.json", TMP / f"t9-old-{seed}.json"
        subprocess.run([sys.executable, str(SELF), "dump", str(new_json)],
                       cwd=str(REPO), env=env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, str(probe_there), "dump", str(old_json)],
                       cwd=str(scratch), env=env, check=True,
                       stdout=subprocess.DEVNULL)
        new, old = json.loads(new_json.read_text()), json.loads(old_json.read_text())
        per_seed.append(new)
        same_len = len(new) == len(old) == 500
        diffs = [] if not same_len else [
            {"q": n["q"], "old_match": o["match"], "new_match": n["match"],
             "old_scoped": o["match_in_set"], "new_scoped": n["match_in_set"]}
            for o, n in zip(old, new)
            if o["match"] != n["match"] or o["match_in_set"] != n["match_in_set"]]
        check(f"PYTHONHASHSEED={seed}: 500 queries x (match, match_in_set) identical "
              f"to {base_rev}", same_len and not diffs,
              "" if same_len and not diffs
              else f"len={len(new)}/{len(old)} diffs={len(diffs)} "
                   f"first={diffs[0] if diffs else None}")

    check("this tree is itself identical across all four hash seeds (Task 7's "
          "determinism guarantee, re-checked after the precompute)",
          all(p == per_seed[0] for p in per_seed))
    n_nonnull = sum(1 for r in per_seed[0] if r["match"] or r["match_in_set"])
    n_amb = sum(1 for r in per_seed[0]
                if (r["match"] or {}).get("ambiguous")
                or (r["match_in_set"] or {}).get("ambiguous"))
    print(f"\n  corpus reach: {n_nonnull}/500 queries produced at least one match, "
          f"{n_amb} carried an ambiguous flag (so the flag is genuinely exercised, "
          f"not vacuously equal)")
    finish("identity")


# --- equivalence: the precomputes vs the code they replaced --------------------
async def section_equivalence() -> None:
    from app.pack.name_index import get_name_index
    idx = await get_name_index()
    keys = idx._keys
    print(f"  catalog: {len(keys)} distinct names, {len(idx._by_set)} sets")

    t = time.perf_counter()
    brute = frozenset(k for k in keys
                      if any(o != k and _old_is_token_subsequence(k, o) for o in keys))
    brute_s = time.perf_counter() - t
    check(f"substring hazard: {len(idx._substr_hazard)} keys == brute force "
          f"(the old scan, run once for every key, took {brute_s:.1f}s)",
          brute == idx._substr_hazard,
          f"only-new={sorted(idx._substr_hazard - brute)[:5]} "
          f"only-old={sorted(brute - idx._substr_hazard)[:5]}")

    bad_keys, bad_entries = [], []
    for sid, pool in idx._by_set.items():
        if idx._set_keys[sid] != sorted({k for k, _e in pool}):
            bad_keys.append(sid)
        for k in idx._set_keys[sid]:
            if idx._set_key_entries[sid][k] != [e for kk, e in pool if kk == k]:
                bad_entries.append((sid, k))
    check(f"per-set key list == sorted({{k for k,_e in pool}}) for all "
          f"{len(idx._by_set)} sets", not bad_keys, str(bad_keys[:3]))
    check("per-set key->printings == [e for k,e in pool if k==key], ORDER included "
          "(match_in_set returns matched[0])", not bad_entries, str(bad_entries[:3]))
    check("_set_keys covers exactly the _by_set ids and none is empty, so "
          "`if not keys` is the same gate `if not pool` was",
          set(idx._set_keys) == set(idx._by_set)
          and all(idx._set_keys[s] for s in idx._set_keys))

    import copy
    before = copy.deepcopy(vars(idx))
    for q in ("PIKACHU", "KLINK", "erika s oddish", "zzzz", "", "hop s wooloo"):
        idx.match(q, denominator="131")
        idx.match_in_set(q, set_id="sv09", denominator="131")
    check("NameIndex unmutated by 12 match calls — the precondition for running "
          "them in threads", vars(idx) == before)
    finish("equivalence")


# --- latency -------------------------------------------------------------------
async def section_latency() -> None:
    from app.pack.name_index import NameIndex, get_name_index

    t = time.perf_counter()
    idx = await get_name_index()
    print(f"  index build incl. DB read: {(time.perf_counter() - t) * 1000:.0f}ms")
    rows = [e for entries in idx._entries.values() for e in entries]
    t = time.perf_counter()
    NameIndex(rows)
    print(f"  NameIndex(rows) rebuild, no DB: {(time.perf_counter() - t) * 1000:.0f}ms "
          f"for {len(rows)} rows — the precompute is inside this, paid once at warm-up")

    corpus = build_corpus(idx)
    hundred = [q["text"] for q in corpus][:100]
    scoped = [(q["text"], q["set_id"]) for q in corpus if q["set_id"]][:100]

    def bench(fn, items, reps):
        ts = []
        for _ in range(reps):
            for it in items:
                t0 = time.perf_counter()
                fn(it)
                ts.append((time.perf_counter() - t0) * 1000)
        return ts

    def report(label, ts):
        print(f"  {label:<34} n={len(ts):<4} mean {statistics.mean(ts):7.3f}ms  "
              f"p50 {statistics.median(ts):7.3f}ms  "
              f"p95 {sorted(ts)[int(len(ts) * .95)]:7.3f}ms  "
              f"max {max(ts):7.3f}ms  total {sum(ts):7.1f}ms")

    report("match() cold (100 queries)", bench(lambda s: idx.match(s), hundred, 1))
    report("match() warm (100 x5)", bench(lambda s: idx.match(s), hundred, 5))
    report("match_in_set() cold", bench(
        lambda p: idx.match_in_set(p[0], set_id=p[1]), scoped, 1))
    report("match_in_set() warm (x5)", bench(
        lambda p: idx.match_in_set(p[0], set_id=p[1]), scoped, 5))
    print("\n  For the BEFORE numbers run this same section in the base worktree:")
    print(f"    git worktree add --detach {TMP}/task9-base {BASE_REV}")
    print(f"    cp {SELF} {TMP}/task9-base/ && cd {TMP}/task9-base && "
          f"PYTHONPATH=. python task-9-probe.py latency")
    print("\nlatency: PASS (measurement only)")


# --- concurrency: is the loop still answering during a match storm? ------------
async def section_concurrency() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.pack.name_index import get_name_index
    idx = await get_name_index()
    corpus = [q["text"] for q in build_corpus(idx)][:270]
    for t in corpus[:20]:              # let rapidfuzz warm its caches first
        idx.match(t)

    # 0. Does rapidfuzz actually release the GIL? The threading claim rests on it,
    #    so measure it rather than assert it: the same work split over 4 threads
    #    against the same work run straight through.
    def run_all(items):
        for it in items:
            idx.match(it)

    t = time.perf_counter()
    run_all(corpus)
    seq = (time.perf_counter() - t) * 1000
    chunks = [corpus[i::4] for i in range(4)]
    with ThreadPoolExecutor(4) as ex:
        t = time.perf_counter()
        list(ex.map(run_all, chunks))
        par = (time.perf_counter() - t) * 1000
    print(f"  GIL check: {len(corpus)} matches sequential {seq:.0f}ms vs "
          f"4 threads {par:.0f}ms => {seq / par:.2f}x speedup.")
    print("    ~1x means rapidfuzz does NOT release the GIL here (measured: it does "
          "not, 3.14.3), so the matches below do not run in PARALLEL. What the "
          "thread still buys is that the loop is runnable during a sweep instead "
          "of being the thing running it — it regains the GIL at the interpreter's "
          "switch interval rather than at the end of the match. That shows up in "
          "the TAIL, below, and nowhere else.")

    async def ticker(stop: asyncio.Event) -> list[float]:
        """A 1ms heartbeat. How LATE each tick fires is the loop's responsiveness —
        the same shape as Task 3's probe."""
        late = []
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            late.append((time.perf_counter() - t0 - 0.001) * 1000)
        return late

    async def storm(inline: bool, workers: int = 9) -> tuple[list[float], float]:
        stop = asyncio.Event()
        tick = asyncio.create_task(ticker(stop))
        await asyncio.sleep(0.05)

        async def one(text: str):
            if inline:
                return idx.match(text)             # what resolve_identity used to do
            return await asyncio.to_thread(idx.match, text)

        t0 = time.perf_counter()
        for i in range(0, len(corpus), workers):   # binder-shaped: 9 cells at a time
            await asyncio.gather(*(one(t) for t in corpus[i:i + workers]))
        wall = (time.perf_counter() - t0) * 1000
        stop.set()
        return await tick, wall

    def pct(v, p):
        return sorted(v)[min(len(v) - 1, int(len(v) * p))]

    trials = 5
    res: dict[bool, list[tuple[list[float], float]]] = {True: [], False: []}
    for _ in range(trials):
        for inline in (True, False):
            res[inline].append(await storm(inline))
            await asyncio.sleep(0.05)

    summary = {}
    for inline, label in ((True, "inline (pre-change)"),
                          (False, "to_thread (this change)")):
        maxes = [max(l) for l, _w in res[inline]]
        p95s = [pct(l, .95) for l, _w in res[inline]]
        p50s = [statistics.median(l) for l, _w in res[inline]]
        walls = [w for _l, w in res[inline]]
        summary[inline] = (statistics.median(maxes), statistics.median(p95s),
                           statistics.median(p50s), statistics.median(walls))
        print(f"  {label:<24} {len(corpus)} matches, median of {trials} trials: "
              f"wall {statistics.median(walls):6.0f}ms | tick lateness "
              f"max {statistics.median(maxes):6.1f}ms  "
              f"p95 {statistics.median(p95s):6.2f}ms  "
              f"p50 {statistics.median(p50s):5.2f}ms")

    # THE VERDICT, and why resolve_identity does NOT use to_thread for matching.
    # Over 30 trials the tick-lateness distributions are indistinguishable — which
    # is exactly what should be expected once the GIL check above comes back at 1x:
    # a single match is ~0.3-1ms, SHORTER than the interpreter's 5ms switch
    # interval, so handing it to a worker thread does not give the loop control
    # any sooner than just running it. The hop is not free, though, and that shows.
    d_max = summary[False][0] - summary[True][0]
    d_p95 = summary[False][1] - summary[True][1]
    d_wall = summary[False][3] - summary[True][3]
    print(f"\n  delta (threaded - inline): max {d_max:+.2f}ms  p95 {d_p95:+.2f}ms  "
          f"p50 {summary[False][2] - summary[True][2]:+.2f}ms  "
          f"wall {d_wall:+.0f}ms ({100 * d_wall / summary[True][3]:+.1f}%)")
    check("tick lateness is UNCHANGED by to_thread — no responsiveness win to be "
          "had, because a match is shorter than the 5ms GIL switch interval",
          abs(d_max) < 0.15 * summary[True][0] and abs(d_p95) < 0.15 * summary[True][1],
          f"max {summary[True][0]:.1f} -> {summary[False][0]:.1f}ms, "
          f"p95 {summary[True][1]:.2f} -> {summary[False][1]:.2f}ms")
    check("and the hop costs wall time, consistently — the reason B6 was measured "
          "and then NOT shipped", d_wall > 0,
          f"inline {summary[True][3]:.0f}ms -> threaded {summary[False][3]:.0f}ms")
    finish("concurrency")


# --- caches --------------------------------------------------------------------
async def section_caches() -> None:
    import app.cards as cards
    import app.pack.identify_core as ic
    import app.pokewallet as pw
    import app.prices as prices
    from sqlalchemy import select
    from app.db.models import SetIdMap, TcgdexCard
    from app.db.session import async_session_maker

    async with async_session_maker() as s:
        pw_set = (await s.execute(
            select(SetIdMap.pokewallet_set_id).join(
                TcgdexCard, TcgdexCard.set_id == SetIdMap.tcgdex_set_id).limit(1)
        )).scalars().first()
    print(f"  (using mapped+ingested set_id {pw_set!r})")

    print("\n-- 1. get_set_numerators: TTL and the A1 empty-result policy --")
    calls: list[str] = []
    real_query = cards._query_set_numerators

    async def counting(set_id: str):
        calls.append(set_id)
        return await real_query(set_id)

    cards._query_set_numerators = counting
    cards._numerators_cache.clear()
    a = await cards.get_set_numerators(pw_set)
    b = await cards.get_set_numerators(pw_set)
    check("a populated set is queried once and served from cache after",
          len(calls) == 1 and a == b and len(a) > 0,
          f"queries={len(calls)} numerators={len(a)}")
    check("returns a frozenset, so the shared cached value cannot be mutated by a "
          "caller", isinstance(a, frozenset), type(a).__name__)
    left = cards._numerators_cache[str(pw_set)][0] - time.monotonic()
    check(f"populated entry lives ~{cards._NUMERATORS_TTL_S:.0f}s",
          cards._NUMERATORS_TTL_S - 5 < left <= cards._NUMERATORS_TTL_S,
          f"{left:.0f}s left")

    calls.clear()
    empty = await cards.get_set_numerators("no-such-set")
    left = cards._numerators_cache["no-such-set"][0] - time.monotonic()
    check("A1 HOLDS: an unmapped set is still empty — fail closed, never a stale "
          "populated catalog", empty == frozenset())
    check(f"an EMPTY result gets the short TTL (~{cards._NUMERATORS_EMPTY_TTL_S:.0f}s), "
          f"not the long one", 0 < left <= cards._NUMERATORS_EMPTY_TTL_S
          and left > cards._NUMERATORS_EMPTY_TTL_S - 5, f"{left:.1f}s left")

    # The failure path proper: a DB that raises. It must still degrade to empty
    # (A1), must be cached only briefly, and must recover the moment it can.
    class Boom:
        def __call__(self):
            raise RuntimeError("simulated DB outage")

    real_maker = cards.async_session_maker
    cards._query_set_numerators = real_query
    cards.async_session_maker = Boom()
    cards._numerators_cache.clear()
    failed = await cards.get_set_numerators(pw_set)
    left = cards._numerators_cache[str(pw_set)][0] - time.monotonic()
    check("a raising DB degrades to empty instead of propagating (unchanged)",
          failed == frozenset())
    check("and that failure is pinned for the SHORT ttl only — 10 minutes of it "
          "would keep every card in the set flagged",
          left <= cards._NUMERATORS_EMPTY_TTL_S, f"{left:.1f}s left")
    cards.async_session_maker = real_maker
    cards._numerators_cache[str(pw_set)] = (time.monotonic() - 0.001, frozenset())
    cards._query_set_numerators = counting
    calls.clear()
    recovered = await cards.get_set_numerators(pw_set)
    check("once the short TTL is up the real catalog comes straight back — the "
          "outage costs 30s of flagging, not 10 minutes",
          len(calls) == 1 and len(recovered) > 0, f"numerators={len(recovered)}")
    cards._query_set_numerators = real_query

    print("\n-- 2. set_id_map preload --")
    ic._set_id_map = None
    t = time.perf_counter()
    first = await ic._pw_set_id_for("sv06")
    t_first = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    for _ in range(200):
        await ic._pw_set_id_for("sv06")
    t_200 = (time.perf_counter() - t) * 1000
    check("the preload holds the whole table",
          ic._set_id_map is not None and len(ic._set_id_map) > 40,
          f"rows={len(ic._set_id_map or {})}")
    print(f"  first call (one query for every row) {t_first:.1f}ms; 200 further "
          f"lookups {t_200:.2f}ms total = {t_200 / 200:.4f}ms each — each of those "
          f"was a connection acquire + round trip before")
    async with async_session_maker() as s:
        rows = (await s.execute(select(SetIdMap.tcgdex_set_id,
                                       SetIdMap.pokewallet_set_id))).all()
    mism = [t for t, p in rows if (ic._set_id_map or {}).get(t) != p]
    check(f"all {len(rows)} rows resolve exactly as the per-row query did",
          not mism, str(mism[:3]))
    check("an unmapped tcgdex id still yields None",
          await ic._pw_set_id_for("definitely-not-a-set") is None)

    real_load = ic._load_set_id_map

    async def failed_load():
        return None

    ic._load_set_id_map = failed_load          # pretend the preload never worked
    fb = await ic._pw_set_id_for("sv06")
    check("fallback: with the preload unavailable the single-row query still "
          "answers, identically", fb == first, f"{fb!r} vs {first!r}")
    ic._load_set_id_map = real_load

    # THE EMPTY-TABLE SCENARIO. A database with migrations applied but
    # scripts/build_id_maps.py not yet run answers the preload with zero rows.
    # `{}` is `is not None`, so treating the load as successful would pin it for
    # the life of the process: every confidently identified card silently loses
    # its price and image, the documented fallback never fires, and running the
    # builder afterwards changes nothing until a redeploy. Shape: empty table ->
    # observe -> populate -> the SAME process must resolve.
    class EmptyTable:
        """A session whose set_id_map SELECT returns no rows, and whose per-row
        fallback query still works — i.e. exactly a freshly migrated DB."""

        def __init__(self, real):
            self.real = real
            self.preloads = 0
            self.fallbacks = 0

        def __call__(self):
            outer = self

            class S:
                async def __aenter__(self):
                    self.s = outer.real()
                    return self

                async def __aexit__(self, *a):
                    await self.s.__aexit__(*a)
                    return False

                async def execute(self, stmt):
                    # the preload selects two columns; the fallback selects one
                    if len(stmt.selected_columns) == 2:
                        outer.preloads += 1
                        return _NoRows()
                    outer.fallbacks += 1
                    sess = await self.s.__aenter__()
                    return await sess.execute(stmt)
            return S()

    class _NoRows:
        def all(self):
            return []

    empty = EmptyTable(ic.async_session_maker)
    ic._set_id_map = None
    ic.async_session_maker = empty
    e1 = await ic._pw_set_id_for("sv06")
    e2 = await ic._pw_set_id_for("sv06")
    check("an EMPTY set_id_map is not mistaken for a loaded one — it stays falsy "
          "instead of being pinned as {} for the process's life",
          not ic._set_id_map, f"_set_id_map={ic._set_id_map!r}")
    check("so the documented per-row fallback actually fires, and the card keeps "
          "the id that gives it a price and an image",
          e1 == first and e2 == first and empty.fallbacks >= 2,
          f"{e1!r}/{e2!r} vs {first!r}, fallback queries={empty.fallbacks}")
    check("and the preload is re-attempted rather than answered from a pinned "
          "empty dict", empty.preloads >= 2, f"preload attempts={empty.preloads}")
    ic.async_session_maker = real_maker        # the builder has now been run
    after = await ic._pw_set_id_for("sv06")
    check("once the table IS populated the SAME process picks it up — no redeploy "
          "needed, which a cached {} would have required",
          after == first and bool(ic._set_id_map),
          f"{after!r}, rows={len(ic._set_id_map or {})}")
    print("  invalidation: none for a POPULATED table, by design — set_id_map is "
          "written only by the offline scripts/build_id_maps.py, so a serving "
          "process cannot watch it change from one set of rows to another, and a "
          "rebuild reaches production via a deploy (restart). The empty->populated "
          "transition IS observable by a live process, which is why the empty case "
          "above is never cached.")

    print("\n-- 3. latest_price_map TTL --")
    prices.invalidate_price_cache()
    pcalls: list[int] = []
    real_price = prices._query_price_map

    async def counting_price(session):
        pcalls.append(1)
        return await real_price(session)

    prices._query_price_map = counting_price
    async with async_session_maker() as s:
        m1, a1 = await prices.latest_price_map(s)
        m2, a2 = await prices.latest_price_map(s)
        check("a second call inside the window does not touch the DB",
              len(pcalls) == 1 and m1 is m2 and a1 == a2, f"queries={len(pcalls)}")
        left = prices._price_cache[0] - time.monotonic()
        check(f"the entry lives ~{prices._PRICE_TTL_S:.0f}s",
              prices._PRICE_TTL_S - 5 < left <= prices._PRICE_TTL_S, f"{left:.0f}s")
        prices._price_cache = (time.monotonic() + 0.05, {"stale": (1.0, 2.0)}, "old")
        _m3, a3 = await prices.latest_price_map(s)
        check("a value 50ms short of expiry is still served — stale <= 60s is the "
              "deal", a3 == "old" and len(pcalls) == 1)
        await asyncio.sleep(0.08)
        _m4, a4 = await prices.latest_price_map(s)
        check("past expiry it refreshes from the DB",
              len(pcalls) == 2 and a4 == a1, f"queries={len(pcalls)} asof={a4!r}")
        prices.invalidate_price_cache()
        await prices.latest_price_map(s)
        check("invalidate_price_cache() forces a re-read — wired into the pricing "
              "batch, which writes its snapshot in this same process",
              len(pcalls) == 3)
    prices._query_price_map = real_price

    grep = subprocess.run(["git", "-C", str(REPO), "grep", "-n",
                           "await latest_price_map(", "--", "app"],
                          capture_output=True, text=True).stdout
    sites = [l.strip() for l in grep.splitlines() if l.strip()]
    print(f"  every caller goes through the one cached function ({len(sites)} sites):")
    for l in sites:
        print("   ", l)
    check("live frame, binder page, and the pull / battle / collection drains are "
          "all on the cached path",
          all(any(f in l for l in sites)
              for f in ("live_api", "binder", "pulls", "battles", "collection")))

    print("\n-- 4. shared PokeWallet client --")
    prev = os.environ.get("POKEWALLET_BASE_URL")
    await pw.close_shared_client()
    c1 = await pw.shared_client()
    c2 = await pw.shared_client()
    check("the same object on every call — one connection pool, one TLS session",
          c1 is c2, f"id={id(c1)} vs {id(c2)}")
    check("30s timeout preserved from the throwaway clients",
          c1.timeout.read == 30.0 and c1.timeout.connect == 30.0, str(c1.timeout))
    check("bound to the env base url",
          str(c1.base_url).rstrip("/") == pw._base_url().rstrip("/"),
          f"{c1.base_url} vs {pw._base_url()}")
    os.environ["POKEWALLET_BASE_URL"] = "http://127.0.0.1:59999"
    c3 = await pw.shared_client()
    check("a changed POKEWALLET_BASE_URL rebuilds it — tests point this at a local "
          "stub mid-process",
          c3 is not c1 and str(c3.base_url).rstrip("/") == "http://127.0.0.1:59999")
    check("and the superseded client was closed, not leaked", c1.is_closed)
    await pw.close_shared_client()
    check("close_shared_client() closes it and clears the slot",
          c3.is_closed and pw._shared_client is None)
    await pw.close_shared_client()
    check("closing an already-closed/absent client is a no-op, so the lifespan "
          "hook cannot raise on a double shutdown", True)

    conns: list[int] = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b'{"results": [], "pagination": {}, "metadata": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class S(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def process_request(self, request, addr):
            conns.append(1)          # one per ACCEPTED TCP connection
            super().process_request(request, addr)

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["POKEWALLET_BASE_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
    await pw.close_shared_client()
    t = time.perf_counter()
    for _ in range(10):
        await pw.search_cards("pikachu 25", api_key="probe")
    shared_ms, shared_conns = (time.perf_counter() - t) * 1000, len(conns)
    conns.clear()
    t = time.perf_counter()
    for _ in range(10):
        async with pw.make_async_client() as c:
            await pw.search_cards("pikachu 25", api_key="probe", client=c)
    throwaway_ms, throwaway_conns = (time.perf_counter() - t) * 1000, len(conns)
    check(f"10 searches on the shared client opened {shared_conns} TCP connection(s); "
          f"10 throwaway clients opened {throwaway_conns}",
          shared_conns == 1 and throwaway_conns == 10)
    print(f"  wall: shared {shared_ms:.1f}ms vs throwaway-per-call {throwaway_ms:.1f}ms "
          f"— on localhost that gap is a socket; against api.pokewallet.io it is a "
          f"TLS handshake per call")
    await pw.close_shared_client()
    srv.shutdown()
    if prev is None:
        os.environ.pop("POKEWALLET_BASE_URL", None)
    else:
        os.environ["POKEWALLET_BASE_URL"] = prev
    finish("caches")


# --- loops: every cache survives a SECOND event loop, under contention ---------
def section_loops() -> None:
    """WHY THIS EXISTS. `asyncio.Lock` binds to the first event loop that ever
    BLOCKS on it and raises `RuntimeError: bound to a different event loop` for
    every other one — demonstrated below rather than asserted. A process
    routinely has more than one loop (a TestClient per test, a script calling
    asyncio.run twice) and these caches EXPIRE, so a second loop can reach the
    slow path. That is why none of the three DB caches takes a single-flight lock.

    It also separates two things that look alike: the caches are loop-agnostic,
    while the SQLAlchemy engine underneath them is NOT — and that second fact is
    pre-existing, so the last part re-runs the same stress using nothing but
    `async_session_maker`."""
    import app.cards as cards
    import app.pack.identify_core as ic
    import app.pokewallet as pw
    import app.prices as prices
    from app.db.session import async_session_maker

    # 0. The hazard itself, on a bare lock — the reason for the design.
    lock = asyncio.Lock()

    async def contend():
        async def hold():
            async with lock:
                await asyncio.sleep(0.01)
        await asyncio.gather(hold(), hold(), hold())

    asyncio.run(contend())
    try:
        asyncio.run(contend())
        check("a module-level asyncio.Lock survives contention on a second loop",
              False, "it did NOT raise here — re-examine the no-lock rationale")
    except RuntimeError as e:
        check("a CONTENDED module-level asyncio.Lock raises on a second event "
              "loop — so none of the three DB caches takes one", True, str(e))

    async def round_trip(tag: str, cold: bool) -> dict:
        if cold:
            cards._numerators_cache.clear()
            ic._set_id_map = None
            prices.invalidate_price_cache()
        await pw.close_shared_client()
        nums = await asyncio.gather(*(cards.get_set_numerators("23876")
                                      for _ in range(9)))
        ids = await asyncio.gather(*(ic._pw_set_id_for("sv06") for _ in range(9)))

        async def one_price():
            # A session per caller, as every real caller has — one AsyncSession
            # cannot serve concurrent operations.
            async with async_session_maker() as s:
                return await prices.latest_price_map(s)

        maps = await asyncio.gather(*(one_price() for _ in range(9)))
        clients = await asyncio.gather(*(pw.shared_client() for _ in range(9)))
        out = {"numerators": len(nums[0]), "same_nums": all(n == nums[0] for n in nums),
               "set_id": ids[0], "same_ids": len(set(ids)) == 1,
               "asof": maps[0][1], "same_maps": all(m[1] == maps[0][1] for m in maps),
               "one_client": len({id(c) for c in clients}) == 1}
        await pw.close_shared_client()
        print(f"  loop {tag} (cache {'cold' if cold else 'warm'}): {out}")
        return out

    a = asyncio.run(round_trip("A", cold=True))
    b = asyncio.run(round_trip("B", cold=False))
    # Loop A's cold read is the only part of this section that needs the database
    # to actually answer. Under heavy machine load it can time out, and
    # get_set_numerators then correctly degrades to empty (A1) — which is not a
    # cache defect but WOULD make the comparison below meaningless, so say so.
    check("loop A's cold read reached the database at all (an empty catalog here "
          "means a DB hiccup degraded to fail-closed, not a cache bug — re-run)",
          a["numerators"] > 0, f"numerators={a['numerators']}")
    check("nine concurrent callers per cache on a SECOND event loop — the shape a "
          "pytest run has, with the module caches still warm — answer identically "
          "and raise nothing", a == b, f"{a} vs {b}")
    check("and all nine concurrent callers agreed with each other, on both loops",
          all(a[k] and b[k] for k in
              ("same_nums", "same_ids", "same_maps", "one_client")))
    check("the shared httpx client is rebuilt for the new loop rather than reused "
          "across a dead one", b["one_client"])

    # The specific trap: CPython hands successive asyncio.run() calls the SAME
    # loop id(), so a cache keyed on id() would call a brand-new loop "current"
    # and serve it the dead loop's socket pool. Nothing is closed between these
    # loops on purpose — the module must notice by itself.
    #
    # Whether the freed loop's address comes straight back is the allocator's
    # call, so ONE pair reproduces the collision only ~80% of runs on this
    # machine — which made this section flake when it was a two-run pair. The
    # claim is "id() CAN collide, so keying a cache on it is unsafe", and a
    # single collision anywhere in a short series establishes exactly that. The
    # fresh-client guarantee that actually protects the cache is checked on
    # EVERY consecutive pair either way, so nothing is weakened by the retry.
    seen: list = []

    async def grab():
        seen.append((id(asyncio.get_running_loop()), await pw.shared_client()))

    for _ in range(8):
        asyncio.run(grab())
        if len({s[0] for s in seen}) < len(seen):
            break
    ids = [s[0] for s in seen]
    check("successive asyncio.run() loops really do reuse the same id() — which is "
          "why the client is keyed on a weakref to the loop OBJECT, not its id",
          len(set(ids)) < len(ids), f"{len(ids)} loops, ids {ids}")
    check("and every new loop is handed a FRESH client rather than the previous "
          "loop's dead pool",
          all(a[1] is not b[1] for a, b in zip(seen, seen[1:])),
          f"clients {[id(s[1]) for s in seen]}")
    asyncio.run(pw.close_shared_client())

    # The engine underneath is loop-bound, and always was. Same stress, no caches.
    async def raw_db():
        from sqlalchemy import text

        async def one():
            async with async_session_maker() as s:
                return (await s.execute(text("SELECT 1"))).scalar()
        return await asyncio.gather(*(one() for _ in range(9)))

    try:
        asyncio.run(raw_db())
        print("  note: a cold DB checkout on a fresh loop worked here.")
    except Exception as e:
        print(f"  note: nine cold DB checkouts on a SECOND loop fail with "
              f"{type(e).__name__}: {str(e)[:90]}... — that is the module-level "
              f"engine in app/db/session.py, PRE-EXISTING and unrelated to these "
              f"caches (this block touches none of them). It is also why 'add a "
              f"lock so the second loop only queries once' would not have helped.")
    finish("loops")


# --- page: the DB round trips a real binder page makes --------------------------
async def section_page() -> None:
    """Binder page wall time is 87% OCR (Task 1) and swings +-15% on this machine,
    so it cannot resolve a change of a few tens of milliseconds. SQL STATEMENTS
    CAN BE COUNTED EXACTLY, and that is what these caches remove, so this section
    counts them per page instead of timing them.

    Uses nothing but a SQLAlchemy event hook, so it runs unchanged on the
    pre-change tree for the before numbers."""
    from sqlalchemy import event

    from app.db.session import engine
    from app.pack.binder import scan_binder_page

    counts: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        counts.append(" ".join(statement.split())[:70])

    pages = sorted((REPO / "tests" / "corpus" / "binder" / "real").glob("page_*.jpeg"))
    print(f"  SQL statements issued per binder page ({len(pages)} real fixtures):")
    total_first = total_rest = 0
    for i, p in enumerate(pages):
        counts.clear()
        await scan_binder_page(p.read_bytes())
        n = len(counts)
        by_kind: dict[str, int] = {}
        for s in counts:
            key = ("set_id_map" if "set_id_map" in s else
                   "tcgdex_card" if "tcgdex_card" in s else
                   "card_price/snapshot" if "card_price" in s or "price_snapshot" in s
                   else "card" if " card " in s or "FROM card" in s else "other")
            by_kind[key] = by_kind.get(key, 0) + 1
        print(f"    {p.name:<14} {n:>4} statements  {by_kind}")
        total_first += n if i == 0 else 0
        total_rest += n if i else 0
    print(f"  first page {total_first}, remaining {len(pages) - 1} pages "
          f"{total_rest} ({total_rest / max(1, len(pages) - 1):.1f} each) — the "
          f"gap between them IS the memo, since a cold process pays the first one "
          f"either way")
    print("\npage: PASS (measurement only)")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "equivalence"
    if cmd == "dump":
        asyncio.run(section_dump(sys.argv[2] if len(sys.argv) > 2 else "dump.json"))
    elif cmd == "identity":
        section_identity(sys.argv[2] if len(sys.argv) > 2 else BASE_REV)
    elif cmd == "equivalence":
        asyncio.run(section_equivalence())
    elif cmd == "latency":
        asyncio.run(section_latency())
    elif cmd == "concurrency":
        asyncio.run(section_concurrency())
    elif cmd == "caches":
        asyncio.run(section_caches())
    elif cmd == "loops":
        section_loops()
    elif cmd == "page":
        asyncio.run(section_page())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
