"""Admin API: list trainers, grant roles. Admin-only."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Role, Trainer
from app.db.session import async_session_maker
from app.db.users import CurrentAdmin
from app.db.users import fastapi_users
from app.stats.config import stats_settings
from app.stats.run_batch import run_batch

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


@router.post("/matcher/index/{set_id}")
async def build_matcher_index(set_id: str, admin: CurrentAdmin) -> dict:
    """Enumerate a set and (re)build its matcher index. Synchronous; admin-only."""
    from app.cards import enumerated_cards_for_index
    from app.matcher_client import build_index, enabled

    if not enabled():
        raise HTTPException(503, "MATCHER_URL not configured")
    cards = await enumerated_cards_for_index(set_id)
    if not cards:
        raise HTTPException(502, "no reference cards available for set")
    report = await build_index(set_id, cards)
    if report is None:
        raise HTTPException(502, "matcher index build failed")
    return report
