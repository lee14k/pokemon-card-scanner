# Batched Pricing Snapshots — Design Spec

**Date:** 2026-07-09
**Status:** Approved pending user review
**Sub-project:** E (fifth of six; A scanner, B auth/DB, C pull-rate stats, D Pokédex — all shipped on `main`)

## Product context

The vision: *"Only put prices up every week or month, segregate and track data for statistical anomalies."* PokéWallet lookups already return TCGPlayer and Cardmarket price blobs on every card hit — the scanner pipeline currently discards them. Sub-project E captures those prices on a deliberate weekly/monthly cadence as snapshots, shows trainers what their pulls are worth, and flags suspicious price jumps to analysts.

## Decisions log

| Decision | Choice |
|---|---|
| Coverage | **Cards trainers have actually pulled** (distinct match_ids in `pull_card` ∪ `pull_card_derived`) — not whole-set catalogs |
| Cadence & trigger | **Staleness-gated stage inside the existing nightly batch** (`run_batch`): prices refresh only when the newest snapshot is older than `PRICE_SNAPSHOT_INTERVAL_DAYS` (default **7**; 30 ⇒ monthly). No new cron/service/token. |
| Surface | **Trainer-facing pull values** (My Pulls estimated value + per-card prices, labeled "prices as of \<date\>") **+ analyst price-jump anomalies** in the existing dashboard triage |
| Storage | **Snapshot tables** (`price_snapshot` header + `card_price` rows, raw blobs retained as JSONB) — history enables anomaly detection; no live fetching, no denormalizing onto pull rows |
| Price fields | USD market **low/high** (min/max of TCGPlayer `market_price` across subtypes — physical variant unknown) + Cardmarket `trend`; `estimated_value` uses per-card **midpoints** |
| Anomalies | **`price_jump`** detector: midpoint change ≥ `PRICE_JUMP_THRESHOLD` (default 0.5) between consecutive snapshots → rows in the **existing** `anomaly` table (dashboard triage needs zero changes) |
| Fetch client | Reuse `lookup_card_exact(set_id, numerator)` per card, rate-limited (`PRICE_LOOKUP_DELAY_MS`, default 200) — no new API surface |
| Tests | **None** (standing directive); manual smoke only |

## Scope

**In:** migration `0004` (two tables); `app/stats/pricing.py` stage wired into `run_batch`; three `StatsSettings` additions; read-time price enrichment of pull responses (`estimated_value`, `priced_as_of`, per-card `price_usd_low/high`); My Pulls value display + expandable per-card prices; stub-fixture price blobs (smokeability); `.env.example` docs.

**Out (explicitly):** automated tests; whole-set price catalogs; on-demand per-card refresh; live pricing; public/set-value pages; currency conversion or display beyond USD headline + EUR trend stored; price charts/history UI (data accumulates for later); any new Railway service.

## Data model (migration `0004`)

**`price_snapshot`**: `id` UUID PK, `created_at` timestamptz default now, `status` (`running｜done｜failed`). Latest `done` row = current prices; its `created_at` = the trainer-visible "prices as of" date and the staleness clock.

**`card_price`**: `id` UUID PK, `snapshot_id` FK→price_snapshot (cascade, indexed), `match_id` text (indexed), `set_id`, `card_number`, `name` (denormalized), `usd_market_low` float null, `usd_market_high` float null, `eur_trend` float null, `raw` JSONB (both source blobs). Null price fields = source missing for that card.

## Batch pricing stage (`app/stats/pricing.py`)

Called from `run_batch` after anomaly detection, inside the existing advisory lock:

1. **Staleness gate:** newest `done` price_snapshot younger than `PRICE_SNAPSHOT_INTERVAL_DAYS` ⇒ log + return (the nightly batch simply skips pricing most nights).
2. **Card universe:** distinct `(match_id, set_id, card_number, name)` with non-null match_id from `pull_card` ∪ `pull_card_derived`.
3. Open `price_snapshot(running)`. Per card: `lookup_card_exact(set_id, numerator)` over a shared client with `PRICE_LOOKUP_DELAY_MS` between calls; extract min/max TCGPlayer `market_price` across subtype rows and Cardmarket `trend`; insert `card_price` (misses/errors ⇒ null-price row, logged; never abort).
4. **`price_jump` detection:** for cards in both this and the previous `done` snapshot, midpoint pct-change ≥ threshold ⇒ `anomaly(detector='price_jump', target_type='card', set_id, card_match_id, severity=|pct|, detail={old, new, pct, from_snapshot, to_snapshot})` attached to the current batch's stats snapshot — surfaces in the existing analyst triage untouched.
5. Mark `done`; any stage failure marks the price snapshot `failed` without failing the stats batch.

Config (`StatsSettings`): `PRICE_SNAPSHOT_INTERVAL_DAYS=7`, `PRICE_JUMP_THRESHOLD=0.5`, `PRICE_LOOKUP_DELAY_MS=200`.

## API & frontend

- Read-time enrichment helper: latest `done` snapshot's `card_price` rows → `match_id → (low, high)` map. `PullOut` gains `estimated_value: float | None` (sum of midpoints of priced cards; None if none priced) and `priced_as_of: str | None`; `CardOut` gains `price_usd_low/price_usd_high: float | None`. Applied to save/list/detail uniformly. No new endpoints; no role changes.
- **My Pulls:** rows show `≈ $X.XX · prices as of <date>` when priced; rows click-expand to list cards with name + price ("—" unpriced). `api.ts`: `SavedPull` + optional price fields on `PackCard` (scan responses omit them).
- **Dashboard:** zero changes — `price_jump` anomalies render through the existing generic triage.

## Fixtures

`scripts/make_test_fixtures.py` adds sample `tcgplayer`/`cardmarket` blobs to the 3 stub cards (markets 1.25 / 3.50 / 10.00) and `tests/fixtures/pokewallet_cards.json` is regenerated — the pricing flow is smokeable end-to-end against the stub.

## Verification (manual smoke — no automated tests)

1. `alembic upgrade head` applies `0004`; both tables present.
2. Save a pull (stub) → run batch with `PRICE_SNAPSHOT_INTERVAL_DAYS=0` → `price_snapshot` done; `card_price` rows carry the stub numbers.
3. My Pulls shows `≈ $…` + as-of date; expansion lists per-card prices.
4. Immediate second batch run **skips** pricing (staleness gate logged).
5. Bump a stub price, force a run with `PRICE_JUMP_THRESHOLD=0.01` → `price_jump` anomaly visible in dashboard triage.
6. Scanner suite green; frontend builds; no leftover processes.

## Success criteria

- Prices refresh on the configured cadence only; trainers see pull values labeled with the snapshot date.
- Price history accumulates per snapshot; jumps beyond threshold reach analyst triage.
- One PokéWallet lookup per pulled card per snapshot (bounded, rate-limited); pricing failure never breaks the stats batch.
- No regression to scanner/auth/pulls/stats/dex; no new infra.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| PokéWallet lookups scale with distinct pulled cards | Bounded universe (pulled cards only), rate-limited, weekly cadence; misses degrade to null rows |
| Variant ambiguity (Normal vs Holofoil) | Stored as low/high range; midpoint for totals; raw blob kept for smarter handling later |
| Price source gaps (card missing on one market) | Nullable fields; estimated_value sums only priced cards |
| Anomaly noise on cheap cards (tiny base → huge %) | Threshold on pct change; analysts triage/dismiss; tune threshold via env |
| Pricing failure mid-run | Snapshot marked `failed`; stats batch unaffected; next eligible run retries |
