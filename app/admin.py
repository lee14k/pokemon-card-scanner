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
