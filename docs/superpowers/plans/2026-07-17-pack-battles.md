# Pack Battles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Battle verified pulls by market value — instant anonymized random battles, consensual friend challenges by handle, and labeled bot opponents — with a personal history + W-L-T tally.

**Architecture:** Migration `0005` adds one `battle` table. A tiny shared `app/prices.py` module extracts E's latest-price-map helper for reuse; `app/battles.py` holds all endpoints, the serialization guard (random-mode anonymity enforced at the API layer), and the bot-pack generator (sampled from the set's priced cards, weighted by C's blended rates). Frontend gets a Battles page + a save-summary nudge. Scores are computed once at resolution and stored (immutable).

**Tech Stack:** Existing FastAPI + SQLAlchemy async + Alembic; Vite/React/TS. No new deps/services/env.

**Global constraints:** NO automated tests (manual smokes only; never create test files). Env/process hygiene identical to sub-projects C–E (same exports, one app + one stub max, kill after, handles ≥3 chars). Alembic currently at `0004_price_snapshots`.

---

## File structure

```
app/db/models.py         # MODIFY: + Battle
alembic/versions/0005_battles.py  # CREATE
app/prices.py            # CREATE: latest_price_map(session) (moved from pulls.py)
app/pulls.py             # MODIFY: import latest_price_map from app.prices (drop local copy)
app/battles.py           # CREATE: endpoints + serialization + bot generator
app/main.py              # MODIFY: mount battles router
frontend/src/api.ts      # MODIFY: battle types + 7 client fns
frontend/src/battles/Battles.tsx  # CREATE
frontend/src/App.tsx     # MODIFY: nav/view + summary "Battle this pack" nudge
```

---

### Task 1: Migration `0005` + Battle model

**Files:** Modify `app/db/models.py`; Create `alembic/versions/0005_battles.py`.

- [ ] **Step 1: Append to `app/db/models.py`:**

```python
class Battle(Base):
    __tablename__ = "battle"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)      # random|friend|bot
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="resolved")  # pending|resolved|declined
    challenger_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trainer.id", ondelete="CASCADE"), index=True, nullable=False
    )
    challenger_pull_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pull.id", ondelete="CASCADE"), nullable=False
    )
    opponent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trainer.id", ondelete="CASCADE"), index=True, nullable=True
    )
    opponent_pull_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pull.id", ondelete="CASCADE"), nullable=True
    )
    bot_pack: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    challenger_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    opponent_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    winner: Mapped[str | None] = mapped_column(String(12), nullable=True)  # challenger|opponent|tie
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
```

- [ ] **Step 2: Create `alembic/versions/0005_battles.py`:**

```python
"""pack battles

Revision ID: 0005_battles
Revises: 0004_price_snapshots
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_battles"
down_revision = "0004_price_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "battle",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("challenger_id", sa.Uuid(), nullable=False),
        sa.Column("challenger_pull_id", sa.Uuid(), nullable=False),
        sa.Column("opponent_id", sa.Uuid(), nullable=True),
        sa.Column("opponent_pull_id", sa.Uuid(), nullable=True),
        sa.Column("bot_pack", JSONB(), nullable=True),
        sa.Column("challenger_score", sa.Float(), nullable=True),
        sa.Column("opponent_score", sa.Float(), nullable=True),
        sa.Column("winner", sa.String(length=12), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["challenger_id"], ["trainer.id"], name="fk_battle_challenger_id_trainer", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["challenger_pull_id"], ["pull.id"], name="fk_battle_challenger_pull_id_pull", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_id"], ["trainer.id"], name="fk_battle_opponent_id_trainer", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_pull_id"], ["pull.id"], name="fk_battle_opponent_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_battle"),
    )
    op.create_index("ix_battle_challenger_id", "battle", ["challenger_id"])
    op.create_index("ix_battle_opponent_id", "battle", ["opponent_id"])


def downgrade() -> None:
    op.drop_table("battle")
```

- [ ] **Step 3: Apply + verify:** `alembic upgrade head` then confirm `battle` table exists (information_schema query). Expected: table present, alembic at `0005_battles (head)`.
- [ ] **Step 4: Commit** — `feat(battles): battle table + model`.

---

### Task 2: Shared price helper (`app/prices.py`)

**Files:** Create `app/prices.py`; Modify `app/pulls.py`.

**Interfaces — Produces:** `latest_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]` and `midpoint(low, high) -> float | None`. Battles (Task 3) and pulls both consume these.

- [ ] **Step 1: Create `app/prices.py`:**

```python
"""Read-side price helpers shared by pull enrichment and battles."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import CardPrice, PriceSnapshot


def midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2


async def latest_price_map(session) -> tuple[dict[str, tuple[float | None, float | None]], str | None]:
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
```

- [ ] **Step 2: Refactor `app/pulls.py`** — delete its local `_latest_price_map` function, add `from app.prices import latest_price_map`, and replace the three call sites `_latest_price_map(session)` → `latest_price_map(session)`. (Keep `_enrich_prices` as is; it can keep its inline `(lo+hi)/2`.) Remove the now-unused `CardPrice, PriceSnapshot` imports from `app/pulls.py` if nothing else uses them.
- [ ] **Step 3: Smoke** — Task 4's E smoke repeated in miniature: start app, login `pr@x.com`, `GET /pulls` still returns `estimated_value: 1.25`. Kill server.
- [ ] **Step 4: Commit** — `refactor(pricing): shared latest_price_map in app/prices.py`.

---

### Task 3: Battles backend (`app/battles.py` + mount)

**Files:** Create `app/battles.py`; Modify `app/main.py`.

**Interfaces — Produces:** endpoints `POST /battles/random|bot|friend`, `POST /battles/{id}/accept|decline`, `GET /battles`, `GET /battles/inbox`. Response shape (Task 4 depends on it): `BattleOut = {id, mode, status, created_at, resolved_at, outcome: win|loss|tie|pending|declined, me: {label, score, cards:[{name, price}]}, opponent: {label, score, cards:[{name, price}]}}`; `GET /battles` returns `{wins, losses, ties, battles: [BattleOut]}`.

- [ ] **Step 1: Create `app/battles.py`:**

```python
"""Pack battles: value-scored duels between verified pulls (random/friend/bot)."""

from __future__ import annotations

import datetime
import logging
import random as _random
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models import Battle, CardPrice, CardStat, PriceSnapshot, Pull, StatsSnapshot, Trainer
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer
from app.prices import latest_price_map, midpoint

log = logging.getLogger("pokemon_scanner.battles")
router = APIRouter(prefix="/battles", tags=["battles"])


# ── Response models ──────────────────────────────────────────────────────────
class BattleCard(BaseModel):
    name: str | None
    price: float | None


class BattleSide(BaseModel):
    label: str                     # "@handle" | "a wild trainer" | "BOT" | "you"
    score: float | None
    cards: list[BattleCard]


class BattleOut(BaseModel):
    id: uuid.UUID
    mode: str
    status: str
    created_at: str
    resolved_at: str | None
    outcome: str                   # win|loss|tie|pending|declined
    me: BattleSide
    opponent: BattleSide


class BattleList(BaseModel):
    wins: int
    losses: int
    ties: int
    battles: list[BattleOut]


class PullRef(BaseModel):
    pull_id: uuid.UUID


class FriendChallenge(BaseModel):
    pull_id: uuid.UUID
    opponent_handle: str


# ── Helpers ──────────────────────────────────────────────────────────────────
async def _owned_verified_pull(session, trainer_id: uuid.UUID, pull_id: uuid.UUID) -> Pull:
    pull = (
        await session.execute(
            select(Pull).where(Pull.id == pull_id).options(selectinload(Pull.cards))
        )
    ).scalar_one_or_none()
    if pull is None or pull.trainer_id != trainer_id:
        raise HTTPException(404, "pull not found")
    if not pull.verified:
        raise HTTPException(400, "only verified pulls can battle")
    return pull


def _score_cards(cards, prices: dict[str, tuple[float | None, float | None]]) -> float:
    total = 0.0
    for c in cards:
        if c.match_id and c.match_id in prices:
            m = midpoint(*prices[c.match_id])
            if m is not None:
                total += m
    return round(total, 2)


def _pull_cards_out(cards, prices) -> list[BattleCard]:
    out = []
    for c in cards:
        m = midpoint(*prices.get(c.match_id, (None, None))) if c.match_id else None
        out.append(BattleCard(name=c.name, price=m))
    return out


def _resolve(battle: Battle, challenger_score: float, opponent_score: float) -> None:
    battle.challenger_score = challenger_score
    battle.opponent_score = opponent_score
    if challenger_score > opponent_score:
        battle.winner = "challenger"
    elif opponent_score > challenger_score:
        battle.winner = "opponent"
    else:
        battle.winner = "tie"
    battle.status = "resolved"
    battle.resolved_at = datetime.datetime.now(datetime.timezone.utc)


async def _require_prices(session) -> dict[str, tuple[float | None, float | None]]:
    prices, as_of = await latest_price_map(session)
    if as_of is None:
        raise HTTPException(409, "prices not available yet — run a stats/pricing batch first")
    return prices


async def _battle_out(session, b: Battle, viewer_id: uuid.UUID, prices) -> BattleOut:
    i_am_challenger = b.challenger_id == viewer_id

    async def side_for_pull(pull_id: uuid.UUID | None, score: float | None) -> tuple[list[BattleCard], float | None]:
        if pull_id is None:
            return [], score
        pull = (
            await session.execute(
                select(Pull).where(Pull.id == pull_id).options(selectinload(Pull.cards))
            )
        ).scalar_one()
        return _pull_cards_out(pull.cards, prices), score

    my_pull_id = b.challenger_pull_id if i_am_challenger else b.opponent_pull_id
    my_score = b.challenger_score if i_am_challenger else b.opponent_score
    my_cards, my_score = await side_for_pull(my_pull_id, my_score)
    me = BattleSide(label="you", score=my_score, cards=my_cards)

    if b.mode == "bot":
        opp = BattleSide(
            label="BOT", score=b.opponent_score,
            cards=[BattleCard(name=c.get("name"), price=midpoint(c.get("price_usd_low"), c.get("price_usd_high")))
                   for c in (b.bot_pack or [])],
        )
    else:
        opp_pull_id = b.opponent_pull_id if i_am_challenger else b.challenger_pull_id
        opp_score = b.opponent_score if i_am_challenger else b.challenger_score
        opp_cards, opp_score = await side_for_pull(opp_pull_id, opp_score)
        # Fairness: a pending friend challenge must not reveal the challenger's pack
        # to the opponent before they commit their own — no cherry-picking.
        if b.status == "pending" and not i_am_challenger:
            opp_cards, opp_score = [], None
        if b.mode == "random":
            label = "a wild trainer"          # identity never serialized for random mode
        else:
            other_id = (b.opponent_id if i_am_challenger else b.challenger_id)
            other = await session.get(Trainer, other_id)
            label = f"@{other.handle}" if other else "unknown"
        opp = BattleSide(label=label, score=opp_score, cards=opp_cards)

    if b.status == "pending":
        outcome = "pending"
    elif b.status == "declined":
        outcome = "declined"
    elif b.winner == "tie":
        outcome = "tie"
    elif (b.winner == "challenger") == i_am_challenger:
        outcome = "win"
    else:
        outcome = "loss"

    return BattleOut(
        id=b.id, mode=b.mode, status=b.status,
        created_at=b.created_at.isoformat(),
        resolved_at=b.resolved_at.isoformat() if b.resolved_at else None,
        outcome=outcome, me=me, opponent=opp,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────
@router.post("/random", response_model=BattleOut, status_code=201)
async def random_battle(trainer: CurrentTrainer, body: PullRef) -> BattleOut:
    async with async_session_maker() as session:
        prices = await _require_prices(session)
        mine = await _owned_verified_pull(session, trainer.id, body.pull_id)
        opp_pull = (
            await session.execute(
                select(Pull)
                .where(Pull.verified.is_(True), Pull.trainer_id != trainer.id)
                .options(selectinload(Pull.cards))
                .order_by(func.random()).limit(1)
            )
        ).scalar_one_or_none()
        if opp_pull is None:
            raise HTTPException(409, "no opponents yet — try a bot battle")
        b = Battle(mode="random", challenger_id=trainer.id, challenger_pull_id=mine.id,
                   opponent_id=opp_pull.trainer_id, opponent_pull_id=opp_pull.id)
        _resolve(b, _score_cards(mine.cards, prices), _score_cards(opp_pull.cards, prices))
        session.add(b)
        await session.commit()
        await session.refresh(b)
        return await _battle_out(session, b, trainer.id, prices)


@router.post("/bot", response_model=BattleOut, status_code=201)
async def bot_battle(trainer: CurrentTrainer, body: PullRef) -> BattleOut:
    async with async_session_maker() as session:
        prices = await _require_prices(session)
        mine = await _owned_verified_pull(session, trainer.id, body.pull_id)

        # candidate universe: priced cards of my pull's (modal) set, from the latest snapshot
        from collections import Counter
        set_ids = [c.set_id for c in mine.cards if c.set_id]
        my_set = Counter(set_ids).most_common(1)[0][0] if set_ids else None
        snap_id = (
            await session.execute(
                select(PriceSnapshot.id).where(PriceSnapshot.status == "done")
                .order_by(PriceSnapshot.created_at.desc()).limit(1)
            )
        ).scalar_one()
        cand_q = select(CardPrice).where(
            CardPrice.snapshot_id == snap_id,
            CardPrice.usd_market_low.is_not(None),
        )
        cands = (await session.execute(
            cand_q.where(CardPrice.set_id == my_set) if my_set else cand_q
        )).scalars().all()
        if not cands and my_set:
            log.info("battles.bot fallback_all_sets set=%s", my_set)
            cands = (await session.execute(cand_q)).scalars().all()
        if not cands:
            raise HTTPException(409, "prices not available yet — run a stats/pricing batch first")

        # weight by blended pull rates (current stats snapshot) when available
        stats_snap = (
            await session.execute(
                select(StatsSnapshot.id).where(StatsSnapshot.status == "done")
                .order_by(StatsSnapshot.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        weights = None
        if stats_snap is not None:
            rate_rows = (
                await session.execute(
                    select(CardStat.match_id, CardStat.blended_rate)
                    .where(CardStat.snapshot_id == stats_snap)
                )
            ).all()
            rates = dict(rate_rows)
            if any(c.match_id in rates for c in cands):
                weights = [max(rates.get(c.match_id, 0.01), 0.001) for c in cands]

        n = max(len(mine.cards), 1)
        picks = _random.choices(cands, weights=weights, k=n)
        bot_pack = [
            {"name": c.name, "match_id": c.match_id,
             "price_usd_low": c.usd_market_low, "price_usd_high": c.usd_market_high}
            for c in picks
        ]
        bot_score = round(sum(midpoint(c["price_usd_low"], c["price_usd_high"]) or 0.0 for c in bot_pack), 2)

        b = Battle(mode="bot", challenger_id=trainer.id, challenger_pull_id=mine.id, bot_pack=bot_pack)
        _resolve(b, _score_cards(mine.cards, prices), bot_score)
        session.add(b)
        await session.commit()
        await session.refresh(b)
        return await _battle_out(session, b, trainer.id, prices)


@router.post("/friend", response_model=BattleOut, status_code=201)
async def friend_battle(trainer: CurrentTrainer, body: FriendChallenge) -> BattleOut:
    async with async_session_maker() as session:
        prices, _ = await latest_price_map(session)  # allowed to be empty for a pending challenge
        mine = await _owned_verified_pull(session, trainer.id, body.pull_id)
        opp = (
            await session.execute(
                select(Trainer).where(Trainer.handle == body.opponent_handle.strip().lower())
            )
        ).scalar_one_or_none()
        if opp is None:
            raise HTTPException(404, "no trainer with that handle")
        if opp.id == trainer.id:
            raise HTTPException(400, "you can't battle yourself")
        b = Battle(mode="friend", status="pending", challenger_id=trainer.id,
                   challenger_pull_id=mine.id, opponent_id=opp.id)
        session.add(b)
        await session.commit()
        await session.refresh(b)
        return await _battle_out(session, b, trainer.id, prices)


@router.post("/{battle_id}/accept", response_model=BattleOut)
async def accept_battle(trainer: CurrentTrainer, battle_id: uuid.UUID, body: PullRef) -> BattleOut:
    async with async_session_maker() as session:
        prices = await _require_prices(session)
        b = await session.get(Battle, battle_id)
        if b is None or b.mode != "friend" or b.status != "pending" or b.opponent_id != trainer.id:
            raise HTTPException(404, "challenge not found")
        mine = await _owned_verified_pull(session, trainer.id, body.pull_id)
        challenger_pull = (
            await session.execute(
                select(Pull).where(Pull.id == b.challenger_pull_id).options(selectinload(Pull.cards))
            )
        ).scalar_one()
        b.opponent_pull_id = mine.id
        _resolve(b, _score_cards(challenger_pull.cards, prices), _score_cards(mine.cards, prices))
        await session.commit()
        await session.refresh(b)
        return await _battle_out(session, b, trainer.id, prices)


@router.post("/{battle_id}/decline", response_model=BattleOut)
async def decline_battle(trainer: CurrentTrainer, battle_id: uuid.UUID) -> BattleOut:
    async with async_session_maker() as session:
        b = await session.get(Battle, battle_id)
        if b is None or b.mode != "friend" or b.status != "pending" or b.opponent_id != trainer.id:
            raise HTTPException(404, "challenge not found")
        b.status = "declined"
        await session.commit()
        await session.refresh(b)
        prices, _ = await latest_price_map(session)
        return await _battle_out(session, b, trainer.id, prices)


@router.get("", response_model=BattleList)
async def list_battles(trainer: CurrentTrainer) -> BattleList:
    async with async_session_maker() as session:
        prices, _ = await latest_price_map(session)
        rows = (
            await session.execute(
                select(Battle).where(
                    (Battle.challenger_id == trainer.id)
                    | ((Battle.opponent_id == trainer.id) & (Battle.mode == "friend") & (Battle.status != "pending"))
                ).order_by(Battle.created_at.desc())
            )
        ).scalars().all()
        outs = [await _battle_out(session, b, trainer.id, prices) for b in rows]
        wins = sum(1 for o in outs if o.outcome == "win")
        losses = sum(1 for o in outs if o.outcome == "loss")
        ties = sum(1 for o in outs if o.outcome == "tie")
        return BattleList(wins=wins, losses=losses, ties=ties, battles=outs)


@router.get("/inbox", response_model=list[BattleOut])
async def battle_inbox(trainer: CurrentTrainer) -> list[BattleOut]:
    async with async_session_maker() as session:
        prices, _ = await latest_price_map(session)
        rows = (
            await session.execute(
                select(Battle).where(
                    Battle.opponent_id == trainer.id, Battle.mode == "friend", Battle.status == "pending"
                ).order_by(Battle.created_at.desc())
            )
        ).scalars().all()
        return [await _battle_out(session, b, trainer.id, prices) for b in rows]
```

Note: displayed per-card prices come from the *current* latest snapshot; the stored side **scores** are the immutable resolution values (this is the spec's immutability guarantee — labels drift, results don't).

- [ ] **Step 2: Mount** in `app/main.py`: `from app.battles import router as battles_router` + `app.include_router(battles_router)` with the other routers.
- [ ] **Step 3: Smoke** — two trainers with verified pulls + price snapshot (existing stub flow): random battle → 201, correct scores, response JSON contains no handle for the opponent; friend challenge → pending in opponent inbox → accept → resolved in both `GET /battles` with handles; decline path; bot battle → 201 with labeled BOT side and set-consistent pack; guards: unverified pull 400, foreign pull_id 404, self-challenge 400, no-snapshot 409 (truncate price_snapshot first), empty-pool 409 (single-trainer DB). Kill servers.
- [ ] **Step 4: Commit** — `feat(battles): battle endpoints (random/bot/friend), anonymity-safe serialization`.

---

### Task 4: Frontend (Battles page + nudge) + final smoke

**Files:** Modify `frontend/src/api.ts`, `frontend/src/App.tsx`; Create `frontend/src/battles/Battles.tsx`.

- [ ] **Step 1: `api.ts`** — append:

```typescript
export interface BattleCard { name: string | null; price: number | null; }
export interface BattleSide { label: string; score: number | null; cards: BattleCard[]; }
export interface Battle {
  id: string; mode: string; status: string; created_at: string; resolved_at: string | null;
  outcome: string; me: BattleSide; opponent: BattleSide;
}
export interface BattleList { wins: number; losses: number; ties: number; battles: Battle[]; }

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return parse(await fetch(`${base}${path}`, {
    method: "POST", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}
export const randomBattle = (pullId: string) => postJson<Battle>("/battles/random", { pull_id: pullId });
export const botBattle = (pullId: string) => postJson<Battle>("/battles/bot", { pull_id: pullId });
export const friendBattle = (pullId: string, handle: string) =>
  postJson<Battle>("/battles/friend", { pull_id: pullId, opponent_handle: handle });
export const acceptBattle = (id: string, pullId: string) =>
  postJson<Battle>(`/battles/${id}/accept`, { pull_id: pullId });
export const declineBattle = (id: string) => postJson<Battle>(`/battles/${id}/decline`, {});
export async function listBattles(): Promise<BattleList> {
  return parse(await fetch(`${base}/battles`, { credentials: "include" }));
}
export async function battleInbox(): Promise<Battle[]> {
  return parse(await fetch(`${base}/battles/inbox`, { credentials: "include" }));
}
```

- [ ] **Step 2: Create `frontend/src/battles/Battles.tsx`:**

```tsx
import { useEffect, useState } from "react";
import {
  acceptBattle, battleInbox, botBattle, declineBattle, friendBattle, listPulls,
  listBattles, randomBattle, type Battle, type BattleList, type SavedPull,
} from "../api";

const badge = (o: string) =>
  o === "win" ? "🏆 win" : o === "loss" ? "💀 loss" : o === "tie" ? "🤝 tie" : o;

export default function Battles({ preselectPullId }: { preselectPullId?: string | null }) {
  const [data, setData] = useState<BattleList | null>(null);
  const [inbox, setInbox] = useState<Battle[]>([]);
  const [pulls, setPulls] = useState<SavedPull[]>([]);
  const [sel, setSel] = useState<string>("");
  const [handle, setHandle] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = () => {
    listBattles().then(setData).catch(() => setData(null));
    battleInbox().then(setInbox).catch(() => setInbox([]));
  };
  useEffect(() => {
    refresh();
    listPulls().then((ps) => {
      const verified = ps.filter((p) => p.verified);
      setPulls(verified);
      setSel(preselectPullId && verified.some((p) => p.id === preselectPullId)
        ? preselectPullId : verified[0]?.id ?? "");
    }).catch(() => setPulls([]));
  }, [preselectPullId]);

  const act = async (fn: () => Promise<unknown>) => {
    setMsg(null);
    try { await fn(); refresh(); } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section>
      <h2>Pack Battles{data && <> — {data.wins}W / {data.losses}L / {data.ties}T</>}</h2>

      <h3>New battle</h3>
      {pulls.length === 0 ? <p>You need a verified pull to battle — scan a pack!</p> : (
        <div className="auth-form">
          <label>Your pack
            <select value={sel} onChange={(e) => setSel(e.target.value)}>
              {pulls.map((p) => (
                <option key={p.id} value={p.id}>
                  {new Date(p.created_at).toLocaleDateString()} · {p.cards.length} cards
                  {p.estimated_value != null ? ` · ≈$${p.estimated_value.toFixed(2)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="card-row-flag">
            <button type="button" className="primary" disabled={!sel} onClick={() => act(() => randomBattle(sel))}>Random</button>
            <button type="button" disabled={!sel} onClick={() => act(() => botBattle(sel))}>Bot</button>
            <input placeholder="friend's handle" value={handle} onChange={(e) => setHandle(e.target.value)} />
            <button type="button" disabled={!sel || !handle.trim()} onClick={() => act(() => friendBattle(sel, handle.trim()))}>Challenge</button>
          </div>
          {msg && <p className="camera-error">{msg}</p>}
        </div>
      )}

      {inbox.length > 0 && (
        <>
          <h3>Challenges for you</h3>
          <ul className="card-rows">
            {inbox.map((b) => (
              <li key={b.id} className="card-row flagged">
                <div className="card-row-body">
                  <strong>{b.opponent.label} challenged you!</strong>
                  <div className="card-row-flag">
                    <button type="button" disabled={!sel} onClick={() => act(() => acceptBattle(b.id, sel))}>
                      Accept with selected pack
                    </button>
                    <button type="button" onClick={() => act(() => declineBattle(b.id))}>Decline</button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>History</h3>
      {(!data || data.battles.length === 0) && <p>No battles yet.</p>}
      <ul className="card-rows">
        {(data?.battles ?? []).map((b) => (
          <li key={b.id} className="card-row">
            <div className="card-row-body">
              <button type="button" className="pull-row-toggle" onClick={() => setOpen(open === b.id ? null : b.id)}>
                <strong>{badge(b.outcome)} · vs {b.opponent.label} · {b.mode}</strong>
              </button>
              <span>
                you ${b.me.score?.toFixed(2) ?? "?"} vs {b.opponent.label} ${b.opponent.score?.toFixed(2) ?? "?"}
                {" · "}{new Date(b.created_at).toLocaleString()}
              </span>
              {open === b.id && (
                <div>
                  {[b.me, b.opponent].map((side, i) => (
                    <ul key={i} className="card-rows">
                      <li className="card-row"><div className="card-row-body"><strong>{side.label}</strong></div></li>
                      {side.cards.map((c, j) => (
                        <li key={j} className="card-row"><div className="card-row-body">
                          <span>{c.name ?? "?"} · {c.price != null ? `$${c.price.toFixed(2)}` : "—"}</span>
                        </div></li>
                      ))}
                    </ul>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 3: `App.tsx` wiring** — import `Battles`; extend the view union with `"battles"`; add `const [battlePull, setBattlePull] = useState<string | null>(null);`; nav button `<button type="button" onClick={() => setView("battles")} disabled={!trainer}>Battles</button>` next to Pokédex; render `{view === "battles" && trainer && <Battles preselectPullId={battlePull} />}`. Summary nudge: the summary step already stores `verified`; extend the summary Step variant with `pullId: string` (set from `saved.id` in `doSave`) and add, next to "Scan another pack", `{step.verified && (<button type="button" onClick={() => { setBattlePull(step.pullId); setView("battles"); }}>⚔️ Battle this pack</button>)}`.
- [ ] **Step 4: Build + final smoke** — `npm run build` green; backend full-flow smoke from Task 3 re-run once more end-to-end; scanner suite `7 passed`; no stray processes.
- [ ] **Step 5: Commit** — `feat(battles): Battles page, inbox, tally + summary nudge`.

---

## Completion checklist (maps to spec)

- [ ] `battle` table + model (Task 1)
- [ ] Shared price helper, no duplication (Task 2)
- [ ] Verified-only + ownership guards; anonymized random; friend consent flow; labeled bots; immutable stored scores (Task 3)
- [ ] History + W-L-T + inbox; Battles UI + save-summary nudge (Tasks 3–4)
- [ ] 409s for empty pool / missing prices (Task 3)
- [ ] No tests; scanner suite + build green at the end (Task 4)
