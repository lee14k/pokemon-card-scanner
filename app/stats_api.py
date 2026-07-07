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
