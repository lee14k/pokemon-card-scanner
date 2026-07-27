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


def _hazard_keys(keys) -> frozenset[str]:
    """The keys that are a WHOLE-WORD run inside some other key — "pikachu" is,
    because "surfing pikachu" exists; "hatterene v" is not, because "hatterene
    vmax" contains it only as a character substring, not as a token run.

    `match` reports these as ``ambiguous``: the OCR line that scored best against
    such a key may really have been the longer name with a token dropped.

    Read the definition backwards and it is cheap. A key is hazardous exactly
    when it equals a PROPER contiguous token-run of some key — proper meaning
    strictly fewer tokens, which is also why a run can never re-join into its own
    source key, so "some key" and "some OTHER key" are the same statement here.
    Card names are 1-5 tokens, so enumerating every proper run of every key is a
    handful of joins each; the alternative (asking the question per match) is a
    full rescan of all ~2.4k keys with a split on each, on every match call.

    A name that normalizes to "" has no tokens and is produced by no run, so it
    is never hazardous."""
    keys = frozenset(keys)
    runs: set[str] = set()
    for key in keys:
        toks = key.split()
        for length in range(1, len(toks)):
            for i in range(len(toks) - length + 1):
                runs.add(" ".join(toks[i:i + length]))
    return frozenset(runs & keys)


@dataclass
class NameMatch:
    tcgdex_set_id: str
    set_name: str
    local_id: str
    card_name: str
    score: float
    ambiguous: bool


class NameIndex:
    """Built once, then READ-ONLY for the life of the process.

    Nothing below mutates any attribute after ``__init__`` returns — every
    ``match``/``match_in_set`` call only reads the dicts/lists/frozensets built
    here. That is what makes it safe for one shared instance to serve every
    concurrent scan, and it is the reason all of the per-call work that CAN be
    precomputed is precomputed in ``__init__`` rather than memoized lazily: a
    lazily-filled instance attribute would turn this into shared mutable state,
    and this object is reachable from every request in the process."""

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

        # --- precomputed hot-path structures (build once; never mutated after) ---
        # (a) SUBSTRING HAZARD, once for the whole catalog instead of a full
        # rescan of every key inside every `match` call. See `_hazard_keys`.
        self._substr_hazard: frozenset[str] = _hazard_keys(self._entries.keys())

        # (b) PER-SET KEY LISTS. `match_in_set` used to rebuild
        # `sorted({k for k, _e in pool})` on every call — a set build plus a sort
        # of the whole pool, per cell, per frame. The pool is fixed at build time,
        # so the sorted key list is too. The SORT IS LOAD-BEARING (Task 7): it is
        # what makes rapidfuzz's ranking input deterministic instead of dependent
        # on the process's string hash seed, so it is preserved exactly here.
        # `_set_key_entries` likewise replaces the per-call `[e for k, e in pool
        # if k == key]` scan, keeping pool order so `matched[0]` picks the same
        # printing it always did.
        self._set_keys: dict[str, list[str]] = {}
        self._set_key_entries: dict[str, dict[str, list[tuple]]] = {}
        for sid, pool in self._by_set.items():
            by_key: dict[str, list[tuple]] = {}
            for k, e in pool:
                by_key.setdefault(k, []).append(e)
            self._set_key_entries[sid] = by_key
            self._set_keys[sid] = sorted(by_key)

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
        # containment only, so "hatterene v" is not flagged by "hatterene vmax").
        # Precomputed in __init__ — see `_substr_hazard`.
        substr = key in self._substr_hazard
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
        when the scope can't be pinned to a single set.

        A TOP-SCORE TIE BETWEEN DISTINCT CARDS IS AMBIGUOUS, and that is the whole
        safety of this method. Scoping to one set is exactly the situation where
        near-identical names cluster — a set's "Trainer's Pokemon" cycle
        ("N's Klink", "N's Klinklang"), its evolution lines, its Pokemon-plus-suffix
        pairs — so a query that half-matches the family scores the SAME against
        several of them. Picking one of those is a coin flip that the caller then
        treats as a resolved identity:

          * scoped to sv09, a read "KLINK" tied "n s klink" (#103) and
            "n s klinklang" (#105) and returned one confidently;
          * scoped to svp, a read "LILLIE'S DETERMINATION" (a real me01 card) tied
            "hop s snorlax" (#184) and "n s darmanitan" (#181) at 85.5, and paired
            with a misread "SVP184" that is a confident WRONG identity.

        Worse, WHICH one it returned depended on the process: the candidate keys
        came out of a ``set``, whose iteration order moves with Python's per-process
        string hash seed. The same photo could resolve differently on two runs of
        the same code, and a test could pass all day and fail in production. So the
        keys are now SORTED (deterministic ranking input) and a tie is reported
        rather than broken.

        ``ambiguous`` therefore now means "this name does not pick out one card
        here" for BOTH of its reasons: several printings of the same name (its
        original meaning), or several DIFFERENT names the query fits equally well.
        Both callers — the identify ladder's scoped re-match and the binder's
        name-vs-number veto — already refuse an ambiguous match, so a tie simply
        withholds the recovery instead of guessing."""
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
        # Sorted-unique keys and key -> printings, both precomputed in __init__
        # (they are functions of the catalog, not of the query). The sort is the
        # determinism guarantee described above, not a tidiness choice.
        keys = self._set_keys.get(set_id)
        if not keys:
            return None
        top = process.extract(q, keys, scorer=fuzz.WRatio,
                              score_cutoff=min_score, limit=2)
        if not top:
            return None
        key, score, _ = top[0]
        tied = len(top) > 1 and top[1][1] >= score
        matched = self._set_key_entries[set_id][key]
        s, sn, lid, cn, _o = matched[0]
        return NameMatch(s, sn, lid, cn, score, ambiguous=len(matched) > 1 or tied)


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
