# Crowd-Sourced Pull-Rate Statistics — Design Spec

**Date:** 2026-06-24
**Status:** Approved pending user review
**Sub-project:** C (third of six; A = pack scanner, B = auth + DB foundation, both shipped on `main`)

## Product context

Sub-project B lets trainers save verified pulls (a pack of cards + a globally-unique code card) to Postgres. Sub-project C turns that accumulating data into **crowd-sourced pull-rate statistics**: per-card and per-rarity rates per set, computed periodically, trustworthy against spoofing, seeded with an approximate prior that real submissions progressively override, and watched for statistical anomalies — all surfaced through a **role-gated analyst dashboard** (not public).

This is the analytics layer the later sub-projects (pack battles' "how good is your pack", pricing context) build on. It also closes the trustworthiness gap B explicitly deferred (client-submitted card data was trusted for verified pulls).

## Decisions log

| Decision | Choice |
|---|---|
| Scope | core stats **+** trustworthiness (server re-derivation) **+** scraped prior **+** anomaly tracking — all four in v1 |
| Computation | periodic **batch** → materialized stat tables + per-run **snapshots** (not live) |
| Aggregation | per-card rate **+** per-rarity rollups **+** sample size. "Pull rate" = fraction of a set's verified packs containing the card |
| Access | data is **role-restricted, not public**; 3-tier role enum `trainer｜analyst｜admin`; admin grants roles; first admin via bootstrap script |
| Surface | a separate **role-gated dashboard** in the SPA |
| Trustworthiness | **batch server-side re-derivation** of verified pulls from stored staircase photos; stats use only server-derived cards |
| Prior | curated **seed file** behind a pluggable `PriorSource`; **Beta-Binomial** pseudo-count blend; live scraper deferred |
| Anomalies | **deviation-from-prior** + **submitter-concentration** detectors; flag-for-review; snapshot-shift deferred |
| Scheduling | batch runs **inside the web service** (volume access); Railway **cron service triggers it via token**; manual admin recompute endpoint |
| Tests | **none** (per standing user directive); manual smoke verification only |

## Scope

### In scope
- `trainer.role` enum + role-based authorization dependencies + admin grant endpoint + bootstrap script.
- Server-side re-derivation of verified pulls (`pull_card_derived`); `pull.capture_meta` persisted so re-derivation stays guided-accurate.
- Batch pipeline: re-derive → aggregate (set/card/rarity) → blend prior → detect anomalies → snapshot.
- Materialized stat tables + snapshots; `PriorSource` (seed-file impl) + Beta-Binomial blend.
- Two anomaly detectors; anomaly triage (review/dismiss).
- Stats + admin API; role-gated dashboard in the SPA.
- Railway cron service (token-triggered) + manual admin recompute; one Alembic migration.

### Out of scope (explicitly)
Automated tests; a live Reddit/social scraper (the `PriorSource` interface makes it a later drop-in); snapshot-over-time shift detection; public/trainer-facing pull-rate display; pricing, battles, Pokédex; co-occurrence stats; an audit table (role changes log to the app logger only).

## Architecture

Single FastAPI service (unchanged deployment shape) + one new thin Railway cron service that only triggers the batch over HTTP. New code under `app/stats/`; auth/role additions in `app/db/`; a dashboard area in the SPA. The scanner pipeline (`app/pack/`) and B's save path are untouched except the small `capture_meta` persistence addition to `/pulls`.

```
app/
  db/
    models.py     # MODIFY: trainer.role; pull.capture_meta + derive_status/derived_at;
                  #         new: PullCardDerived, StatsSnapshot, SetStat, RarityStat, CardStat, Anomaly
    users.py      # MODIFY: require_analyst, require_admin deps; /users/me returns role
  pulls.py        # MODIFY: persist capture_meta on save
  stats/
    __init__.py
    config.py     # min_sample, z threshold, concentration threshold, prior strength, cron token
    rederive.py   # verified pulls -> scan_pack on stored staircase -> pull_card_derived
    aggregate.py  # derived cards -> set/card/rarity counts for a snapshot
    prior.py      # PriorSource interface + SeedFilePriorSource + beta_binomial_blend()
    anomaly.py    # deviation_from_prior + submitter_concentration detectors
    run_batch.py  # run_batch(trigger) orchestrator + `python -m app.stats.run_batch` CLI
    data/priors.json
  admin.py        # /admin/trainers, /admin/trainers/{id}/role, /admin/stats/recompute
  stats_api.py    # /stats/sets, /stats/sets/{id}, /stats/anomalies (+ PATCH)
  main.py         # MODIFY: include admin + stats routers
alembic/versions/0002_*.py   # role, capture_meta, derived cards, stats + anomaly tables
scripts/grant_role.py        # bootstrap: set a trainer's role by email
frontend/src/
  api.ts          # MODIFY: stats + admin client fns; Trainer gains role
  dashboard/{Dashboard,SetStats,Anomalies,RoleAdmin}.tsx
  App.tsx         # MODIFY: role-gated /dashboard nav
deploy: a Railway cron service running a curl/script that POSTs /admin/stats/recompute with STATS_CRON_TOKEN
```

### Data flow (batch run)

```
trigger (cron token POST  OR  admin click  OR  CLI)
  → run_batch(trigger):
     1. rederive: for each verified pull with derive_status='pending':
          load stored staircase photo → scan_pack (guided via pull.capture_meta, else ungrided)
          → write pull_card_derived rows → derive_status='done' (or 'failed')
     2. open StatsSnapshot(status='running')      [advisory lock: no overlapping run]
     3. aggregate: from pull_card_derived of verified pulls →
          per set: verified_pack_count; per card(match_id): hits/packs; per rarity: packs_with_rarity
     4. blend: PriorSource α/β → blended_rate = (α+hits)/(β+packs)
     5. anomaly: deviation_from_prior + submitter_concentration (sets/cards with packs ≥ min_sample)
     6. snapshot.status='done'  → becomes "current"
dashboard reads the current snapshot's SetStat/CardStat/RarityStat + open Anomaly rows
```

## Data model (Alembic `0002`)

**`trainer`** (modify): add `role` enum `trainer｜analyst｜admin` not null default `trainer`. `admin` ⇒ treated as superuser for admin guards.

**`pull`** (modify): add `capture_meta` JSONB null (guide positions etc., persisted from `/pulls`); `derive_status` enum `pending｜done｜failed` not null default `pending`; `derived_at` timestamptz null.

**`pull_card_derived`** (new): `id`, `pull_id` FK→pull (cascade, indexed), `row_index`, `card_number`, `set_id`, `set_code`, `set_name`, `name`, `rarity`, `match_id` (PokéWallet id, nullable), `confidence`. Server-authoritative; stats read only this.

**`stats_snapshot`** (new): `id`, `created_at`, `trigger` (`cron｜manual｜cli`), `status` (`running｜done｜failed`).

**`set_stat`** (new): `id`, `snapshot_id` FK (indexed), `set_id`, `verified_pack_count`, `computed_at`.

**`rarity_stat`** (new): `id`, `snapshot_id` FK, `set_id`, `rarity`, `packs_with_rarity`, `raw_rate`, `blended_rate`.

**`card_stat`** (new): `id`, `snapshot_id` FK, `set_id`, `match_id` (card key), `card_number`, `name` (denormalized for display), `hits`, `packs`, `raw_rate`, `blended_rate`.

**`anomaly`** (new): `id`, `snapshot_id` FK, `detector` (`deviation_from_prior｜submitter_concentration`), `target_type` (`set｜card`), `set_id`, `card_match_id` null, `severity` float, `detail` JSONB (observed/expected/z or concentration %), `status` (`open｜reviewed｜dismissed`, default `open`), `created_at`.

"Current" stats = the latest `done` snapshot. Older snapshots retained for trends / future snapshot-shift detection. Per-card stats key on `match_id`; derived cards that didn't match PokéWallet are excluded from per-card stats but still count toward the pack's sample size and per-rarity rollups (when rarity is known).

## Batch pipeline (`app/stats/`)

- **rederive.py** — selects verified pulls (`verified=true AND derive_status='pending'`), loads each stored staircase via `app.storage.open_photo`, runs `app.pack.pipeline.scan_pack(staircase_bytes, code_bytes=b"", capture_meta=pull.capture_meta)` to get server cards, writes `pull_card_derived`, marks `done`/`failed`. PokéWallet calls rate-limited; only pending pulls processed (idempotent, incremental). The code card isn't re-OCR'd here (verification already settled in B).
- **aggregate.py** — a pull's set = modal `set_id` of its derived cards. Per set: `verified_pack_count`; per card: `hits` (packs containing that `match_id`), `packs` (= set's pack count), `raw_rate`; per rarity: `packs_with_rarity`, `raw_rate`.
- **prior.py** — `PriorSource.get(set_id, key) -> (alpha, beta)` (key = match_id or rarity). `SeedFilePriorSource` reads `data/priors.json` (per-rarity always; per-card where known). `beta_binomial_blend(hits, packs, alpha, beta) = (alpha+hits)/(beta+packs)`; missing prior ⇒ blended = raw.
- **anomaly.py** — `deviation_from_prior`: for entries with `packs ≥ min_sample`, expected = prior mean, SE from the prior/Binomial; flag `|z| > Z_THRESHOLD`. `submitter_concentration`: per set, `max_trainer_share = max over trainers of (their verified packs / set verified packs)`; flag `> CONCENTRATION_THRESHOLD` (only when set pack count ≥ min_sample). Writes `anomaly` rows.
- **run_batch.py** — orchestrates 1–6 under a Postgres advisory lock (overlap guard); `run_batch(trigger: str)`; CLI `python -m app.stats.run_batch` for local dev.

## Roles, access & granting

- `require_analyst` (role ∈ {analyst, admin}) guards `/stats/*`; `require_admin` (role == admin) guards `/admin/*`. Both 403 on insufficient role. Implemented in `app/db/users.py` over the FastAPI-Users `current_active_user`.
- `/users/me` returns `role` (frontend gates the dashboard nav; API still enforces).
- `PATCH /admin/trainers/{id}/role` + `GET /admin/trainers?query=` (admin-only). Role changes log who/whom/old→new to the app logger.
- `scripts/grant_role.py <email> <role>` bootstraps the first admin (run locally or via `railway run`). No DB writes in web startup.

## API & dashboard

**Stats API** (`require_analyst`, current snapshot): `GET /stats/sets`; `GET /stats/sets/{set_id}`; `GET /stats/anomalies?status=open`; `PATCH /stats/anomalies/{id}` `{status}`.
**Admin API** (`require_admin`): `POST /admin/stats/recompute` (also accepts the `STATS_CRON_TOKEN` bearer; runs `run_batch` as a background task, returns 202); `PATCH /admin/trainers/{id}/role`; `GET /admin/trainers?query=`.
**Dashboard** (`frontend/src/dashboard/`, role-gated `/dashboard`): overview (sets, sample size, freshness, admin "Recompute now"); set detail (card-rate table raw vs blended + rarity-odds panel); anomaly triage (review/dismiss); role admin (admin-only). Nav link only when role ∈ {analyst, admin}.

## Deployment & scheduling

**Volume constraint:** a Railway volume attaches to one service (the web service, which writes pull photos). Re-derivation reads those photos, so the batch runs **inside the web service**. The Railway **cron service** is thin: it `curl`s `POST /admin/stats/recompute` with `STATS_CRON_TOKEN`; the actual run happens in-web (volume access). Manual admin recompute uses the same endpoint via cookie. An advisory lock prevents overlapping runs.

**Config/env:** `STATS_CRON_TOKEN` (required for cron trigger), `PACK_STATS_MIN_SAMPLE` (default e.g. 30), `PACK_STATS_Z_THRESHOLD`, `PACK_STATS_CONCENTRATION` (e.g. 0.5), prior default strength. Migration `0002` applies via the existing `preDeployCommand` (`alembic upgrade head`).

## Verification (manual smoke — no automated tests)

1. `alembic upgrade head` applies `0002`; role/capture_meta columns + 5 new tables present.
2. `grant_role.py` makes an admin; admin grants an analyst; `trainer`→403 on `/stats/*`, analyst→200 + sees dashboard.
3. Seed several verified pulls (reuse B's flow); `POST /admin/stats/recompute`; `set_stat/card_stat/rarity_stat` populate; dashboard shows raw + blended rates and sample sizes.
4. **Trustworthiness:** save a verified pull with deliberately spoofed client cards → after recompute, `pull_card_derived` differs from `pull_card`, and stats reflect the derived (real) cards, not the spoof.
5. **Prior:** a low-N card's blended rate sits near its prior; as N grows it approaches raw.
6. **Anomalies:** craft a deviation case and a single-trainer concentration case → `anomaly` rows appear in triage; mark one reviewed/dismissed.
7. **Cron trigger:** `POST /admin/stats/recompute` with the `STATS_CRON_TOKEN` bearer (no cookie) starts a run; a second concurrent trigger is a no-op (advisory lock).
8. Railway: cron service triggers recompute on cadence; dashboard freshness updates (user action on deploy).

## Success criteria
- Batch runs end-to-end (re-derive → aggregate → blend → anomalies → snapshot) and the dashboard serves the current snapshot to analysts only.
- Stats are computed from server-derived cards (spoofed client data does not move the numbers).
- Blended rates behave correctly (prior-dominated at low N, data-dominated at high N).
- Both anomaly detectors produce reviewable rows on crafted cases.
- Roles enforced everywhere (trainer 403, analyst read, admin grant/recompute); first admin bootstrappable.
- Migrations apply from B's schema; scanner + B flows unaffected.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Re-derivation re-runs `scan_pack` (PokéWallet calls) per pull → slow/expensive at scale | Incremental (only `pending` pulls), rate-limited, cached in `pull_card_derived`; runs off the web critical path (background) |
| Batch needs volume → can't be a standalone cron process | Batch runs in the web service; cron only triggers via token (designed in) |
| Ungrided re-derivation would bias hit counts | Persist `pull.capture_meta`; re-derive guided; ungrided only for legacy pulls without it (logged) |
| Small samples → noisy/ misleading rates | `min_sample` gate before display/anomaly; Beta-Binomial prior smooths low N |
| Spoofed verified pulls skew stats | Server re-derivation (stats ignore client cards) + submitter-concentration anomaly |
| Live scraping fragility/ToS | Deferred; `PriorSource` seed file now, scraper a later drop-in |
| Overlapping batch runs (cron + manual) | Postgres advisory lock + "running" snapshot status |
| `STATS_CRON_TOKEN` leak ⇒ anyone can trigger recompute | Recompute is idempotent and non-destructive (recomputes a snapshot); token kept secret; endpoint also rate-limited by the overlap guard |
