# Live Card Scan + Speed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live camera scan mode (cards shown one at a time, identified in near-real-time) feeding the existing review→save→battle flow, plus OCR speed fixes, an me-era catalog bridge, and the RunPod VLM redeploy verification.

**Architecture:** Discrete card events, not video streaming: the browser detects steady+sharp holds and POSTs two crops per card to a new live-session API; the server OCRs the name band + number strip (RapidOCR), matches against an in-memory TCGdex name index with a session prior, and patches uncertain cards asynchronously via the existing RunPod VLM client. Sessions are in-memory with frames persisted to photo storage; the client keeps tray state so nothing is ever lost.

**Tech Stack:** FastAPI + SQLAlchemy async (existing), RapidOCR/onnxruntime (existing), rapidfuzz (new), React/TS + getUserMedia/rVFC/WakeLock (frontend), RunPod serverless Qwen2.5-VL (existing worker).

## Global Constraints

- **NO automated test additions** (user's standing rule). Verification = the exact smoke commands in each task. The EXISTING suite must stay green: `.venv/bin/python -m pytest tests/ -q` → `7 passed, 1 skipped`.
- Dev env for every backend command: `export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false` (plain exports — `env $VAR` word-splitting broke under zsh before). Dev admin `tdadmin@x.io` / `trainerpass1`, dev user `tduser@x.io` / `trainerpass1`.
- opencv pin stays `opencv-python>=4.8,<5` — NEVER install a second opencv distribution (a 5.x or headless install corrupts the shared `cv2/` dir; this bit us twice).
- Machine care: max one uvicorn app + one stub at a time. Before/after any server smoke: `pkill -f "uvicorn app.main"; pkill -f vlm_stub`. Long jobs via `caffeinate -i`.
- Git: commit to local `main` only; the USER pushes. Commit messages end with the Co-Authored-By line used throughout this repo.
- Frontend dev: `cd frontend && npm run dev` (Vite, proxies `/api` to :8000). Type-check with `npx tsc --noEmit`.
- The reel video fixture lives at `/private/tmp/claude-501/-Users-kailee-pokemon-card-scanner/2472d085-aebd-4499-89c6-199d4c2694c0/scratchpad/vid/clip.mp4` (17.2s, 1670×1080). Task 4 copies extracted frames into `tests/corpus/reel/` so they survive the scratchpad.

---

### Task 1: ORT thread pinning + shared OCR gate (speed foundation)

**Files:**
- Modify: `app/pack/rapidocr_reader.py` (engine construction)
- Modify: `app/pack/pipeline.py` (export the semaphore for reuse)

**Interfaces:**
- Produces: `app/pack/pipeline.py::OCR_GATE` — an `asyncio.Semaphore` importable by the live API (Task 6). Env `OCR_THREADS` (int, default 0 = library default).

Rationale: RapidOCR/onnxruntime sizes its thread pool to the HOST core count; under Railway's 2-vCPU cgroup that oversubscribes and slows every scan. Pin it, env-configurable.

- [ ] **Step 1: Pin threads in the engine constructor**

In `app/pack/rapidocr_reader.py`, the lazy `_get()` currently does `_engine = RapidOCR()`. Replace that line with:

```python
        import os as _os
        _threads = int(_os.environ.get("OCR_THREADS", "0"))
        kwargs = {}
        if _threads > 0:
            # rapidocr-onnxruntime 1.4.x accepts intra_op_num_threads per stage
            kwargs = {
                "det_use_cuda": False, "rec_use_cuda": False, "cls_use_cuda": False,
                "intra_op_num_threads": _threads, "inter_op_num_threads": 1,
            }
        try:
            import cv2 as _cv2
            if _threads > 0:
                _cv2.setNumThreads(_threads)
        except Exception:
            pass
        _engine = RapidOCR(**kwargs)
```

If `RapidOCR(**kwargs)` raises `TypeError` (param name drift between rapidocr versions), catch it and fall back to `RapidOCR()` — pinning is an optimization, never a crash:

```python
        try:
            _engine = RapidOCR(**kwargs)
        except TypeError:
            _engine = RapidOCR()
```

- [ ] **Step 2: Export the OCR gate from pipeline.py**

In `app/pack/pipeline.py`, find the module-level OCR semaphore (`asyncio.Semaphore(3)` near the top, used via `asyncio.to_thread` in `_read_numbers`). Rename/alias it to a public name without changing behavior:

```python
# Global OCR admission gate — shared by pack scans AND live-frame OCR so
# concurrent scanners can't oversubscribe the (small) Railway CPU.
OCR_GATE = asyncio.Semaphore(int(os.environ.get("OCR_CONCURRENCY", "3")))
```

Update the internal references to use `OCR_GATE`. (If the current name is already module-level, keep one object — do not create two semaphores.)

- [ ] **Step 3: Measure before/after locally**

```bash
cd /Users/kailee/pokemon-card-scanner
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls
caffeinate -i .venv/bin/python - <<'EOF'
import time, warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import cv2
from app.pack.rapidocr_reader import detect_lines
img = cv2.imread("tests/corpus/IMG_7102.heic".replace(".heic",".heic")) # if None, use any corpus jpeg
if img is None:
    from app.pack.pipeline import _decode
    img = _decode(open("tests/corpus/IMG_7102.heic","rb").read())
detect_lines(img[:20,:20])  # warm
t=time.time(); detect_lines(img); print(f"default threads: {time.time()-t:.2f}s")
EOF
OCR_THREADS=2 caffeinate -i .venv/bin/python - <<'EOF'
import time, warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
from app.pack.pipeline import _decode
from app.pack.rapidocr_reader import detect_lines
img = _decode(open("tests/corpus/IMG_7102.heic","rb").read())
detect_lines(img[:20,:20])
import time; t=time.time(); detect_lines(img); print(f"OCR_THREADS=2: {time.time()-t:.2f}s")
EOF
```
Expected: both runs succeed. On this M-series Mac the pinned run may be similar or slightly slower (many cores available) — that's fine; the win is on Railway's 2-vCPU box. Record both numbers in the commit message.

- [ ] **Step 4: Suite green**

Run: `.venv/bin/python -m pytest tests/ -q` → `7 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add app/pack/rapidocr_reader.py app/pack/pipeline.py
git commit -m "perf(ocr): pin ORT/cv2 threads via OCR_THREADS; export shared OCR_GATE"
```
(Append the repo's standard Co-Authored-By line.)

---

### Task 2: me-era catalog bridge (denominators + SetIdMap)

**Files:**
- Modify: `app/pack/data/set_denominators.json` (add me-era sets)
- Modify (data, via script rerun): `set_id_map` DB table
- Reference: `scripts/build_denominator_table.py`, `scripts/build_id_maps.py`

**Interfaces:**
- Produces: `load_denominator_table()` resolves me-era denominators (me02/086 etc.); `SetIdMap` rows for me-era sets where PokéWallet has them (price/rarity), absent otherwise (identity-only is acceptable per spec).

- [ ] **Step 1: Inspect what TCGdex has for the me era**

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs
.venv/bin/python - <<'EOF'
import asyncio
from sqlalchemy import text
from app.db.session import async_session_maker
async def main():
    async with async_session_maker() as s:
        rows = (await s.execute(text(
            "select id, name, card_count_official from tcgdex_set where id like 'me%' order by id"))).all()
        for r in rows: print(r)
asyncio.run(main())
EOF
```
Expected: me01…me05 (+ me02.5) with official counts (me02 Phantasmal Flames should show 86 — matching the user's /086 photos).

- [ ] **Step 2: Extend the denominator table**

Run `scripts/build_denominator_table.py --help` first to learn its invocation; if it supports a series/set filter, rerun it including the me sets and regenerate `app/pack/data/set_denominators.json`. If the script only knows PokéWallet sets, add the me-era entries to the JSON by hand from Step 1's counts, following the existing entry shape exactly (open the JSON, copy an existing record, fill set_id with the TCGdex id `me02` when no PokéWallet id exists — `resolve_set` only needs denominator uniqueness; `set_code` uses the TCGdex id).

Verify:
```bash
.venv/bin/python -c "
from app.pack.set_resolution import load_denominator_table
t = load_denominator_table()
hits = [s for s in t.sets if str(getattr(s,'set_code','')).startswith('me') or '86' in [str(d) for d in s.denominators]]
for s in hits: print(s.set_id, s.set_code, s.set_name, s.denominators)
assert any('86' in [str(d) for d in s.denominators] for s in t.sets), 'me02/86 missing'
print('denominator table OK')"
```

- [ ] **Step 3: Try the PokéWallet id-map extension**

Run `scripts/build_id_maps.py` for the me sets (check `--help`; likely `--series me`). If PokéWallet lacks the me era, the script will report no matches — that is an ACCEPTED outcome (spec: identity + TCGdex image, price "—"). Record the outcome in the commit message either way.

- [ ] **Step 4: Fixture regression gate**

The mixed-set fixture tests are sensitive to denominator-table changes (majority-gating exists for exactly this reason). Run: `.venv/bin/python -m pytest tests/ -q` → `7 passed, 1 skipped`. If a fixture test fails, the new entries collided with a fixture denominator — the fix is constraining the new entries (never loosening majority gating).

- [ ] **Step 5: Commit**

```bash
git add app/pack/data/set_denominators.json scripts/
git commit -m "feat(catalog): me-era sets in denominator table + id-map attempt"
```

---

### Task 3: Name index (`app/pack/name_index.py`)

**Files:**
- Create: `app/pack/name_index.py`
- Modify: `requirements.txt` (or the dependency file the repo uses — check `pyproject.toml`/`requirements*.txt`) to add `rapidfuzz>=3.0`

**Interfaces:**
- Consumes: `TcgdexCard`/`TcgdexSet` models (find exact names via `grep -rn "class TcgdexCard" app/`).
- Produces:
  - `normalize_name(s: str) -> str`
  - `async get_name_index() -> NameIndex` (lazy singleton, loads all cards once)
  - `NameIndex.match(ocr_text: str, *, denominator: str | None = None, min_score: int = 82) -> NameMatch | None` where `NameMatch = dataclass(tcgdex_set_id: str, set_name: str, local_id: str, card_name: str, score: float, ambiguous: bool)`
  - `ambiguous=True` when the best name is a substring of another catalog name OR multiple sets tie without a denominator to disambiguate — callers must then require number corroboration (spec).

- [ ] **Step 1: Install rapidfuzz**

```bash
.venv/bin/pip install "rapidfuzz>=3.0" && grep -q rapidfuzz requirements.txt || echo 'rapidfuzz>=3.0' >> requirements.txt
```

- [ ] **Step 2: Write the module**

```python
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
        from app.db.models import TcgdexCard, TcgdexSet  # adjust import to actual location
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(TcgdexSet.id, TcgdexSet.name, TcgdexCard.local_id,
                       TcgdexCard.name, TcgdexSet.card_count_official)
                .join(TcgdexCard, TcgdexCard.set_id == TcgdexSet.id))).all()
        _index = NameIndex([tuple(r) for r in rows])
        return _index
```

Adjust the model import path/columns to the real ones found in Step 1's grep (e.g. the set-id FK column may be `tcgdex_set_id`). Denominator narrowing uses `card_count_official` — the same field the denominator table was built from.

- [ ] **Step 3: Verify against the live dev DB**

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs
.venv/bin/python - <<'EOF'
import asyncio
from app.pack.name_index import get_name_index, normalize_name
async def main():
    idx = await get_name_index()
    for q, den in [("MEGA CHANDELUREEX?", "86"), ("Mega Chandelure ex", None),
                   ("PIKACHU", None), ("Flabebe", None)]:
        m = idx.match(q, denominator=den)
        print(f"{q!r} den={den} -> {m}")
asyncio.run(main())
EOF
```
Expected: the OCR-garbled `MEGA CHANDELUREEX?` with den=86 → me02 (or me05 — whichever set has it at that denominator) with `ambiguous=False`; bare `PIKACHU` → `ambiguous=True` (many printings); `Flabebe` matches the accented catalog name.

- [ ] **Step 4: Commit**

```bash
git add app/pack/name_index.py requirements.txt
git commit -m "feat(live): normalized fuzzy name index over TCGdex catalog"
```

---

### Task 4: Live identify core (`app/pack/live_identify.py`) + reel fixtures

**Files:**
- Create: `app/pack/live_identify.py`
- Create: `tests/corpus/reel/` (extracted steady frames from the user's reel video)
- Modify: `app/pack/ocr.py` (factor out `is_code_card`)

**Interfaces:**
- Consumes: `detect_lines(img, cap)` (rapidocr_reader), `parse_number(text, conf)` + `read_code_card(img)` (ocr.py), `NameIndex` (Task 3), `load_denominator_table()`, `get_set_numerators(set_id)`, `cached_lookup_card(...)`, `card_fields_from_match(...)`, `get_api_key()`.
- Produces:
  - `is_code_card(img_bgr) -> bool` (in ocr.py)
  - `async identify_frame(card_bgr, strip_bgr, prior: SessionPrior | None) -> FrameResult`
  - `SessionPrior = dataclass(set_id: str | None, set_name: str | None, denominator: str | None)`
  - `FrameResult = dataclass(kind: Literal["card","code_card","no_card","unreadable"], card: PackCard | None, code: CodeCardResult | None, identity_key: str | None, needs_vlm: bool)`
  - `identity_key` = `f"{set_id or set_name or '?'}:{numerator or name}"` — the session dedup key.

- [ ] **Step 1: Extract reel fixtures (do once)**

```bash
mkdir -p tests/corpus/reel
S=/private/tmp/claude-501/-Users-kailee-pokemon-card-scanner/2472d085-aebd-4499-89c6-199d4c2694c0/scratchpad/vid
ffmpeg -v error -i $S/clip.mp4 -vf "select='eq(n\,150)+eq(n\,255)+eq(n\,330)+eq(n\,420)',crop=478:864:0:130" -vsync vfr tests/corpus/reel/steady_%d.png -y
ls tests/corpus/reel/
```
Expected: 4 pngs, each a phone-view crop with a card held up. (Frame numbers chosen from the contact-sheet analysis; if a frame is mid-transition/blurred that is fine — blurred fixtures exercise the unreadable path.)

- [ ] **Step 2: Factor `is_code_card` out of `_read_code_via_qr` in ocr.py**

`_read_code_via_qr` already runs `cv2.QRCodeDetector().detectAndDecode` and checks the points. Add a public helper right above it that reuses the same detector logic without OCR:

```python
def is_code_card(image_bgr: np.ndarray) -> bool:
    """Cheap classifier: a detectable QR square means this frame is the code
    card, not a Pokémon card."""
    try:
        _txt, pts, _ = cv2.QRCodeDetector().detectAndDecode(image_bgr)
    except cv2.error:
        return False
    return pts is not None
```

- [ ] **Step 3: Write `app/pack/live_identify.py`**

```python
"""Single-card identification for the live scan mode.

Two SMALL OCR passes (name band of the card crop + the native-res number
strip) instead of whole-card OCR — that is the latency win. Decision ladder:
name+number agree > name+denominator-unique > number+catalog-valid > VLM."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.cards import cached_lookup_card, get_set_numerators
from app.pack.name_index import get_name_index
from app.pack.ocr import CodeReading, is_code_card, parse_number, read_code_card
from app.pack.rapidocr_reader import detect_lines
from app.pack.set_resolution import load_denominator_table
from app.pack.matching import card_fields_from_match
from app.pokewallet import get_api_key
from app.schemas import CodeCardResult, PackCard

log = logging.getLogger("pokemon_scanner.pack.live")


@dataclass
class SessionPrior:
    set_id: str | None
    set_name: str | None
    denominator: str | None


@dataclass
class FrameResult:
    kind: Literal["card", "code_card", "no_card", "unreadable"]
    card: PackCard | None
    code: CodeCardResult | None
    identity_key: str | None
    needs_vlm: bool


def _name_band(card_bgr: np.ndarray) -> np.ndarray:
    h = card_bgr.shape[0]
    return card_bgr[: max(1, int(h * 0.25))]


def _pw_set_id_for(tcgdex_set_id: str) -> str | None:
    """tcgdex set -> PokeWallet set_id via the denominator table (set_code
    holds the scanner set code; me-era sets may have no PW id -> None)."""
    table = load_denominator_table()
    for s in table.sets:
        if getattr(s, "tcgdex_id", None) == tcgdex_set_id or s.set_code == tcgdex_set_id:
            return s.set_id
    return None


async def identify_frame(card_bgr: np.ndarray, strip_bgr: np.ndarray | None,
                         prior: SessionPrior | None) -> FrameResult:
    if is_code_card(card_bgr):
        cr: CodeReading = read_code_card(card_bgr)
        return FrameResult(
            "code_card", None,
            CodeCardResult(code=cr.code, confidence=round(cr.confidence, 3),
                           format_ok=cr.format_ok),
            None, needs_vlm=False)

    name_lines = detect_lines(_name_band(card_bgr), cap=1400)
    strip_lines = detect_lines(strip_bgr, cap=1600) if strip_bgr is not None else []
    if not name_lines and not strip_lines:
        return FrameResult("no_card", None, None, None, needs_vlm=False)

    # number: best pattern_ok parse from the strip (fall back to name-band lines)
    reading = None
    for _y, text, conf in sorted(strip_lines + name_lines, key=lambda t: -t[2]):
        r = parse_number(text, conf)
        if r is not None and r.pattern_ok:
            reading = r
            break

    # name: highest-confidence line in the TITLE band only (hard filter)
    idx = await get_name_index()
    name_match = None
    for _y, text, conf in sorted(name_lines, key=lambda t: -t[2]):
        den = reading.denominator if reading else (prior.denominator if prior else None)
        m = idx.match(text, denominator=den)
        if m is not None:
            name_match = m
            break

    numerator = reading.numerator.lstrip("0") or "0" if reading and reading.numerator else None
    set_id = set_code = set_name = None
    confident = False

    if name_match and numerator and name_match.local_id.lstrip("0") == numerator:
        confident = True                      # name + number agree
    elif name_match and not name_match.ambiguous:
        confident = True                      # unique name (+denominator prior)
        numerator = numerator or (name_match.local_id.lstrip("0") or "0")
    if name_match and confident:
        set_name = name_match.set_name
        set_code = name_match.tcgdex_set_id
        set_id = _pw_set_id_for(name_match.tcgdex_set_id)

    if not confident and reading is not None and prior and prior.set_id:
        valid = get_set_numerators(prior.set_id)
        if numerator and (not valid or numerator in valid):
            confident = True                  # number valid in session's set
            set_id, set_name = prior.set_id, prior.set_name

    if not confident and (reading is None and name_match is None):
        return FrameResult("unreadable", None, None, None, needs_vlm=True)

    fields: dict = {"name": None, "rarity": None, "image_url": None, "match_id": None}
    if set_id and numerator:
        try:
            match = await cached_lookup_card(set_id, numerator,
                                             set_name=set_name, api_key=get_api_key())
            fields = card_fields_from_match(match)
        except Exception as e:
            log.warning("live.lookup_failed err=%r", e)
    if fields.get("name") is None and name_match is not None:
        fields["name"] = name_match.card_name

    display_number = None
    if numerator:
        den = reading.denominator if reading and reading.denominator else \
            (prior.denominator if prior else None)
        display_number = f"{numerator.zfill(3)}/{den}" if den else numerator

    card = PackCard(
        row_index=-1,  # assigned by the session store
        card_number=display_number, set_id=set_id, set_code=set_code,
        set_name=set_name, confidence=0.9 if confident else 0.3,
        low_confidence_reason=None if confident else "number_ambiguous",
        needs_review=not confident, **fields)
    key = f"{set_code or set_name or '?'}:{numerator or normalize_key(fields.get('name'))}"
    return FrameResult("card", card, None, key, needs_vlm=not confident)


def normalize_key(name: str | None) -> str:
    from app.pack.name_index import normalize_name
    return normalize_name(name or "unknown")
```

Two integration notes for the implementer: (1) check `PackCard`'s actual required fields in `app/schemas.py` and pass them all; (2) `_pw_set_id_for` depends on how Task 2 stored me-era entries — open `set_denominators.json` and match its real field names (add a `tcgdex_id` field to entries there if absent; the loader is tolerant of extra JSON keys — verify by reading `load_denominator_table`).

- [ ] **Step 4: Verify on reel fixtures + a corpus crop**

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs
caffeinate -i .venv/bin/python - <<'EOF'
import asyncio, warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)
import cv2, glob
from app.pack.live_identify import identify_frame, SessionPrior
async def main():
    for p in sorted(glob.glob("tests/corpus/reel/*.png")):
        img = cv2.imread(p)
        h = img.shape[0]
        strip = img[int(h*0.75):]          # coarse bottom band as the "strip"
        r = await identify_frame(img, strip, SessionPrior(None, None, "86"))
        c = r.card
        print(p.split("/")[-1], "->", r.kind,
              c and (c.card_number, c.set_name, c.name, c.needs_review), "vlm:", r.needs_vlm)
asyncio.run(main())
EOF
```
Expected: at least one steady frame yields `kind=card` with the Mega Chandelure identity (name via index; needs_review False when name+denominator resolves), blurred frames yield `unreadable`/`no_card` with `needs_vlm=True`. Record actual output in the commit message.

- [ ] **Step 5: Suite green, commit**

```bash
.venv/bin/python -m pytest tests/ -q   # 7 passed, 1 skipped
git add app/pack/live_identify.py app/pack/ocr.py tests/corpus/reel/
git commit -m "feat(live): single-frame identify core (QR gate, band OCR, name ladder) + reel fixtures"
```

---

### Task 5: Live session store (`app/pack/live_session.py`)

**Files:**
- Create: `app/pack/live_session.py`

**Interfaces:**
- Consumes: `FrameResult`/`SessionPrior` (Task 4), `vlm_client.identify`, the `_vlm_fallback` merge conventions (accept ≥0.7, set-name→set resolution via denominator table), photo storage dir from `app/db/config.py::db_settings().photo_storage_dir`.
- Produces:
  - `async start_session(trainer_id: str) -> str`
  - `async get_session(session_id: str, trainer_id: str) -> LiveSession | None` (ownership enforced)
  - `LiveSession` fields: `id`, `trainer_id`, `cards: list[LiveCard]`, `code: CodeCardResult | None`, `frame_lock: asyncio.Lock`, `prior() -> SessionPrior`, `add_frame_result(res: FrameResult, card_jpeg: bytes) -> LiveEvent`, `resolve_duplicate(row_index: int, add: bool)`, `mark_replaceable(row_index: int)`, `finish() -> list[PackCard]`, `frame_path(row_index) -> Path`
  - `LiveCard = dataclass(card: PackCard, identity_key: str, state: Literal["ok","pending_vlm","vlm_failed","dup_prompt"], captured_at: float, replaceable: bool)`
  - `LiveEvent = dataclass(event: Literal["card","code_card","duplicate_prompt","no_card","unreadable"], card: PackCard | None, pending_vlm: bool)`
  - Constants: `DUP_WINDOW_S = 2.0`, `SESSION_TTL_S = 1800` (sliding), `VLM_ACCEPT = 0.7`.

Behavior contract (implement exactly):
1. `add_frame_result`: `no_card`/`unreadable` pass through as events (unreadable with `needs_vlm` queues nothing — there is no card row yet; the client retries with its 2nd-best frame). `code_card` sets `session.code` (later read wins if `format_ok`). `card`: assign `row_index = len(cards)`, persist `card_jpeg` to `<photo_dir>/live_sessions/<session_id>/frame_<row>.jpg`, then dedup: if an existing non-replaceable card has the same `identity_key` — within `DUP_WINDOW_S` of that card's `captured_at` → silently update its confidence (return event `card` with the EXISTING card, no new row); after the window → add the new row with `state="dup_prompt"` and return `duplicate_prompt`. If a `replaceable` row matches any identity, overwrite that row in place. Otherwise append normally.
2. `needs_vlm` cards enter a per-session pending list. A single background task per session (`asyncio.create_task`, held in a registry `dict[str, asyncio.Task]` with a done-callback that discards) drains the list in ONE `vlm_client.identify` batch every time it wakes (debounce ~2s so consecutive uncertain cards share a call). On answer ≥ `VLM_ACCEPT`: merge exactly like `_vlm_fallback` (number/denominator/set_name → set resolution → `cached_lookup_card` → clear needs_review, state="ok"). On failure/timeout/low confidence: `state="vlm_failed"` (terminal — polling stops).
3. Sliding TTL: every touched session refreshes `expires_at = now + SESSION_TTL_S`. A lazy sweep on `start_session` deletes expired sessions AND their frame dirs (`shutil.rmtree`).
4. `finish()`: renumber `row_index` 0..n-1 in captured order (dropping `dup_prompt` rows the user ignored), return the PackCard list; leave frames on disk for Task 7 to move.
5. In-memory store is a module-level dict guarded by an `asyncio.Lock`; one `frame_lock` per session gives the 409 semantics in Task 6.

- [ ] **Step 1: Write the module** (follow the contract above; ~150 lines. Reuse `_vlm_fallback`'s merge by importing the shared pieces — if lifting a helper out of `pipeline.py` is needed, extract `apply_vlm_answer(card: PackCard, ans: dict) -> None` into `app/pack/vlm_merge.py` and re-point `_vlm_fallback` to it so both paths share one merge implementation.)

- [ ] **Step 2: Smoke the store standalone**

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs PHOTO_STORAGE_DIR=./var/pulls
.venv/bin/python - <<'EOF'
import asyncio
from app.pack.live_session import start_session, get_session, DUP_WINDOW_S
from app.pack.live_identify import FrameResult
from app.schemas import PackCard
async def main():
    sid = await start_session("smoke-trainer")
    s = await get_session(sid, "smoke-trainer")
    mk = lambda key: FrameResult("card", PackCard(row_index=-1, card_number="126/167",
        set_id=None, set_code="sv06", set_name="Twilight Masquerade", name="Test",
        rarity=None, image_url=None, match_id=None, confidence=0.9,
        low_confidence_reason=None, needs_review=False), None, key, False)
    e1 = s.add_frame_result(mk("sv06:126"), b"jpegbytes")
    e2 = s.add_frame_result(mk("sv06:126"), b"jpegbytes")   # inside window -> same card
    assert e1.event == "card" and e2.event == "card" and len(s.cards) == 1, (e1, e2, len(s.cards))
    s.cards[0].captured_at -= DUP_WINDOW_S + 1
    e3 = s.add_frame_result(mk("sv06:126"), b"jpegbytes")   # later -> prompt
    assert e3.event == "duplicate_prompt" and len(s.cards) == 2
    assert (await get_session(sid, "someone-else")) is None, "ownership leak"
    print("session store OK; frames at", s.frame_path(0))
asyncio.run(main())
EOF
```
Expected: `session store OK` and a real jpg on disk under `var/pulls/live_sessions/<sid>/`.

- [ ] **Step 3: Commit**

```bash
git add app/pack/live_session.py app/pack/vlm_merge.py app/pack/pipeline.py
git commit -m "feat(live): in-memory session store — dedup window, VLM batch task, TTL sweep"
```

---

### Task 6: Live API router + PackCard price fields

**Files:**
- Create: `app/pack/live_api.py`
- Modify: `app/schemas.py` (PackCard price fields)
- Modify: `app/main.py` (router include)

**Interfaces:**
- Consumes: `identify_frame`/`SessionPrior` (Task 4), session store (Task 5), `OCR_GATE` (Task 1), `_decode` (pipeline), `latest_price_map` (app/prices.py), `CurrentTrainer` dep (app/db/users.py).
- Produces the HTTP contract the frontend (Task 8) consumes:
  - `POST /scan/live/start` → `{"session_id": str}`
  - `POST /scan/live/{sid}/frame` (multipart: `card` UploadFile required, `strip` UploadFile optional) → `LiveFrameOut = {event, card: PackCard|null, pending_vlm: bool, code_card: CodeCardResult|null, cards_count: int}`; `409` `{"detail":"busy"}` when the session's frame_lock is held; `404` when session missing/expired/foreign.
  - `GET /scan/live/{sid}` → `LiveStateOut = {cards: [PackCardWithState], code_card, any_pending: bool}` where `PackCardWithState = PackCard + {state: str}`
  - `GET /scan/live/{sid}/card/{row}/image` → image/jpeg (FileResponse of the stored frame)
  - `POST /scan/live/{sid}/card/{row}/duplicate` (body `{"add": bool}`) → resolves a dup_prompt row
  - `POST /scan/live/{sid}/card/{row}/replace` → marks row replaceable ("wrong? re-scan")
  - `POST /scan/live/{sid}/finish` → `PackScanResponse` (existing schema; segmentation_warning=null)

- [ ] **Step 1: Add price fields to PackCard** in `app/schemas.py`:

```python
    price_usd_low: float | None = None
    price_usd_high: float | None = None
```
(frontend api.ts already declares these as optional — verify names match exactly.)

- [ ] **Step 2: Write the router.** Skeleton with every endpoint's core logic:

```python
"""Live scan session API. All endpoints owner-scoped via CurrentTrainer."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from app.db.users import CurrentTrainer
from app.db.session import async_session_maker
from app.pack.pipeline import OCR_GATE, _decode
from app.pack.live_identify import identify_frame
from app.pack import live_session as store
from app.prices import latest_price_map
from app.schemas import PackScanResponse

log = logging.getLogger("pokemon_scanner.pack.live_api")
router = APIRouter(prefix="/scan/live", tags=["live-scan"])


@router.post("/start")
async def start(trainer: CurrentTrainer):
    return {"session_id": await store.start_session(str(trainer.id))}


async def _sess(sid: str, trainer) -> "store.LiveSession":
    s = await store.get_session(sid, str(trainer.id))
    if s is None:
        raise HTTPException(404, "session not found")
    return s


@router.post("/{sid}/frame")
async def frame(sid: str, trainer: CurrentTrainer,
                card: UploadFile = File(...), strip: UploadFile | None = File(None)):
    s = await _sess(sid, trainer)
    if s.frame_lock.locked():
        raise HTTPException(409, "busy")
    async with s.frame_lock:
        card_bytes = await card.read()
        img = _decode(card_bytes)
        if img is None:
            raise HTTPException(422, "unreadable image")
        strip_img = None
        if strip is not None:
            strip_img = _decode(await strip.read())
        async with OCR_GATE:
            res = await identify_frame(img, strip_img, s.prior())
        if res.card is not None:
            await _attach_price(res.card)
        event = s.add_frame_result(res, card_bytes)
        return {"event": event.event, "card": event.card,
                "pending_vlm": event.pending_vlm,
                "code_card": s.code, "cards_count": len(s.cards)}


async def _attach_price(card) -> None:
    if not card.match_id:
        return
    try:
        async with async_session_maker() as session:
            pm, _asof = await latest_price_map(session)
        lo_hi = pm.get(card.match_id)
        if lo_hi:
            card.price_usd_low, card.price_usd_high = lo_hi
    except Exception as e:
        log.warning("live.price_failed err=%r", e)


@router.get("/{sid}")
async def state(sid: str, trainer: CurrentTrainer):
    s = await _sess(sid, trainer)
    return {"cards": [{**c.card.model_dump(), "state": c.state} for c in s.cards],
            "code_card": s.code,
            "any_pending": any(c.state == "pending_vlm" for c in s.cards)}


@router.get("/{sid}/card/{row}/image")
async def card_image(sid: str, row: int, trainer: CurrentTrainer):
    s = await _sess(sid, trainer)
    p = s.frame_path(row)
    if not p.exists():
        raise HTTPException(404, "no frame")
    return FileResponse(p, media_type="image/jpeg")


@router.post("/{sid}/card/{row}/duplicate")
async def dup(sid: str, row: int, trainer: CurrentTrainer, body: dict):
    s = await _sess(sid, trainer)
    s.resolve_duplicate(row, bool(body.get("add", True)))
    return {"ok": True}


@router.post("/{sid}/card/{row}/replace")
async def replace(sid: str, row: int, trainer: CurrentTrainer):
    s = await _sess(sid, trainer)
    s.mark_replaceable(row)
    return {"ok": True}


@router.post("/{sid}/finish")
async def finish(sid: str, trainer: CurrentTrainer):
    s = await _sess(sid, trainer)
    cards = s.finish()
    from app.pack.confidence import pack_confidence
    from app.schemas import CodeCardResult
    return PackScanResponse(
        cards=cards,
        code_card=s.code or CodeCardResult(code=None, confidence=0.0, format_ok=False),
        pack_confidence=pack_confidence([c.confidence for c in cards]),
        segmentation_warning=None)
```

Check the actual pydantic version conventions in the repo (`model_dump` vs `dict`) and the exact `CurrentTrainer` import path before writing. Register in `app/main.py` next to the existing `include_router` block: `app.include_router(live_api.router)`.

- [ ] **Step 3: End-to-end curl smoke**

```bash
pkill -f "uvicorn app.main"; sleep 1
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false
nohup .venv/bin/uvicorn app.main:app --port 8000 >/tmp/liveapi.log 2>&1 &
sleep 4
# login (cookie jar)
curl -s -c /tmp/cj -X POST http://127.0.0.1:8000/auth/jwt/login -d "username=tduser@x.io&password=trainerpass1" -H "content-type: application/x-www-form-urlencoded" >/dev/null
SID=$(curl -s -b /tmp/cj -X POST http://127.0.0.1:8000/scan/live/start | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "session: $SID"
curl -s -b /tmp/cj -F "card=@tests/corpus/reel/steady_2.png" http://127.0.0.1:8000/scan/live/$SID/frame | python3 -m json.tool | head -25
curl -s -b /tmp/cj http://127.0.0.1:8000/scan/live/$SID | python3 -m json.tool | tail -5
curl -s -b /tmp/cj -X POST http://127.0.0.1:8000/scan/live/$SID/finish | python3 -c "import sys,json; d=json.load(sys.stdin); print('finish cards:', len(d['cards']), 'code:', d['code_card']['code'])"
curl -s -o /dev/null -w "no-auth -> %{http_code}\n" -X POST http://127.0.0.1:8000/scan/live/start
pkill -f "uvicorn app.main"
```
Expected: frame POST returns `event: card` with an identity (or `unreadable` for a blurred fixture — then try `steady_1.png`); state shows the card; finish returns a PackScanResponse; unauthenticated start → 401.

- [ ] **Step 4: Suite green, commit**

```bash
.venv/bin/python -m pytest tests/ -q   # 7 passed, 1 skipped
git add app/pack/live_api.py app/schemas.py app/main.py
git commit -m "feat(live): session API — start/frame/state/image/duplicate/replace/finish + PackCard prices"
```

---

### Task 7: Save integrity — live pulls, contact sheet, code rescue

**Files:**
- Modify: `app/pulls.py` (accept live saves; PATCH code endpoint)
- Modify: `app/storage.py` (persist per-card frames with the pull)
- Modify: the stats re-derivation entry point (find via `grep -rn "capture_path" app/stats/ app/admin*` — wherever pulls' staircase photos are re-OCRed) and the training harvest (`app/training_data.py` / `training/fetch_uploads.py`) to SKIP `capture_path="live"`.

**Interfaces:**
- Consumes: session store frames dir (Task 5), existing `save_pull` flow, `read_code_card`.
- Produces:
  - `POST /pulls` unchanged in signature; live saves send `capture_path="live"`, staircase=composite jpeg, code_card=live code frame (client builds both — see Task 10).
  - `PATCH /pulls/{pull_id}/code` (multipart `code_card` UploadFile; owner-only; only when `verified is False`) → re-runs the existing code re-OCR + verified/duplicate logic → `{verified: bool, code: str|null}`.
  - `move_session_frames(session_id: str, trainer_id: str, pull_id: str) -> int` in storage.py — moves `live_sessions/<sid>/frame_*.jpg` into the pull's photo dir as `frame_NN.jpg`, returns count. Called from a new optional `live_session_id` Form field on `POST /pulls` (None for staircase saves — zero behavior change).

- [ ] **Step 1: rederive/harvest skip.** Locate every consumer that re-reads pull staircase photos for machine purposes. For each, add at the query/filter level: `Pull.capture_path != "live"`. Verify by grep that no other machine consumer reads `staircase_photo_path` (photo-serving endpoints for humans are fine).

- [ ] **Step 2: `live_session_id` on save.** In `app/pulls.py::save_pull`, add `live_session_id: str | None = Form(None)`; after the pull row exists and photos are saved, if set: `moved = move_session_frames(live_session_id, str(trainer.id), str(pull.id))`, log count. Non-fatal if the session already expired (frames are a bonus, not a requirement).

- [ ] **Step 3: `PATCH /pulls/{pull_id}/code`.** Mirror the code-validation block already in `save_pull` (`read_code_card` → `_normalize_code` → uniqueness check → `verified`): load the owned pull (404 if not owner), 409 if already verified, decode upload, re-OCR, update `code`, `code_format_ok`, `verified`, save the new code photo via the storage helper (overwrite `code.jpg`), return `{"verified": pull.verified, "code": pull.code}`.

- [ ] **Step 4: Smoke** — save a live-style pull end-to-end:

```bash
pkill -f "uvicorn app.main"; sleep 1
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false
nohup .venv/bin/uvicorn app.main:app --port 8000 >/tmp/live7.log 2>&1 &
sleep 4
curl -s -c /tmp/cj -X POST http://127.0.0.1:8000/auth/jwt/login -d "username=tduser@x.io&password=trainerpass1" -H "content-type: application/x-www-form-urlencoded" >/dev/null
CARDS='[{"row_index":0,"card_number":"126/167","set_id":"23473","set_code":"sv06","set_name":"Twilight Masquerade","name":"Smoke","rarity":null,"image_url":null,"match_id":null,"confidence":0.9,"low_confidence_reason":null,"needs_review":false}]'
curl -s -b /tmp/cj -F "staircase=@tests/corpus/reel/steady_2.png" -F "code_card=@tests/corpus/reel/steady_1.png" \
  -F "cards=$CARDS" -F "capture_path=live" -F "pack_confidence=0.9" http://127.0.0.1:8000/pulls | python3 -c "
import sys,json; d=json.load(sys.stdin); print('saved pull', d['id'], 'verified:', d['verified'], 'capture_path ok')"
# PATCH rescue with an unreadable code photo still returns verified:false (graceful)
pkill -f "uvicorn app.main"
```
Expected: pull saves (verified false — reel frame has no QR), PATCH endpoint exists and responds 200/409 per state. Also verify the rederive skip: run the stats recompute admin path (or its function directly) and confirm live pulls are not re-OCRed (log or query check).

- [ ] **Step 5: Suite green, commit**

```bash
.venv/bin/python -m pytest tests/ -q
git add app/pulls.py app/storage.py app/stats/ app/training_data.py
git commit -m "feat(live): live pull saves (frames moved, machine consumers skip), PATCH code rescue"
```

---

### Task 8: Frontend API client for live sessions

**Files:**
- Modify: `frontend/src/api.ts`

**Interfaces:**
- Consumes: Task 6's HTTP contract.
- Produces (exact exports Tasks 9–11 import):

```typescript
export type LiveCardState = "ok" | "pending_vlm" | "vlm_failed" | "dup_prompt";
export type LiveEventKind = "card" | "code_card" | "duplicate_prompt" | "no_card" | "unreadable";
export interface LiveCard extends PackCard { state?: LiveCardState }
export interface LiveFrameOut {
  event: LiveEventKind; card: PackCard | null; pending_vlm: boolean;
  code_card: CodeCardResult | null; cards_count: number;
}
export interface LiveState { cards: LiveCard[]; code_card: CodeCardResult | null; any_pending: boolean }

export async function liveStart(): Promise<string>;
export async function liveFrame(sid: string, card: Blob, strip?: Blob): Promise<LiveFrameOut>;  // throws {status:409} on busy
export async function liveState(sid: string): Promise<LiveState>;
export function liveCardImageUrl(sid: string, row: number): string;
export async function liveDuplicate(sid: string, row: number, add: boolean): Promise<void>;
export async function liveReplace(sid: string, row: number): Promise<void>;
export async function liveFinish(sid: string): Promise<PackScanResponse>;
export async function patchPullCode(pullId: string, code: Blob): Promise<{verified: boolean; code: string | null}>;
```

- [ ] **Step 1: Implement** following the file's existing fetch conventions exactly (base URL const, `credentials: "include"` for authed routes — note the live endpoints are ALL authed, unlike `/scan/pack`). `liveFrame` builds `FormData` with `card` (`card.jpg`) and optional `strip` (`strip.jpg`); on 409 throw an object `{status: 409}` so the capture loop can back off. `liveCardImageUrl` returns `` `${base}/scan/live/${sid}/card/${row}/image` `` (used directly in `<img src>`; cookies ride along same-origin).

- [ ] **Step 2: Type-check:** `cd frontend && npx tsc --noEmit` → clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api.ts
git commit -m "feat(live): frontend api client for live sessions"
```

---

### Task 9: LiveCapture component (camera loop)

**Files:**
- Create: `frontend/src/capture/LiveCapture.tsx`

**Interfaces:**
- Consumes: nothing from other tasks (pure capture component; reuses CSS classes from `CameraCapture.tsx` — read it first and follow its patterns for stream setup/teardown and the overlay canvas).
- Produces:

```typescript
interface LiveCaptureProps {
  onFire: (card: Blob, strip: Blob, secondBest: Blob | null) => void;  // called per capture event
  paused: boolean;          // parent pauses firing while a POST is in flight + queue full
  autoFire: boolean;        // toggle state owned by parent
  onCameraInfo?: (settings: MediaTrackSettings) => void;
}
export default function LiveCapture(props: LiveCaptureProps): JSX.Element;
```

- [ ] **Step 1: Write the component.** Complete implementation requirements (each maps to a spec/critique line — do not skip any):

```typescript
// frontend/src/capture/LiveCapture.tsx — live camera with auto-fire.
// Key numbers (from the design spec):
const METRICS_W = 160;        // motion canvas width
const STRIP_SHARP_W = 500;    // sharpness measured on the number-strip region
const STABLE_MS = 300;        // steady+sharp duration before firing
const COOLDOWN_MS = 1500;     // min gap between fires
const REARM_MOTION = 14;      // mean-abs-diff (0-255) that re-arms after a fire
const FIRE_MOTION = 6;        // below this = "stable"
const GUIDE = { x: 0.14, y: 0.08, w: 0.72, h: 0.80 };  // card fills 70-80% of height
const STRIP_FRAC = 0.16;      // bottom fraction of guide = number strip
const TARGET_CARD_H = 1400;   // upload card crop height cap
```

1. **Stream:** `getUserMedia({ video: { facingMode: "environment", width: { ideal: 3840 }, height: { ideal: 2160 } } })`; on `OverconstrainedError`/failure retry with `{ facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } }`; report `track.getSettings()` via `onCameraInfo`. `<video autoPlay playsInline muted>` exactly like CameraCapture.
2. **Wake lock:** on mount `navigator.wakeLock?.request("screen")` (store sentinel); re-request in a `visibilitychange` listener when `document.visibilityState === "visible"`; release on unmount. Failure = non-fatal (show nothing; the design's hint copy lives in Task 10's screen).
3. **Lifecycle:** listen for track `mute`/`ended` and `visibilitychange`→hidden: set an internal `interrupted` state that renders a full-screen "Camera paused — tap to resume" button; tapping re-runs the getUserMedia setup, re-reads settings, and clears the state. Recompute all guide-box pixel mappings from `videoWidth/videoHeight` on every resume and on `resize`.
4. **Canvases (exactly two, persistent, module-scope refs):** `metricsCanvas` (`METRICS_W` wide, `getContext("2d", { willReadFrequently: true })`) and `captureCanvas` (stream-res). Never allocate per-frame.
5. **Loop:** `video.requestVideoFrameCallback` when available else `requestAnimationFrame` throttled to ~12Hz. Per tick: draw video → metricsCanvas; compute (a) motion = mean |gray diff| vs previous metrics frame; (b) card presence = fraction of Sobel-ish edge pixels (simple neighbor-diff threshold) inside the guide region > 0.05; (c) when motion < FIRE_MOTION and presence, compute strip sharpness: draw ONLY the strip region (guide bottom `STRIP_FRAC`) into metricsCanvas at `STRIP_SHARP_W` wide and take variance of a 3×3 Laplacian over the gray pixels. Track a rolling `stableSince` timestamp; when `now - stableSince >= STABLE_MS` and cooldown elapsed and `!paused` and (`autoFire` or shutterPressed): **fire**.
6. **Fire:** draw the CURRENT video frame to captureCanvas at native res; crop guide box → scale so card height ≤ TARGET_CARD_H → `toBlob("image/jpeg", 0.8)` = card blob; crop the strip region at NATIVE res (no scaling) → strip blob; keep the previous candidate frame's card blob as `secondBest` (maintain a 2-slot ring of {blob, sharpness} during the stable window, chosen by strip sharpness). Call `props.onFire(card, strip, secondBest)`. Then require `motion > REARM_MOTION` (card leaves) before the next stable window can begin.
7. **Manual shutter:** always-rendered button (same styling as CameraCapture's shutter); pressing it fires immediately from the current frame regardless of stability gates (still respects `paused`).
8. **Overlay:** guide rectangle + a subtle state tint (searching / locking / fired flash). Flash on fire + `navigator.vibrate?.(30)`.

- [ ] **Step 2: Type-check:** `cd frontend && npx tsc --noEmit` → clean.

- [ ] **Step 3: Desktop browser sanity** (webcam ≠ phone but proves the loop): `npm run dev`, open the app, temporarily mount `<LiveCapture>` behind the scan flow (Task 10 wires it properly — for now visit via the Task 10 branch or a scratch route). Confirm in devtools: exactly 2 canvases, fire events log with 3 blobs, holding a card (or phone photo of one) to the webcam fires within ~1s, waving re-arms. This is a smoke, not the acceptance gate — the phone test is Task 13.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/capture/LiveCapture.tsx
git commit -m "feat(live): LiveCapture — camera contract, wake lock, steady+sharp auto-fire, 2-canvas loop"
```

---

### Task 10: Live scan screen + mode chooser + App wiring

**Files:**
- Create: `frontend/src/capture/LiveScanScreen.tsx`
- Modify: `frontend/src/App.tsx` (Step union + mode chooser + flow)

**Interfaces:**
- Consumes: LiveCapture (Task 9), api client (Task 8), existing `Step` machine + ReviewScreen + savePull in App.tsx.
- Produces: `LiveScanScreen` props `{ onDone: (scan: PackScanResponse, sessionId: string, compositeBlob: Blob, codeBlob: Blob | null) => void; onCancel: () => void }`; App gains Step variants `{name:"mode"}` and `{name:"live"}` and a `{name:"code_choice", ...}` interstitial.

- [ ] **Step 1: LiveScanScreen.** Owns the session + tray:
1. On mount: `liveStart()` → sid (store also in `sessionStorage` for crash recovery). Maintain `tray: LiveCard[]`, `codeCard`, `queue: {card,strip,secondBest}[]`, `inFlight: boolean`, plus a client-side archive `capturedBlobs: Blob[]` (durable tray source per spec — kept in memory).
2. `onFire`: push to queue, optimistic chip (thumbnail via `URL.createObjectURL(card)`, spinner). Drain loop: one `liveFrame` at a time (`inFlight` guards; on `{status:409}` wait 500ms and retry once); `drop-stale`: if the queue holds >1 entry from the same hold (fires within `COOLDOWN_MS`), keep the newest. Map responses: `card` → replace optimistic chip with identity (name, number, set, price, ❓ if `pending_vlm`); `code_card` → fill the code slot chip; `duplicate_prompt` → chip renders "Another copy of NAME? [Add] [Ignore]" calling `liveDuplicate(sid,row,add)`; `no_card`/`unreadable` → if `secondBest` exists send it once, else drop the optimistic chip with a brief "didn't catch that — show it again" toast.
3. While any tray card `pending_vlm`: poll `liveState(sid)` every 2s and patch chips in place; stop when none pending (and on unmount).
4. `aria-live="polite"` region announcing additions ("Pikachu, 25 of 165, added").
5. Auto-fire toggle + camera-settings line (from `onCameraInfo`: shows e.g. "1080p — fill the guide with the card" hint below 1080p).
6. **Finish:** builds the composite contact sheet client-side: draw up to N tray thumbnails into a grid canvas (cols = ceil(sqrt(n)), each cell ~400px wide) → `toBlob("image/jpeg", 0.85)` = compositeBlob. If `codeCard` empty → render the code-choice interstitial FIRST: "Scan code card (needed to battle & count in stats)" [primary → keep scanning, camera stays live] / "Save anyway — this pull can't battle" [secondary]. Then `liveFinish(sid)` → `onDone(scan, sid, compositeBlob, codeBlob)`. (`codeBlob` = the frame the server classified as code — capture it client-side when a fire's response says `code_card`; null if skipped.)
7. Session-death resilience: any 404 from `liveFrame`/`liveState` → toast "session expired — restarting", `liveStart()` again, REPLAY the tray by re-POSTing `capturedBlobs` sequentially (the drain loop already serializes), preserving user dup/ignore decisions client-side.
- [ ] **Step 2: App wiring.** In `App.tsx`: `Step` gains `{name:"mode"}` (rendered when view==="scan" && step.name==="mode": two big buttons "One photo" / "Live") and `{name:"live"}` rendering `<LiveScanScreen onDone={(scan, sid, composite, code) => setStep({name:"review", scan, staircase: composite, code: code ?? composite, meta: undefined, liveSessionId: sid})} onCancel={() => setStep({name:"mode"})} />`. Entry points that currently `setStep({name:"staircase"})` now `setStep({name:"mode"})`. The review Step type gains optional `liveSessionId?: string`; `doSave` passes it to `savePull` (extend `savePull` signature with optional `liveSessionId` → `live_session_id` Form field, and set `capture_path: "live"` when present). Falls through to the existing save/summary/battle flow untouched.
- [ ] **Step 3: Type-check + dev-server click-through** (desktop webcam): mode chooser appears; Live opens camera; manual shutter fires; finish (0 cards ok) reaches review; cancel returns to chooser. `npx tsc --noEmit` clean.
- [ ] **Step 4: Commit**

```bash
git add frontend/src/capture/LiveScanScreen.tsx frontend/src/App.tsx frontend/src/api.ts
git commit -m "feat(live): live scan screen (tray, dup prompts, code choice, session recovery) + mode chooser"
```

---

### Task 11: Review screen — thumbnails, universal fix, pending states

**Files:**
- Modify: `frontend/src/review/ReviewScreen.tsx`, `frontend/src/review/CardRow.tsx`
- Modify: `frontend/src/App.tsx` (pass live context to review)

**Interfaces:**
- Consumes: `liveCardImageUrl(sid, row)` (Task 8); review currently blocks confirm on `(needs_review ?? low_confidence_reason !== null) && !resolved`.
- Produces: ReviewScreen accepts optional `liveSessionId?: string`; every row (flagged or not) is tappable into FixCardForm; live rows show the captured-frame thumbnail; `pending_vlm` rows show "still identifying…" and auto-refresh.

- [ ] **Step 1:** ReviewScreen: add optional `liveSessionId` prop (threaded from App's review step). CardRow: when `liveSessionId` present render `<img src={liveCardImageUrl(liveSessionId, card.row_index)} className="review-thumb">` (add a ~64px thumb style consistent with existing card rows); make the whole row clickable to open FixCardForm for ANY card (keep the existing Fix button for flagged ones — the affordance the spec requires); rows with state `pending_vlm` (threaded via the scan's cards when live) show a spinner + "still identifying — wait or fix manually" instead of the flag copy, and ReviewScreen polls `liveState` every 2s while any such row exists, patching in VLM answers (per spec: Finish is never blocked; review stays patchable).
- [ ] **Step 2:** Type-check + click-through: from a desktop live session with 1 card, review shows the thumbnail, tapping an unflagged row opens FixCardForm, confirm saves. `npx tsc --noEmit` clean.
- [ ] **Step 3: Commit**

```bash
git add frontend/src/review/ frontend/src/App.tsx
git commit -m "feat(live): review thumbnails, any-card fix, pending-VLM rows"
```

---

### Task 12: SSE progressive staircase scan

**Files:**
- Create: `app/pack/scan_stream.py` (SSE wrapper)
- Modify: `app/pack/pipeline.py` (optional progress callback), `app/main.py` (route)
- Modify: `frontend/src/api.ts` + `frontend/src/App.tsx` (progressive submit path)

**Interfaces:**
- Produces: `POST /scan/pack/stream` — same multipart form as `/scan/pack`, `text/event-stream` response emitting `event: progress` lines (`{"stage":"decoded"}`, `{"stage":"cards_found","count":N}`, `{"stage":"identifying","done":k,"total":n}`) every stage + `: hb` comment heartbeats every 15s + terminal `event: result` carrying the full `PackScanResponse` JSON; existing `/scan/pack` untouched (fallback).
- `scan_pack` gains optional `progress: Callable[[dict], None] | None = None` kwarg, called at: after decode, after detect/segmentation (count), after each card's OCR+lookup completes, before return. Default None = zero behavior change.

- [ ] **Step 1:** Thread the callback through `scan_pack` at the four points above (fire-and-forget; wrap calls in try/except). In `scan_stream.py`, bridge to SSE via an `asyncio.Queue`: the endpoint starts `scan_pack` as a task with `progress=queue.put_nowait`, and an async generator yields queued events as `event: progress\ndata: {...}\n\n`, a `: hb\n\n` comment on 15s idle timeouts (`asyncio.wait_for`), then `event: result` with the response JSON. Return `StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})`.
- [ ] **Step 2:** Frontend: `scanPackStream(staircase, code, onProgress): Promise<PackScanResponse>` using `fetch` + `ReadableStream` reader (EventSource cannot POST); parse SSE frames minimally; App's `submit()` uses it when `"ReadableStream" in window` with automatic fallback to `scanPack` on any stream error; the submitting step renders stage text + `count` skeleton card rows once `cards_found` arrives.
- [ ] **Step 3: Smoke:**

```bash
pkill -f "uvicorn app.main"; sleep 1
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false
nohup .venv/bin/uvicorn app.main:app --port 8000 >/tmp/sse.log 2>&1 &
sleep 4
curl -sN -F "staircase=@tests/corpus/IMG_7102.heic" -F "code_card=@tests/corpus/IMG_7103.heic" http://127.0.0.1:8000/scan/pack/stream | head -40
pkill -f "uvicorn app.main"
```
Expected: `progress` events appear INCREMENTALLY (not in one burst at the end — that's the buffering failure mode), ending with `event: result`. Note in the commit message: verify with `curl -N` against Railway after the user deploys (spec requirement).
- [ ] **Step 4:** Suite green; browser check: upload flow shows stages + skeletons, result identical to before. Commit:

```bash
git add app/pack/scan_stream.py app/pack/pipeline.py app/main.py frontend/src/api.ts frontend/src/App.tsx
git commit -m "feat(scan): SSE progressive variant of /scan/pack + skeleton rows (fallback preserved)"
```

---

### Task 13: Acceptance gates + docs + cleanup

**Files:**
- Modify: `docs/training-runbook.md` (live-mode data note) or `docs/` deploy notes as found
- Modify: `.env.example` (OCR_THREADS, OCR_CONCURRENCY)

- [ ] **Step 1: Reel-fixture gate (spec acceptance #2).** Re-run Task 4's fixture script — record results. Gate: every SHARP fixture frame resolves `kind=card` with correct name identity; blurred frames route to `unreadable` (never a wrong confident identity).
- [ ] **Step 2: Full local E2E (spec acceptance #1 proxy).** One scripted pass: start app → login → live session → POST all reel fixtures as frames → finish → save with composite → verify pull row (`capture_path="live"`, frames moved into pull dir, card rows present) → confirm rederive skips it. Every command from earlier tasks; assemble into one shell block and run.
- [ ] **Step 3: Phone smoke (manual, with the user).** Vite needs HTTPS for phone getUserMedia: `cd frontend && npm run dev -- --host` + a tunnel/mkcert (document the exact incantation tried in the commit). Walk the user through a 10-card flip at reel pace; measure: capture rate (10/10 chips), ID rate without VLM (target ≥8/10), tray lag (≤ ~2 cards). This step BLOCKS on user availability — record results when it happens; do not fake them.
- [ ] **Step 4: env + docs.** `.env.example`: `OCR_THREADS=2`/`OCR_CONCURRENCY=3` with a Railway note ("set OCR_THREADS to the plan's vCPU count"). Runbook: live-mode frames land in pull dirs as `frame_NN.jpg` (future real-photo training data); live pulls excluded from harvest/rederive.
- [ ] **Step 5:** Suite green one last time; commit:

```bash
git add .env.example docs/
git commit -m "docs(live): acceptance results, env knobs, runbook notes"
```

---

### Task 14: VLM worker redeploy verification (Workstream 4 — blocked on user)

**Files:** none (ops verification; fix already committed as cd688f4)

The user rebuilds/pushes (`docker buildx build --platform linux/amd64 -t lee14k/pcs-vlm:v2 --push runpod_worker/`) and points the RunPod endpoint at `:v2`.

- [ ] **Step 1 (after user confirms redeploy):** Health probe — workers stay up:

```bash
curl -s -X POST "https://api.runpod.ai/v2/$ENDPOINT_ID/runsync" -H "Authorization: Bearer $RUNPOD_KEY" \
  -H "content-type: application/json" -d '{"input":{"cards":[]}}'
```
Expected: `{"output":{"cards":[]}, "status":"COMPLETED"}` within cold-start time (~1–3 min first call while the model downloads; seconds thereafter). If workers still die: fetch worker logs from the dashboard — the local-emulation test in this session proved imports pass, so remaining failure modes are GPU/disk sizing (needs 24GB VRAM + 40GB disk).
- [ ] **Step 2: End-to-end (spec acceptance #3).** With `VLM_ENDPOINT=https://api.runpod.ai/v2/$ENDPOINT_ID VLM_API_KEY=$RUNPOD_KEY` exported, run the Phase-2 smoke from `tests/vlm_stub.py`'s header comment against the REAL endpoint: scan the /086 corpus photo (`tests/corpus/C05EF4AE-8C93-487A-B272-448A8BF207ED.heic`) through `scan_pack` and confirm needs_review cards come back definitively identified (real per-card numbers this time, not stub echoes). Also fire one live-session frame with a needs_review card and watch the pending_vlm → ok transition via `GET /scan/live/{sid}`.
- [ ] **Step 3:** Record model/latency observations in `docs/` deploy notes; suggest the user set the same two env vars on Railway.

---

## Execution notes

- Task order is dependency-ordered; 1–3 are independent of each other (parallelizable), 4 needs 2+3, 5 needs 4, 6 needs 1+4+5, 7 needs 5–6, 8 needs 6–7, 9 is independent of 8, 10 needs 8+9, 11 needs 10, 12 is independent (any time after 1), 13 last (14 whenever the user redeploys).
- Machine care between every server smoke: `pkill -f "uvicorn app.main"; pkill -f vlm_stub`.
- If a smoke output differs from "Expected", STOP and investigate (systematic-debugging) — do not adjust the expectation to match.
