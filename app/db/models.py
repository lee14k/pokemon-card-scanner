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
