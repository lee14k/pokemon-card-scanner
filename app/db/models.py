"""SQLAlchemy models: Trainer (FastAPI-Users), Pull, PullCard."""

from __future__ import annotations

import datetime
import enum
import uuid

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Role(str, enum.Enum):
    trainer = "trainer"
    analyst = "analyst"
    admin = "admin"


class DeriveStatus(str, enum.Enum):
    pending = "pending"
    done = "done"
    failed = "failed"


class Trainer(SQLAlchemyBaseUserTableUUID, Base):
    """Account. SQLAlchemyBaseUserTableUUID supplies id (UUID), email,
    hashed_password, is_active, is_superuser, is_verified."""

    __tablename__ = "trainer"

    # Case-insensitive unique public handle (CITEXT). Requires the citext extension
    # (created in the initial migration). Format/casing enforced in the schema layer.
    handle: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, name="role", native_enum=False, length=16),
        nullable=False, default=Role.trainer, server_default=Role.trainer.value,
    )
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
    capture_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    derive_status: Mapped[DeriveStatus] = mapped_column(
        SAEnum(DeriveStatus, name="derive_status", native_enum=False, length=16),
        nullable=False, default=DeriveStatus.pending, server_default=DeriveStatus.pending.value,
    )
    derived_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    derived_cards: Mapped[list["PullCardDerived"]] = relationship(
        back_populates="pull", cascade="all, delete-orphan"
    )

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
    species: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    pull: Mapped["Pull"] = relationship(back_populates="cards")


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


class PriceSnapshot(Base):
    __tablename__ = "price_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running|done|failed


class CardPrice(Base):
    __tablename__ = "card_price"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_snapshot.id", ondelete="CASCADE"), index=True, nullable=False
    )
    match_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    set_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    usd_market_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_market_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    eur_trend: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
