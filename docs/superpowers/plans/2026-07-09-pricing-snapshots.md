# Batched Pricing Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture TCGPlayer/Cardmarket prices for every card trainers have pulled, as weekly (configurable) snapshots inside the existing batch, surface pull values to trainers, and route price-jump anomalies to the analyst triage.

**Architecture:** Migration `0004` adds `price_snapshot` + `card_price`. A staleness-gated stage (`app/stats/pricing.py`) runs inside `run_batch`'s advisory lock, re-using `lookup_card_exact` per pulled card (rate-limited) and writing snapshot rows + `price_jump` anomalies into the existing `anomaly` table. Pull responses are enriched at read time (`estimated_value`, `priced_as_of`, per-card USD range); My Pulls rows show the value and expand to per-card prices.

**Tech Stack:** Existing FastAPI + SQLAlchemy 2.0 async + Alembic + PokéWallet client; Vite/React/TS frontend. No new dependencies or services.

## Global Constraints

- **NO automated tests** (standing user directive) — every task ends with a manual smoke command. Never create test files.
- Env for backend runs/smokes: `DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs`, `AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789`, `PHOTO_STORAGE_DIR=./var/pulls`, `COOKIE_SECURE=false`, `STATS_CRON_TOKEN=dev-cron-token-123`. Local Postgres serves 5432; alembic currently at `0003_pull_card_species`.
- Process hygiene: before any uvicorn smoke `pkill -f "uvicorn app.main" 2>/dev/null; pkill -f pokewallet_stub 2>/dev/null; sleep 1`; ONE app + ONE stub max; kill everything you start.
- Smoke registrations: handles `[a-z0-9_]{3,20}` (≥3 chars), passwords ≥8.
- New config keys (spec-fixed defaults): `PRICE_SNAPSHOT_INTERVAL_DAYS=7`, `PRICE_JUMP_THRESHOLD=0.5`, `PRICE_LOOKUP_DELAY_MS=200`.

---

## File structure

```
app/db/models.py            # MODIFY: + PriceSnapshot, CardPrice
alembic/versions/0004_price_snapshots.py   # CREATE
app/stats/config.py         # MODIFY: + price settings
scripts/make_test_fixtures.py  # MODIFY: stub cards gain price blobs
tests/fixtures/pokewallet_cards.json       # REGENERATED (committed)
app/stats/pricing.py        # CREATE: refresh_prices_if_stale(stats_snapshot_id)
app/stats/run_batch.py      # MODIFY: call pricing stage inside the lock
app/pulls.py                # MODIFY: read-time price enrichment
frontend/src/api.ts         # MODIFY: price fields on SavedPull/PackCard
frontend/src/pulls/MyPulls.tsx  # MODIFY: value line + expandable per-card prices
.env.example                # MODIFY: 3 new vars
```

---

### Task 1: Migration `0004` + models + config

**Files:**
- Modify: `app/db/models.py`, `app/stats/config.py`
- Create: `alembic/versions/0004_price_snapshots.py`

**Interfaces:**
- Produces: `PriceSnapshot(id, created_at, status)`, `CardPrice(snapshot_id, match_id, set_id, card_number, name, usd_market_low, usd_market_high, eur_trend, raw)`; `StatsSettings.price_interval_days / price_jump_threshold / price_lookup_delay_ms`.

- [ ] **Step 1: Append models to `app/db/models.py`** (end of file):

```python
class PriceSnapshot(Base):
    __tablename__ = "price_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|done|failed


class CardPrice(Base):
    __tablename__ = "card_price"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    match_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    usd_market_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_market_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    eur_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
```

- [ ] **Step 2: Create `alembic/versions/0004_price_snapshots.py`:**

```python
"""price snapshots: price_snapshot + card_price

Revision ID: 0004_price_snapshots
Revises: 0003_pull_card_species
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_price_snapshots"
down_revision = "0003_pull_card_species"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_price_snapshot"),
    )
    op.create_table(
        "card_price",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Text(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("usd_market_low", sa.Float(), nullable=True),
        sa.Column("usd_market_high", sa.Float(), nullable=True),
        sa.Column("eur_trend", sa.Float(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["price_snapshot.id"], name="fk_card_price_snapshot_id_price_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_card_price"),
    )
    op.create_index("ix_card_price_snapshot_id", "card_price", ["snapshot_id"])
    op.create_index("ix_card_price_match_id", "card_price", ["match_id"])


def downgrade() -> None:
    op.drop_table("card_price")
    op.drop_table("price_snapshot")
```

- [ ] **Step 3: Add price settings to `app/stats/config.py`** — inside `class StatsSettings`, after `cron_token`:

```python
    price_interval_days: float = field(default_factory=lambda: _f("PRICE_SNAPSHOT_INTERVAL_DAYS", 7.0))
    price_jump_threshold: float = field(default_factory=lambda: _f("PRICE_JUMP_THRESHOLD", 0.5))
    price_lookup_delay_ms: int = field(default_factory=lambda: _i("PRICE_LOOKUP_DELAY_MS", 200))
```

- [ ] **Step 4: Apply + verify** (env exported):

```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/alembic upgrade head
.venv/bin/python -c "
import asyncio
from sqlalchemy import text
from app.db.session import engine
from app.stats.config import stats_settings
async def c():
    async with engine.connect() as cx:
        t = (await cx.execute(text(\"select table_name from information_schema.tables where table_schema='public' and table_name in ('price_snapshot','card_price') order by 1\"))).scalars().all()
        print('tables:', t)
s = stats_settings()
print('settings:', s.price_interval_days, s.price_jump_threshold, s.price_lookup_delay_ms)
asyncio.run(c())
"
```
Expected: `tables: ['card_price', 'price_snapshot']`; `settings: 7.0 0.5 200`.

- [ ] **Step 5: Commit**

```bash
git add app/db/models.py alembic/versions/0004_price_snapshots.py app/stats/config.py
git commit -m "feat(pricing): price snapshot tables + config"
```

---

### Task 2: Stub fixture price blobs

**Files:**
- Modify: `scripts/make_test_fixtures.py`
- Regenerate (committed): `tests/fixtures/pokewallet_cards.json`

**Interfaces:**
- Produces: stub cards whose `tcgplayer.prices[].market_price` = 1.25 / 3.50 / 10.00 and `cardmarket.prices[].trend` = 90% of market. Task 3's extraction and all smokes rely on these numbers.

- [ ] **Step 1: Give the ENTRIES prices** — in `scripts/make_test_fixtures.py`, change the `ENTRIES` list and the card-dict construction. Replace:

```python
ENTRIES = [
    ("012/202", "", "SSH", "Test Mon A"),
    ("045/185", "", "VIV", "Test Mon B"),
    ("101/198", "SVI", "SVI", "Test Mon C"),
]
```
with:
```python
# (number, printed_code, set_code, fake name, usd market price)
ENTRIES = [
    ("012/202", "", "SSH", "Test Mon A", 1.25),
    ("045/185", "", "VIV", "Test Mon B", 3.50),
    ("101/198", "SVI", "SVI", "Test Mon C", 10.00),
]
```
and update the loop + card dict (the tuple now has 5 elements):
```python
    for i, (number, printed_code, code, name, market) in enumerate(ENTRIES):
        entry = table.by_code[code]
        cards.append(
            {
                "id": f"test-{code.lower()}-{i}",
                "set_id": entry.set_id,
                "card_info": {
                    "name": name,
                    "set_name": entry.set_name,
                    "card_number": number,
                    "rarity": "Common",
                },
                "tcgplayer": {"prices": [{"sub_type_name": "Normal", "market_price": market}]},
                "cardmarket": {"prices": [{"variant_type": "Normal", "trend": round(market * 0.9, 2)}]},
            }
        )
```
and the staircase call (tuple slice changes):
```python
    meta = make_staircase([(n, pc) for n, pc, _, _, _ in ENTRIES], E2E / "staircase.jpg")
```

- [ ] **Step 2: Regenerate + verify** (env not needed beyond DATABASE_URL/AUTH_SECRET for the table import):

```bash
cd /Users/kailee/pokemon-card-scanner && DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 .venv/bin/python scripts/make_test_fixtures.py
.venv/bin/python -c "
import json
cards = json.load(open('tests/fixtures/pokewallet_cards.json'))
ms = [c['tcgplayer']['prices'][0]['market_price'] for c in cards]
ts = [c['cardmarket']['prices'][0]['trend'] for c in cards]
assert ms == [1.25, 3.5, 10.0], ms
print('stub prices ok:', ms, ts)
"
git diff --stat tests/fixtures/
```
Expected: `stub prices ok: [1.25, 3.5, 10.0] [1.12, 3.15, 9.0]`; the diff shows only `pokewallet_cards.json` changed (the jpgs/truth are byte-identical).

- [ ] **Step 3: Commit**

```bash
git add scripts/make_test_fixtures.py tests/fixtures/pokewallet_cards.json
git commit -m "feat(pricing): stub cards carry tcgplayer/cardmarket price blobs"
```

---

### Task 3: Pricing stage + batch wiring

**Files:**
- Create: `app/stats/pricing.py`
- Modify: `app/stats/run_batch.py`

**Interfaces:**
- Consumes: `lookup_card_exact`, `make_async_client`, `get_api_key` (app.pokewallet); models from Task 1; `stats_settings()` price fields; `Anomaly` model.
- Produces: `refresh_prices_if_stale(stats_snapshot_id: uuid.UUID) -> str | None` (price snapshot id, or None when skipped/no key). Task 4 reads `PriceSnapshot`/`CardPrice` directly.

- [ ] **Step 1: Create `app/stats/pricing.py`:**

```python
"""Weekly (configurable) price snapshots for pulled cards + price-jump anomalies.

Runs inside run_batch's advisory lock. Staleness-gated: most nightly batches skip
pricing entirely. Failure here must never fail the stats batch.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid

from sqlalchemy import func, select

from app.db.models import Anomaly, CardPrice, PriceSnapshot, Pull, PullCard, PullCardDerived
from app.db.session import async_session_maker
from app.pokewallet import get_api_key, lookup_card_exact, make_async_client
from app.stats.config import stats_settings

log = logging.getLogger("pokemon_scanner.stats.pricing")


def _extract_prices(match: dict | None) -> tuple[float | None, float | None, float | None, dict]:
    """(usd_low, usd_high, eur_trend, raw) from a PokéWallet card hit."""
    if not match:
        return None, None, None, {}
    tp = match.get("tcgplayer") or {}
    cm = match.get("cardmarket") or {}
    markets = [
        r.get("market_price") for r in (tp.get("prices") or [])
        if isinstance(r.get("market_price"), (int, float))
    ]
    trends = [
        r.get("trend") for r in (cm.get("prices") or [])
        if isinstance(r.get("trend"), (int, float))
    ]
    low = min(markets) if markets else None
    high = max(markets) if markets else None
    trend = trends[0] if trends else None
    return low, high, trend, {"tcgplayer": tp, "cardmarket": cm}


async def _card_universe(session) -> list[tuple[str, str | None, str | None, str | None]]:
    """Distinct (match_id, set_id, card_number, name) across confirmed + derived cards.
    Derived (server-authoritative) metadata wins when both exist."""
    universe: dict[str, tuple[str, str | None, str | None, str | None]] = {}
    for model in (PullCard, PullCardDerived):
        rows = (
            await session.execute(
                select(model.match_id, model.set_id, model.card_number, model.name)
                .where(model.match_id.is_not(None))
                .distinct()
            )
        ).all()
        for mid, sid, num, name in rows:
            universe[mid] = (mid, sid, num, name)  # later model (derived) overwrites
    return list(universe.values())


async def refresh_prices_if_stale(stats_snapshot_id: uuid.UUID) -> str | None:
    cfg = stats_settings()
    api_key = get_api_key()
    if not api_key:
        log.info("pricing.skipped no_api_key")
        return None

    async with async_session_maker() as session:
        latest = (
            await session.execute(
                select(func.max(PriceSnapshot.created_at)).where(PriceSnapshot.status == "done")
            )
        ).scalar_one_or_none()
        if latest is not None:
            age = datetime.datetime.now(datetime.timezone.utc) - latest
            if age < datetime.timedelta(days=cfg.price_interval_days):
                log.info("pricing.skipped fresh age_days=%.2f", age.total_seconds() / 86400)
                return None

        universe = await _card_universe(session)
        if not universe:
            log.info("pricing.skipped no_pulled_cards")
            return None

        snap = PriceSnapshot(status="running")
        session.add(snap)
        await session.flush()
        snap_id = snap.id
        try:
            async with make_async_client() as client:
                for mid, sid, num, name in universe:
                    low = high = trend = None
                    raw: dict = {}
                    if sid and num:
                        try:
                            hit = await lookup_card_exact(
                                sid, num.split("/")[0], api_key=api_key, client=client
                            )
                            low, high, trend, raw = _extract_prices(hit)
                        except Exception as e:  # one bad card must not kill the snapshot
                            log.warning("pricing.lookup_failed match=%s err=%r", mid, e)
                    session.add(CardPrice(
                        snapshot_id=snap_id, match_id=mid, set_id=sid, card_number=num,
                        name=name, usd_market_low=low, usd_market_high=high,
                        eur_trend=trend, raw=raw,
                    ))
                    await asyncio.sleep(cfg.price_lookup_delay_ms / 1000)
            await session.flush()

            # price-jump anomalies vs the previous done snapshot
            prev_id = (
                await session.execute(
                    select(PriceSnapshot.id).where(PriceSnapshot.status == "done")
                    .order_by(PriceSnapshot.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if prev_id is not None:
                prev = {
                    r.match_id: r for r in (
                        await session.execute(select(CardPrice).where(CardPrice.snapshot_id == prev_id))
                    ).scalars()
                }
                cur = (
                    await session.execute(select(CardPrice).where(CardPrice.snapshot_id == snap_id))
                ).scalars().all()
                for row in cur:
                    old = prev.get(row.match_id)
                    if old is None:
                        continue
                    o = _mid(old.usd_market_low, old.usd_market_high)
                    n = _mid(row.usd_market_low, row.usd_market_high)
                    if o is None or n is None or o == 0:
                        continue
                    pct = (n - o) / o
                    if abs(pct) >= cfg.price_jump_threshold:
                        session.add(Anomaly(
                            snapshot_id=stats_snapshot_id, detector="price_jump",
                            target_type="card", set_id=row.set_id or "unknown",
                            card_match_id=row.match_id, severity=abs(pct),
                            detail={"old": o, "new": n, "pct": pct,
                                    "from_snapshot": str(prev_id), "to_snapshot": str(snap_id),
                                    "name": row.name},
                        ))

            snap.status = "done"
            await session.commit()
            log.info("pricing.done snapshot=%s cards=%s", snap_id, len(universe))
            return str(snap_id)
        except Exception:
            await session.rollback()
            async with async_session_maker() as s2:
                failed = await s2.get(PriceSnapshot, snap_id)
                if failed is not None:
                    failed.status = "failed"
                    await s2.commit()
            log.exception("pricing.failed snapshot=%s", snap_id)
            return None


def _mid(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2
```

- [ ] **Step 2: Wire into `app/stats/run_batch.py`** — add the import:

```python
from app.stats.pricing import refresh_prices_if_stale
```

and inside `run_batch`, after `snap.status = "done"` / `await session.commit()` / the `log.info("run_batch.done ...")` line but still INSIDE the `try:` that owns the advisory lock (before the `finally:`), add:

```python
                try:
                    await refresh_prices_if_stale(snap.id)
                except Exception:
                    log.exception("run_batch.pricing_stage_failed (stats batch unaffected)")
```

(Indent to match the surrounding block; the pricing stage manages its own session. Note the rollback path inside `refresh_prices_if_stale` also protects itself; this outer guard is belt-and-suspenders per the spec: pricing must never fail the batch.)

- [ ] **Step 3: Smoke** (env exported incl. `STATS_CRON_TOKEN`; stub running; force pricing with interval 0):

```bash
cd /Users/kailee/pokemon-card-scanner
export DATABASE_URL="postgresql://pcs:pcs@localhost:5432/pcs" AUTH_SECRET="dev-secret-not-for-prod-pad-0123456789" PHOTO_STORAGE_DIR="./var/pulls" COOKIE_SECURE="false" STATS_CRON_TOKEN="dev-cron-token-123"
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE; TRUNCATE stats_snapshot CASCADE; TRUNCATE price_snapshot CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" 2>/dev/null; pkill -f pokewallet_stub 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key
PRICE_SNAPSHOT_INTERVAL_DAYS=0 .venv/bin/uvicorn app.main:app --port 8040 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8040
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"pr@x.com","password":"longpassword1","handle":"pricer"}' -o /dev/null
curl -s -c /tmp/cp -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=pr@x.com&password=longpassword1' -o /dev/null
curl -s -b /tmp/cp -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[{"row_index":0,"card_number":"012/202","set_id":"23876","name":"Test Mon A","match_id":"test-ssh-0","confidence":0.9}]' -F capture_path=guided -o /dev/null -w 'save=%{http_code}\n'
curl -s -X POST $BASE/admin/stats/recompute -H "authorization: Bearer dev-cron-token-123" -o /dev/null -w 'recompute=%{http_code}\n'; sleep 5
echo -n "price snapshot done: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from price_snapshot where status='done';"
echo -n "card_price rows: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select match_id||' '||usd_market_low||'-'||usd_market_high||' trend '||eur_trend from card_price;"
echo "second recompute (must SKIP pricing — interval respected by a fresh app on default 7d):"
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8040 >/tmp/app.log 2>&1 & sleep 4
curl -s -X POST $BASE/admin/stats/recompute -H "authorization: Bearer dev-cron-token-123" -o /dev/null -w 'recompute2=%{http_code}\n'; sleep 4
echo -n "still one price snapshot: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from price_snapshot;"
grep -c "pricing.skipped fresh" /tmp/app.log
pkill -f "uvicorn app.main" 2>/dev/null; pkill -f pokewallet_stub 2>/dev/null
```
Expected: `save=201`, `recompute=202`; `price snapshot done: 1`; **three** `card_price` rows (`test-ssh-0 1.25-1.25 trend 1.12`, `test-viv-1 3.5-3.5 trend 3.15`, `test-svi-2 10-10 trend 9`) — the batch re-derives the staircase first, so the universe is the union of the 1 client-confirmed card and the 3 server-derived cards; after the restart on default interval, `recompute2=202` but `still one price snapshot: 1` and `pricing.skipped fresh` ≥1 in the log.

- [ ] **Step 4: Commit**

```bash
git add app/stats/pricing.py app/stats/run_batch.py
git commit -m "feat(pricing): staleness-gated price snapshots + price-jump anomalies in the batch"
```

---

### Task 4: Read-time price enrichment of pull responses

**Files:**
- Modify: `app/pulls.py`

**Interfaces:**
- Consumes: `PriceSnapshot`/`CardPrice` (Task 1).
- Produces: `CardOut.price_usd_low/price_usd_high: float | None`; `PullOut.estimated_value: float | None`, `PullOut.priced_as_of: str | None` — Task 5's frontend relies on these exact names.

- [ ] **Step 1: Import the models** — in `app/pulls.py`, change the models import line to:

```python
from app.db.models import CardPrice, PriceSnapshot, Pull, PullCard
```

- [ ] **Step 2: Add price fields to the response models.** In `CardOut`, after `confidence: float`:

```python
    price_usd_low: float | None = None
    price_usd_high: float | None = None
```

In `PullOut`, after `encounters: list[EncounterOut] = []`:

```python
    estimated_value: float | None = None
    priced_as_of: str | None = None
```

- [ ] **Step 3: Add the price-map helper** — after `_pull_to_out`, add:

```python
async def _latest_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]:
    """(match_id -> (usd_low, usd_high), snapshot iso date) from the newest done snapshot."""
    snap = (
        await session.execute(
            select(PriceSnapshot).where(PriceSnapshot.status == "done")
            .order_by(PriceSnapshot.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if snap is None:
        return {}, None
    rows = (
        await session.execute(
            select(CardPrice.match_id, CardPrice.usd_market_low, CardPrice.usd_market_high)
            .where(CardPrice.snapshot_id == snap.id)
        )
    ).all()
    return {m: (lo, hi) for m, lo, hi in rows}, snap.created_at.isoformat()


def _enrich_prices(out: PullOut, prices: dict[str, tuple[float | None, float | None]],
                   priced_as_of: str | None) -> PullOut:
    if not prices:
        return out
    total = 0.0
    any_priced = False
    for card in out.cards:
        if card.match_id and card.match_id in prices:
            lo, hi = prices[card.match_id]
            card.price_usd_low, card.price_usd_high = lo, hi
            if lo is not None and hi is not None:
                total += (lo + hi) / 2
                any_priced = True
    if any_priced:
        out.estimated_value = round(total, 2)
        out.priced_as_of = priced_as_of
    return out
```

- [ ] **Step 4: Apply it in all three endpoints.** In `save_pull`, change the response block to:

```python
        out = _pull_to_out(saved)
        try:
            out.encounters = await _compute_encounters(session, trainer.id, saved)
        except Exception:  # the dex moment must never break persistence
            out.encounters = []
        prices, as_of = await _latest_price_map(session)
        return _enrich_prices(out, prices, as_of)
```

In `list_pulls`, change the return to:

```python
        prices, as_of = await _latest_price_map(session)
        return [_enrich_prices(_pull_to_out(p), prices, as_of) for p in rows]
```

In `get_pull`, change the return to:

```python
        prices, as_of = await _latest_price_map(session)
        return _enrich_prices(_pull_to_out(pull), prices, as_of)
```

- [ ] **Step 5: Smoke** (state from Task 3's smoke still in the DB; env exported):

```bash
cd /Users/kailee/pokemon-card-scanner
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8041 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8041
curl -s -c /tmp/cp -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=pr@x.com&password=longpassword1' -o /dev/null
curl -s -b /tmp/cp $BASE/pulls | python3 -c "
import sys, json
p = json.load(sys.stdin)[0]
print('estimated_value:', p['estimated_value'], '| priced_as_of set:', p['priced_as_of'] is not None)
print('card price:', p['cards'][0]['price_usd_low'], '-', p['cards'][0]['price_usd_high'])
"
pkill -f "uvicorn app.main" 2>/dev/null
```
Expected: `estimated_value: 1.25 | priced_as_of set: True`; `card price: 1.25 - 1.25`.

- [ ] **Step 6: Commit**

```bash
git add app/pulls.py
git commit -m "feat(pricing): enrich pull responses with latest snapshot prices"
```

---

### Task 5: Frontend (My Pulls value + expandable prices) + env docs + final smoke

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/pulls/MyPulls.tsx`, `.env.example`

**Interfaces:**
- Consumes: `estimated_value`/`priced_as_of` on `SavedPull`, `price_usd_low/high` on cards (Task 4).

- [ ] **Step 1: Types in `frontend/src/api.ts`.** In `PackCard`, after `low_confidence_reason`:

```typescript
  price_usd_low?: number | null;
  price_usd_high?: number | null;
```

In `SavedPull`, after `encounters: Encounter[];`:

```typescript
  estimated_value?: number | null;
  priced_as_of?: string | null;
```

- [ ] **Step 2: Replace `frontend/src/pulls/MyPulls.tsx`** with the expandable version:

```tsx
import { useEffect, useState } from "react";
import { listPulls, type PackCard, type SavedPull } from "../api";

function cardPrice(c: PackCard): string {
  if (c.price_usd_low == null || c.price_usd_high == null) return "—";
  if (c.price_usd_low === c.price_usd_high) return `$${c.price_usd_low.toFixed(2)}`;
  return `$${c.price_usd_low.toFixed(2)}–$${c.price_usd_high.toFixed(2)}`;
}

export default function MyPulls() {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    listPulls().then(setPulls).catch((e) => setError(String(e)));
  }, []);

  if (error) return <p className="camera-error">{error}</p>;
  if (!pulls) return <p className="status">Loading your pulls…</p>;
  if (pulls.length === 0) return <p>No pulls saved yet — scan a pack!</p>;

  return (
    <ul className="card-rows">
      {pulls.map((p) => (
        <li key={p.id} className="card-row">
          <div className="card-row-body">
            <button type="button" className="pull-row-toggle"
                    onClick={() => setOpen(open === p.id ? null : p.id)}>
              <strong>{new Date(p.created_at).toLocaleString()}</strong>
            </button>
            <span>
              {p.cards.length} cards · code {p.code ?? "—"} ·{" "}
              {p.verified ? "✓ verified" : "unverified"}
              {p.estimated_value != null && p.priced_as_of != null && (
                <> · ≈ ${p.estimated_value.toFixed(2)} (prices as of{" "}
                {new Date(p.priced_as_of).toLocaleDateString()})</>
              )}
            </span>
            {open === p.id && (
              <ul className="card-rows">
                {p.cards.map((c) => (
                  <li key={c.row_index} className="card-row">
                    <div className="card-row-body">
                      <span>{c.name ?? c.card_number ?? "?"} · {cardPrice(c)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 3: `.env.example`** — in the "Stats batch" section, after `PACK_STATS_PRIOR_STRENGTH=20`, add:

```
PRICE_SNAPSHOT_INTERVAL_DAYS=7
PRICE_JUMP_THRESHOLD=0.5
PRICE_LOOKUP_DELAY_MS=200
```

- [ ] **Step 4: Build + final smoke (incl. price-jump anomaly):**

```bash
cd /Users/kailee/pokemon-card-scanner/frontend && npm run build 2>&1 | grep -E "built in|error" | head -3
cd /Users/kailee/pokemon-card-scanner
export DATABASE_URL="postgresql://pcs:pcs@localhost:5432/pcs" AUTH_SECRET="dev-secret-not-for-prod-pad-0123456789" PHOTO_STORAGE_DIR="./var/pulls" COOKIE_SECURE="false" STATS_CRON_TOKEN="dev-cron-token-123"
# bump the stub price for the pulled card and force a second snapshot with a tiny threshold
python3 - <<'EOF'
import json
p = 'tests/fixtures/pokewallet_cards.json'
cards = json.load(open(p))
cards[0]['tcgplayer']['prices'][0]['market_price'] = 5.00   # 1.25 -> 5.00 (300% jump)
json.dump(cards, open(p, 'w'), indent=2)
EOF
pkill -f "uvicorn app.main" 2>/dev/null; pkill -f pokewallet_stub 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key
PRICE_SNAPSHOT_INTERVAL_DAYS=0 PRICE_JUMP_THRESHOLD=0.01 .venv/bin/uvicorn app.main:app --port 8042 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8042
curl -s -X POST $BASE/admin/stats/recompute -H "authorization: Bearer dev-cron-token-123" -o /dev/null -w 'recompute=%{http_code}\n'; sleep 5
echo -n "price snapshots now: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from price_snapshot where status='done';"
echo -n "price_jump anomaly: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select detector||' sev '||round(severity::numeric,2) from anomaly where detector='price_jump';"
# restore the fixture (committed value)
git checkout -- tests/fixtures/pokewallet_cards.json
pkill -f "uvicorn app.main" 2>/dev/null; pkill -f pokewallet_stub 2>/dev/null
echo -n "scanner suite still green: "; env -u DATABASE_URL -u AUTH_SECRET .venv/bin/python -m pytest tests/test_scan_pack.py -q 2>&1 | tail -1
```
Expected: build `✓ built in …`; `recompute=202`; `price snapshots now: 2`; a `price_jump sev 3.00` anomaly row; fixture restored; scanner suite `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api.ts frontend/src/pulls/MyPulls.tsx .env.example
git commit -m "feat(pricing): My Pulls estimated value + per-card prices; env docs"
```

---

## Completion checklist (maps to spec)

- [ ] `price_snapshot` + `card_price` tables, raw blobs retained (Task 1)
- [ ] Pulled-cards-only universe; staleness-gated weekly stage inside `run_batch`; rate-limited lookups; pricing failure never fails the batch (Task 3)
- [ ] `price_jump` anomalies in the existing triage (Task 3; smoked in Task 5)
- [ ] Trainer-visible: estimated value + as-of date + per-card prices in My Pulls (Tasks 4, 5)
- [ ] Stub fixtures smokeable with real numbers (Task 2)
- [ ] 3 env vars documented; no new services (Tasks 1, 5)
- [ ] No automated tests — manual smokes per task; scanner suite green at the end (all tasks)
