# Acceptance probes

One-shot regression and measurement scripts for behaviour the pytest suite
deliberately does not cover. The suite stays small and VLM-free (7 passed /
1 skipped) and `binder_gate.py` scores accuracy on committed fixtures; these
probes cover everything in between — the VLM follow-up path, the binder page
prior, the OCR gate, the name-index caches and the collection save guard.

Each probe is a script, not a test: it prints its evidence and **exits 0 only
when every assertion held**. Several print measurements alongside the
assertions, so read the output rather than just the exit code when you are
using one as a before/after instrument.

Two of these are the *only* coverage of their feature: `task-6-probe.py` for
the A3 binder page-level set prior (no committed fixture ever elects a prior,
so the gate cannot see it at all), and `task-11-probe.py` for the collection
save guard and the degenerate `identity_key`.

## Running

Same environment as `docs/acceptance/binder_gate.py` — exported in the shell,
there is no `.env` file. Run from the repo root; `PYTHONPATH=.` is what puts
`app` on the import path. Fixture paths inside each probe are resolved from
the file's own location, so they do not depend on the working directory, but
the import path still does.

```sh
cd <repo root>
export PYTHONPATH=. \
  DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \
  AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \
  PHOTO_STORAGE_DIR=./var/pulls \
  COOKIE_SECURE=false

.venv/bin/python docs/acceptance/probes/task-4-probe.py
.venv/bin/python docs/acceptance/probes/task-9-probe.py caches
```

Every probe needs a running Postgres with the catalog loaded — the same one
the gate uses. Nothing else has to be started: the probes that need HTTP drive
the real app through `TestClient`, and the ones that need a VLM start
`tests/vlm_stub.py` in-process on a free port.

## The probes

### `task-4-probe.py` — the VLM is off the response path

No subcommands. Starts `tests/vlm_stub.py` with a 5s delay and drives the real
`/scan/binder`, `/scan/pack` and `/scan/pack/stream` endpoints: each must
answer in scan-time rather than scan-time + 5s, carry a top-level `scan_id`
and `state="pending_vlm"` on flagged rows, and patch those rows through the
`GET /scan/{binder,pack}/{scan_id}` follow-up. Also checks the two scan kinds
do not resolve through each other's follow-up route, and that with the VLM
disabled the responses carry neither `scan_id` nor `state`.

### `task-5-probe.py` — warm-up ping, per-kind prompt, binder hints

No subcommands. Same stub-and-`TestClient` shape as task 4, for: `kick()`
firing one warm-up ping per debounce window across four scan entry points;
the worker receiving `kind="strip"` for a pack and `kind="full_card"` for a
binder page; a page with >=2 confident same-set cells sending `hint_set` /
`hint_denominator` and a page without sending `None`; and
`runpod_worker.prompts.build_prompt` producing a byte-identical strip prompt.

### `task-6-probe.py` — the A3 binder page-level set prior

No subcommands; sections A–F run in one pass. The prior is identity-deciding
inside `resolve_identity`, so a prior handed to the wrong cell mints a
confident-wrong identity — and no committed fixture elects one, which is why
this file exists. Covers the `_prior_denominator_ok` veto case by case, the
`_apply_page_prior` pass-2 loop over real pass-1 results (rescue, veto,
tie-page no-op), a structural proof that pass-1 confident cells are
unreachable from the write path, the `_vlm_payload` exclusion of rescued
cells, and forced priors over the five committed real binder photographs.

Section D also builds a synthetic single-set sv06 page end-to-end. It fetches
card art from `assets.tcgdex.net`, caches under `$TASK6_ASSET_CACHE`
(default `$TMPDIR/pcs-task6-assets`), and SKIPs cleanly when offline. The
generated page is written to a temporary directory and is never committed.

### `task-7-probe.py` — bare numerators and the identity ladder

No subcommands. The bare-reading rules: which module may mint one (asserted
on the module set, so `app/pack/binder.py` and nowhere else), the `pattern_ok`
consumers, the promo-prefix handling, and the pass-2 interaction. Section G
runs real pixels: page_1's weakness-row "232" must stay unread even under the
higher-resolution strip retry, and page_3 must come back 9/9 confident with
no cell resting on a bare numerator.

### `task-8-probe.py` — eager init, the OCR init race, gate scoping

Measurement harness; every section is a subcommand and runs in its own
process on purpose, because everything measured is a first-use cost that a
warm import would erase.

| subcommand | what it shows |
| --- | --- |
| `stall` | cold-start cost inside `_apply_constraints` (deferred imports + first `get_set_numerators`) vs warm |
| `firstscan cold` \| `firstscan warm` | first binder scan of a process, with and without the lifespan warm task |
| `lock` | the RapidOCR init race: 8 threads from a barrier, old algorithm (7/8 dropped reads) vs new |
| `health` | the real lifespan through `TestClient` — `/health` answers while the warm task is still running |
| `audit` | the catalog warm-up log, and the same run with every promo `local_id_prefixed` flag flipped |
| `gate` | who holds an `OCR_GATE` slot during one warm pack scan |
| `contention` | a binder page against three live frames — slot waits, holds, and live-frame occupancy |

### `task-9-probe.py` — name-index caches, precompute, threaded matching

Measurement and behaviour-identity harness; every section is a subcommand.

| subcommand | what it shows |
| --- | --- |
| `dump <out.json>` | the 500-query corpus through `NameIndex.match` / `match_in_set`, serialized. Depends on nothing the change added, so it runs unmodified on an older tree |
| `identity [base_rev]` | `dump` here and on `base_rev` (default `bf212ed`) under four `PYTHONHASHSEED`s, asserting every field identical |
| `equivalence` | the precomputed structures vs a brute-force recomputation of the code they replaced, over all ~2.4k names and 53 sets |
| `latency` | per-match latency, cold and warm, for `match()` and `match_in_set()` |
| `concurrency` | event-loop tick lateness during a match storm, inline vs `to_thread` |
| `caches` | the four caches: numerator TTL, `set_id_map` preload and fallback, price-map staleness, shared HTTP client reuse and shutdown |
| `page` | SQL statements issued per real binder page — the DB memos counted rather than timed |
| `loops` | every cache driven from two event loops in one process — the case that rules out a module-level `asyncio.Lock` around a cache miss |

`identity` checks `base_rev` out into a scratch git worktree at
`$TMPDIR/task9-base` and leaves it there for reuse. Clean it up with
`git worktree remove $TMPDIR/task9-base` when you are done with it.

`loops` prints asyncpg "Event loop is closed" tracebacks during teardown.
Those are the pre-existing loop-bound module engine in `app/db/session.py`,
which the probe names explicitly in its own output; they do not affect the
verdict line.

### `task-11-probe.py` — the save guard and the degenerate identity key

| subcommand | what it shows |
| --- | --- |
| `save` | the real confirm path through `TestClient` on a throwaway trainer row: every card `confirmed: false`, one flagged card `confirmed: true`, an old-client payload with the field absent, and a re-scan checking qty increments only for cards actually saved |
| `collision` | the degenerate key at both consumers — `identity_key()` against the expression it replaced, live dedup via `add_frame_result`, and the collection upsert refusing any cell with an empty right-hand side |

`collision` runs its three sections (`keys`, `live`, `collection`) in separate
processes, because the app's async engine binds to the first event loop that
uses it; those names also work as direct subcommands. `save` creates a trainer
row and deletes it at the end, taking the collection rows with it via
`ON DELETE CASCADE`.
