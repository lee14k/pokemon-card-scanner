"""FastAPI-Users wiring: schemas, UserManager, cookie+JWT backend, deps."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from pydantic import field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.config import db_settings
from app.db.models import Trainer
from app.db.session import get_async_session

_HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")


# ── Schemas ──────────────────────────────────────────────────────────────────
class UserRead(schemas.BaseUser[uuid.UUID]):
    handle: str


class UserCreate(schemas.BaseUserCreate):
    handle: str

    @field_validator("handle")
    @classmethod
    def _norm_handle(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HANDLE_RE.match(v):
            raise ValueError("handle must be 3-20 chars of a-z, 0-9, or underscore")
        return v


class UserUpdate(schemas.BaseUserUpdate):
    handle: Optional[str] = None


# ── DB adapter ───────────────────────────────────────────────────────────────
async def get_user_db(session: AsyncSession = Depends(get_async_session)):
    yield SQLAlchemyUserDatabase(session, Trainer)


# ── Manager ──────────────────────────────────────────────────────────────────
class UserManager(UUIDIDMixin, BaseUserManager[Trainer, uuid.UUID]):
    reset_password_token_secret = db_settings().auth_secret
    verification_token_secret = db_settings().auth_secret

    async def create(self, user_create, safe: bool = False, request: Optional[Request] = None):
        # Email uniqueness is handled by the base (UserAlreadyExists -> 400).
        # Handle uniqueness is a DB constraint (citext unique); translate the
        # IntegrityError into a clean 400 instead of a 500.
        try:
            return await super().create(user_create, safe=safe, request=request)
        except IntegrityError as e:
            if "handle" in str(e.orig).lower():
                raise HTTPException(status_code=400, detail="REGISTER_HANDLE_ALREADY_EXISTS") from e
            raise


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ── Auth backend (cookie + JWT) ──────────────────────────────────────────────
_settings = db_settings()

cookie_transport = CookieTransport(
    cookie_name="pcs_auth",
    cookie_max_age=_settings.session_lifetime_seconds,
    cookie_secure=_settings.cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=_settings.auth_secret, lifetime_seconds=_settings.session_lifetime_seconds)


auth_backend = AuthenticationBackend(
    name="cookie", transport=cookie_transport, get_strategy=get_jwt_strategy
)

fastapi_users = FastAPIUsers[Trainer, uuid.UUID](get_user_manager, [auth_backend])

current_active_user = fastapi_users.current_user(active=True)
CurrentTrainer = Annotated[Trainer, Depends(current_active_user)]
