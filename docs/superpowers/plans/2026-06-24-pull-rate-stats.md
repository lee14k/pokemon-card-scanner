# Crowd-Sourced Pull-Rate Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the verified pulls stored by sub-project B into trustworthy crowd-sourced pull-rate statistics — computed by a periodic batch (server-side re-derivation → aggregation → prior blend → anomaly detection → snapshot), surfaced through a role-gated analyst dashboard.

**Architecture:** One new `app/stats/` package holds the batch pipeline; role-based auth deps + an admin API gate access; a `/dashboard` area in the SPA reads the materialized stat tables. The batch runs **inside the web service** (it reads pull photos off the Railway volume) and is triggered by a manual admin endpoint or a thin Railway cron service via a shared token. The pack scanner (`app/pack/`) is reused (re-derivation calls `scan_pack`); B's `/pulls` gains one field (`capture_meta`).

**Tech Stack:** FastAPI, FastAPI-Users 15.x (roles), SQLAlchemy 2.0 async + asyncpg, Alembic, the existing `app/pack` scan pipeline + `app/storage`, Postgres advisory locks, Vite/React/TS dashboard, Railway cron.

**TESTING POLICY — read first:** Per standing user directive, this plan has **NO automated tests** — no test files, no pytest additions. Every task ends with a **manual smoke** (a command + expected output) instead. Code-review subagents between tasks are not tests and are fine.

**Spec:** `docs/superpowers/specs/2026-06-24-pull-rate-stats-design.md`

---

## Prerequisites (dev environment)

A local Postgres 14+ on `localhost:5432` (db/user/password `pcs`) with `citext` — the same one sub-projects A/B used. Every backend task exports:

```bash
export DATABASE_URL="postgresql://pcs:pcs@localhost:5432/pcs"
export AUTH_SECRET="dev-secret-not-for-prod-pad-0123456789"   # >= 32 bytes
export PHOTO_STORAGE_DIR="./var/pulls"
export COOKIE_SECURE="false"
export STATS_CRON_TOKEN="dev-cron-token-123"
```

Before any `uvicorn` smoke: `pkill -f "uvicorn app.main" 2>/dev/null; sleep 1` and use a fresh port. B's migration must already be applied (`alembic upgrade head`).

---

## File structure

```
app/db/models.py        # MODIFY: Role enum on Trainer; Pull.capture_meta/derive_status/derived_at;
                        #         + PullCardDerived, StatsSnapshot, SetStat, RarityStat, CardStat, Anomaly
app/db/users.py         # MODIFY: require_analyst, require_admin deps; UserRead.role
app/pulls.py            # MODIFY: persist capture_meta on save
app/stats/__init__.py
app/stats/config.py     # CREATE: StatsSettings (min_sample, z, concentration, prior strength, cron token)
app/stats/prior.py      # CREATE: PriorSource + SeedFilePriorSource + beta_binomial_blend
app/stats/data/priors.json   # CREATE: seed per-rarity priors
app/stats/rederive.py   # CREATE: re-derive verified pulls -> pull_card_derived
app/stats/aggregate.py  # CREATE: derived cards -> set/card/rarity stats for a snapshot
app/stats/anomaly.py    # CREATE: deviation_from_prior + submitter_concentration detectors
app/stats/run_batch.py  # CREATE: run_batch() orchestrator + advisory lock + CLI
app/admin.py            # CREATE: /admin/trainers, /admin/trainers/{id}/role, /admin/stats/recompute
app/stats_api.py        # CREATE: /stats/sets, /stats/sets/{id}, /stats/anomalies (+ PATCH)
app/main.py             # MODIFY: include admin + stats routers
alembic/versions/0002_pull_rate_stats.py   # CREATE
scripts/grant_role.py   # CREATE: bootstrap role by email
frontend/src/api.ts     # MODIFY: role on Trainer; stats + admin client fns; savePull sends capture_meta
frontend/src/App.tsx    # MODIFY: role-gated dashboard nav; pass capture_meta to savePull
frontend/src/dashboard/{Dashboard,SetStats,Anomalies,RoleAdmin}.tsx   # CREATE
railway-cron.toml notes # deploy doc in Task 12 (Railway cron service triggers recompute)
```

---

### Task 1: Migration + models (role, derived cards, stat tables)

**Files:**
- Modify: `app/db/models.py`
- Create: `alembic/versions/0002_pull_rate_stats.py`
- Create: `app/stats/__init__.py`

- [ ] **Step 1: Add the role enum + pull columns + new models to `app/db/models.py`.**

At the top, after the existing imports, add:
```python
import enum
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB


class Role(str, enum.Enum):
    trainer = "trainer"
    analyst = "analyst"
    admin = "admin"


class DeriveStatus(str, enum.Enum):
    pending = "pending"
    done = "done"
    failed = "failed"
```

In `class Trainer`, add (after `handle`):
```python
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, name="role", native_enum=False, length=16),
        nullable=False, default=Role.trainer, server_default=Role.trainer.value,
    )
```

In `class Pull`, add (after `segmentation_warning`):
```python
    capture_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    derive_status: Mapped[DeriveStatus] = mapped_column(
        SAEnum(DeriveStatus, name="derive_status", native_enum=False, length=16),
        nullable=False, default=DeriveStatus.pending, server_default=DeriveStatus.pending.value,
    )
    derived_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    derived_cards: Mapped[list["PullCardDerived"]] = relationship(
        back_populates="pull", cascade="all, delete-orphan"
    )
```

At the end of the file, add the new models:
```python
class PullCardDerived(Base):
    __tablename__ = "pull_card_derived"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pull_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pull.id", ondelete="CASCADE"), index=True, nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    card_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    set_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    rarity: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pull: Mapped["Pull"] = relationship(back_populates="derived_cards")


class StatsSnapshot(Base):
    __tablename__ = "stats_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # cron|manual|cli
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|done|failed


class SetStat(Base):
    __tablename__ = "set_stat"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stats_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    set_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    verified_pack_count: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)


class RarityStat(Base):
    __tablename__ = "rarity_stat"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stats_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    set_id: Mapped[str] = mapped_column(Text, nullable=False)
    rarity: Mapped[str] = mapped_column(Text, nullable=False)
    packs_with_rarity: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_rate: Mapped[float] = mapped_column(Float, nullable=False)
    blended_rate: Mapped[float] = mapped_column(Float, nullable=False)


class CardStat(Base):
    __tablename__ = "card_stat"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stats_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    set_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    match_id: Mapped[str] = mapped_column(Text, nullable=False)
    card_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    packs: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_rate: Mapped[float] = mapped_column(Float, nullable=False)
    blended_rate: Mapped[float] = mapped_column(Float, nullable=False)


class Anomaly(Base):
    __tablename__ = "anomaly"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stats_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    detector: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(8), nullable=False)  # set|card
    set_id: Mapped[str] = mapped_column(Text, nullable=False)
    card_match_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[float] = mapped_column(Float, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")  # open|reviewed|dismissed
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)
```

- [ ] **Step 2: Create `app/stats/__init__.py`** (empty):
```python
```

- [ ] **Step 3: Write the migration `alembic/versions/0002_pull_rate_stats.py`** (hand-written; native_enum=False means the enums are VARCHAR + CHECK, no PG enum types to manage):

```python
"""pull-rate stats: role, derived cards, stat + anomaly tables

Revision ID: 0002_pull_rate_stats
Revises: 0001_initial
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_pull_rate_stats"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trainer", sa.Column("role", sa.String(length=16), nullable=False, server_default="trainer"))
    op.add_column("pull", sa.Column("capture_meta", JSONB(), nullable=True))
    op.add_column("pull", sa.Column("derive_status", sa.String(length=16), nullable=False, server_default="pending"))
    op.add_column("pull", sa.Column("derived_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "pull_card_derived",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("set_code", sa.Text(), nullable=True),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("match_id", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pull_id"], ["pull.id"], name="fk_pull_card_derived_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull_card_derived"),
    )
    op.create_index("ix_pull_card_derived_pull_id", "pull_card_derived", ["pull_id"])
    op.create_index("ix_pull_card_derived_set_id", "pull_card_derived", ["set_id"])
    op.create_index("ix_pull_card_derived_match_id", "pull_card_derived", ["match_id"])

    op.create_table(
        "stats_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stats_snapshot"),
    )

    op.create_table(
        "set_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("verified_pack_count", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_set_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_set_stat"),
    )
    op.create_index("ix_set_stat_snapshot_id", "set_stat", ["snapshot_id"])
    op.create_index("ix_set_stat_set_id", "set_stat", ["set_id"])

    op.create_table(
        "rarity_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("rarity", sa.Text(), nullable=False),
        sa.Column("packs_with_rarity", sa.Integer(), nullable=False),
        sa.Column("raw_rate", sa.Float(), nullable=False),
        sa.Column("blended_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_rarity_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rarity_stat"),
    )
    op.create_index("ix_rarity_stat_snapshot_id", "rarity_stat", ["snapshot_id"])

    op.create_table(
        "card_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("match_id", sa.Text(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("packs", sa.Integer(), nullable=False),
        sa.Column("raw_rate", sa.Float(), nullable=False),
        sa.Column("blended_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_card_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_card_stat"),
    )
    op.create_index("ix_card_stat_snapshot_id", "card_stat", ["snapshot_id"])
    op.create_index("ix_card_stat_set_id", "card_stat", ["set_id"])

    op.create_table(
        "anomaly",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=8), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("card_match_id", sa.Text(), nullable=True),
        sa.Column("severity", sa.Float(), nullable=False),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_anomaly_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_anomaly"),
    )
    op.create_index("ix_anomaly_snapshot_id", "anomaly", ["snapshot_id"])


def downgrade() -> None:
    op.drop_table("anomaly")
    op.drop_table("card_stat")
    op.drop_table("rarity_stat")
    op.drop_table("set_stat")
    op.drop_table("stats_snapshot")
    op.drop_table("pull_card_derived")
    op.drop_column("pull", "derived_at")
    op.drop_column("pull", "derive_status")
    op.drop_column("pull", "capture_meta")
    op.drop_column("trainer", "role")
```

- [ ] **Step 4: Apply + verify** (env exported, B's `0001` already applied):
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/alembic upgrade head
.venv/bin/python -c "
import asyncio
from sqlalchemy import text
from app.db.session import engine
async def c():
    async with engine.connect() as cx:
        t = (await cx.execute(text(\"select table_name from information_schema.tables where table_schema='public' order by 1\"))).scalars().all()
        cols = (await cx.execute(text(\"select column_name from information_schema.columns where table_name='pull' and column_name in ('capture_meta','derive_status','derived_at') order by 1\"))).scalars().all()
        role = (await cx.execute(text(\"select column_name from information_schema.columns where table_name='trainer' and column_name='role'\"))).scalars().all()
        print('new tables present:', all(x in t for x in ['anomaly','card_stat','pull_card_derived','rarity_stat','set_stat','stats_snapshot']))
        print('pull cols:', cols, '| trainer.role:', role)
asyncio.run(c())
"
```
Expected: `new tables present: True`; `pull cols: ['capture_meta', 'derive_status', 'derived_at'] | trainer.role: ['role']`.

- [ ] **Step 5: Commit**
```bash
git add app/db/models.py app/stats/__init__.py alembic/versions/0002_pull_rate_stats.py
git commit -m "feat(stats): migration + models for roles, derived cards, stat/anomaly tables"
```

---

### Task 2: Role authorization dependencies + /users/me role

**Files:**
- Modify: `app/db/users.py`

- [ ] **Step 1: Expose `role` in `UserRead`** — in `app/db/users.py`, change the `UserRead` schema:
```python
class UserRead(schemas.BaseUser[uuid.UUID]):
    handle: str
    role: str
```

- [ ] **Step 2: Import `Role`** — in `app/db/users.py`, change the existing `from app.db.models import Trainer` line (from Task B-4) to:
```python
from app.db.models import Role, Trainer
```

- [ ] **Step 3: Add role dependencies** — append to the end of `app/db/users.py`. `HTTPException`, `Annotated`, `Depends`, and `Trainer` are already imported in this module:
```python
def require_analyst(trainer: CurrentTrainer) -> Trainer:
    if trainer.role not in (Role.analyst, Role.admin):
        raise HTTPException(status_code=403, detail="analyst role required")
    return trainer


def require_admin(trainer: CurrentTrainer) -> Trainer:
    if trainer.role != Role.admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return trainer


CurrentAnalyst = Annotated[Trainer, Depends(require_analyst)]
CurrentAdmin = Annotated[Trainer, Depends(require_admin)]
```

- [ ] **Step 4: Smoke** (env exported; reset + start fresh app):
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8020 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8020
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"a@x.com","password":"longpassword1","handle":"aaa"}' -o /dev/null
curl -s -c /tmp/c -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=a@x.com&password=longpassword1' -o /dev/null
echo -n "me role: "; curl -s -b /tmp/c $BASE/users/me | python3 -c "import sys,json;print(json.load(sys.stdin)['role'])"
pkill -f "uvicorn app.main" 2>/dev/null
```
Expected: `me role: trainer`.

- [ ] **Step 5: Commit**
```bash
git add app/db/users.py
git commit -m "feat(stats): role authorization deps (require_analyst/require_admin); role in /users/me"
```

---

### Task 3: Admin API (role grants) + bootstrap script

**Files:**
- Create: `app/admin.py`
- Create: `scripts/grant_role.py`
- Modify: `app/main.py`

- [ ] **Step 1: Create `app/admin.py`** (the `/admin/stats/recompute` endpoint is added in Task 9; this task adds trainer/role admin):
```python
"""Admin API: list trainers, grant roles. Admin-only."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Role, Trainer
from app.db.session import async_session_maker
from app.db.users import CurrentAdmin

log = logging.getLogger("pokemon_scanner.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


class TrainerOut(BaseModel):
    id: uuid.UUID
    email: str
    handle: str
    role: str


class RoleUpdate(BaseModel):
    role: Role


@router.get("/trainers", response_model=list[TrainerOut])
async def list_trainers(admin: CurrentAdmin, query: str = "") -> list[TrainerOut]:
    async with async_session_maker() as session:
        stmt = select(Trainer).order_by(Trainer.created_at.desc()).limit(100)
        if query:
            like = f"%{query.lower()}%"
            stmt = select(Trainer).where(
                (Trainer.email.ilike(like)) | (Trainer.handle.ilike(like))
            ).limit(100)
        rows = (await session.execute(stmt)).scalars().all()
        return [TrainerOut(id=t.id, email=t.email, handle=t.handle, role=t.role.value) for t in rows]


@router.patch("/trainers/{trainer_id}/role", response_model=TrainerOut)
async def set_role(admin: CurrentAdmin, trainer_id: uuid.UUID, body: RoleUpdate) -> TrainerOut:
    async with async_session_maker() as session:
        t = await session.get(Trainer, trainer_id)
        if t is None:
            raise HTTPException(404, "trainer not found")
        old = t.role
        t.role = body.role
        await session.commit()
        log.info("admin.role_change by=%s target=%s %s->%s", admin.id, t.id, old.value, body.role.value)
        return TrainerOut(id=t.id, email=t.email, handle=t.handle, role=t.role.value)
```

- [ ] **Step 2: Create `scripts/grant_role.py`**:
```python
"""Bootstrap / change a trainer's role by email.

Usage: DATABASE_URL=... AUTH_SECRET=... .venv/bin/python scripts/grant_role.py <email> <trainer|analyst|admin>
(AUTH_SECRET only needs to be present+valid; it is not used for DB writes.)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import Role, Trainer  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402


async def main(email: str, role: str) -> None:
    r = Role(role)  # raises ValueError on bad role
    async with async_session_maker() as session:
        t = (await session.execute(select(Trainer).where(Trainer.email == email))).scalar_one_or_none()
        if t is None:
            raise SystemExit(f"no trainer with email {email!r}")
        t.role = r
        await session.commit()
        print(f"{email} -> {r.value}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: grant_role.py <email> <trainer|analyst|admin>")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 3: Mount the admin router in `app/main.py`** — add the import near the other app imports:
```python
from app.admin import router as admin_router
```
and include it alongside the other routers (before the static mount):
```python
app.include_router(admin_router)
```

- [ ] **Step 4: Smoke** (env exported):
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8021 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8021
# two trainers
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"boss@x.com","password":"longpassword1","handle":"boss"}' -o /dev/null
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"ana@x.com","password":"longpassword2","handle":"ana"}' -o /dev/null
# bootstrap boss -> admin via script
.venv/bin/python scripts/grant_role.py boss@x.com admin
# boss logs in, lists trainers, grants ana analyst
curl -s -c /tmp/cb -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=boss@x.com&password=longpassword1' -o /dev/null
ANA=$(curl -s -b /tmp/cb "$BASE/admin/trainers?query=ana" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo -n "grant ana analyst: "; curl -s -b /tmp/cb -X PATCH "$BASE/admin/trainers/$ANA/role" -H 'content-type: application/json' -d '{"role":"analyst"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['role'])"
# a plain trainer cannot use admin API
curl -s -c /tmp/ca -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=ana@x.com&password=longpassword2' -o /dev/null
echo -n "analyst hitting admin api: "; curl -s -b /tmp/ca "$BASE/admin/trainers" -o /dev/null -w '%{http_code}\n'
pkill -f "uvicorn app.main" 2>/dev/null
```
Expected: `boss@x.com -> admin`; `grant ana analyst: analyst`; `analyst hitting admin api: 403`.

- [ ] **Step 5: Commit**
```bash
git add app/admin.py scripts/grant_role.py app/main.py
git commit -m "feat(stats): admin role API + grant_role bootstrap script"
```

---

### Task 4: Persist `capture_meta` on save (B touchpoint)

**Files:**
- Modify: `app/pulls.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Accept + store `capture_meta` in `app/pulls.py`.** In `save_pull`, add a form field and parse it. Change the signature to add (after `segmentation_warning`):
```python
    capture_meta: str | None = Form(None, description="Guided-capture metadata JSON"),
```
After the `cards` JSON parse block, add:
```python
    meta_obj: dict | None = None
    if capture_meta:
        try:
            meta_obj = json.loads(capture_meta)
            assert isinstance(meta_obj, dict)
        except (json.JSONDecodeError, AssertionError):
            raise HTTPException(400, "capture_meta: must be a JSON object")
```
Pass `capture_meta=meta_obj` into `_insert_pull(...)` (add the kwarg), and in `_insert_pull` add the parameter `capture_meta` and set it on the `Pull(...)` constructor: `capture_meta=capture_meta,`.

Concretely, `_insert_pull`'s signature gains `capture_meta,` in the keyword-only list, the `Pull(...)` gains `capture_meta=capture_meta,`, and the `save_pull` call site passes `capture_meta=meta_obj`.

- [ ] **Step 2: Send `capture_meta` from the frontend.** In `frontend/src/api.ts`, extend `savePull`'s `meta` param and body:
```python
# (TypeScript) — meta gains an optional capture_meta object:
```
```typescript
export async function savePull(
  staircase: Blob,
  codeCard: Blob,
  cards: PackCard[],
  meta: {
    capture_path: string;
    pack_confidence: number;
    segmentation_warning: string | null;
    capture_meta?: CaptureMeta | null;
  }
): Promise<SavedPull> {
  const form = new FormData();
  form.append("staircase", staircase, "staircase.jpg");
  form.append("code_card", codeCard, "code.jpg");
  form.append("cards", JSON.stringify(cards));
  form.append("capture_path", meta.capture_path);
  form.append("pack_confidence", String(meta.pack_confidence));
  if (meta.segmentation_warning) form.append("segmentation_warning", meta.segmentation_warning);
  if (meta.capture_meta) form.append("capture_meta", JSON.stringify(meta.capture_meta));
  return parse(
    await fetch(`${base}/pulls`, { method: "POST", credentials: "include", body: form })
  );
}
```

- [ ] **Step 3: Pass the meta through in `frontend/src/App.tsx`.** In `doSave`, the review step already carries `s.meta` (a `CaptureMeta | undefined`). Update the `savePull` call:
```typescript
      const saved = await savePull(s.staircase, s.code, cards, {
        capture_path: s.meta ? "guided" : "upload",
        pack_confidence: s.scan.pack_confidence,
        segmentation_warning: s.scan.segmentation_warning,
        capture_meta: s.meta ?? null,
      });
```

- [ ] **Step 4: Smoke** (backend stores capture_meta). Env exported:
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8022 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8022
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"m@x.com","password":"longpassword1","handle":"mmm"}' -o /dev/null
curl -s -c /tmp/cm -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=m@x.com&password=longpassword1' -o /dev/null
META='{"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}'
curl -s -b /tmp/cm -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[{"row_index":0,"card_number":"012/202","name":"A","confidence":0.9}]' -F capture_path=guided -F "capture_meta=$META" -o /dev/null -w 'save=%{http_code}\n'
echo -n "stored capture_meta: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select capture_meta->>'declared_count' from pull limit 1;"
pkill -f "uvicorn app.main" 2>/dev/null
echo -n "frontend builds: "; (cd frontend && npm run build >/tmp/fe.log 2>&1 && echo ok || (tail -5 /tmp/fe.log; echo FAIL))
```
Expected: `save=201`; `stored capture_meta: 3`; `frontend builds: ok`.

- [ ] **Step 5: Commit**
```bash
git add app/pulls.py frontend/src/api.ts frontend/src/App.tsx
git commit -m "feat(stats): persist capture_meta on pull save (enables guided re-derivation)"
```

---

### Task 5: Stats config + re-derivation

**Files:**
- Create: `app/stats/config.py`
- Create: `app/stats/rederive.py`

- [ ] **Step 1: Create `app/stats/config.py`**:
```python
"""Env-driven tuning for the stats batch."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class StatsSettings:
    min_sample: int = field(default_factory=lambda: _i("PACK_STATS_MIN_SAMPLE", 30))
    z_threshold: float = field(default_factory=lambda: _f("PACK_STATS_Z_THRESHOLD", 3.0))
    concentration: float = field(default_factory=lambda: _f("PACK_STATS_CONCENTRATION", 0.5))
    prior_strength: float = field(default_factory=lambda: _f("PACK_STATS_PRIOR_STRENGTH", 20.0))
    cron_token: str = field(default_factory=lambda: os.environ.get("STATS_CRON_TOKEN", "").strip())


def stats_settings() -> StatsSettings:
    return StatsSettings()
```

- [ ] **Step 2: Create `app/stats/rederive.py`**:
```python
"""Re-derive verified pulls server-side from their stored staircase photos.

Stats must not trust client-submitted cards; this regenerates authoritative cards
by re-running the scan pipeline, guided by the persisted capture_meta when present.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select

from app.db.models import DeriveStatus, Pull, PullCardDerived
from app.db.session import async_session_maker
from app.pack.pipeline import scan_pack
from app.storage import open_photo

log = logging.getLogger("pokemon_scanner.stats.rederive")


async def rederive_pending(limit: int = 200) -> int:
    """Re-derive up to `limit` verified pulls awaiting derivation. Returns the count processed."""
    processed = 0
    async with async_session_maker() as session:
        pulls = (
            await session.execute(
                select(Pull)
                .where(Pull.verified.is_(True), Pull.derive_status == DeriveStatus.pending)
                .limit(limit)
            )
        ).scalars().all()

        for pull in pulls:
            try:
                staircase = open_photo(pull.staircase_photo_path)
            except FileNotFoundError:
                log.warning("rederive.photo_missing pull=%s", pull.id)
                pull.derive_status = DeriveStatus.failed
                pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
                continue
            try:
                # code bytes are irrelevant here (verification already settled in B);
                # pass empty so the pipeline skips code OCR. capture_meta drives guided seg.
                resp = await scan_pack(staircase, b"", pull.capture_meta)
            except Exception as e:  # pragma: no cover - defensive; one bad photo must not kill the batch
                log.warning("rederive.scan_failed pull=%s err=%r", pull.id, e)
                pull.derive_status = DeriveStatus.failed
                pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
                continue

            for card in resp.cards:
                session.add(PullCardDerived(
                    pull_id=pull.id, row_index=card.row_index, card_number=card.card_number,
                    set_id=card.set_id, set_code=card.set_code, set_name=card.set_name,
                    name=card.name, rarity=card.rarity, match_id=card.match_id,
                    confidence=card.confidence,
                ))
            pull.derive_status = DeriveStatus.done
            pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
            processed += 1

        await session.commit()
    log.info("rederive.done processed=%s", processed)
    return processed
```

- [ ] **Step 3: Smoke** (re-derive a saved pull against the stub PokéWallet so matches resolve). Env exported + point at the stub from sub-project A:
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key
.venv/bin/uvicorn app.main:app --port 8023 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8023
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"r@x.com","password":"longpassword1","handle":"rrr"}' -o /dev/null
curl -s -c /tmp/cr -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=r@x.com&password=longpassword1' -o /dev/null
META='{"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}'
# client claims a single bogus card; re-derivation will find the real 3
curl -s -b /tmp/cr -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[{"row_index":0,"card_number":"999/999","name":"FAKE","confidence":0.1}]' -F capture_path=guided -F "capture_meta=$META" -o /dev/null -w 'save=%{http_code}\n'
.venv/bin/python -c "
import asyncio
from app.stats.rederive import rederive_pending
print('processed:', asyncio.run(rederive_pending()))
"
echo -n "derived card count: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from pull_card_derived;"
echo -n "derived names (server, not FAKE): "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select string_agg(name,',') from pull_card_derived;"
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null
```
Expected: `save=201`; `processed: 1`; `derived card count: 3`; derived names contain the stub cards (`Test Mon A/B/C`), NOT `FAKE` — proving stats won't trust client data.

- [ ] **Step 4: Commit**
```bash
git add app/stats/config.py app/stats/rederive.py
git commit -m "feat(stats): config + server-side re-derivation of verified pulls"
```

---

### Task 6: Prior source + blend

**Files:**
- Create: `app/stats/prior.py`
- Create: `app/stats/data/priors.json`

- [ ] **Step 1: Create `app/stats/data/priors.json`** (approximate per-rarity per-pack rates; values are seeds that real data overrides):
```json
{
  "default_strength": 20,
  "default_card_rate": 0.05,
  "rarity": {
    "Common": 0.99,
    "Uncommon": 0.95,
    "Rare": 0.5,
    "Double Rare": 0.33,
    "Ultra Rare": 0.16,
    "Illustration Rare": 0.2,
    "Special Illustration Rare": 0.05,
    "Hyper Rare": 0.04,
    "Shiny Rare": 0.08,
    "Promo": 0.0
  }
}
```

- [ ] **Step 2: Create `app/stats/prior.py`**:
```python
"""Prior source + Beta-Binomial blend.

A prior is encoded as pseudo-counts (alpha hits out of beta pseudo-packs). The blend
is (alpha + hits) / (beta + packs): prior-dominated at low N, data-dominated at high N.
The seed-file source ships approximate per-rarity rates; a live scraper would be a
drop-in PriorSource later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.stats.config import stats_settings

_DATA = Path(__file__).resolve().parent / "data" / "priors.json"


def beta_binomial_blend(hits: int, packs: int, alpha: float, beta: float) -> float:
    denom = beta + packs
    if denom <= 0:
        return 0.0
    return (alpha + hits) / denom


class PriorSource(Protocol):
    def rarity_prior(self, set_id: str, rarity: str) -> tuple[float, float]: ...
    def card_prior(self, set_id: str, match_id: str) -> tuple[float, float]: ...


class SeedFilePriorSource:
    def __init__(self, path: Path | None = None) -> None:
        raw = json.loads((path or _DATA).read_text(encoding="utf-8"))
        self._strength = float(raw.get("default_strength", 20))
        self._default_card_rate = float(raw.get("default_card_rate", 0.05))
        self._rarity: dict[str, float] = raw.get("rarity", {})

    def _ab(self, rate: float, strength: float) -> tuple[float, float]:
        rate = min(max(rate, 0.0), 1.0)
        return rate * strength, (1.0 - rate) * strength

    def rarity_prior(self, set_id: str, rarity: str) -> tuple[float, float]:
        rate = self._rarity.get(rarity, self._default_card_rate)
        return self._ab(rate, self._strength)

    def card_prior(self, set_id: str, match_id: str) -> tuple[float, float]:
        # No per-card seed in v1 -> a weak generic prior that just smooths low N.
        return self._ab(self._default_card_rate, self._strength)


def default_prior_source() -> SeedFilePriorSource:
    return SeedFilePriorSource()
```

- [ ] **Step 3: Smoke**:
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/python -c "
from app.stats.prior import default_prior_source, beta_binomial_blend
ps = default_prior_source()
a, b = ps.rarity_prior('x', 'Special Illustration Rare')
print('SIR prior a,b:', round(a,2), round(b,2))
# low N: blended near prior rate (~0.05)
print('low N blended:', round(beta_binomial_blend(0, 2, a, b), 3))
# high N: blended approaches observed (0.5)
print('high N blended:', round(beta_binomial_blend(500, 1000, a, b), 3))
"
```
Expected: `SIR prior a,b: 1.0 19.0`; `low N blended:` ~`0.045`; `high N blended:` ~`0.491` (close to the observed 0.5, prior washed out).

- [ ] **Step 4: Commit**
```bash
git add app/stats/prior.py app/stats/data/priors.json
git commit -m "feat(stats): Beta-Binomial prior (seed-file source) + blend"
```

---

### Task 7: Aggregation (set / card / rarity stats + prior blend)

**Files:**
- Create: `app/stats/aggregate.py`

- [ ] **Step 1: Create `app/stats/aggregate.py`**:
```python
"""Aggregate server-derived cards of verified pulls into snapshot-scoped stats."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CardStat, Pull, PullCardDerived, RarityStat, SetStat
from app.stats.prior import PriorSource

log = logging.getLogger("pokemon_scanner.stats.aggregate")


def _pull_set_id(cards: list[PullCardDerived]) -> str | None:
    ids = [c.set_id for c in cards if c.set_id]
    if not ids:
        return None
    return Counter(ids).most_common(1)[0][0]


async def aggregate_snapshot(session: AsyncSession, snapshot_id: uuid.UUID, prior: PriorSource) -> None:
    """Compute set/card/rarity stats for all verified pulls into the given snapshot."""
    # Load verified pulls + their derived cards.
    pulls = (
        await session.execute(select(Pull).where(Pull.verified.is_(True)))
    ).scalars().all()
    derived = (
        await session.execute(
            select(PullCardDerived).join(Pull, PullCardDerived.pull_id == Pull.id).where(Pull.verified.is_(True))
        )
    ).scalars().all()

    by_pull: dict[uuid.UUID, list[PullCardDerived]] = defaultdict(list)
    for c in derived:
        by_pull[c.pull_id].append(c)

    set_pack_count: Counter[str] = Counter()
    # per set: card match_id -> packs containing it; rarity -> packs containing it
    card_hits: dict[str, Counter[str]] = defaultdict(Counter)
    card_meta: dict[str, dict[str, tuple[str | None, str | None]]] = defaultdict(dict)
    rarity_hits: dict[str, Counter[str]] = defaultdict(Counter)

    for pull in pulls:
        cards = by_pull.get(pull.id, [])
        set_id = _pull_set_id(cards)
        if set_id is None:
            continue
        set_pack_count[set_id] += 1
        seen_cards = {c.match_id for c in cards if c.match_id}
        for mid in seen_cards:
            card_hits[set_id][mid] += 1
            sample = next(c for c in cards if c.match_id == mid)
            card_meta[set_id][mid] = (sample.card_number, sample.name)
        seen_rarities = {c.rarity for c in cards if c.rarity}
        for rar in seen_rarities:
            rarity_hits[set_id][rar] += 1

    for set_id, packs in set_pack_count.items():
        session.add(SetStat(snapshot_id=snapshot_id, set_id=set_id, verified_pack_count=packs))
        for mid, hits in card_hits[set_id].items():
            a, b = prior.card_prior(set_id, mid)
            cn, nm = card_meta[set_id][mid]
            session.add(CardStat(
                snapshot_id=snapshot_id, set_id=set_id, match_id=mid, card_number=cn, name=nm,
                hits=hits, packs=packs, raw_rate=hits / packs,
                blended_rate=(a + hits) / (b + packs),
            ))
        for rar, hits in rarity_hits[set_id].items():
            a, b = prior.rarity_prior(set_id, rar)
            session.add(RarityStat(
                snapshot_id=snapshot_id, set_id=set_id, rarity=rar,
                packs_with_rarity=hits, raw_rate=hits / packs,
                blended_rate=(a + hits) / (b + packs),
            ))
    await session.flush()
    log.info("aggregate.done sets=%s", len(set_pack_count))
```

- [ ] **Step 2: Smoke** (verified against derived rows produced in Task 5; this just checks aggregate runs and writes rows). Env exported, stub running, with a re-derived pull present from Task 5's flow — re-run a quick end-to-end:
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE; TRUNCATE stats_snapshot CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key
.venv/bin/uvicorn app.main:app --port 8024 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8024
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"g@x.com","password":"longpassword1","handle":"ggg"}' -o /dev/null
curl -s -c /tmp/cg -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=g@x.com&password=longpassword1' -o /dev/null
META='{"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}'
curl -s -b /tmp/cg -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[]' -F capture_path=guided -F "capture_meta=$META" -o /dev/null
.venv/bin/python -c "
import asyncio, uuid
from app.stats.rederive import rederive_pending
from app.stats.aggregate import aggregate_snapshot
from app.stats.prior import default_prior_source
from app.db.models import StatsSnapshot
from app.db.session import async_session_maker
async def go():
    await rederive_pending()
    async with async_session_maker() as s:
        snap = StatsSnapshot(trigger='cli', status='running'); s.add(snap); await s.flush()
        await aggregate_snapshot(s, snap.id, default_prior_source())
        snap.status='done'; await s.commit()
        print('snapshot', snap.id)
asyncio.run(go())
"
echo -n "set_stat rows: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select set_id||':'||verified_pack_count from set_stat;"
echo -n "card_stat rows: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from card_stat;"
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null
```
Expected: a `set_stat` row for the SVI/SSH/VIV set the fixture resolves to with count `1`; `card_stat rows: 3`.

- [ ] **Step 3: Commit**
```bash
git add app/stats/aggregate.py
git commit -m "feat(stats): aggregate derived cards into set/card/rarity stats with prior blend"
```

---

### Task 8: Anomaly detectors

**Files:**
- Create: `app/stats/anomaly.py`

- [ ] **Step 1: Create `app/stats/anomaly.py`**:
```python
"""Anomaly detectors over a freshly-aggregated snapshot.

deviation_from_prior: observed card/rarity rate is statistically far from the prior.
submitter_concentration: one trainer contributes an outsized share of a set's packs.
Both only consider sets/cards with packs >= min_sample. Findings are flagged for review.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Anomaly, CardStat, Pull, PullCardDerived, SetStat
from app.stats.config import stats_settings
from app.stats.prior import PriorSource

log = logging.getLogger("pokemon_scanner.stats.anomaly")


def _z(observed_rate: float, prior_rate: float, packs: int) -> float:
    # Binomial SE under the prior mean; guard tiny denominators.
    se = math.sqrt(max(prior_rate * (1 - prior_rate), 1e-9) / max(packs, 1))
    return (observed_rate - prior_rate) / se if se > 0 else 0.0


async def detect(session: AsyncSession, snapshot_id: uuid.UUID, prior: PriorSource) -> int:
    cfg = stats_settings()
    found = 0

    set_packs = {
        s.set_id: s.verified_pack_count
        for s in (await session.execute(select(SetStat).where(SetStat.snapshot_id == snapshot_id))).scalars()
    }

    # deviation_from_prior on card stats
    cards = (await session.execute(select(CardStat).where(CardStat.snapshot_id == snapshot_id))).scalars().all()
    for c in cards:
        if c.packs < cfg.min_sample:
            continue
        a, b = prior.card_prior(c.set_id, c.match_id)
        prior_rate = a / (a + b) if (a + b) > 0 else 0.0
        z = _z(c.raw_rate, prior_rate, c.packs)
        if abs(z) > cfg.z_threshold:
            session.add(Anomaly(
                snapshot_id=snapshot_id, detector="deviation_from_prior", target_type="card",
                set_id=c.set_id, card_match_id=c.match_id, severity=abs(z),
                detail={"observed": c.raw_rate, "prior": prior_rate, "z": z, "packs": c.packs},
            ))
            found += 1

    # submitter_concentration per set (needs trainer attribution from verified pulls)
    rows = (
        await session.execute(
            select(Pull.id, Pull.trainer_id, PullCardDerived.set_id)
            .join(PullCardDerived, PullCardDerived.pull_id == Pull.id)
            .where(Pull.verified.is_(True))
        )
    ).all()
    # determine each pull's set (modal) + trainer
    pull_set: dict[uuid.UUID, Counter] = defaultdict(Counter)
    pull_trainer: dict[uuid.UUID, uuid.UUID] = {}
    for pull_id, trainer_id, set_id in rows:
        pull_trainer[pull_id] = trainer_id
        if set_id:
            pull_set[pull_id][set_id] += 1
    set_trainer_packs: dict[str, Counter] = defaultdict(Counter)
    for pull_id, ctr in pull_set.items():
        if not ctr:
            continue
        sid = ctr.most_common(1)[0][0]
        set_trainer_packs[sid][pull_trainer[pull_id]] += 1

    for sid, packs in set_packs.items():
        if packs < cfg.min_sample:
            continue
        tc = set_trainer_packs.get(sid)
        if not tc:
            continue
        top_trainer, top_packs = tc.most_common(1)[0]
        share = top_packs / packs
        if share > cfg.concentration:
            session.add(Anomaly(
                snapshot_id=snapshot_id, detector="submitter_concentration", target_type="set",
                set_id=sid, card_match_id=None, severity=share,
                detail={"top_trainer": str(top_trainer), "share": share, "packs": packs},
            ))
            found += 1

    await session.flush()
    log.info("anomaly.done found=%s", found)
    return found
```

- [ ] **Step 2: Smoke** (force a concentration anomaly with a low threshold). Env exported; reuse a re-derived pull:
```bash
cd /Users/kailee/pokemon-card-scanner
export PACK_STATS_MIN_SAMPLE=1 PACK_STATS_CONCENTRATION=0.5 PACK_STATS_Z_THRESHOLD=3.0
.venv/bin/python -c "
import asyncio, uuid
from app.stats.aggregate import aggregate_snapshot
from app.stats.anomaly import detect
from app.stats.prior import default_prior_source
from app.db.models import StatsSnapshot
from app.db.session import async_session_maker
async def go():
    async with async_session_maker() as s:
        snap = StatsSnapshot(trigger='cli', status='running'); s.add(snap); await s.flush()
        await aggregate_snapshot(s, snap.id, default_prior_source())
        n = await detect(s, snap.id, default_prior_source())
        snap.status='done'; await s.commit()
        print('anomalies found:', n)
asyncio.run(go())
"
echo -n "concentration anomalies: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select detector||' '||round(severity::numeric,2) from anomaly where detector='submitter_concentration';"
```
Expected: with a single trainer owning 100% of the set's 1 pack and min_sample=1, a `submitter_concentration` anomaly with severity `1.00` is found.

- [ ] **Step 3: Commit**
```bash
git add app/stats/anomaly.py
git commit -m "feat(stats): deviation-from-prior + submitter-concentration anomaly detectors"
```

---

### Task 9: Batch orchestrator + recompute endpoint (admin or cron token)

**Files:**
- Create: `app/stats/run_batch.py`
- Modify: `app/admin.py`

- [ ] **Step 1: Create `app/stats/run_batch.py`** (advisory lock prevents overlapping runs; usable as a function and a CLI):
```python
"""Orchestrate the stats batch: re-derive -> snapshot -> aggregate -> anomalies.

Runs inside the web service (needs Railway-volume access to read pull photos).
A Postgres advisory lock makes concurrent triggers (cron + manual) a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from app.db.models import StatsSnapshot
from app.db.session import async_session_maker
from app.stats.aggregate import aggregate_snapshot
from app.stats.anomaly import detect
from app.stats.prior import default_prior_source
from app.stats.rederive import rederive_pending

log = logging.getLogger("pokemon_scanner.stats.run_batch")

_LOCK_KEY = 880117  # arbitrary app-wide advisory lock id for the stats batch


async def run_batch(trigger: str = "manual") -> str | None:
    """Run a full batch. Returns the snapshot id, or None if another run holds the lock."""
    await rederive_pending()
    prior = default_prior_source()
    async with async_session_maker() as session:
        got = (await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar()
        if not got:
            log.info("run_batch.skipped lock_held")
            return None
        try:
            snap = StatsSnapshot(trigger=trigger, status="running")
            session.add(snap)
            await session.flush()
            try:
                await aggregate_snapshot(session, snap.id, prior)
                await detect(session, snap.id, prior)
                snap.status = "done"
                await session.commit()
                log.info("run_batch.done snapshot=%s trigger=%s", snap.id, trigger)
                return str(snap.id)
            except Exception:
                snap.status = "failed"
                await session.commit()
                raise
        finally:
            await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            await session.commit()


if __name__ == "__main__":
    print(asyncio.run(run_batch("cli")))
```

- [ ] **Step 2: Add the recompute endpoint to `app/admin.py`.**

First, change the top `from fastapi import APIRouter, HTTPException` line to:
```python
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
```
and add these imports to the existing import block:
```python
from app.db.users import fastapi_users
from app.stats.config import stats_settings
from app.stats.run_batch import run_batch
```
(`Role` and `Trainer` are already imported in `admin.py` from Task 3.)

Then append the endpoint. It accepts **either** an admin session **or** the cron bearer token, using an optional-user dependency (returns `None` instead of 401 when unauthenticated, so the token path works):
```python
# optional user: None when unauthenticated, so the cron-token path isn't blocked by a 401
_optional_user = fastapi_users.current_user(active=True, optional=True)


@router.post("/stats/recompute", status_code=202)
async def recompute_stats(
    background: BackgroundTasks,
    authorization: str | None = Header(default=None),
    user: Trainer | None = Depends(_optional_user),
) -> dict:
    """Trigger a stats batch. Authorized by an admin session OR the cron bearer token."""
    token = stats_settings().cron_token
    bearer = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token and bearer == token:
        background.add_task(run_batch, "cron")
        return {"status": "accepted", "trigger": "cron"}
    if user is not None and user.role == Role.admin:
        background.add_task(run_batch, "manual")
        return {"status": "accepted", "trigger": "manual"}
    raise HTTPException(403, "admin role or cron token required")
```

- [ ] **Step 3: Smoke** (token path + admin path + overlap). Env exported incl `STATS_CRON_TOKEN=dev-cron-token-123`, stub running:
```bash
cd /Users/kailee/pokemon-card-scanner
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE; TRUNCATE stats_snapshot CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key STATS_CRON_TOKEN=dev-cron-token-123
.venv/bin/uvicorn app.main:app --port 8025 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8025
# seed a verified pull
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"b@x.com","password":"longpassword1","handle":"bbb"}' -o /dev/null
curl -s -c /tmp/cb -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=b@x.com&password=longpassword1' -o /dev/null
curl -s -b /tmp/cb -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[]' -F capture_path=guided -F 'capture_meta={"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}' -o /dev/null
echo -n "cron token recompute: "; curl -s -X POST $BASE/admin/stats/recompute -H "authorization: Bearer dev-cron-token-123" -o /dev/null -w '%{http_code}\n'
echo -n "no-auth recompute: "; curl -s -X POST $BASE/admin/stats/recompute -o /dev/null -w '%{http_code}\n'
sleep 3
echo -n "snapshots done: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select count(*) from stats_snapshot where status='done';"
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null
```
Expected: `cron token recompute: 202`; `no-auth recompute: 403`; `snapshots done:` ≥ `1`.

- [ ] **Step 4: Commit**
```bash
git add app/stats/run_batch.py app/admin.py
git commit -m "feat(stats): batch orchestrator (advisory-locked) + token/admin recompute endpoint"
```

---

### Task 10: Stats read API

**Files:**
- Create: `app/stats_api.py`
- Modify: `app/main.py`

- [ ] **Step 1: Create `app/stats_api.py`**:
```python
"""Analyst-facing read API over the current stats snapshot."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Anomaly, CardStat, RarityStat, SetStat, StatsSnapshot
from app.db.session import async_session_maker
from app.db.users import CurrentAnalyst

router = APIRouter(prefix="/stats", tags=["stats"])


class SetSummary(BaseModel):
    set_id: str
    verified_pack_count: int


class SetDetail(BaseModel):
    set_id: str
    verified_pack_count: int
    cards: list[dict]
    rarities: list[dict]


class AnomalyOut(BaseModel):
    id: uuid.UUID
    detector: str
    target_type: str
    set_id: str
    card_match_id: str | None
    severity: float
    detail: dict
    status: str


async def _current_snapshot_id(session) -> uuid.UUID | None:
    return (
        await session.execute(
            select(StatsSnapshot.id).where(StatsSnapshot.status == "done").order_by(StatsSnapshot.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


@router.get("/sets", response_model=list[SetSummary])
async def list_sets(analyst: CurrentAnalyst) -> list[SetSummary]:
    async with async_session_maker() as session:
        snap = await _current_snapshot_id(session)
        if snap is None:
            return []
        rows = (await session.execute(select(SetStat).where(SetStat.snapshot_id == snap))).scalars().all()
        return [SetSummary(set_id=s.set_id, verified_pack_count=s.verified_pack_count) for s in rows]


@router.get("/sets/{set_id}", response_model=SetDetail)
async def set_detail(analyst: CurrentAnalyst, set_id: str) -> SetDetail:
    async with async_session_maker() as session:
        snap = await _current_snapshot_id(session)
        if snap is None:
            raise HTTPException(404, "no stats computed yet")
        ss = (await session.execute(
            select(SetStat).where(SetStat.snapshot_id == snap, SetStat.set_id == set_id)
        )).scalar_one_or_none()
        if ss is None:
            raise HTTPException(404, "set not in current snapshot")
        cards = (await session.execute(
            select(CardStat).where(CardStat.snapshot_id == snap, CardStat.set_id == set_id).order_by(CardStat.raw_rate)
        )).scalars().all()
        rarities = (await session.execute(
            select(RarityStat).where(RarityStat.snapshot_id == snap, RarityStat.set_id == set_id)
        )).scalars().all()
        return SetDetail(
            set_id=set_id, verified_pack_count=ss.verified_pack_count,
            cards=[{"match_id": c.match_id, "card_number": c.card_number, "name": c.name,
                    "hits": c.hits, "packs": c.packs, "raw_rate": c.raw_rate, "blended_rate": c.blended_rate}
                   for c in cards],
            rarities=[{"rarity": r.rarity, "packs_with_rarity": r.packs_with_rarity,
                       "raw_rate": r.raw_rate, "blended_rate": r.blended_rate} for r in rarities],
        )


@router.get("/anomalies", response_model=list[AnomalyOut])
async def list_anomalies(analyst: CurrentAnalyst, status: str = "open") -> list[AnomalyOut]:
    async with async_session_maker() as session:
        snap = await _current_snapshot_id(session)
        if snap is None:
            return []
        rows = (await session.execute(
            select(Anomaly).where(Anomaly.snapshot_id == snap, Anomaly.status == status).order_by(Anomaly.severity.desc())
        )).scalars().all()
        return [AnomalyOut(id=a.id, detector=a.detector, target_type=a.target_type, set_id=a.set_id,
                           card_match_id=a.card_match_id, severity=a.severity, detail=a.detail, status=a.status)
                for a in rows]


class AnomalyStatus(BaseModel):
    status: str  # reviewed|dismissed


@router.patch("/anomalies/{anomaly_id}", response_model=AnomalyOut)
async def update_anomaly(analyst: CurrentAnalyst, anomaly_id: uuid.UUID, body: AnomalyStatus) -> AnomalyOut:
    if body.status not in ("reviewed", "dismissed", "open"):
        raise HTTPException(400, "status must be reviewed|dismissed|open")
    async with async_session_maker() as session:
        a = await session.get(Anomaly, anomaly_id)
        if a is None:
            raise HTTPException(404, "anomaly not found")
        a.status = body.status
        await session.commit()
        return AnomalyOut(id=a.id, detector=a.detector, target_type=a.target_type, set_id=a.set_id,
                          card_match_id=a.card_match_id, severity=a.severity, detail=a.detail, status=a.status)
```

- [ ] **Step 2: Mount the stats router in `app/main.py`** — add import + include (before static mount):
```python
from app.stats_api import router as stats_router
```
```python
app.include_router(stats_router)
```

- [ ] **Step 3: Smoke** (analyst can read, trainer 403). Env exported, with a done snapshot present (run recompute first as in Task 9):
```bash
cd /Users/kailee/pokemon-card-scanner
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key STATS_CRON_TOKEN=dev-cron-token-123
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE; TRUNCATE stats_snapshot CASCADE;" >/dev/null 2>&1
.venv/bin/uvicorn app.main:app --port 8026 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8026
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"an@x.com","password":"longpassword1","handle":"ann"}' -o /dev/null
curl -s -c /tmp/cn -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=an@x.com&password=longpassword1' -o /dev/null
# seed a pull + recompute via token
curl -s -b /tmp/cn -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[]' -F capture_path=guided -F 'capture_meta={"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}' -o /dev/null
curl -s -X POST $BASE/admin/stats/recompute -H "authorization: Bearer dev-cron-token-123" -o /dev/null; sleep 3
echo -n "trainer /stats/sets: "; curl -s -b /tmp/cn $BASE/stats/sets -o /dev/null -w '%{http_code}\n'
.venv/bin/python scripts/grant_role.py an@x.com analyst
curl -s -c /tmp/cn -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=an@x.com&password=longpassword1' -o /dev/null
echo -n "analyst /stats/sets: "; curl -s -b /tmp/cn $BASE/stats/sets | python3 -c "import sys,json;d=json.load(sys.stdin);print('sets',len(d),'packs',d[0]['verified_pack_count'] if d else 0)"
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null
```
Expected: `trainer /stats/sets: 403`; after the grant, `analyst /stats/sets: sets 1 packs 1`.

- [ ] **Step 4: Commit**
```bash
git add app/stats_api.py app/main.py
git commit -m "feat(stats): analyst read API (sets, set detail, anomalies)"
```

---

### Task 11: Dashboard (frontend)

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/dashboard/Dashboard.tsx`, `SetStats.tsx`, `Anomalies.tsx`, `RoleAdmin.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add stats + admin client fns and `role` to `frontend/src/api.ts`.** Add `role` to the `Trainer` interface:
```typescript
export interface Trainer {
  id: string;
  email: string;
  handle: string;
  role: string;
  is_active: boolean;
}
```
Append the client functions:
```typescript
export interface SetSummary { set_id: string; verified_pack_count: number; }
export interface SetDetail {
  set_id: string;
  verified_pack_count: number;
  cards: { match_id: string; card_number: string | null; name: string | null; hits: number; packs: number; raw_rate: number; blended_rate: number; }[];
  rarities: { rarity: string; packs_with_rarity: number; raw_rate: number; blended_rate: number; }[];
}
export interface AnomalyRow {
  id: string; detector: string; target_type: string; set_id: string;
  card_match_id: string | null; severity: number; detail: Record<string, unknown>; status: string;
}
export interface AdminTrainer { id: string; email: string; handle: string; role: string; }

export async function statsSets(): Promise<SetSummary[]> {
  return parse(await fetch(`${base}/stats/sets`, { credentials: "include" }));
}
export async function statsSetDetail(setId: string): Promise<SetDetail> {
  return parse(await fetch(`${base}/stats/sets/${encodeURIComponent(setId)}`, { credentials: "include" }));
}
export async function statsAnomalies(status = "open"): Promise<AnomalyRow[]> {
  return parse(await fetch(`${base}/stats/anomalies?status=${status}`, { credentials: "include" }));
}
export async function updateAnomaly(id: string, status: string): Promise<AnomalyRow> {
  return parse(await fetch(`${base}/stats/anomalies/${id}`, {
    method: "PATCH", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ status }),
  }));
}
export async function recomputeStats(): Promise<void> {
  const res = await fetch(`${base}/admin/stats/recompute`, { method: "POST", credentials: "include" });
  if (!res.ok) throw new Error(`recompute failed (${res.status})`);
}
export async function adminTrainers(query = ""): Promise<AdminTrainer[]> {
  return parse(await fetch(`${base}/admin/trainers?query=${encodeURIComponent(query)}`, { credentials: "include" }));
}
export async function setTrainerRole(id: string, role: string): Promise<AdminTrainer> {
  return parse(await fetch(`${base}/admin/trainers/${id}/role`, {
    method: "PATCH", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ role }),
  }));
}
```

- [ ] **Step 2: Create `frontend/src/dashboard/SetStats.tsx`**:
```tsx
import { useEffect, useState } from "react";
import { statsSets, statsSetDetail, type SetDetail, type SetSummary } from "../api";

const pct = (r: number) => `${(r * 100).toFixed(1)}%`;

export default function SetStats() {
  const [sets, setSets] = useState<SetSummary[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<SetDetail | null>(null);

  useEffect(() => { statsSets().then(setSets).catch(() => setSets([])); }, []);
  useEffect(() => { if (sel) statsSetDetail(sel).then(setDetail).catch(() => setDetail(null)); }, [sel]);

  return (
    <div>
      <h3>Sets</h3>
      {sets.length === 0 && <p>No stats yet — run a recompute.</p>}
      <ul className="card-rows">
        {sets.map((s) => (
          <li key={s.set_id} className="card-row">
            <button type="button" onClick={() => setSel(s.set_id)}>
              {s.set_id} · {s.verified_pack_count} packs
            </button>
          </li>
        ))}
      </ul>
      {detail && (
        <div>
          <h3>Set {detail.set_id} — {detail.verified_pack_count} verified packs</h3>
          <h4>Rarity odds</h4>
          <table><tbody>
            {detail.rarities.map((r) => (
              <tr key={r.rarity}><td>{r.rarity}</td><td>{pct(r.blended_rate)}</td><td>(raw {pct(r.raw_rate)})</td></tr>
            ))}
          </tbody></table>
          <h4>Cards</h4>
          <table><tbody>
            {detail.cards.map((c) => (
              <tr key={c.match_id}>
                <td>{c.name ?? c.match_id}</td><td>{c.card_number}</td>
                <td>{pct(c.blended_rate)}</td><td>({c.hits}/{c.packs})</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create `frontend/src/dashboard/Anomalies.tsx`**:
```tsx
import { useEffect, useState } from "react";
import { statsAnomalies, updateAnomaly, type AnomalyRow } from "../api";

export default function Anomalies() {
  const [rows, setRows] = useState<AnomalyRow[]>([]);
  const load = () => statsAnomalies("open").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  const act = async (id: string, status: string) => { await updateAnomaly(id, status); load(); };

  return (
    <div>
      <h3>Open anomalies</h3>
      {rows.length === 0 && <p>None open.</p>}
      <ul className="card-rows">
        {rows.map((a) => (
          <li key={a.id} className="card-row flagged">
            <div className="card-row-body">
              <strong>{a.detector} · {a.target_type} {a.set_id}</strong>
              <span>severity {a.severity.toFixed(2)} · {JSON.stringify(a.detail)}</span>
              <div className="card-row-flag">
                <button type="button" onClick={() => act(a.id, "reviewed")}>Reviewed</button>
                <button type="button" onClick={() => act(a.id, "dismissed")}>Dismiss</button>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 4: Create `frontend/src/dashboard/RoleAdmin.tsx`**:
```tsx
import { useState } from "react";
import { adminTrainers, setTrainerRole, type AdminTrainer } from "../api";

export default function RoleAdmin() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AdminTrainer[]>([]);
  const search = async () => setRows(await adminTrainers(q));
  const change = async (id: string, role: string) => { await setTrainerRole(id, role); search(); };

  return (
    <div>
      <h3>Trainer roles</h3>
      <input value={q} placeholder="email or handle" onChange={(e) => setQ(e.target.value)} />
      <button type="button" onClick={search}>Search</button>
      <ul className="card-rows">
        {rows.map((t) => (
          <li key={t.id} className="card-row">
            <div className="card-row-body">
              <strong>@{t.handle}</strong><span>{t.email} · {t.role}</span>
              <div className="card-row-flag">
                {["trainer", "analyst", "admin"].map((r) => (
                  <button key={r} type="button" disabled={r === t.role} onClick={() => change(t.id, r)}>{r}</button>
                ))}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 5: Create `frontend/src/dashboard/Dashboard.tsx`**:
```tsx
import { useState } from "react";
import { recomputeStats } from "../api";
import { useAuth } from "../auth/AuthContext";
import SetStats from "./SetStats";
import Anomalies from "./Anomalies";
import RoleAdmin from "./RoleAdmin";

export default function Dashboard() {
  const { trainer } = useAuth();
  const [tab, setTab] = useState<"sets" | "anomalies" | "roles">("sets");
  const [msg, setMsg] = useState<string | null>(null);
  const isAdmin = trainer?.role === "admin";

  const recompute = async () => {
    setMsg("Recomputing…");
    try { await recomputeStats(); setMsg("Recompute started — refresh in a moment."); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section>
      <h2>Pull-rate dashboard</h2>
      <nav className="app-header">
        <button type="button" onClick={() => setTab("sets")}>Sets</button>
        <button type="button" onClick={() => setTab("anomalies")}>Anomalies</button>
        {isAdmin && <button type="button" onClick={() => setTab("roles")}>Roles</button>}
        {isAdmin && <button type="button" className="primary" onClick={recompute}>Recompute now</button>}
      </nav>
      {msg && <p className="status">{msg}</p>}
      {tab === "sets" && <SetStats />}
      {tab === "anomalies" && <Anomalies />}
      {tab === "roles" && isAdmin && <RoleAdmin />}
    </section>
  );
}
```

- [ ] **Step 6: Add the role-gated nav + view in `frontend/src/App.tsx`.** Add `Dashboard` import and a `"dashboard"` view. In the header nav (where "My Pulls" is), add — only for analyst/admin:
```tsx
          {(trainer?.role === "analyst" || trainer?.role === "admin") && (
            <button type="button" onClick={() => setView("dashboard")}>Dashboard</button>
          )}
```
Add the import:
```tsx
import Dashboard from "./dashboard/Dashboard";
```
Extend the `view` state type to include `"dashboard"` and render it:
```tsx
      {view === "dashboard" && (trainer?.role === "analyst" || trainer?.role === "admin") && <Dashboard />}
```
(The `view` state is currently `useState<"scan" | "pulls">("scan")`; change to `useState<"scan" | "pulls" | "dashboard">("scan")`.)

- [ ] **Step 7: Build**
```bash
cd /Users/kailee/pokemon-card-scanner/frontend && npm run build 2>&1 | grep -E "built in|error" | head
```
Expected: `✓ built in …`, no errors.

- [ ] **Step 8: Commit**
```bash
git add frontend/src/api.ts frontend/src/dashboard/ frontend/src/App.tsx
git commit -m "feat(stats): role-gated pull-rate dashboard (sets, anomalies, role admin)"
```

---

### Task 12: Deployment — env, cron service, final smoke

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add the stats env vars to `.env.example`** — append before the `CORS_ORIGINS` line:
```
# Stats batch (sub-project C)
STATS_CRON_TOKEN=
PACK_STATS_MIN_SAMPLE=30
PACK_STATS_Z_THRESHOLD=3.0
PACK_STATS_CONCENTRATION=0.5
PACK_STATS_PRIOR_STRENGTH=20
```

- [ ] **Step 2: Document the Railway cron service.** Append a `## Stats cron` section to `.env.example` as a comment block (the cron service is configured in the Railway dashboard, not in repo config, since it's a second service):
```
# --- Railway stats cron (USER ACTION, configured in Railway, not here) ---
# Add a second Railway service ("stats-cron") in the same project, sharing env, that runs
# on a schedule (e.g. cron "0 7 * * *") with the command:
#   sh -c 'curl -fsS -X POST "$WEB_ORIGIN/admin/stats/recompute" -H "authorization: Bearer $STATS_CRON_TOKEN"'
# where WEB_ORIGIN is the deployed web service URL. The web service runs the batch
# (it has the photo Volume); the cron service only triggers it.
```

- [ ] **Step 3: Full local end-to-end smoke (the success-criteria walk).** Env exported incl `STATS_CRON_TOKEN`, stub running, fresh DB:
```bash
cd /Users/kailee/pokemon-card-scanner
export DATABASE_URL="postgresql://pcs:pcs@localhost:5432/pcs" AUTH_SECRET="dev-secret-not-for-prod-pad-0123456789" PHOTO_STORAGE_DIR="./var/pulls" COOKIE_SECURE="false" STATS_CRON_TOKEN="dev-cron-token-123" PACK_STATS_MIN_SAMPLE=1 PACK_STATS_CONCENTRATION=0.5
PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -c "TRUNCATE trainer CASCADE; TRUNCATE stats_snapshot CASCADE;" >/dev/null 2>&1
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null; sleep 1
.venv/bin/uvicorn tests.pokewallet_stub:app --port 8901 >/tmp/stub.log 2>&1 & sleep 2
export POKEWALLET_BASE_URL=http://127.0.0.1:8901 POKEWALLET_API_KEY=test-key
.venv/bin/uvicorn app.main:app --port 8027 >/tmp/app.log 2>&1 & sleep 4
BASE=http://127.0.0.1:8027
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"boss@x.com","password":"longpassword1","handle":"boss"}' -o /dev/null
.venv/bin/python scripts/grant_role.py boss@x.com admin
curl -s -c /tmp/cb -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=boss@x.com&password=longpassword1' -o /dev/null
# save a verified pull with spoofed client cards
curl -s -b /tmp/cb -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F 'cards=[{"row_index":0,"name":"SPOOF","confidence":0.1}]' -F capture_path=guided -F 'capture_meta={"guide_positions":[1130,1250,1370],"image_dims":[910,1450],"declared_count":3}' -o /dev/null
# recompute as admin (cookie)
curl -s -b /tmp/cb -X POST $BASE/admin/stats/recompute -o /dev/null -w 'recompute=%{http_code}\n'; sleep 3
echo -n "sets (admin can read): "; curl -s -b /tmp/cb $BASE/stats/sets | python3 -c "import sys,json;d=json.load(sys.stdin);print(len(d),'packs',d[0]['verified_pack_count'] if d else 0)"
echo -n "stats ignore SPOOF: "; PGPASSWORD=pcs psql -h localhost -U pcs -d pcs -tc "select case when count(*) filter (where name='SPOOF')=0 then 'ok (no spoof in stats)' else 'FAIL' end from card_stat;"
echo -n "anomalies present: "; curl -s -b /tmp/cb "$BASE/stats/anomalies?status=open" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"
pkill -f "uvicorn app.main" -f "pokewallet_stub" 2>/dev/null
echo -n "scanner suite still green: "; env -u DATABASE_URL -u AUTH_SECRET .venv/bin/python -m pytest tests/test_scan_pack.py -q 2>&1 | tail -1
```
Expected: `recompute=202`; `sets (admin can read): 1 packs 1`; `stats ignore SPOOF: ok (no spoof in stats)`; `anomalies present:` ≥ `1` (the single-trainer concentration); scanner suite `7 passed`.

- [ ] **Step 4: Railway deploy (USER ACTION).** Migration `0002` applies via the existing `preDeployCommand`. In Railway: set `STATS_CRON_TOKEN` (strong) + the `PACK_STATS_*` vars on the web service; add the **stats-cron** service per Step 2; deploy. Verify on the dashboard that "Recompute now" works and the cron fires on schedule.

- [ ] **Step 5: Commit**
```bash
git add .env.example
git commit -m "chore(stats): env + Railway cron docs for the stats batch"
```

---

## Completion checklist (maps to spec)

- [ ] Role enum + analyst/admin deps + admin grant API + `grant_role.py` bootstrap (Tasks 1–3)
- [ ] `capture_meta` persisted so re-derivation stays guided-accurate (Task 4)
- [ ] Server-side re-derivation; stats use only `pull_card_derived` (Tasks 5, 7, 12 — spoof-ignored asserted)
- [ ] Per-card + per-rarity rates + sample size, snapshot-scoped, batch-materialized (Tasks 1, 7)
- [ ] Beta-Binomial prior from a pluggable seed-file source; low-N→prior, high-N→raw (Task 6)
- [ ] Deviation-from-prior + submitter-concentration anomalies, triageable (Tasks 8, 10, 11)
- [ ] Batch orchestrator inside the web service, advisory-locked; admin + cron-token recompute (Task 9)
- [ ] Role-gated dashboard (sets, anomalies, role admin); not public; API enforces 403 (Tasks 10, 11)
- [ ] Railway cron triggers recompute via token; migration on deploy (Task 12)
- [ ] No automated tests — manual smoke per task; scanner + B flows unaffected (every task; Task 12 re-verifies the scanner suite)
```
