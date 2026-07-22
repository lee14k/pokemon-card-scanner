"""In-memory card-name index over the TCGdex catalog (8.4k cards).

Names are stored raw in Postgres (diacritics, gender symbols); OCR output is
uppercase ASCII-ish. normalize both sides, fuzzy-match with rapidfuzz.
Lazy-loaded once per process; rebuild by restarting the app."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

_SYMBOLS = {"♀": " f", "♂": " m", "★": "", "☆": "", "◇": ""}


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    for k, v in _SYMBOLS.items():
        s = s.replace(k, v)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class NameMatch:
    tcgdex_set_id: str
    set_name: str
    local_id: str
    card_name: str
    score: float
    ambiguous: bool


class NameIndex:
    def __init__(self, rows: list[tuple[str, str, str, str, int | None]]):
        # rows: (set_id, set_name, local_id, card_name, card_count_official)
        self._entries: dict[str, list[tuple[str, str, str, str, int | None]]] = {}
        for set_id, set_name, local_id, card_name, official in rows:
            if not card_name:
                continue  # a few catalog rows have NULL name; skip them
            self._entries.setdefault(normalize_name(card_name), []).append(
                (set_id, set_name, local_id, card_name, official))
        self._keys = list(self._entries.keys())

    def match(self, ocr_text: str, *, denominator: str | None = None,
              min_score: int = 82) -> NameMatch | None:
        q = normalize_name(ocr_text)
        if len(q) < 3:
            return None
        best = process.extractOne(q, self._keys, scorer=fuzz.WRatio,
                                  score_cutoff=min_score)
        if best is None:
            return None
        key, score, _ = best
        cands = self._entries[key]
        # substring hazard: "pikachu" inside "surfing pikachu" etc.
        substr = any(key != k and key in k for k in self._keys)
        if denominator is not None and denominator.isdigit():
            den = int(denominator)
            narrowed = [c for c in cands if c[4] == den]
            if len(narrowed) == 1:
                s, sn, lid, cn, _o = narrowed[0]
                return NameMatch(s, sn, lid, cn, score, ambiguous=substr)
        if len(cands) == 1:
            s, sn, lid, cn, _o = cands[0]
            return NameMatch(s, sn, lid, cn, score, ambiguous=substr)
        # multiple printings, no unique denominator narrowing -> ambiguous
        s, sn, lid, cn, _o = cands[0]
        return NameMatch(s, sn, lid, cn, score, ambiguous=True)


_index: NameIndex | None = None
_lock = asyncio.Lock()


async def get_name_index() -> NameIndex:
    global _index
    if _index is not None:
        return _index
    async with _lock:
        if _index is not None:
            return _index
        from sqlalchemy import select
        from app.db.session import async_session_maker
        from app.db.models import TcgdexCard, TcgdexSet
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(TcgdexSet.id, TcgdexSet.name, TcgdexCard.local_id,
                       TcgdexCard.name, TcgdexSet.card_count_official)
                .join(TcgdexCard, TcgdexCard.set_id == TcgdexSet.id))).all()
        _index = NameIndex([tuple(r) for r in rows])
        return _index
