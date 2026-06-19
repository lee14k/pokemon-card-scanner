# Auth + DB Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trainer accounts (email + password, unique handle) and Postgres persistence to the existing FastAPI pack scanner, so a logged-in trainer can save a scanned pack (cards + code card + original photos) with a database-enforced "one verified pull per code card" rule.

**Architecture:** Single FastAPI service. FastAPI-Users (cookie + JWT) over SQLAlchemy 2.0 async + asyncpg provides auth; Alembic owns the schema (no auto-create). A new `app/db/` package holds config/session/models/users; `app/pulls.py` adds the save/list/detail/photo endpoints; `app/storage.py` is the only filesystem boundary (Railway Volume). The pack-scanner pipeline (`app/pack/`) is untouched and stays public.

**Tech Stack:** FastAPI, fastapi-users[sqlalchemy] 15.x, SQLAlchemy 2.0 async, asyncpg, Alembic 1.18, Postgres (Railway), Railway Volume, Vite/React/TS frontend.

**TESTING POLICY — read first:** Per explicit user direction ("don't waste time on any tests"), this plan contains **NO automated tests** — no test files, no pytest additions, no testcontainers. Every task ends with a **manual smoke verification** (run a command, hit an endpoint, check output) instead of a test step. Do not add test files. Code-review subagents (used by the execution skill between tasks) are not tests and are fine.

**Spec:** `docs/superpowers/specs/2026-06-18-auth-db-foundation-design.md`

---

## Prerequisite: a local Postgres for development & verification

Every backend task is verified against a real local Postgres. Start one once (Docker), reused across all tasks:

```bash
docker run --name pcs-pg -e POSTGRES_PASSWORD=pcs -e POSTGRES_USER=pcs -e POSTGRES_DB=pcs \
  -p 5432:5432 -d postgres:16
```

Export this in every shell that runs the app or migrations (note: app config converts `postgresql://` → `postgresql+asyncpg://` automatically):

```bash
export DATABASE_URL="postgresql://pcs:pcs@localhost:5432/pcs"
export AUTH_SECRET="dev-secret-not-for-prod"
export PHOTO_STORAGE_DIR="./var/pulls"
export COOKIE_SECURE="false"
```

If Docker is unavailable, point `DATABASE_URL` at any reachable Postgres 13+.

---

## File structure

```
requirements.txt                  # MODIFIED: + fastapi-users, sqlalchemy, asyncpg, alembic
.env.example                      # MODIFIED: + DATABASE_URL, AUTH_SECRET, PHOTO_STORAGE_DIR, COOKIE_SECURE
.gitignore                        # MODIFIED: + var/  (local photo dir)
railway.toml                      # MODIFIED: + preDeployCommand "alembic upgrade head"
alembic.ini                       # CREATE
alembic/env.py                    # CREATE (async)
alembic/script.py.mako            # CREATE (from template)
alembic/versions/0001_initial.py  # CREATE: citext ext, trainer/pull/pull_card, partial unique index
app/db/__init__.py                # CREATE
app/db/config.py                  # CREATE: env-driven settings + asyncpg URL conversion
app/db/session.py                 # CREATE: Base, engine, async_session_maker, get_async_session
app/db/models.py                  # CREATE: Trainer, Pull, PullCard
app/db/users.py                   # CREATE: schemas, UserManager, cookie+JWT backend, fastapi_users, deps
app/storage.py                    # CREATE: save_pull_photos / open_photo / ensure_photo_dir
app/pulls.py                      # CREATE: POST/GET /pulls, GET /pulls/{id}, photo route, save logic
app/main.py                       # MODIFIED: include auth/users/pulls routers; ensure photo dir on startup
frontend/src/api.ts               # MODIFIED: + register/login/logout/me/savePull/listPulls
frontend/src/auth/AuthContext.tsx # CREATE: trainer session context
frontend/src/auth/AuthForms.tsx   # CREATE: login + register forms
frontend/src/pulls/MyPulls.tsx    # CREATE: list of the trainer's saved pulls
frontend/src/App.tsx              # MODIFIED: auth gate on save, thread photo blobs, real save, my-pulls nav
```

---

### Task 1: Dependencies, DB config, session & Base

**Files:**
- Modify: `requirements.txt`
- Create: `app/db/__init__.py`, `app/db/config.py`, `app/db/session.py`

- [ ] **Step 1: Pin dependencies** — append to `requirements.txt` (final file):

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
python-multipart>=0.0.9
pillow>=10.0.0
numpy>=1.24.0
opencv-python-headless>=4.8.0
pytesseract>=0.3.10
httpx>=0.27.0
fastapi-users[sqlalchemy]>=15.0,<16
sqlalchemy[asyncio]>=2.0.30,<2.1
asyncpg>=0.29
alembic>=1.18,<2
```

Install: `cd /Users/kailee/pokemon-card-scanner && .venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Create `app/db/__init__.py`** (empty):

```python
```

- [ ] **Step 3: Create `app/db/config.py`**:

```python
"""Env-driven settings for the database/auth layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is required but not set")
    return v


def _asyncpg_url(raw: str) -> str:
    # Railway (and most hosts) provide postgresql:// or postgres://; asyncpg needs
    # the +asyncpg driver segment. Replace only the scheme prefix, once.
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw


@dataclass(frozen=True)
class DbSettings:
    database_url: str = field(default_factory=lambda: _asyncpg_url(_require("DATABASE_URL")))
    auth_secret: str = field(default_factory=lambda: _require("AUTH_SECRET"))
    photo_storage_dir: str = field(
        default_factory=lambda: os.environ.get("PHOTO_STORAGE_DIR", "").strip() or "./var/pulls"
    )
    cookie_secure: bool = field(
        default_factory=lambda: os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"
    )
    session_lifetime_seconds: int = 7 * 24 * 3600  # 7 days


def db_settings() -> DbSettings:
    """Fresh read each call so env changes (dev) take effect without reload."""
    return DbSettings()
```

- [ ] **Step 4: Create `app/db/session.py`**:

```python
"""Async SQLAlchemy engine, session factory, declarative Base, and FastAPI session dependency."""

from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator

from sqlalchemy import MetaData, TIMESTAMP
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.db.config import db_settings

_settings = db_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    # Consistent constraint names so Alembic autogenerate diffs are stable.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    # Every Mapped[datetime] becomes timestamptz.
    type_annotation_map = {datetime.datetime: TIMESTAMP(timezone=True)}


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
```

- [ ] **Step 5: Verify config + engine construct (against local Postgres)**

Run (with the env vars from the prerequisite exported):
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/python -c "
import asyncio
from sqlalchemy import text
from app.db.config import db_settings
from app.db.session import engine
print('url:', db_settings().database_url)
assert db_settings().database_url.startswith('postgresql+asyncpg://')
async def ping():
    async with engine.connect() as c:
        print('db ok:', (await c.execute(text('select 1'))).scalar())
asyncio.run(ping())
"
```
Expected: prints a `postgresql+asyncpg://…` URL then `db ok: 1`. (Confirms the driver conversion and a live async connection.)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt app/db/__init__.py app/db/config.py app/db/session.py
git commit -m "feat(db): async SQLAlchemy engine, session, config (asyncpg + Postgres)"
```

---

### Task 2: Models — Trainer, Pull, PullCard

**Files:**
- Create: `app/db/models.py`

- [ ] **Step 1: Create `app/db/models.py`**:

```python
"""SQLAlchemy models: Trainer (FastAPI-Users), Pull, PullCard."""

from __future__ import annotations

import datetime
import uuid

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Trainer(SQLAlchemyBaseUserTableUUID, Base):
    """Account. SQLAlchemyBaseUserTableUUID supplies id (UUID), email,
    hashed_password, is_active, is_superuser, is_verified."""

    __tablename__ = "trainer"

    # Case-insensitive unique public handle (CITEXT). Requires the citext extension
    # (created in the initial migration). Format/casing enforced in the schema layer.
    handle: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    pulls: Mapped[list["Pull"]] = relationship(back_populates="trainer")


class Pull(Base):
    __tablename__ = "pull"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    trainer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trainer.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    capture_path: Mapped[str] = mapped_column(String(16), nullable=False)  # guided|upload
    pack_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    segmentation_warning: Mapped[str | None] = mapped_column(Text, nullable=True)

    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    code_format_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    staircase_photo_path: Mapped[str] = mapped_column(Text, nullable=False)
    code_photo_path: Mapped[str] = mapped_column(Text, nullable=False)

    trainer: Mapped["Trainer"] = relationship(back_populates="pulls")
    cards: Mapped[list["PullCard"]] = relationship(
        back_populates="pull", cascade="all, delete-orphan", order_by="PullCard.row_index"
    )


class PullCard(Base):
    __tablename__ = "pull_card"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pull_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pull.id", ondelete="CASCADE"), index=True, nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)

    card_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    rarity: Mapped[str | None] = mapped_column(Text, nullable=True)
    low_confidence_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pull: Mapped["Pull"] = relationship(back_populates="cards")
```

- [ ] **Step 2: Verify models import and register on the metadata**

Run:
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/python -c "
from app.db.session import Base
import app.db.models  # noqa: register tables
tables = sorted(Base.metadata.tables)
print('tables:', tables)
assert tables == ['pull', 'pull_card', 'trainer'], tables
print('models ok')
"
```
Expected: `tables: ['pull', 'pull_card', 'trainer']` then `models ok`. (No DB needed; just metadata registration.)

- [ ] **Step 3: Commit**

```bash
git add app/db/models.py
git commit -m "feat(db): Trainer, Pull, PullCard models"
```

---

### Task 3: Alembic (async) + initial migration

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_initial.py`

- [ ] **Step 1: Scaffold the async template, then we overwrite env.py**

Run: `cd /Users/kailee/pokemon-card-scanner && .venv/bin/alembic init -t async alembic`
This creates `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`. We replace `alembic.ini`'s url and `env.py` below; keep the generated `script.py.mako`.

- [ ] **Step 2: Edit `alembic.ini`** — set the url to a placeholder (env.py overrides it at runtime). Change the `sqlalchemy.url = …` line to:

```
sqlalchemy.url = overridden_in_env_py
```

- [ ] **Step 3: Replace `alembic/env.py`** with this async version that reads `DATABASE_URL` and targets our metadata:

```python
"""Async Alembic environment: reads DATABASE_URL, targets app metadata."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db.config import db_settings
from app.db.session import Base
import app.db.models  # noqa: F401  register tables on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Runtime override: always use the app's (asyncpg-converted) DATABASE_URL.
config.set_main_option("sqlalchemy.url", db_settings().database_url)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=db_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
```

- [ ] **Step 4: Create the initial migration `alembic/versions/0001_initial.py`** (hand-written; autogenerate would miss the citext extension and the partial unique index):

```python
"""initial schema: trainer, pull, pull_card + citext + partial unique index

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "trainer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("handle", CITEXT(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_trainer"),
        sa.UniqueConstraint("handle", name="uq_trainer_handle"),
    )
    op.create_index("ix_trainer_email", "trainer", ["email"], unique=True)

    op.create_table(
        "pull",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trainer_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("capture_path", sa.String(length=16), nullable=False),
        sa.Column("pack_confidence", sa.Float(), nullable=False),
        sa.Column("segmentation_warning", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("code_normalized", sa.Text(), nullable=True),
        sa.Column("code_confidence", sa.Float(), nullable=False),
        sa.Column("code_format_ok", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("staircase_photo_path", sa.Text(), nullable=False),
        sa.Column("code_photo_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["trainer_id"], ["trainer.id"], name="fk_pull_trainer_id_trainer", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull"),
    )
    op.create_index("ix_pull_trainer_id", "pull", ["trainer_id"])
    op.create_index("ix_pull_verified", "pull", ["verified"])
    # The anti-fraud invariant: at most one VERIFIED pull per normalized code.
    op.create_index(
        "uq_pull_verified_code",
        "pull",
        ["code_normalized"],
        unique=True,
        postgresql_where=sa.text("verified = true AND code_normalized IS NOT NULL"),
    )

    op.create_table(
        "pull_card",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("set_code", sa.Text(), nullable=True),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("low_confidence_reason", sa.Text(), nullable=True),
        sa.Column("match_id", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pull_id"], ["pull.id"], name="fk_pull_card_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull_card"),
    )
    op.create_index("ix_pull_card_pull_id", "pull_card", ["pull_id"])


def downgrade() -> None:
    op.drop_table("pull_card")
    op.drop_index("uq_pull_verified_code", table_name="pull")
    op.drop_index("ix_pull_verified", table_name="pull")
    op.drop_index("ix_pull_trainer_id", table_name="pull")
    op.drop_table("pull")
    op.drop_index("ix_trainer_email", table_name="trainer")
    op.drop_table("trainer")
```

- [ ] **Step 5: Apply the migration and verify schema (against local Postgres)**

Run:
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/alembic upgrade head
.venv/bin/python -c "
import asyncio
from sqlalchemy import text
from app.db.session import engine
async def check():
    async with engine.connect() as c:
        tabs = (await c.execute(text(\"select table_name from information_schema.tables where table_schema='public' order by 1\"))).scalars().all()
        print('tables:', tabs)
        idx = (await c.execute(text(\"select indexname from pg_indexes where tablename='pull' and indexname='uq_pull_verified_code'\"))).scalars().all()
        print('partial index present:', idx == ['uq_pull_verified_code'])
        ext = (await c.execute(text(\"select extname from pg_extension where extname='citext'\"))).scalars().all()
        print('citext ext:', ext == ['citext'])
asyncio.run(check())
"
```
Expected: tables include `alembic_version, pull, pull_card, trainer`; `partial index present: True`; `citext ext: True`.

- [ ] **Step 6: Verify the partial-unique invariant directly in SQL**

Run:
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/python -c "
import asyncio, uuid
from sqlalchemy import text
from app.db.session import engine
async def t():
    async with engine.begin() as c:
        tid = uuid.uuid4()
        await c.execute(text('insert into trainer (id,email,hashed_password,is_active,is_superuser,is_verified,handle) values (:i,:e,:h,true,false,false,:hd)'),
                        {'i':tid,'e':f'{tid}@x.com','h':'x','hd':f'h{str(tid)[:8]}'})
        def ins(code, verified):
            return c.execute(text('insert into pull (id,trainer_id,capture_path,pack_confidence,code,code_normalized,code_confidence,code_format_ok,verified,staircase_photo_path,code_photo_path) values (:i,:t,:cp,0,:c,:cn,0,true,:v,:s,:o)'),
                             {'i':uuid.uuid4(),'t':tid,'cp':'guided','c':code,'cn':code,'v':verified,'s':'a','o':'b'})
        await ins('ABCDEF', True)   # first verified — ok
        try:
            await ins('ABCDEF', True)   # second verified same code — must fail
            print('FAIL: duplicate verified code allowed')
        except Exception as e:
            print('duplicate verified code rejected ok:', type(e).__name__)
        await ins('ABCDEF', False)  # unverified duplicate — allowed
        print('unverified duplicate allowed ok')
        raise SystemExit  # rollback via exception inside begin()
asyncio.run(t())
" 2>/dev/null || true
```
Expected: `duplicate verified code rejected ok: IntegrityError` then `unverified duplicate allowed ok`. (Confirms the DB invariant before any app code relies on it.)

- [ ] **Step 7: Commit**

```bash
git add alembic.ini alembic/env.py alembic/script.py.mako alembic/versions/0001_initial.py
git commit -m "feat(db): async Alembic + initial migration (citext, tables, partial unique index)"
```

---

### Task 4: FastAPI-Users wiring + mount auth/users routers

**Files:**
- Create: `app/db/users.py`
- Modify: `app/main.py`

- [ ] **Step 1: Create `app/db/users.py`** (schemas + manager + cookie/JWT backend + deps):

```python
"""FastAPI-Users wiring: schemas, UserManager, cookie+JWT backend, deps."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from pydantic import field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.config import db_settings
from app.db.models import Trainer
from app.db.session import get_async_session

_HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")


# ── Schemas ──────────────────────────────────────────────────────────────────
class UserRead(schemas.BaseUser[uuid.UUID]):
    handle: str


class UserCreate(schemas.BaseUserCreate):
    handle: str

    @field_validator("handle")
    @classmethod
    def _norm_handle(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HANDLE_RE.match(v):
            raise ValueError("handle must be 3-20 chars of a-z, 0-9, or underscore")
        return v


class UserUpdate(schemas.BaseUserUpdate):
    handle: Optional[str] = None


# ── DB adapter ───────────────────────────────────────────────────────────────
async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, Trainer)


# ── Manager ──────────────────────────────────────────────────────────────────
class UserManager(UUIDIDMixin, BaseUserManager[Trainer, uuid.UUID]):
    reset_password_token_secret = db_settings().auth_secret
    verification_token_secret = db_settings().auth_secret

    async def create(self, user_create, safe: bool = False, request: Optional[Request] = None):
        # Email uniqueness is handled by the base (UserAlreadyExists -> 400).
        # Handle uniqueness is a DB constraint (citext unique); translate the
        # IntegrityError into a clean 400 instead of a 500.
        try:
            return await super().create(user_create, safe=safe, request=request)
        except IntegrityError as e:
            if "handle" in str(e.orig).lower():
                raise HTTPException(status_code=400, detail="REGISTER_HANDLE_ALREADY_EXISTS") from e
            raise


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ── Auth backend (cookie + JWT) ──────────────────────────────────────────────
_settings = db_settings()

cookie_transport = CookieTransport(
    cookie_name="pcs_auth",
    cookie_max_age=_settings.session_lifetime_seconds,
    cookie_secure=_settings.cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=_settings.auth_secret, lifetime_seconds=_settings.session_lifetime_seconds)


auth_backend = AuthenticationBackend(
    name="cookie", transport=cookie_transport, get_strategy=get_jwt_strategy
)

fastapi_users = FastAPIUsers[Trainer, uuid.UUID](get_user_manager, [auth_backend])

current_active_user = fastapi_users.current_user(active=True)
CurrentTrainer = Annotated[Trainer, Depends(current_active_user)]
```

- [ ] **Step 2: Mount the auth + users routers in `app/main.py`**

Add these imports near the other app imports in `app/main.py`:

```python
from app.db.users import (
    UserCreate,
    UserRead,
    UserUpdate,
    auth_backend,
    fastapi_users,
)
```

Immediately after the `app.add_middleware(CORSMiddleware, ...)` block (and the `_limit_body_size` middleware) and before the static-mount section, add:

```python
# --- Auth & user routes (FastAPI-Users) ---
app.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/cookie", tags=["auth"]
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"]
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
```

This yields: `POST /auth/register`, `POST /auth/cookie/login`, `POST /auth/cookie/logout`, `GET /users/me`. Do NOT add any table-creation on startup — Alembic owns the schema.

- [ ] **Step 3: Smoke the auth flow (app + local Postgres)**

Start the app in one shell (env vars exported, migration already applied):
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/uvicorn app.main:app --port 8000 &
sleep 3
```
Then exercise register → login → me → duplicate-handle, capturing the cookie:
```bash
BASE=http://127.0.0.1:8000
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' \
  -d '{"email":"ash@x.com","password":"pikapika123","handle":"ash"}' | head -c 300; echo
# login (stores cookie)
curl -s -c /tmp/pcs.cookie -X POST $BASE/auth/cookie/login \
  -H 'content-type: application/x-www-form-urlencoded' \
  --data 'username=ash@x.com&password=pikapika123' -o /dev/null -w 'login_status=%{http_code}\n'
# me
curl -s -b /tmp/pcs.cookie $BASE/users/me; echo
# duplicate handle (different email) -> 400
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' \
  -d '{"email":"misty@x.com","password":"togepi123","handle":"ash"}' -w '\nstatus=%{http_code}\n'
# unauthenticated /users/me -> 401
curl -s $BASE/users/me -w '\nstatus=%{http_code}\n'
kill %1 2>/dev/null
```
Expected: register returns a user JSON with `handle:"ash"` (no password); `login_status=200`; `/users/me` returns ash's record; duplicate handle → `status=400` with `REGISTER_HANDLE_ALREADY_EXISTS`; unauth `/users/me` → `status=401`.

- [ ] **Step 4: Commit**

```bash
git add app/db/users.py app/main.py
git commit -m "feat(auth): FastAPI-Users cookie+JWT auth, trainer handle registration"
```

---

### Task 5: Photo storage boundary

**Files:**
- Create: `app/storage.py`
- Modify: `.gitignore`

- [ ] **Step 1: Create `app/storage.py`**:

```python
"""Filesystem boundary for pull photos (Railway Volume in prod, local dir in dev).

Paths are built only from server-generated UUIDs — no user-controlled segments,
so there is no path-traversal surface.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.db.config import db_settings


def _root() -> Path:
    return Path(db_settings().photo_storage_dir)


def ensure_photo_dir() -> None:
    _root().mkdir(parents=True, exist_ok=True)


def _pull_dir(trainer_id: uuid.UUID, pull_id: uuid.UUID) -> Path:
    return _root() / str(trainer_id) / str(pull_id)


def save_pull_photos(
    trainer_id: uuid.UUID, pull_id: uuid.UUID, staircase: bytes, code: bytes
) -> tuple[str, str]:
    """Write both photos; return (staircase_path, code_path) relative to the storage root."""
    d = _pull_dir(trainer_id, pull_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "staircase.jpg").write_bytes(staircase)
    (d / "code.jpg").write_bytes(code)
    rel = Path(str(trainer_id)) / str(pull_id)
    return str(rel / "staircase.jpg"), str(rel / "code.jpg")


def open_photo(rel_path: str) -> bytes:
    """Read a stored photo by its root-relative path. Raises FileNotFoundError if missing."""
    # rel_path comes from the DB (we wrote it); reject anything escaping the root.
    full = (_root() / rel_path).resolve()
    if _root().resolve() not in full.parents and full != _root().resolve():
        raise FileNotFoundError(rel_path)
    return full.read_bytes()
```

- [ ] **Step 2: Ignore the local photo dir** — add to `.gitignore`:

```
var/
```

- [ ] **Step 3: Verify storage round-trips**

Run:
```bash
cd /Users/kailee/pokemon-card-scanner && PHOTO_STORAGE_DIR=./var/pulls .venv/bin/python -c "
import uuid
from app.storage import save_pull_photos, open_photo, ensure_photo_dir
ensure_photo_dir()
tid, pid = uuid.uuid4(), uuid.uuid4()
sp, cp = save_pull_photos(tid, pid, b'STAIR', b'CODE')
print('paths:', sp, cp)
assert open_photo(sp) == b'STAIR' and open_photo(cp) == b'CODE'
# traversal attempt is rejected
try:
    open_photo('../../etc/passwd'); print('FAIL traversal allowed')
except FileNotFoundError:
    print('traversal rejected ok')
print('storage ok')
"
```
Expected: prints two `<uuid>/<uuid>/staircase.jpg`-style paths, `traversal rejected ok`, `storage ok`.

- [ ] **Step 4: Commit**

```bash
git add app/storage.py .gitignore
git commit -m "feat(storage): pull-photo filesystem boundary (volume/local dir)"
```

---

### Task 6: Pull persistence endpoints

**Files:**
- Create: `app/pulls.py`
- Modify: `app/main.py`

- [ ] **Step 1: Create `app/pulls.py`**:

```python
"""Trainer pull persistence: save (with photos + server-verified code), list, detail, photo serving."""

from __future__ import annotations

import json
import re
import uuid

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pull, PullCard
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.pack.ocr import read_code_card
from app.storage import open_photo, save_pull_photos

router = APIRouter(prefix="/pulls", tags=["pulls"])

_MAX_UPLOAD = 15 * 1024 * 1024


def _normalize_code(code: str | None) -> str | None:
    if not code:
        return None
    norm = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return norm or None


# ── Response models ──────────────────────────────────────────────────────────
class CardOut(BaseModel):
    row_index: int
    card_number: str | None
    set_id: str | None
    set_code: str | None
    set_name: str | None
    name: str | None
    rarity: str | None
    low_confidence_reason: str | None
    match_id: str | None
    image_url: str | None
    confidence: float


class PullOut(BaseModel):
    id: uuid.UUID
    created_at: str
    capture_path: str
    pack_confidence: float
    segmentation_warning: str | None
    code: str | None
    code_format_ok: bool
    verified: bool
    cards: list[CardOut]


def _pull_to_out(pull: Pull) -> PullOut:
    return PullOut(
        id=pull.id,
        created_at=pull.created_at.isoformat(),
        capture_path=pull.capture_path,
        pack_confidence=pull.pack_confidence,
        segmentation_warning=pull.segmentation_warning,
        code=pull.code,
        code_format_ok=pull.code_format_ok,
        verified=pull.verified,
        cards=[
            CardOut(
                row_index=c.row_index, card_number=c.card_number, set_id=c.set_id,
                set_code=c.set_code, set_name=c.set_name, name=c.name, rarity=c.rarity,
                low_confidence_reason=c.low_confidence_reason, match_id=c.match_id,
                image_url=c.image_url, confidence=c.confidence,
            )
            for c in pull.cards
        ],
    )


async def _read_image(upload: UploadFile, field: str) -> bytes:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(400, f"{field}: upload an image file")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, f"{field}: image too large (max 15MB)")
    return data


@router.post("", response_model=PullOut, status_code=201)
async def save_pull(
    trainer: CurrentTrainer,
    staircase: UploadFile = File(...),
    code_card: UploadFile = File(...),
    cards: str = Form(..., description="JSON array of confirmed cards"),
    capture_path: str = Form("upload"),
    pack_confidence: float = Form(0.0),
    segmentation_warning: str | None = Form(None),
) -> PullOut:
    stair_bytes = await _read_image(staircase, "staircase")
    code_bytes = await _read_image(code_card, "code_card")
    try:
        card_list = json.loads(cards)
        assert isinstance(card_list, list)
    except (json.JSONDecodeError, AssertionError):
        raise HTTPException(400, "cards: must be a JSON array")

    pull_id = uuid.uuid4()
    staircase_path, code_path = save_pull_photos(trainer.id, pull_id, stair_bytes, code_bytes)

    # Server re-OCRs the code (authoritative — clients cannot spoof the verified flag).
    code_img = cv2.imdecode(np.frombuffer(code_bytes, np.uint8), cv2.IMREAD_COLOR)
    cr = read_code_card(code_img) if code_img is not None else None
    code = cr.code if cr else None
    code_norm = _normalize_code(code)
    code_ok = bool(cr and cr.format_ok)
    code_conf = float(cr.confidence) if cr else 0.0

    want_verified = bool(code_norm) and code_ok

    async with async_session_maker() as session:
        saved = await _insert_pull(
            session, trainer_id=trainer.id, pull_id=pull_id, capture_path=capture_path,
            pack_confidence=pack_confidence, segmentation_warning=segmentation_warning,
            code=code, code_norm=code_norm, code_conf=code_conf, code_ok=code_ok,
            want_verified=want_verified, staircase_path=staircase_path, code_path=code_path,
            card_list=card_list,
        )
        return _pull_to_out(saved)


async def _insert_pull(session: AsyncSession, *, trainer_id, pull_id, capture_path,
                       pack_confidence, segmentation_warning, code, code_norm, code_conf,
                       code_ok, want_verified, staircase_path, code_path, card_list) -> Pull:
    """Insert pull (+cards). Tries verified=want_verified; on the partial-unique-index
    conflict (code already verified by someone), retries verified=False."""
    for verified in ([True, False] if want_verified else [False]):
        try:
            pull = Pull(
                id=pull_id, trainer_id=trainer_id, capture_path=capture_path,
                pack_confidence=pack_confidence, segmentation_warning=segmentation_warning,
                code=code, code_normalized=code_norm, code_confidence=code_conf,
                code_format_ok=code_ok, verified=verified,
                staircase_photo_path=staircase_path, code_photo_path=code_path,
            )
            session.add(pull)
            await session.flush()  # surfaces the unique-index violation here
            for i, c in enumerate(card_list):
                session.add(PullCard(
                    pull_id=pull_id, row_index=int(c.get("row_index", i)),
                    card_number=c.get("card_number"), set_id=c.get("set_id"),
                    set_code=c.get("set_code"), set_name=c.get("set_name"),
                    name=c.get("name"), rarity=c.get("rarity"),
                    low_confidence_reason=c.get("low_confidence_reason"),
                    match_id=c.get("match_id"), image_url=c.get("image_url"),
                    confidence=float(c.get("confidence", 0.0)),
                ))
            await session.commit()
            await session.refresh(pull, attribute_names=["cards"])
            return pull
        except IntegrityError:
            await session.rollback()
            continue
    raise HTTPException(500, "could not persist pull")


@router.get("", response_model=list[PullOut])
async def list_pulls(trainer: CurrentTrainer) -> list[PullOut]:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Pull).where(Pull.trainer_id == trainer.id).order_by(Pull.created_at.desc())
            )
        ).scalars().all()
        # eager-load cards per pull
        out = []
        for p in rows:
            await session.refresh(p, attribute_names=["cards"])
            out.append(_pull_to_out(p))
        return out


@router.get("/{pull_id}", response_model=PullOut)
async def get_pull(trainer: CurrentTrainer, pull_id: uuid.UUID) -> PullOut:
    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        if pull is None or pull.trainer_id != trainer.id:
            raise HTTPException(404, "pull not found")
        await session.refresh(pull, attribute_names=["cards"])
        return _pull_to_out(pull)


@router.get("/{pull_id}/photo/{kind}")
async def get_pull_photo(trainer: CurrentTrainer, pull_id: uuid.UUID, kind: str) -> Response:
    if kind not in ("staircase", "code"):
        raise HTTPException(404, "unknown photo kind")
    async with async_session_maker() as session:
        pull = await session.get(Pull, pull_id)
        if pull is None or pull.trainer_id != trainer.id:
            raise HTTPException(404, "pull not found")
        rel = pull.staircase_photo_path if kind == "staircase" else pull.code_photo_path
    try:
        data = open_photo(rel)
    except FileNotFoundError:
        raise HTTPException(404, "photo missing")
    return Response(content=data, media_type="image/jpeg")
```

- [ ] **Step 2: Mount the pulls router in `app/main.py`** — add the import:

```python
from app.pulls import router as pulls_router
```

and include it alongside the auth routers (before the static mount):

```python
app.include_router(pulls_router)
```

- [ ] **Step 3: Ensure the photo dir exists on startup** — in `app/main.py`'s `_lifespan`, add `ensure_photo_dir()` (import `from app.storage import ensure_photo_dir`). The lifespan body becomes:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()
    log.info("startup log_level=%s", os.environ.get("LOG_LEVEL", "INFO"))
    load_symbol_index()
    load_denominator_table()
    ensure_photo_dir()
    yield
```

(The Railway Volume is mounted at container start, so creating the dir in startup is safe. No DB table creation here — Alembic owns schema.)

- [ ] **Step 4: Smoke the full save flow (app + local Postgres + sub-project A's code fixture)**

Start the app (env exported, migration applied). Use sub-project A's synthetic photos (`tests/fixtures/e2e/staircase.jpg`, `code.jpg`; code = `TEST1-CODE2-CARD3`). The save requires a login cookie:
```bash
cd /Users/kailee/pokemon-card-scanner
.venv/bin/uvicorn app.main:app --port 8000 & sleep 3
BASE=http://127.0.0.1:8000
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' \
  -d '{"email":"t1@x.com","password":"longpassword1","handle":"trainer1"}' -o /dev/null
curl -s -c /tmp/c1 -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' \
  --data 'username=t1@x.com&password=longpassword1' -o /dev/null
CARDS='[{"row_index":0,"card_number":"012/202","set_id":"23876","name":"Test Mon A","confidence":0.96}]'
save() { curl -s -b /tmp/c1 -X POST $BASE/pulls \
  -F staircase=@tests/fixtures/e2e/staircase.jpg \
  -F code_card=@tests/fixtures/e2e/code.jpg \
  -F "cards=$CARDS" -F capture_path=guided ; }
echo "first save:";  save | python3 -c "import sys,json;d=json.load(sys.stdin);print('verified=',d['verified'],'code=',d['code'])"
echo "second save (same code):"; save | python3 -c "import sys,json;d=json.load(sys.stdin);print('verified=',d['verified'],'code=',d['code'])"
echo "my pulls count:"; curl -s -b /tmp/c1 $BASE/pulls | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"
echo "unauth save -> status:"; curl -s -X POST $BASE/pulls -F staircase=@tests/fixtures/e2e/staircase.jpg -F code_card=@tests/fixtures/e2e/code.jpg -F "cards=[]" -o /dev/null -w '%{http_code}\n'
kill %1 2>/dev/null
```
Expected: first save → `verified= True code= TEST1-CODE2-CARD3`; second save (same code) → `verified= False`; my pulls count → `2`; unauth save → `401`. (Confirms server re-OCR, the verified flag, the duplicate-code DB invariant end-to-end, and the auth gate.)

- [ ] **Step 5: Verify photo serving + cross-trainer isolation**

```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/uvicorn app.main:app --port 8000 & sleep 3
BASE=http://127.0.0.1:8000
PULL=$(curl -s -b /tmp/c1 $BASE/pulls | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "owner photo status:"; curl -s -b /tmp/c1 "$BASE/pulls/$PULL/photo/staircase" -o /dev/null -w '%{http_code}\n'
# second trainer cannot see trainer1's pull
curl -s -X POST $BASE/auth/register -H 'content-type: application/json' -d '{"email":"t2@x.com","password":"longpassword2","handle":"trainer2"}' -o /dev/null
curl -s -c /tmp/c2 -X POST $BASE/auth/cookie/login -H 'content-type: application/x-www-form-urlencoded' --data 'username=t2@x.com&password=longpassword2' -o /dev/null
echo "non-owner pull status:"; curl -s -b /tmp/c2 "$BASE/pulls/$PULL" -o /dev/null -w '%{http_code}\n'
echo "non-owner photo status:"; curl -s -b /tmp/c2 "$BASE/pulls/$PULL/photo/staircase" -o /dev/null -w '%{http_code}\n'
kill %1 2>/dev/null
```
Expected: owner photo → `200`; non-owner pull → `404`; non-owner photo → `404`.

- [ ] **Step 6: Commit**

```bash
git add app/pulls.py app/main.py
git commit -m "feat(pulls): save/list/detail/photo endpoints with server-verified code uniqueness"
```

---

### Task 7: Frontend — auth client, context & forms

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/auth/AuthContext.tsx`, `frontend/src/auth/AuthForms.tsx`

- [ ] **Step 1: Add auth + save functions to `frontend/src/api.ts`** (append; reuse the existing `base` and `parse` helpers):

```typescript
export interface Trainer {
  id: string;
  email: string;
  handle: string;
  is_active: boolean;
}

export async function register(email: string, password: string, handle: string): Promise<Trainer> {
  return parse(
    await fetch(`${base}/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, password, handle }),
    })
  );
}

export async function login(email: string, password: string): Promise<void> {
  const form = new URLSearchParams({ username: email, password });
  const res = await fetch(`${base}/auth/cookie/login`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    credentials: "include",
    body: form,
  });
  if (!res.ok) throw new Error((await res.text()) || `login failed (${res.status})`);
}

export async function logout(): Promise<void> {
  await fetch(`${base}/auth/cookie/logout`, { method: "POST", credentials: "include" });
}

export async function me(): Promise<Trainer | null> {
  const res = await fetch(`${base}/users/me`, { credentials: "include" });
  if (res.status === 401) return null;
  return parse(res);
}

export interface SavedPull {
  id: string;
  created_at: string;
  capture_path: string;
  pack_confidence: number;
  segmentation_warning: string | null;
  code: string | null;
  code_format_ok: boolean;
  verified: boolean;
  cards: PackCard[];
}

export async function savePull(
  staircase: Blob,
  codeCard: Blob,
  cards: PackCard[],
  meta: { capture_path: string; pack_confidence: number; segmentation_warning: string | null }
): Promise<SavedPull> {
  const form = new FormData();
  form.append("staircase", staircase, "staircase.jpg");
  form.append("code_card", codeCard, "code.jpg");
  form.append("cards", JSON.stringify(cards));
  form.append("capture_path", meta.capture_path);
  form.append("pack_confidence", String(meta.pack_confidence));
  if (meta.segmentation_warning) form.append("segmentation_warning", meta.segmentation_warning);
  return parse(
    await fetch(`${base}/pulls`, { method: "POST", credentials: "include", body: form })
  );
}

export async function listPulls(): Promise<SavedPull[]> {
  return parse(await fetch(`${base}/pulls`, { credentials: "include" }));
}
```

- [ ] **Step 2: Create `frontend/src/auth/AuthContext.tsx`**:

```tsx
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { login as apiLogin, logout as apiLogout, me, register as apiRegister, type Trainer } from "../api";

interface AuthState {
  trainer: Trainer | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, handle: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [trainer, setTrainer] = useState<Trainer | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setTrainer(await me());
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const login = useCallback(async (email: string, password: string) => {
    await apiLogin(email, password);
    await refresh();
  }, [refresh]);

  const register = useCallback(async (email: string, password: string, handle: string) => {
    await apiRegister(email, password, handle);
    await apiLogin(email, password);
    await refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    await apiLogout();
    setTrainer(null);
  }, []);

  return <Ctx.Provider value={{ trainer, loading, login, register, logout }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used within AuthProvider");
  return v;
}
```

- [ ] **Step 3: Create `frontend/src/auth/AuthForms.tsx`**:

```tsx
import { useState } from "react";
import { useAuth } from "./AuthContext";

export default function AuthForms({ onDone }: { onDone?: () => void }) {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "register") await register(email, password, handle);
      else await login(email, password);
      onDone?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="auth-form" onSubmit={submit}>
      <h2>{mode === "login" ? "Trainer login" : "Become a trainer"}</h2>
      <label>Email<input type="email" value={email} required onChange={(e) => setEmail(e.target.value)} /></label>
      {mode === "register" && (
        <label>Handle<input value={handle} required placeholder="3-20 chars a-z 0-9 _"
          onChange={(e) => setHandle(e.target.value)} /></label>
      )}
      <label>Password<input type="password" value={password} required minLength={8}
        onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p className="camera-error">{error}</p>}
      <button type="submit" className="primary" disabled={busy}>
        {busy ? "…" : mode === "login" ? "Log in" : "Sign up"}
      </button>
      <button type="button" onClick={() => setMode(mode === "login" ? "register" : "login")}>
        {mode === "login" ? "Need an account? Sign up" : "Have an account? Log in"}
      </button>
    </form>
  );
}
```

- [ ] **Step 4: Verify it typechecks**

Run: `cd /Users/kailee/pokemon-card-scanner/frontend && npx tsc --noEmit -p tsconfig.json`
Expected: exits 0 (App.tsx not yet using these is fine; they're standalone modules + api.ts additions). If `tsc` reports an unused-import error in a not-yet-wired file, it's only because Task 8 wires them — proceed; the build gate is Task 8.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api.ts frontend/src/auth/
git commit -m "feat(frontend): auth client, context, login/register forms"
```

---

### Task 8: Frontend — wire save, auth gate, and My Pulls

**Files:**
- Create: `frontend/src/pulls/MyPulls.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/main.tsx`, `frontend/src/App.css`

- [ ] **Step 1: Wrap the app in `AuthProvider`** — in `frontend/src/main.tsx`, wrap `<App />`:

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>
);
```

(If `main.tsx` differs, preserve its existing imports/CSS and only add the `AuthProvider` wrapper.)

- [ ] **Step 2: Create `frontend/src/pulls/MyPulls.tsx`**:

```tsx
import { useEffect, useState } from "react";
import { listPulls, type SavedPull } from "../api";

export default function MyPulls() {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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
            <strong>{new Date(p.created_at).toLocaleString()}</strong>
            <span>
              {p.cards.length} cards · code {p.code ?? "—"} ·{" "}
              {p.verified ? "✓ verified" : "unverified"}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 3: Wire save + auth gate + nav into `frontend/src/App.tsx`.** Replace the file with:

```tsx
import { useState } from "react";
import "./App.css";
import {
  savePull,
  scanPack,
  type CaptureMeta,
  type PackCard,
  type PackScanResponse,
} from "./api";
import StaircaseCapture from "./capture/StaircaseCapture";
import CodeCardCapture from "./capture/CodeCardCapture";
import ReviewScreen from "./review/ReviewScreen";
import { useAuth } from "./auth/AuthContext";
import AuthForms from "./auth/AuthForms";
import MyPulls from "./pulls/MyPulls";

type Step =
  | { name: "staircase" }
  | { name: "code"; staircase: Blob; meta?: CaptureMeta }
  | { name: "submitting" }
  | { name: "review"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta }
  | { name: "saving"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; cards: PackCard[] }
  | { name: "summary"; verified: boolean; count: number }
  | { name: "error"; message: string };

export default function App() {
  const { trainer, loading, logout } = useAuth();
  const [step, setStep] = useState<Step>({ name: "staircase" });
  const [view, setView] = useState<"scan" | "pulls">("scan");
  const [authOpen, setAuthOpen] = useState(false);

  const submit = async (staircase: Blob, code: Blob, meta?: CaptureMeta) => {
    setStep({ name: "submitting" });
    try {
      const scan = await scanPack(staircase, code, meta);
      setStep({ name: "review", scan, staircase, code, meta });
    } catch (e) {
      setStep({ name: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  const doSave = async (s: Extract<Step, { name: "review" }>, cards: PackCard[]) => {
    if (!trainer) {
      setAuthOpen(true);
      return;
    }
    setStep({ name: "saving", ...s, cards });
    try {
      const saved = await savePull(s.staircase, s.code, cards, {
        capture_path: s.meta ? "guided" : "upload",
        pack_confidence: s.scan.pack_confidence,
        segmentation_warning: s.scan.segmentation_warning,
      });
      setStep({ name: "summary", verified: saved.verified, count: saved.cards.length });
    } catch (e) {
      setStep({ name: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <main className="app">
      <header className="app-header">
        <h1>Pack Scanner</h1>
        <nav>
          <button type="button" onClick={() => setView("scan")}>Scan</button>
          <button type="button" onClick={() => setView("pulls")} disabled={!trainer}>My Pulls</button>
          {!loading && (trainer
            ? <button type="button" onClick={logout}>@{trainer.handle} · log out</button>
            : <button type="button" onClick={() => setAuthOpen(true)}>Log in</button>)}
        </nav>
      </header>

      {authOpen && !trainer && (
        <div className="auth-modal">
          <AuthForms onDone={() => setAuthOpen(false)} />
          <button type="button" onClick={() => setAuthOpen(false)}>Cancel</button>
        </div>
      )}

      {view === "pulls" && trainer && <MyPulls />}

      {view === "scan" && (
        <>
          {step.name === "staircase" && (
            <StaircaseCapture onDone={(photo, meta) => setStep({ name: "code", staircase: photo, meta })} />
          )}
          {step.name === "code" && (
            <CodeCardCapture onDone={(codePhoto) => submit(step.staircase, codePhoto, step.meta)} />
          )}
          {step.name === "submitting" && <p className="status">Reading cards…</p>}
          {step.name === "saving" && <p className="status">Saving your pull…</p>}
          {step.name === "review" && (
            <ReviewScreen
              scan={step.scan}
              onRetake={() => setStep({ name: "staircase" })}
              onConfirm={(cards) => doSave(step, cards)}
            />
          )}
          {step.name === "summary" && (
            <section>
              <h2>Pack logged</h2>
              <p>{step.count} cards saved · {step.verified ? "verified ✓" : "unverified (duplicate or unreadable code)"}.</p>
              <button type="button" className="primary" onClick={() => setStep({ name: "staircase" })}>
                Scan another pack
              </button>
            </section>
          )}
          {step.name === "error" && (
            <section>
              <p className="camera-error">Something went wrong: {step.message}</p>
              <button type="button" onClick={() => setStep({ name: "staircase" })}>Start over</button>
            </section>
          )}
        </>
      )}
    </main>
  );
}
```

- [ ] **Step 4: Append styles to `frontend/src/App.css`**:

```css
.app-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
.app-header nav { display: flex; gap: 8px; }
.auth-modal { border: 1px solid #ccc; border-radius: 8px; padding: 12px; margin: 12px 0; }
.auth-form { display: flex; flex-direction: column; gap: 8px; max-width: 320px; }
.auth-form label { display: flex; flex-direction: column; gap: 2px; font-size: 14px; }
```

- [ ] **Step 5: Build the frontend**

Run: `cd /Users/kailee/pokemon-card-scanner/frontend && npm run build`
Expected: `tsc -b` + Vite build succeed with no errors; `dist/` regenerated.

- [ ] **Step 6: Manual browser smoke (full stack)**

With local Postgres up, migration applied, and env exported, run the app serving the built SPA:
```bash
cd /Users/kailee/pokemon-card-scanner && .venv/bin/uvicorn app.main:app --port 8000 & sleep 3
```
Open `http://127.0.0.1:8000` in a browser. Verify: "Log in" → register a trainer (email + handle + password) → header shows `@handle`. Scan via the upload fallback (use `tests/fixtures/e2e/staircase.jpg` then `code.jpg`) → review → "Looks good" saves → summary shows "verified ✓". Click "My Pulls" → the saved pull appears. Repeat the same scan → summary shows "unverified". Kill the server when done (`kill %1`).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/App.tsx frontend/src/main.tsx frontend/src/App.css frontend/src/pulls/
git commit -m "feat(frontend): auth gate, real pull save, My Pulls view"
```

---

### Task 9: Deployment config (Railway) + final smoke

**Files:**
- Modify: `.env.example`, `railway.toml`

- [ ] **Step 1: Document the new env vars** — replace `.env.example` with:

```
# PokéWallet API (required for card matching)
POKEWALLET_API_KEY=

# Database (Railway Postgres injects DATABASE_URL as postgresql://; the app converts to +asyncpg)
DATABASE_URL=

# Auth (REQUIRED in prod — app refuses to start without it). Generate: python -c "import secrets;print(secrets.token_urlsafe(48))"
AUTH_SECRET=

# Pull photo storage (Railway Volume mount path in prod; local dir in dev)
PHOTO_STORAGE_DIR=/data

# Cookies: true in prod (HTTPS), false for local http dev
COOKIE_SECURE=true

# Pack pipeline tuning (defaults shown; T from scripts/sweep_threshold.py)
PACK_CONFIDENCE_THRESHOLD=0.80
PACK_MIN_ROWS=5
PACK_MAX_ROWS=13
PACK_GUIDE_SNAP_TOL=0.35
PACK_STRIP_BAND_FRAC=0.85

CORS_ORIGINS=*
LOG_LEVEL=INFO

# Tests/dev only: point at the local PokéWallet stub
# POKEWALLET_BASE_URL=http://127.0.0.1:8901
```

- [ ] **Step 2: Run migrations before the web process on deploy** — add a `preDeployCommand` to `railway.toml`. Final file:

```toml
# https://docs.railway.com/reference/config-as-code
[build]
builder = "RAILPACK"

[deploy]
startCommand = "uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'"
preDeployCommand = "alembic upgrade head"
healthcheckPath = "/health"
healthcheckTimeout = 120
```

(`preDeployCommand` runs in an ephemeral container with `DATABASE_URL` available but **no** volume mounted — fine for migrations, which only need the DB. The photo dir is created at app startup via `ensure_photo_dir()`, when the volume IS mounted.)

- [ ] **Step 3: Manual full-stack smoke from a clean DB (local)**

Tear down and recreate the local DB to prove migrations apply from empty, then run the end-to-end flow once more:
```bash
cd /Users/kailee/pokemon-card-scanner
docker exec pcs-pg psql -U pcs -d pcs -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
.venv/bin/alembic upgrade head
.venv/bin/python -m pyflakes app/db app/pulls.py app/storage.py app/main.py 2>/dev/null || true
.venv/bin/uvicorn app.main:app --port 8000 & sleep 3
curl -s http://127.0.0.1:8000/health
kill %1 2>/dev/null
```
Expected: `alembic upgrade head` runs cleanly on the empty schema; `/health` returns `{"status":"ok"}`.

- [ ] **Step 4: Railway deploy (USER ACTION)**

This step is performed by the user on Railway (requires the Railway project + credentials):
1. Add a **Postgres** service (injects `DATABASE_URL`).
2. Add a **Volume** on the app service mounted at `/data`; set `PHOTO_STORAGE_DIR=/data`.
3. Set `AUTH_SECRET` (strong random) and `COOKIE_SECURE=true`; set `CORS_ORIGINS` to the site origin.
4. Deploy. `preDeployCommand` runs `alembic upgrade head` automatically before the web process.
5. Smoke on a phone: register → log in → scan a real pack → save → see it in My Pulls; restart the service and confirm the saved pull's photo still loads (volume persistence).

- [ ] **Step 5: Commit**

```bash
git add .env.example railway.toml
git commit -m "chore: Railway Postgres + Volume + migrate-on-deploy config"
```

---

## Completion checklist (maps to spec)

- [ ] Trainer accounts: register/login/logout/`/users/me`, email+password, **unique handle** (Tasks 2, 4)
- [ ] Postgres via SQLAlchemy 2.0 async + asyncpg; Alembic owns schema, no auto-create (Tasks 1, 3)
- [ ] Save a confirmed pull (cards + code + photos) tied to the trainer (Tasks 5, 6, 8)
- [ ] **Server re-OCRs the code**; `verified=true` only if readable, well-formed, globally first-seen — DB-enforced partial unique index (Tasks 3, 6)
- [ ] Original photos stored on a Railway Volume, served owner-only (Tasks 5, 6)
- [ ] List/detail of own pulls; cross-trainer access → 404 (Task 6)
- [ ] Scanning stays public; only saving requires login (Tasks 4, 8)
- [ ] Railway Postgres + Volume + `alembic upgrade head` on deploy (Task 9)
- [ ] **No automated tests** — manual smoke verification only (every task)
- [ ] Pack scanner (sub-project A) unchanged for anonymous users (Tasks 4, 6 leave `app/pack/` untouched)
```
