"""Bootstrap / change a trainer's role by email.

Usage: DATABASE_URL=... AUTH_SECRET=... .venv/bin/python scripts/grant_role.py <email> <trainer|analyst|admin>
(AUTH_SECRET only needs to be present+valid; it is not used for DB writes.)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db.models import Role, Trainer  # noqa: E402
from app.db.session import async_session_maker  # noqa: E402


async def main(email: str, role: str) -> None:
    r = Role(role)  # raises ValueError on bad role
    async with async_session_maker() as session:
        t = (await session.execute(select(Trainer).where(Trainer.email == email))).scalar_one_or_none()
        if t is None:
            raise SystemExit(f"no trainer with email {email!r}")
        t.role = r
        await session.commit()
        print(f"{email} -> {r.value}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: grant_role.py <email> <trainer|analyst|admin>")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
