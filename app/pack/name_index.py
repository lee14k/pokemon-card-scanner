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


def _alpha_prefix(s: str) -> str:
    """Leading alpha run of a denominator/local_id, uppercased ("TG30"->"TG",
    "GG07"->"GG", "126"->""). Empty when there is no alpha prefix."""
    m = re.match(r"[A-Za-z]+", s or "")
    return m.group(0).upper() if m else ""


def _is_token_subsequence(short: str, long: str) -> bool:
    """True if `short`'s space-separated tokens appear as a contiguous run
    inside `long`'s tokens (whole-word containment), e.g. 'pikachu' in
    'surfing pikachu' -> True, but 'hatterene v' in 'hatterene vmax' -> False."""
    a, b = short.split(), long.split()
    if not a or len(a) >= len(b):
        return False
    for i in range(len(b) - len(a) + 1):
        if b[i:i + len(a)] == a:
            return True
    return False


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
        # Secondary indexes for SET-SCOPED matching — used to recover prefixed
        # "Trainer's Pokemon" names (e.g. Ascended Heroes' "Erika's Oddish") when
        # OCR drops the prefix and the bare name would match a commoner printing
        # elsewhere. _by_set: set_id -> [(normalized_key, entry)]. _official_to_sets:
        # card_count_official -> {set_id} (a denominator that maps to exactly one
        # set uniquely identifies it).
        self._by_set: dict[str, list[tuple[str, tuple]]] = {}
        self._official_to_sets: dict[int, set[str]] = {}
        # Gallery/promo denominators are non-numeric (e.g. "TG30", "GG70"); they
        # can't key _official_to_sets. Map the local_id's alpha prefix -> the set
        # ids that use it, so an alpha denominator scopes match_in_set only when a
        # single set owns that prefix. ("TG" spans several swsh sets, so it won't
        # scope — that is correct; "GG" is Crown Zenith-only and will.)
        self._alpha_den_to_sets: dict[str, set[str]] = {}
        for key, entries in self._entries.items():
            for e in entries:
                self._by_set.setdefault(e[0], []).append((key, e))
                if e[4] is not None:
                    self._official_to_sets.setdefault(int(e[4]), set()).add(e[0])
                prefix = _alpha_prefix(e[2])   # e[2] is the local_id
                if prefix:
                    self._alpha_den_to_sets.setdefault(prefix, set()).add(e[0])

    def match(self, ocr_text: str, *, denominator: str | None = None,
              min_score: int = 82) -> NameMatch | None:
        q = normalize_name(ocr_text)
        if len(q) < 3 or not any(c.isalpha() for c in q):
            return None
        best = process.extractOne(q, self._keys, scorer=fuzz.WRatio,
                                  score_cutoff=min_score)
        if best is None:
            return None
        key, score, _ = best
        if len(q) < 0.5 * len(key):
            return None
        cands = self._entries[key]
        # substring hazard: "pikachu" inside "surfing pikachu" etc. (whole-word
        # containment only, so "hatterene v" is not flagged by "hatterene vmax")
        substr = any(k != key and _is_token_subsequence(key, k) for k in self._keys)
        if denominator is not None and denominator.isdigit():
            den = int(denominator)
            narrowed = [c for c in cands if c[4] == den]
            if len(narrowed) == 1:
                s, sn, lid, cn, _o = narrowed[0]
                return NameMatch(s, sn, lid, cn, score, ambiguous=substr)
        elif denominator is not None:
            # Non-numeric denominator (e.g. "TG30", "GG70"): the printed
            # denominator carries the gallery prefix, so narrow to printings whose
            # local_id shares that prefix. A unique survivor is confident, exactly
            # like the numeric-denominator narrowing above.
            prefix = _alpha_prefix(denominator)
            if prefix:
                narrowed = [c for c in cands
                            if str(c[2]).upper().startswith(prefix)]
                if len(narrowed) == 1:
                    s, sn, lid, cn, _o = narrowed[0]
                    return NameMatch(s, sn, lid, cn, score, ambiguous=substr)
        if len(cands) == 1:
            s, sn, lid, cn, _o = cands[0]
            return NameMatch(s, sn, lid, cn, score, ambiguous=substr)
        # multiple printings, no unique denominator narrowing -> ambiguous
        s, sn, lid, cn, _o = cands[0]
        return NameMatch(s, sn, lid, cn, score, ambiguous=True)

    def match_in_set(self, ocr_text: str, *, set_id: str | None = None,
                     denominator: str | None = None, min_score: int = 80) -> NameMatch | None:
        """Fuzzy-match the OCR name against ONLY one set's cards. Scope is the
        given ``set_id`` (e.g. the session's already-resolved set), else the set a
        unique ``denominator`` (card_count_official) identifies. Recovers prefixed
        "Trainer's Pokemon" names — a bare "oddish" partial-matches "erika's oddish"
        within Ascended Heroes instead of a commoner Oddish elsewhere. Returns None
        when the scope can't be pinned to a single set."""
        q = normalize_name(ocr_text)
        if len(q) < 3 or not any(c.isalpha() for c in q):
            return None
        if set_id is None and denominator is not None:
            if denominator.isdigit():
                sets = self._official_to_sets.get(int(denominator))
            else:
                sets = self._alpha_den_to_sets.get(_alpha_prefix(denominator))
            if sets and len(sets) == 1:
                set_id = next(iter(sets))
        if set_id is None:
            return None
        pool = self._by_set.get(set_id)
        if not pool:
            return None
        keys = list({k for k, _e in pool})
        best = process.extractOne(q, keys, scorer=fuzz.WRatio, score_cutoff=min_score)
        if best is None:
            return None
        key, score, _ = best
        matched = [e for k, e in pool if k == key]
        s, sn, lid, cn, _o = matched[0]
        return NameMatch(s, sn, lid, cn, score, ambiguous=len(matched) > 1)


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
                .join(TcgdexCard, TcgdexCard.set_id == TcgdexSet.id)
                .order_by(TcgdexSet.id, TcgdexCard.local_id))).all()
        _index = NameIndex([tuple(r) for r in rows])
        return _index
