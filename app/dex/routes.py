"""Personal Pokédex: species seen across the trainer's saved pulls."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.db.models import CollectionCard, Pull, PullCard
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
        # Pull side: per species, PullCard row count + earliest pull time.
        pull_rows = (
            await session.execute(
                select(PullCard.species, func.count(), func.min(Pull.created_at))
                .join(Pull, PullCard.pull_id == Pull.id)
                .where(Pull.trainer_id == trainer.id, PullCard.species.is_not(None))
                .group_by(PullCard.species)
            )
        ).all()
        # Collection side: per species, total qty + earliest save time.
        collection_rows = (
            await session.execute(
                select(
                    CollectionCard.species,
                    func.sum(CollectionCard.qty),
                    func.min(CollectionCard.created_at),
                )
                .where(
                    CollectionCard.trainer_id == trainer.id,
                    CollectionCard.species.is_not(None),
                )
                .group_by(CollectionCard.species)
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
        # Collection art is only a fallback for species with no pull image.
        collection_art_rows = (
            await session.execute(
                select(CollectionCard.species, CollectionCard.image_url)
                .where(
                    CollectionCard.trainer_id == trainer.id,
                    CollectionCard.species.is_not(None),
                    CollectionCard.image_url.is_not(None),
                )
                .order_by(CollectionCard.created_at.desc())
            )
        ).all()
    # Union both sources: count = pull_count + sum(qty); first_seen = least of the two.
    agg: dict[str, tuple[int, object]] = {}
    for sp, n, first in pull_rows:
        agg[sp] = (n, first)
    for sp, qty, first in collection_rows:
        if sp in agg:
            count, seen = agg[sp]
            agg[sp] = (count + int(qty), min(seen, first))
        else:
            agg[sp] = (int(qty), first)
    art: dict[str, str] = {}
    for sp, url in art_rows:
        art.setdefault(sp, url)
    collection_art: dict[str, str] = {}
    for sp, url in collection_art_rows:
        collection_art.setdefault(sp, url)
    entries = [
        DexEntry(
            species=sp, count=count, first_seen=first.isoformat(),
            image_url=art.get(sp) or collection_art.get(sp),
        )
        for sp, (count, first) in agg.items()
    ]
    entries.sort(key=lambda e: e.first_seen, reverse=True)
    return DexOut(seen_count=len(entries), entries=entries)
