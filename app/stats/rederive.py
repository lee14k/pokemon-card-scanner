"""Re-derive verified pulls server-side from their stored staircase photos.

Stats must not trust client-submitted cards; this regenerates authoritative cards
by re-running the scan pipeline, guided by the persisted capture_meta when present.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select

from app.db.models import DeriveStatus, Pull, PullCardDerived
from app.db.session import async_session_maker
from app.pack.pipeline import scan_pack
from app.storage import open_photo

log = logging.getLogger("pokemon_scanner.stats.rederive")


async def rederive_pending(limit: int = 200) -> int:
    """Re-derive up to `limit` verified pulls awaiting derivation. Returns the count processed."""
    processed = 0
    async with async_session_maker() as session:
        pulls = (
            await session.execute(
                select(Pull)
                .where(Pull.verified.is_(True), Pull.derive_status == DeriveStatus.pending)
                .limit(limit)
            )
        ).scalars().all()

        for pull in pulls:
            try:
                staircase = open_photo(pull.staircase_photo_path)
            except FileNotFoundError:
                log.warning("rederive.photo_missing pull=%s", pull.id)
                pull.derive_status = DeriveStatus.failed
                pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
                continue
            try:
                # code bytes are irrelevant here (verification already settled in B);
                # pass empty so the pipeline skips code OCR. capture_meta drives guided seg.
                resp = await scan_pack(staircase, b"", pull.capture_meta)
            except Exception as e:  # pragma: no cover - defensive; one bad photo must not kill the batch
                log.warning("rederive.scan_failed pull=%s err=%r", pull.id, e)
                pull.derive_status = DeriveStatus.failed
                pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
                continue

            for card in resp.cards:
                session.add(PullCardDerived(
                    pull_id=pull.id, row_index=card.row_index, card_number=card.card_number,
                    set_id=card.set_id, set_code=card.set_code, set_name=card.set_name,
                    name=card.name, rarity=card.rarity, match_id=card.match_id,
                    confidence=card.confidence,
                ))
            pull.derive_status = DeriveStatus.done
            pull.derived_at = datetime.datetime.now(datetime.timezone.utc)
            processed += 1

        await session.commit()
    log.info("rederive.done processed=%s", processed)
    return processed
