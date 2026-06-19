# Auth + DB Foundation — Design Spec

**Date:** 2026-06-18
**Status:** Approved pending user review
**Sub-project:** B (second of six; A = pack scanner retool, shipped on `main`)

## Product context

The pack scanner (sub-project A) is stateless: scan a staircase photo + code card → identified cards, no persistence, no accounts. Sub-project B adds the **foundation every later feature needs**: trainer accounts, a Postgres database, and persistence of scanned packs ("pulls") tied to a trainer — including the duplicate-code-card anti-fraud rule that was explicitly deferred from A "to the DB / sub-project B."

Downstream sub-projects (crowd-sourced pull-rate stats, Pokédex, pack battles, pricing) are all aggregations or features built on top of the saved pulls this sub-project introduces. The pull-rate *statistics* themselves are the next sub-project (C); B stops at storing pulls and the `verified` flag that gates stat eligibility.

## Decisions log

| Decision | Choice |
|---|---|
| Auth approach | **Python-native** (FastAPI-Users), single FastAPI service. **Better Auth was considered and rejected** — it is a TypeScript/Node library and would force a polyglot two-service architecture onto a Python backend. |
| Scope | Auth foundation **+ persist pulls + code-card uniqueness** (not auth-only) |
| Auth methods | Email + password only. **No email verification, no email provider** in v1. |
| Trainer identity | Email (login) **+ unique trainer handle** (public username) |
| Photos | **Store original photos** (staircase + code card) |
| Photo storage | **Railway Volume** (mounted disk, served through FastAPI), not S3/R2 |
| Duplicate/unreadable code | **Always save; `verified=true` only if code readable, well-formed, globally first-seen.** Duplicate/unreadable → saved `verified=false`. |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic; driver `asyncpg` |
| Session transport | httpOnly secure cookie + JWT strategy (stateless; no session table) |
| Automated tests | **None.** Per explicit user direction ("don't waste time on any tests"), verification is manual smoke only. |

## Scope

### In scope

- Trainer accounts: register / login / logout / `GET /users/me`, email + password, unique handle.
- Postgres via SQLAlchemy 2.0 async + Alembic migrations.
- Persist a confirmed pull (cards + code + photos) tied to the authenticated trainer.
- Global code-card uniqueness enforced as a DB invariant; `verified` flag per pull.
- Store original staircase + code-card photos on a Railway Volume, served owner-only.
- List/detail of the trainer's own pulls; authenticated photo serving.
- Railway: Postgres service + Volume + migrate-on-deploy.

### Out of scope (explicitly)

Automated tests of any kind; email verification / email provider; OAuth / passwordless; session revocation / "log out everywhere"; pull-rate statistics & aggregations (sub-project C); server-side re-derivation of card data for anti-spoofing (sub-project C); sharing pull photos with other trainers (needed later for pack battles); profile editing beyond what registration sets; pricing, battles, Pokédex.

## Architecture

Single FastAPI service (the existing app), extended with auth + persistence. The pack-scanner pipeline (`app/pack/`) is untouched.

```
app/
  main.py            # MODIFIED: include auth + pulls routers; startup ensures photo dir
  db/
    __init__.py
    config.py        # DATABASE_URL, AUTH_SECRET, PHOTO_STORAGE_DIR, COOKIE_SECURE
    session.py       # async engine + AsyncSession dependency
    models.py        # SQLAlchemy models: Trainer, Pull, PullCard
    users.py         # FastAPI-Users: UserManager, auth backend, schemas, deps
  pulls.py           # /pulls routes (save, list, detail, photo)
  storage.py         # volume read/write boundary (no fs access elsewhere)
alembic/
  env.py             # async Alembic, reads DATABASE_URL, targets models metadata
  versions/0001_*.py # initial schema + partial unique index
alembic.ini
```

Public (no auth): `/scan/pack`, `/sets`, `/cards/lookup`, `/health`, the SPA. Authenticated: everything under `/pulls`, `/users/me`.

### Data flow (save a pull)

```
SPA (logged in) — review screen "Looks good"
  → POST /pulls (multipart: staircase img, code_card img, cards JSON, capture_path, pack_confidence, segmentation_warning)
       │  cookie → current_active_user (trainer)
       ▼
  pulls.save_pull
    1. allocate pull_id (UUID)
    2. storage.save_pull_photos(trainer_id, pull_id, staircase_bytes, code_bytes) → paths
    3. read_code_card(code_bytes)  ← SERVER re-OCRs the code (authoritative)
    4. code_normalized = normalize(code)
    5. INSERT pull (verified = code present ∧ format_ok)
         on partial-unique-index conflict → retry INSERT verified=false
    6. INSERT pull_card rows from confirmed cards payload
    7. commit
  → PullResponse (id, verified, cards, code, ...)
```

## Data model

### `trainer` (extends FastAPI-Users `SQLAlchemyBaseUserTableUUID`)

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | FastAPI-Users default |
| email | text unique | login only, not shown publicly |
| hashed_password | text | argon2 (FastAPI-Users) |
| is_active / is_superuser / is_verified | bool | base columns; `is_verified` unused in v1 |
| handle | citext-style unique | public username; stored case-folded; CHECK: 3–20 chars `[a-z0-9_]` |
| created_at | timestamptz | server default now() |

Handle uniqueness is case-insensitive: store the lowercased handle in a unique column (a `lower()` functional unique index, or a normalized column). Keep the original-cased handle for display if desired (v1: handle is already lowercased on input, so one column suffices).

### `pull`

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | server-allocated before photo write |
| trainer_id | UUID FK→trainer | indexed |
| created_at | timestamptz | default now() |
| capture_path | text | `'guided'｜'upload'` |
| pack_confidence | float | from the scan |
| segmentation_warning | text null | from the scan |
| code | text null | raw OCR code (display) |
| code_normalized | text null | uppercased, non-alphanumeric stripped (dedup key) |
| code_confidence | float | server re-OCR confidence |
| code_format_ok | bool | server re-OCR format check |
| verified | bool | true iff code present ∧ format_ok ∧ globally first-seen |
| staircase_photo_path | text | volume-relative |
| code_photo_path | text | volume-relative |

**Partial unique index:** `CREATE UNIQUE INDEX uq_pull_verified_code ON pull (code_normalized) WHERE verified = true AND code_normalized IS NOT NULL;`
This makes "≤ 1 verified pull per normalized code" a race-safe DB invariant. The save attempts `verified=true`; on `IntegrityError` from this index it retries with `verified=false`.

### `pull_card`

| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| pull_id | UUID FK→pull | indexed; cascade delete |
| row_index | int | |
| card_number / set_id / set_code / set_name / name / rarity | text null | from confirmed payload |
| low_confidence_reason | text null | |
| match_id | text null | PokéWallet card id |
| image_url | text null | PokéWallet art |
| confidence | float | |

## Auth flow & endpoints

FastAPI-Users provides the machinery; we customize registration to require `handle`.

- `POST /auth/register` — `{email, password, handle}`. Validate handle format + uniqueness and email uniqueness; clear 400 on conflict, 422 on malformed handle. Password hashed (argon2).
- `POST /auth/login` — `{email, password}` → sets httpOnly session cookie. `POST /auth/logout` → clears it.
- `GET /users/me` — returns `{id, email, handle}` for the current trainer.
- **Session:** `CookieTransport` (httpOnly, `secure` per `COOKIE_SECURE`, `samesite=lax`) + `JWTStrategy` signed with `AUTH_SECRET`, ~7-day TTL. Stateless: logout clears the cookie; tokens valid until expiry (no revocation in v1).
- **Authorization:** FastAPI-Users `current_active_user` dependency guards `/pulls` and `/users/me`; unauthenticated → 401. Scanner endpoints stay public.

**Frontend:** `api.ts` gains `register/login/logout/me` (same-origin cookies). A small auth context gates the review screen's "Looks good → save" behind login (prompt to sign in if anonymous). Building the full account UI beyond register/login forms and the save gate is minimal in v1.

## Pull persistence & code uniqueness

`POST /pulls` (auth required), multipart: `staircase`, `code_card` images; `cards` JSON (the trainer's confirmed/edited review results); `capture_path`, `pack_confidence`, `segmentation_warning`.

- Photos validated (existing `_read_image`: image content-type, 15 MB cap) before disk.
- Server re-OCRs the **code** from the uploaded code photo (`app.pack.ocr.read_code_card`) — authoritative; the client cannot spoof the code that drives `verified`.
- `verified` decided by the partial unique index (race-safe), as above.
- `pull_card` rows come from the confirmed `cards` payload.
- Single DB transaction around the row inserts; photo writes happen before commit, and a photo failure aborts the save (no committed row without its files).

**Reads:** `GET /pulls` (trainer's own, newest first), `GET /pulls/{id}` (owner-only → 404 otherwise).

**Known limitation (documented, deferred to sub-project C):** per-card data is taken from the client's confirmed payload; only the code is server-verified. A trainer with a real unique code could submit a `verified` pull with fabricated cards and skew future crowd stats. Acceptable for this foundation (it's the trainer's own collection; the one-real-code-per-verified-pull invariant holds). Sub-project C will harden verified-pull card data (e.g., server-side re-derivation).

## Photo storage (Railway Volume)

- Volume mounts at `PHOTO_STORAGE_DIR` (prod e.g. `/data`; dev default `./var/pulls`, gitignored). Created on startup if missing.
- Paths: `{PHOTO_STORAGE_DIR}/{trainer_id}/{pull_id}/{staircase,code}.jpg`. Path components are server-generated UUIDs → no user-controlled segments, no traversal.
- `app/storage.py` is the only module touching the filesystem: `save_pull_photos(...) → (staircase_path, code_path)`, `open_photo(path)`.
- Serving: `GET /pulls/{id}/photo/{kind}` (`kind ∈ {staircase, code}`), owner-checked (404 otherwise, 401 unauth), streams from the volume. Owner-only in v1; sharing for pack battles is deferred.

## Migrations, config & deployment

- **Alembic (async):** `env.py` reads `DATABASE_URL`, targets the models' metadata. Initial migration creates `trainer`, `pull`, `pull_card`, indexes, and the partial unique index. **No auto-create at startup** — schema changes only via `alembic upgrade head`, run as a release step on deploy.
- **Config (env-driven):** `DATABASE_URL` (Railway Postgres), `AUTH_SECRET` (required; app refuses to start without it in prod), `PHOTO_STORAGE_DIR`, `COOKIE_SECURE` (true in prod). Documented in `.env.example`.
- **Railway:** add a Postgres service (provides `DATABASE_URL`) + a Volume mounted at `PHOTO_STORAGE_DIR`; add `alembic upgrade head` to the release/start sequence (railpack/Procfile). New deps: `fastapi-users[sqlalchemy]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`.

## Verification (manual smoke — no automated tests)

Per user direction, no test files or harnesses. Verify by hand:

1. `alembic upgrade head` applies cleanly to an empty local Postgres; tables + partial unique index present.
2. Register a trainer (email + handle); duplicate email and duplicate handle both rejected with clear errors.
3. Log in → cookie set; `GET /users/me` returns the trainer; `GET /pulls` without the cookie → 401.
4. Scan a pack (reuse sub-project A's flow / synthetic `code.jpg`) and save via "Looks good" → pull stored, `verified=true`, photos on the volume, `pull_card` rows present.
5. Save a second pull with the **same** code → `verified=false`; both pulls exist.
6. Save a pull with an unreadable/missing code → `verified=false`.
7. Photo route serves the owner's bytes; another trainer's pull → 404.
8. On Railway: register + log in + save a pull; restart the service → photos still served (volume persistence).

## Success criteria

- Migrations apply from empty to head with no manual SQL.
- A trainer can register, log in, scan, and save a pull end-to-end on the deployed app.
- The duplicate-code invariant holds (second identical code → `verified=false`) and is enforced at the database level.
- Saved photos survive a service restart on the Railway Volume.
- The pack scanner (sub-project A) continues to work unchanged for anonymous users.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| No automated tests → regressions slip in | Manual smoke checklist above; code-review subagents during implementation; small focused modules. |
| Railway Volume single-instance (no horizontal scale) | Accepted for v1 (indie scale); documented. Migration path to R2/S3 is the `storage.py` boundary. |
| Client-spoofed card data on verified pulls | Documented; hardened in sub-project C (server-side re-derivation). |
| `AUTH_SECRET` missing/weak in prod | App refuses to start without it; `.env.example` flags it as required. |
| Photo write succeeds but DB commit fails (orphan files) | Photos written before commit; on failure the transaction aborts. Orphaned files are harmless (unreferenced) and can be GC'd later; not a v1 concern. |
| Re-OCR of code at save adds latency | Single Tesseract call on one small image; offloaded via the existing async pattern if needed. |
