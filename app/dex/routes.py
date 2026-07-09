"""Personal Pokédex: species seen across the trainer's saved pulls."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.db.models import Pull, PullCard
from app.db.session import async_session_maker
from app.db.users import CurrentTrainer

router = APIRouter(prefix="/dex", tags=["dex"])


class DexEntry(BaseModel):
    species: str
    count: int
    first_seen: str
    image_url: str | None


class DexOut(BaseModel):
    seen_count: int
    entries: list[DexEntry]


@router.get("", response_model=DexOut)
async def get_dex(trainer: CurrentTrainer) -> DexOut:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(PullCard.species, func.count(), func.min(Pull.created_at))
                .join(Pull, PullCard.pull_id == Pull.id)
                .where(Pull.trainer_id == trainer.id, PullCard.species.is_not(None))
                .group_by(PullCard.species)
            )
        ).all()
        # newest card art per species (personal scale: one pass over the trainer's cards)
        art_rows = (
            await session.execute(
                select(PullCard.species, PullCard.image_url)
                .join(Pull, PullCard.pull_id == Pull.id)
                .where(
                    Pull.trainer_id == trainer.id,
                    PullCard.species.is_not(None),
                    PullCard.image_url.is_not(None),
                )
                .order_by(Pull.created_at.desc())
            )
        ).all()
    art: dict[str, str] = {}
    for sp, url in art_rows:
        art.setdefault(sp, url)
    entries = [
        DexEntry(species=sp, count=n, first_seen=first.isoformat(), image_url=art.get(sp))
        for sp, n, first in rows
    ]
    entries.sort(key=lambda e: e.first_seen, reverse=True)
    return DexOut(seen_count=len(entries), entries=entries)
