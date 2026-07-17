"""Pack battles: value-scored duels between verified pulls (random/friend/bot)."""

from __future__ import annotations

import datetime
import logging
import random as _random
import uuid
from collections import Counter

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
    label: str                     # "you" | "@handle" | "a wild trainer" | "BOT"
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
            other_id = b.opponent_id if i_am_challenger else b.challenger_id
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
