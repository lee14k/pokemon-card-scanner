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
