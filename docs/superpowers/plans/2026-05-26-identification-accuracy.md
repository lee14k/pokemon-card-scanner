# Identification Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Pokémon-card name extraction and set-symbol localization robust to real-world layouts (possessive trainer names, form prefixes, modifier suffixes, bottom-right SV symbols, framed photos with background visible).

**Architecture:** Insert a card-edge detection + rectification pass before OCR; extend the OCR module with a dex-validated multi-candidate name parser and bottom-right symbol crops; add an OCR'd set-code tiebreaker to the pHash matcher. All changes are additive — when any new component can't make a decision, the pipeline falls back to today's behavior, so the worst-case quality floor is unchanged.

**Tech Stack:** Python 3.11, FastAPI, Pillow, OpenCV (`opencv-python-headless`), pytesseract, pytest. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-05-20-identification-accuracy-design.md`

---

## File Structure

**Create:**
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_smoke.py`
- `tests/test_card_signals.py`
- `tests/test_name_parser.py`
- `tests/test_symbol_crop_boxes.py`
- `tests/test_set_code_resolution.py`
- `tests/test_card_detect.py`
- `tests/test_build_search_queries.py`
- `app/data/pokemon_names.txt` (populated by `scripts/fetch_pokemon_names.py`)
- `app/card_detect.py`
- `scripts/fetch_pokemon_names.py`
- `scripts/debug_set_symbol.py`

**Modify:**
- `requirements.txt` (add pytest)
- `app/card_signals.py` (add `name_candidates` field)
- `app/ocr_extract.py` (name parser, symbol crops, set-code OCR, card-detect integration)
- `app/set_symbol_index.py` (`set_code_to_set_id`, `resolve_set_with_tiebreaker`)
- `app/matching.py` (`build_search_queries` accepts `name_candidates`)
- `app/main.py` (pass `name_candidates` through)

---

## Task 1: Bootstrap pytest

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Add pytest to requirements.txt**

Append to `requirements.txt`:

```
pytest>=8.0.0
```

- [ ] **Step 2: Install the new dependency in the venv**

Run: `.venv/bin/pip install -r requirements.txt`
Expected: pytest installs, other requirements satisfied.

- [ ] **Step 3: Create `tests/__init__.py` (empty file)**

```python
```

- [ ] **Step 4: Create `tests/conftest.py` adding the repo root to `sys.path`**

```python
"""Pytest config: ensure `app` and `scripts` packages import without install."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 5: Create smoke test `tests/test_smoke.py`**

```python
def test_app_package_imports() -> None:
    import app  # noqa: F401
    import app.card_signals  # noqa: F401
```

- [ ] **Step 6: Run pytest to verify the harness works**

Run: `.venv/bin/pytest tests/test_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt tests/
git commit -m "test: bootstrap pytest harness with smoke test"
```

---

## Task 2: Add `name_candidates` field to `CardSignals`

**Files:**
- Modify: `app/card_signals.py`
- Create: `tests/test_card_signals.py`

- [ ] **Step 1: Write failing test in `tests/test_card_signals.py`**

```python
from app.card_signals import CardSignals


def test_card_signals_default_name_candidates_is_empty_list() -> None:
    sig = CardSignals()
    assert sig.name_candidates == []


def test_card_signals_accepts_name_candidates() -> None:
    sig = CardSignals(name_candidates=["Charizard", "Charizard VMAX"])
    assert sig.name_candidates == ["Charizard", "Charizard VMAX"]


def test_card_signals_empty_factory_still_works() -> None:
    sig = CardSignals.empty()
    assert sig.name_candidates == []
```

- [ ] **Step 2: Run test, confirm AttributeError failure**

Run: `.venv/bin/pytest tests/test_card_signals.py -v`
Expected: FAIL with `TypeError: ... got an unexpected keyword argument 'name_candidates'`.

- [ ] **Step 3: Add the field to `app/card_signals.py`**

In the `CardSignals` dataclass, insert this line after `primary_name_guess`:

```python
    name_candidates: list[str] = field(default_factory=list)
```

The block must look like:

```python
@dataclass
class CardSignals:
    """OCR + optional set-symbol match used for PokéWallet queries."""

    ocr_fragments: list[str] = field(default_factory=list)
    card_number: str | None = None
    primary_name_guess: str | None = None
    name_candidates: list[str] = field(default_factory=list)
    bottom_raw_ocr: str = ""
    symbol_raw_note: str = ""
    set_id_from_symbol: str | None = None
    set_code_from_symbol: str | None = None
    symbol_hash_distance: int | None = None
```

- [ ] **Step 4: Re-run test**

Run: `.venv/bin/pytest tests/test_card_signals.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/card_signals.py tests/test_card_signals.py
git commit -m "feat(signals): add name_candidates field to CardSignals"
```

---

## Task 3: Pokémon name corpus loader

**Files:**
- Modify: `app/ocr_extract.py`
- Create: `tests/test_name_parser.py` (this task seeds the file; later tasks extend it)

- [ ] **Step 1: Write failing test for `_load_pokemon_names` and `_is_known_pokemon`**

Create `tests/test_name_parser.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from app.ocr_extract import _is_known_pokemon, _load_pokemon_names


@pytest.fixture
def names_file(tmp_path: Path) -> Path:
    p = tmp_path / "pokemon_names.txt"
    p.write_text(
        "Bulbasaur\nCharizard\nMr. Mime\nFarfetch'd\nTentacool\nBellibolt\nZoroark\nRaichu\n",
        encoding="utf-8",
    )
    return p


def test_load_pokemon_names_reads_lines(names_file: Path) -> None:
    names = _load_pokemon_names(names_file)
    assert "charizard" in names
    assert "mr. mime" in names
    assert "farfetch'd" in names


def test_load_pokemon_names_missing_file_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    names = _load_pokemon_names(missing)
    assert names == frozenset()


def test_is_known_pokemon_case_insensitive(monkeypatch: pytest.MonkeyPatch, names_file: Path) -> None:
    import app.ocr_extract as oe
    monkeypatch.setattr(oe, "_POKEMON_NAMES", _load_pokemon_names(names_file))
    assert _is_known_pokemon("Charizard")
    assert _is_known_pokemon("charizard")
    assert _is_known_pokemon("CHARIZARD")
    assert not _is_known_pokemon("Pikalol")


def test_is_known_pokemon_handles_curly_apostrophe(
    monkeypatch: pytest.MonkeyPatch, names_file: Path
) -> None:
    import app.ocr_extract as oe
    monkeypatch.setattr(oe, "_POKEMON_NAMES", _load_pokemon_names(names_file))
    assert _is_known_pokemon("Farfetch\u2019d")
```

- [ ] **Step 2: Run test, confirm ImportError failure**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: FAIL with `ImportError: cannot import name '_is_known_pokemon' from 'app.ocr_extract'`.

- [ ] **Step 3: Add loader + lookup to `app/ocr_extract.py`**

Insert at module top, after the existing `log_symbol` line:

```python
_POKEMON_NAMES_PATH = Path(__file__).resolve().parent / "data" / "pokemon_names.txt"


def _normalize_name_token(s: str) -> str:
    """Lowercase + normalize curly apostrophes so dex lookup is robust to OCR variants."""
    return (
        s.strip()
        .lower()
        .replace("\u2019", "'")
        .replace("\u2032", "'")
    )


def _load_pokemon_names(path: Path | None = None) -> frozenset[str]:
    """Read newline-delimited Pokémon names. Missing file → empty set (graceful degrade)."""
    p = path or _POKEMON_NAMES_PATH
    if not p.is_file():
        log.warning("name_parser.pokemon_dict_missing path=%s", p)
        return frozenset()
    names: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        norm = _normalize_name_token(line)
        if norm:
            names.add(norm)
    return frozenset(names)


_POKEMON_NAMES: frozenset[str] = _load_pokemon_names()


def _is_known_pokemon(token: str) -> bool:
    return _normalize_name_token(token) in _POKEMON_NAMES
```

- [ ] **Step 4: Re-run test**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/ocr_extract.py tests/test_name_parser.py
git commit -m "feat(ocr): pokémon name corpus loader with apostrophe normalization"
```

---

## Task 4: Helper script that populates `app/data/pokemon_names.txt`

**Files:**
- Create: `scripts/fetch_pokemon_names.py`
- Create: `app/data/pokemon_names.txt` (output artifact, committed)

- [ ] **Step 1: Write the fetch script**

Create `scripts/fetch_pokemon_names.py`:

```python
#!/usr/bin/env python3
"""Fetch canonical English Pokémon species names from PokéAPI.

Writes app/data/pokemon_names.txt (one name per line, sorted, deduped).
Not run in production; humans run this when new gens release.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

SPECIES_LIST_URL = "https://pokeapi.co/api/v2/pokemon-species?limit=2000"
OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "pokemon_names.txt"


def _fetch_species_urls(client: httpx.Client) -> list[str]:
    r = client.get(SPECIES_LIST_URL)
    r.raise_for_status()
    return [row["url"] for row in r.json()["results"]]


def _english_name(species_json: dict) -> str:
    for entry in species_json.get("names", []):
        if entry.get("language", {}).get("name") == "en":
            name = (entry.get("name") or "").strip()
            if name:
                return name
    return (species_json.get("name") or "").strip().capitalize()


def main() -> None:
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        urls = _fetch_species_urls(client)
        print(f"Fetching {len(urls)} species…", file=sys.stderr)
        names: set[str] = set()
        for i, url in enumerate(urls, 1):
            r = client.get(url)
            r.raise_for_status()
            name = _english_name(r.json())
            if name:
                names.add(name)
            if i % 50 == 0:
                print(f"  {i}/{len(urls)}…", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    print(f"Wrote {len(names)} names to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

Run: `.venv/bin/python scripts/fetch_pokemon_names.py`
Expected: prints progress, terminal message like `Wrote 1025 names to .../app/data/pokemon_names.txt`. Takes ~2 minutes due to per-species fetches.

- [ ] **Step 3: Spot-check the output**

Run: `wc -l app/data/pokemon_names.txt && head -3 app/data/pokemon_names.txt && grep -E "^(Charizard|Tentacool|Bellibolt|Mr\. Mime|Farfetch)" app/data/pokemon_names.txt`
Expected: line count ≥ 1000, alphabetic ordering, all four greps return a match.

- [ ] **Step 4: Re-run the full test suite (loader now uses the real file)**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green (smoke + card_signals + name_parser).

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_pokemon_names.py app/data/pokemon_names.txt
git commit -m "data: ship canonical Pokémon names from PokéAPI + fetch helper"
```

---

## Task 5: Possessive-prefix stripping

**Files:**
- Modify: `app/ocr_extract.py`
- Modify: `tests/test_name_parser.py`

- [ ] **Step 1: Append failing tests to `tests/test_name_parser.py`**

```python
from app.ocr_extract import _strip_possessive_prefix


def test_strip_possessive_ascii_apostrophe() -> None:
    trainer, rest = _strip_possessive_prefix("Misty's Tentacool")
    assert trainer == "Misty"
    assert rest == "Tentacool"


def test_strip_possessive_curly_apostrophe() -> None:
    trainer, rest = _strip_possessive_prefix("Iono\u2019s Bellibolt")
    assert trainer == "Iono"
    assert rest == "Bellibolt"


def test_strip_possessive_prime_apostrophe() -> None:
    trainer, rest = _strip_possessive_prefix("N\u2032s Zoroark")
    assert trainer == "N"
    assert rest == "Zoroark"


def test_strip_possessive_no_apostrophe_returns_none_and_original() -> None:
    trainer, rest = _strip_possessive_prefix("Alolan Raichu")
    assert trainer is None
    assert rest == "Alolan Raichu"


def test_strip_possessive_empty_string() -> None:
    trainer, rest = _strip_possessive_prefix("")
    assert trainer is None
    assert rest == ""


def test_strip_possessive_only_trainer_no_pokemon() -> None:
    trainer, rest = _strip_possessive_prefix("Misty's")
    assert trainer == "Misty"
    assert rest == ""
```

- [ ] **Step 2: Run, confirm ImportError failure**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: FAIL — `_strip_possessive_prefix` not importable.

- [ ] **Step 3: Implement `_strip_possessive_prefix` in `app/ocr_extract.py`**

Insert after `_is_known_pokemon` (and the apostrophe variants regex used here):

```python
_POSSESSIVE_RE = re.compile(
    r"^\s*([A-Z][A-Za-z\-]{0,18})['\u2019\u2032]s\s*(.*)$"
)


def _strip_possessive_prefix(line: str) -> tuple[str | None, str]:
    """('Misty', 'Tentacool') for 'Misty's Tentacool'; (None, line) otherwise.

    The apostrophe may be ASCII ('), curly (U+2019), or prime (U+2032).
    """
    m = _POSSESSIVE_RE.match(line or "")
    if not m:
        return None, line
    trainer = m.group(1).strip()
    rest = m.group(2).strip()
    return trainer, rest
```

- [ ] **Step 4: Re-run test**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: all previous tests + the 6 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/ocr_extract.py tests/test_name_parser.py
git commit -m "feat(ocr): possessive-prefix stripping for trainer's-pokemon names"
```

---

## Task 6: Multi-candidate name collection

**Files:**
- Modify: `app/ocr_extract.py`
- Modify: `tests/test_name_parser.py`

- [ ] **Step 1: Append failing tests to `tests/test_name_parser.py`**

```python
from app.ocr_extract import collect_name_candidates


def test_collect_candidates_simple_pokemon() -> None:
    cands = collect_name_candidates("Charizard\nHP 120\nStage 2")
    assert "Charizard" in cands


def test_collect_candidates_form_prefix_kept_and_bare() -> None:
    cands = collect_name_candidates("Alolan Raichu\nHP 90")
    assert "Alolan Raichu" in cands
    assert "Raichu" in cands


def test_collect_candidates_possessive_trainer_emits_full_and_bare() -> None:
    cands = collect_name_candidates("Misty's Tentacool\nBasic Pokemon")
    assert "Misty's Tentacool" in cands
    assert "Tentacool" in cands


def test_collect_candidates_modifier_suffix() -> None:
    cands = collect_name_candidates("Charizard VMAX\n100 HP")
    assert "Charizard VMAX" in cands
    assert "Charizard" in cands


def test_collect_candidates_curly_apostrophe_iono() -> None:
    cands = collect_name_candidates("Iono\u2019s Bellibolt ex")
    assert any(c.endswith("Bellibolt ex") and c.startswith("Iono") for c in cands)
    assert "Bellibolt" in cands


def test_collect_candidates_junk_filtered() -> None:
    cands = collect_name_candidates("GAME FREAK\n\u00a9 1999 Nintendo\nIllus. Mitsuhiro Arita")
    assert cands == []


def test_collect_candidates_unknown_token_falls_back_to_legacy() -> None:
    # 'Zacrid' isn't in the corpus; legacy path still emits a capitalized token.
    cands = collect_name_candidates("Zacrid\nHP 80")
    assert any("Zacrid" in c for c in cands)


def test_collect_candidates_caps_to_max() -> None:
    raw = "Charizard\nPikachu\nRaichu\nTentacool\nZoroark"
    cands = collect_name_candidates(raw, max_candidates=3)
    assert len(cands) <= 3
```

- [ ] **Step 2: Run, confirm failure**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: FAIL on `collect_name_candidates` import.

- [ ] **Step 3: Implement `collect_name_candidates` in `app/ocr_extract.py`**

Insert after `_strip_possessive_prefix`:

```python
_FORM_PREFIXES = frozenset(
    {
        "alolan", "galarian", "hisuian", "paldean",
        "dark", "light", "shining", "crystal", "shadow", "radiant",
        "ancient", "future",
    }
)

_NAME_SUFFIXES = frozenset(
    {
        "v", "vmax", "vstar", "gx", "ex", "break", "lv.x",
        "\u03b4",
    }
)


def _tokenize_name_line(line: str) -> list[str]:
    """Return alphanumeric tokens, preserving original casing."""
    return re.findall(r"[A-Za-z\u03b4][A-Za-z\u03b4\-]*", line)


def _assemble_canonical_name(tokens: list[str], pokemon_idx: int) -> str:
    """Concatenate form-prefix tokens just before the anchor and modifier tokens after."""
    out: list[str] = []
    if pokemon_idx > 0:
        prev = tokens[pokemon_idx - 1]
        if prev.lower() in _FORM_PREFIXES:
            out.append(prev)
    out.append(tokens[pokemon_idx])
    j = pokemon_idx + 1
    while j < len(tokens) and tokens[j].lower() in _NAME_SUFFIXES:
        out.append(tokens[j])
        j += 1
    return " ".join(out)


def collect_name_candidates(raw_top: str, max_candidates: int = 4) -> list[str]:
    """Return de-duplicated candidate printed names from the top band OCR text.

    Strategy: per-line, strip possessive, find first known-Pokémon token, then
    emit the canonical printed name (with form prefix + modifier suffix) AND
    the bare Pokémon name. When no known Pokémon appears, fall back to the
    legacy capitalized-token heuristic so previously-working names still work.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def push(s: str) -> None:
        key = s.strip().lower()
        if key and key not in seen and len(key) >= 3:
            seen.add(key)
            candidates.append(s.strip())

    for line in _lines_from_raw_top_to_bottom(raw_top or ""):
        if len(candidates) >= max_candidates:
            break
        if _is_junk_name_line(line):
            continue
        trainer, rest = _strip_possessive_prefix(line)
        tokens = _tokenize_name_line(rest)
        if not tokens:
            continue

        pokemon_idx = next(
            (i for i, t in enumerate(tokens) if _is_known_pokemon(t)),
            -1,
        )
        if pokemon_idx >= 0:
            canonical = _assemble_canonical_name(tokens, pokemon_idx)
            if trainer:
                push(f"{trainer}'s {canonical}")
            push(canonical)
            push(tokens[pokemon_idx])
            continue

        legacy_title = _extract_title_style_name(line)
        if legacy_title:
            push(legacy_title)
            continue
        legacy_tok = _extract_capitalized_name_token(line)
        if legacy_tok:
            push(legacy_tok)

    return candidates[:max_candidates]
```

- [ ] **Step 4: Re-run all name-parser tests**

Run: `.venv/bin/pytest tests/test_name_parser.py -v`
Expected: all passing (previous tests + 8 new tests).

- [ ] **Step 5: Commit**

```bash
git add app/ocr_extract.py tests/test_name_parser.py
git commit -m "feat(ocr): multi-candidate name parser with form prefixes and modifier suffixes"
```

---

## Task 7: Populate `CardSignals.name_candidates` from `extract_card_signals`

**Files:**
- Modify: `app/ocr_extract.py`

- [ ] **Step 1: Modify `extract_card_signals` to call `collect_name_candidates`**

In `app/ocr_extract.py`, find the line:

```python
    primary_name = pick_primary_name_from_top_band(raw_top)
```

Replace with:

```python
    name_candidates = collect_name_candidates(raw_top)
    primary_name = name_candidates[0] if name_candidates else pick_primary_name_from_top_band(raw_top)
```

Then find the `return CardSignals(...)` block at the bottom of the function and add `name_candidates=name_candidates,` as a new field:

```python
    return CardSignals(
        ocr_fragments=out,
        card_number=card_number,
        primary_name_guess=primary_name,
        name_candidates=name_candidates,
        bottom_raw_ocr=raw_bottom,
        symbol_raw_note=sym_note,
        set_id_from_symbol=set_id,
        set_code_from_symbol=set_code,
        symbol_hash_distance=sym_dist,
    )
```

- [ ] **Step 2: Run smoke + signals tests**

Run: `.venv/bin/pytest tests/test_smoke.py tests/test_card_signals.py -v`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add app/ocr_extract.py
git commit -m "feat(ocr): populate CardSignals.name_candidates from extract pipeline"
```

---

## Task 8: Wire `name_candidates` into `build_search_queries`

**Files:**
- Modify: `app/matching.py`
- Modify: `app/main.py`
- Create: `tests/test_build_search_queries.py`

- [ ] **Step 1: Write failing test in `tests/test_build_search_queries.py`**

```python
from app.matching import build_search_queries


def test_build_queries_uses_name_candidates_when_provided() -> None:
    qs = build_search_queries(
        card_name_hint=None,
        ocr_fragments=[],
        card_number="148/198",
        set_id_from_symbol=None,
        set_code_from_symbol=None,
        primary_name_guess="Tentacool",
        name_candidates=["Misty's Tentacool", "Tentacool"],
        max_queries=8,
    )
    joined = "\n".join(qs)
    assert "Misty's Tentacool 148/198" in joined
    assert "Tentacool 148/198" in joined


def test_build_queries_backward_compatible_without_candidates() -> None:
    qs = build_search_queries(
        card_name_hint=None,
        ocr_fragments=[],
        card_number="4/102",
        set_id_from_symbol="1",
        set_code_from_symbol="BS",
        primary_name_guess="Charizard",
        max_queries=8,
    )
    assert any("Charizard" in q for q in qs)
    assert any(q.startswith("1 4") for q in qs)


def test_build_queries_deduplicates_candidates() -> None:
    qs = build_search_queries(
        card_name_hint=None,
        ocr_fragments=[],
        card_number="25/102",
        primary_name_guess="Pikachu",
        name_candidates=["Pikachu", "pikachu", "Pikachu"],
        max_queries=8,
    )
    pika_qs = [q for q in qs if q.lower().startswith("pikachu")]
    assert len(pika_qs) == len({q.lower() for q in pika_qs})
```

- [ ] **Step 2: Run, confirm failure**

Run: `.venv/bin/pytest tests/test_build_search_queries.py -v`
Expected: FAIL with `unexpected keyword argument 'name_candidates'`.

- [ ] **Step 3: Update `build_search_queries` signature and body in `app/matching.py`**

Change the signature to:

```python
def build_search_queries(
    *,
    card_name_hint: str | None,
    ocr_fragments: list[str],
    card_number: str | None = None,
    set_id_from_symbol: str | None = None,
    set_code_from_symbol: str | None = None,
    primary_name_guess: str | None = None,
    name_candidates: list[str] | None = None,
    max_queries: int = 8,
) -> list[str]:
```

Within the function, after the existing `name` is defined, insert candidate expansion just before the existing "5. Name + numerator only" block. Specifically, after:

```python
    # 4. Name + collection number
    if name and card_number:
        add(f"{name} {card_number}")
```

Insert:

```python
    # 4b. Additional name candidates × collection number (e.g. "Misty's Tentacool 148/198")
    for cand in (name_candidates or []):
        cand = (cand or "").strip()
        if not cand or cand.lower() == name.lower():
            continue
        if card_number:
            add(f"{cand} {card_number}")
        if num_first:
            add(f"{cand} {num_first}")
        add(cand)
```

- [ ] **Step 4: Update `app/main.py` to pass `name_candidates` through**

Both `analyze_image` and `price_from_image` call `build_search_queries`. In each, add `name_candidates=signals.name_candidates,` to the kwargs. The two updated calls look like:

```python
    search_queries = build_search_queries(
        card_name_hint=card_name_hint,
        ocr_fragments=signals.ocr_fragments,
        card_number=signals.card_number,
        set_id_from_symbol=signals.set_id_from_symbol,
        set_code_from_symbol=signals.set_code_from_symbol,
        primary_name_guess=signals.primary_name_guess,
        name_candidates=signals.name_candidates,
        max_queries=8,
    )
```

(applies to the call in `analyze_image`, ~line 136), and:

```python
    search_queries = build_search_queries(
        card_name_hint=card_name_hint,
        ocr_fragments=ocr_fragments,
        card_number=signals.card_number,
        set_id_from_symbol=signals.set_id_from_symbol,
        set_code_from_symbol=signals.set_code_from_symbol,
        primary_name_guess=signals.primary_name_guess,
        name_candidates=signals.name_candidates,
        max_queries=8,
    )
```

(applies to the call in `price_from_image`, ~line 229).

- [ ] **Step 5: Run new tests + smoke**

Run: `.venv/bin/pytest tests/ -v`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add app/matching.py app/main.py tests/test_build_search_queries.py
git commit -m "feat(matching): route name_candidates through search-query builder"
```

---

## Task 9: Bottom-right symbol crop boxes

**Files:**
- Modify: `app/ocr_extract.py`
- Create: `tests/test_symbol_crop_boxes.py`

- [ ] **Step 1: Write failing test in `tests/test_symbol_crop_boxes.py`**

```python
from app.ocr_extract import _symbol_crop_boxes


def test_crop_boxes_include_bottom_left_and_bottom_right() -> None:
    w, h = 750, 1050
    boxes = _symbol_crop_boxes(w, h)
    assert boxes, "no crop boxes returned"

    has_bl = any(x0 == 0 and x1 < w // 2 for (x0, _y0, x1, _y1) in boxes)
    has_br = any(x0 > w // 2 and x1 == w for (x0, _y0, x1, _y1) in boxes)
    assert has_bl, "missing bottom-left crops"
    assert has_br, "missing bottom-right crops"


def test_crop_boxes_have_valid_geometry() -> None:
    w, h = 750, 1050
    for box in _symbol_crop_boxes(w, h):
        x0, y0, x1, y1 = box
        assert 0 <= x0 < x1 <= w
        assert 0 <= y0 < y1 <= h
        assert y0 >= int(h * 0.7), f"box not in bottom region: {box}"


def test_crop_boxes_handle_tiny_image() -> None:
    boxes = _symbol_crop_boxes(60, 80)
    assert boxes
    for x0, y0, x1, y1 in boxes:
        assert x1 > x0 and y1 > y0
```

- [ ] **Step 2: Run, confirm failure on the BR assertion**

Run: `.venv/bin/pytest tests/test_symbol_crop_boxes.py -v`
Expected: FAIL on `test_crop_boxes_include_bottom_left_and_bottom_right` (only BL boxes today).

- [ ] **Step 3: Replace `_symbol_crop_boxes` in `app/ocr_extract.py`**

Replace the existing function body with:

```python
def _symbol_crop_boxes(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """
    Expansion symbol sits in the bottom info strip. Older layouts put it bottom-LEFT
    (left of card number); modern Scarlet & Violet layouts put it bottom-RIGHT
    (right of card number). Emit both regions so multi-crop matching covers both.
    """
    boxes: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()

    def add(box: tuple[int, int, int, int]) -> None:
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        if box not in seen:
            seen.add(box)
            boxes.append(box)

    for y_frac in (0.82, 0.86, 0.88, 0.90, 0.92, 0.94):
        y0 = int(h * y_frac)
        if y0 >= h - 12:
            continue
        for x_frac in (0.11, 0.14, 0.18):
            strip_w = max(int(w * x_frac), 24)
            add((0, y0, strip_w, h))
            add((max(0, w - strip_w), y0, w, h))

    add((0, int(h * 0.76), max(int(w * 0.26), 48), h))
    add((0, int(h * 0.78), max(int(w * 0.22), 40), h))
    add((0, int(h * 0.80), max(int(w * 0.18), 36), h))

    add((w - max(int(w * 0.26), 48), int(h * 0.76), w, h))
    add((w - max(int(w * 0.22), 40), int(h * 0.78), w, h))
    add((w - max(int(w * 0.18), 36), int(h * 0.80), w, h))

    return boxes
```

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_symbol_crop_boxes.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/ocr_extract.py tests/test_symbol_crop_boxes.py
git commit -m "feat(symbol): emit bottom-right crops in addition to bottom-left"
```

---

## Task 10: OCR set-code extraction from bottom strip

**Files:**
- Modify: `app/ocr_extract.py`
- Create: `tests/test_set_code_resolution.py` (seeded here, extended in Tasks 11–12)

- [ ] **Step 1: Write failing test in `tests/test_set_code_resolution.py`**

```python
from app.ocr_extract import _candidate_set_codes


def test_candidate_set_codes_extracts_three_letter() -> None:
    raw = "SVI 132/200\nIllus. Some Artist"
    codes = _candidate_set_codes(raw)
    assert "SVI" in codes


def test_candidate_set_codes_extracts_alphanumeric() -> None:
    raw = "SV4PT5 95/200"
    codes = _candidate_set_codes(raw)
    assert "SV4PT5" in codes


def test_candidate_set_codes_drops_short_noise() -> None:
    raw = "HP 90\nGX OF\n12/108"
    codes = _candidate_set_codes(raw)
    assert "HP" not in codes
    assert "OF" not in codes


def test_candidate_set_codes_empty_string() -> None:
    assert _candidate_set_codes("") == []
    assert _candidate_set_codes(None) == []  # type: ignore[arg-type]
```

- [ ] **Step 2: Run, confirm import failure**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: FAIL — `_candidate_set_codes` not defined.

- [ ] **Step 3: Implement `_candidate_set_codes` in `app/ocr_extract.py`**

Insert after `_symbol_crop_boxes`:

```python
_SET_CODE_RE = re.compile(r"\b([A-Z]{3,6}(?:\d{1,2}(?:PT\d)?)?)\b")


def _candidate_set_codes(raw_bottom: str | None) -> list[str]:
    """Pull short ASCII tokens like 'SVI', 'PAL', 'MEW', 'SV4PT5' from bottom strip OCR.

    Two-letter tokens are dropped — too noisy ('HP', 'OF', etc.) to be useful even
    as a tiebreaker. Resolver downstream filters further by mapping to known set_ids.
    """
    if not raw_bottom:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _SET_CODE_RE.finditer(raw_bottom):
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out
```

- [ ] **Step 4: Re-run test**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/ocr_extract.py tests/test_set_code_resolution.py
git commit -m "feat(ocr): extract candidate set codes from bottom-strip OCR"
```

---

## Task 11: `set_code_to_set_id` map in `set_symbol_index.py`

**Files:**
- Modify: `app/set_symbol_index.py`
- Modify: `tests/test_set_code_resolution.py`

- [ ] **Step 1: Append failing tests to `tests/test_set_code_resolution.py`**

```python
from unittest.mock import patch

from app.set_symbol_index import SymbolRef, set_code_to_set_id


def _fake_refs() -> list[SymbolRef]:
    from pathlib import Path
    return [
        SymbolRef(set_id="24541", set_code="ASC", hash_int=0, path=Path("/tmp/a.png")),
        SymbolRef(set_id="24448", set_code="PFL", hash_int=0, path=Path("/tmp/b.png")),
        SymbolRef(set_id="9999", set_code=None, hash_int=0, path=Path("/tmp/c.png")),
    ]


def test_set_code_to_set_id_maps_uppercase_codes() -> None:
    with patch("app.set_symbol_index.load_symbol_index", return_value=_fake_refs()):
        set_code_to_set_id.cache_clear()
        mapping = set_code_to_set_id()
    assert mapping["ASC"] == "24541"
    assert mapping["PFL"] == "24448"


def test_set_code_to_set_id_skips_none_codes() -> None:
    with patch("app.set_symbol_index.load_symbol_index", return_value=_fake_refs()):
        set_code_to_set_id.cache_clear()
        mapping = set_code_to_set_id()
    assert all(v != "9999" for v in mapping.values())


def test_set_code_to_set_id_is_case_insensitive_lookup() -> None:
    with patch("app.set_symbol_index.load_symbol_index", return_value=_fake_refs()):
        set_code_to_set_id.cache_clear()
        mapping = set_code_to_set_id()
    assert mapping.get("asc".upper()) == "24541"
```

- [ ] **Step 2: Run, confirm import failure**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: FAIL — `set_code_to_set_id` not importable.

- [ ] **Step 3: Implement `set_code_to_set_id` in `app/set_symbol_index.py`**

Add this import near the top:

```python
from functools import lru_cache
```

Insert the new function near the bottom of `app/set_symbol_index.py`, before `match_set_symbol`:

```python
@lru_cache(maxsize=1)
def set_code_to_set_id() -> dict[str, str]:
    """Build {SET_CODE: set_id} from the loaded reference index. Used as a pHash tiebreaker."""
    out: dict[str, str] = {}
    for ref in load_symbol_index():
        if not ref.set_code:
            continue
        out[ref.set_code.strip().upper()] = ref.set_id
    return out
```

Also update `reload_symbol_index` to clear the new cache:

```python
def reload_symbol_index() -> None:
    global _index
    _index = None
    set_code_to_set_id.cache_clear()
    load_symbol_index()
```

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/set_symbol_index.py tests/test_set_code_resolution.py
git commit -m "feat(symbol): set_code → set_id lookup derived from reference index"
```

---

## Task 12: `resolve_set_with_tiebreaker`

**Files:**
- Modify: `app/set_symbol_index.py`
- Modify: `tests/test_set_code_resolution.py`

- [ ] **Step 1: Append failing tests to `tests/test_set_code_resolution.py`**

```python
from app.set_symbol_index import resolve_set_with_tiebreaker


def _ref(set_id: str, set_code: str | None = None) -> SymbolRef:
    from pathlib import Path
    return SymbolRef(set_id=set_id, set_code=set_code, hash_int=0, path=Path(f"/tmp/{set_id}.png"))


def test_tiebreaker_keeps_phash_when_margin_healthy() -> None:
    refs = [_ref("100", "AAA"), _ref("200", "BBB")]
    with patch("app.set_symbol_index.load_symbol_index", return_value=refs):
        set_code_to_set_id.cache_clear()
        out = resolve_set_with_tiebreaker(
            phash_hit=(refs[0], 10, 22),
            ocr_set_codes=["BBB"],
            min_margin=2,
        )
    assert out is refs[0]


def test_tiebreaker_swaps_when_margin_tight_and_ocr_matches_runner_up() -> None:
    refs = [_ref("100", "AAA"), _ref("200", "BBB")]
    with patch("app.set_symbol_index.load_symbol_index", return_value=refs):
        set_code_to_set_id.cache_clear()
        out = resolve_set_with_tiebreaker(
            phash_hit=(refs[0], 18, 19),
            ocr_set_codes=["BBB"],
            min_margin=2,
        )
    assert out.set_id == "200"


def test_tiebreaker_ignores_unknown_codes() -> None:
    refs = [_ref("100", "AAA")]
    with patch("app.set_symbol_index.load_symbol_index", return_value=refs):
        set_code_to_set_id.cache_clear()
        out = resolve_set_with_tiebreaker(
            phash_hit=(refs[0], 18, 19),
            ocr_set_codes=["ZZZ"],
            min_margin=2,
        )
    assert out is refs[0]


def test_tiebreaker_no_swap_when_phash_already_matches_ocr() -> None:
    refs = [_ref("100", "AAA")]
    with patch("app.set_symbol_index.load_symbol_index", return_value=refs):
        set_code_to_set_id.cache_clear()
        out = resolve_set_with_tiebreaker(
            phash_hit=(refs[0], 18, 19),
            ocr_set_codes=["AAA"],
            min_margin=2,
        )
    assert out is refs[0]
```

- [ ] **Step 2: Run, confirm failure**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: FAIL — `resolve_set_with_tiebreaker` not defined.

- [ ] **Step 3: Implement `resolve_set_with_tiebreaker` in `app/set_symbol_index.py`**

Insert near `set_code_to_set_id`:

```python
def resolve_set_with_tiebreaker(
    phash_hit: tuple[SymbolRef, int, int | None],
    ocr_set_codes: list[str],
    *,
    min_margin: int | None = None,
) -> SymbolRef:
    """Prefer a different reference when pHash margin is ambiguous AND an OCR'd set
    code maps to a known set_id different from pHash's best.

    `phash_hit` is `(best_ref, best_distance, second_best_distance_or_None)`.
    Returns `best_ref` unchanged unless tiebreaker triggers.
    """
    best_ref, best_dist, second_d = phash_hit
    if not ocr_set_codes:
        return best_ref
    if min_margin is None:
        min_margin = _env_int("SET_SYMBOL_MIN_MARGIN", _DEFAULT_MIN_MARGIN)
    margin = (second_d - best_dist) if second_d is not None else None
    if margin is None or margin >= min_margin:
        return best_ref

    mapping = set_code_to_set_id()
    refs_by_id = {ref.set_id: ref for ref in load_symbol_index()}
    for code in ocr_set_codes:
        target_id = mapping.get(code.strip().upper())
        if not target_id or target_id == best_ref.set_id:
            continue
        replacement = refs_by_id.get(target_id)
        if replacement is None:
            continue
        log.info(
            "set_symbol.tiebreaker_swap from=%s to=%s via_code=%s margin=%s (<%s)",
            best_ref.set_id,
            replacement.set_id,
            code,
            margin,
            min_margin,
        )
        return replacement
    return best_ref
```

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_set_code_resolution.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add app/set_symbol_index.py tests/test_set_code_resolution.py
git commit -m "feat(symbol): OCR set-code tiebreaker for ambiguous pHash matches"
```

---

## Task 13: Integrate set-code tiebreaker into `extract_card_signals`

**Files:**
- Modify: `app/ocr_extract.py`

- [ ] **Step 1: Modify `extract_card_signals` to pass OCR codes through the tiebreaker**

First, update the import line that already pulls in `match_set_symbol_best_of_crops`:

```python
from app.set_symbol_index import (
    best_set_symbol_match,
    match_set_symbol_best_of_crops,
    resolve_set_with_tiebreaker,
)
```

Then change the `match_set_symbol_best_of_crops` invocation block. Find:

```python
    matched = match_set_symbol_best_of_crops(variants, boxes=sym_boxes)
    if matched:
        ref, dist, box = matched
        set_id = ref.set_id
        set_code = ref.set_code
        sym_dist = dist
        sym_note = (
            f"crop_box={box} px={box[2] - box[0]}x{box[3] - box[1]} "
            f"matched_set_id={set_id} dist={dist}"
        )
```

Replace with:

```python
    ocr_set_codes = _candidate_set_codes(raw_bottom)
    matched = match_set_symbol_best_of_crops(variants, boxes=sym_boxes)
    if matched:
        ref, dist, box = matched
        if ocr_set_codes and not os.environ.get("SYMBOL_OCR_TIEBREAKER_DISABLE"):
            phash_hit = best_set_symbol_match(variants[0].crop(box))
            if phash_hit is not None:
                resolved = resolve_set_with_tiebreaker(phash_hit, ocr_set_codes)
                if resolved.set_id != ref.set_id:
                    ref = resolved
        set_id = ref.set_id
        set_code = ref.set_code
        sym_dist = dist
        sym_note = (
            f"crop_box={box} px={box[2] - box[0]}x{box[3] - box[1]} "
            f"matched_set_id={set_id} dist={dist} ocr_set_codes={ocr_set_codes}"
        )
```

Note: this requires `import os` at the top of the file — confirm it's already there (it is, used by `_configure_tesseract_cmd`).

- [ ] **Step 2: Run full suite to confirm nothing regressed**

Run: `.venv/bin/pytest tests/ -v`
Expected: all passing.

- [ ] **Step 3: Commit**

```bash
git add app/ocr_extract.py
git commit -m "feat(ocr): apply OCR set-code tiebreaker when pHash margin is tight"
```

---

## Task 14: `app/card_detect.py` (detection + perspective rectification)

**Files:**
- Create: `app/card_detect.py`
- Create: `tests/test_card_detect.py`

- [ ] **Step 1: Write failing test in `tests/test_card_detect.py`**

```python
from __future__ import annotations

import numpy as np
from PIL import Image

from app.card_detect import detect_and_rectify


def _synthetic_card_on_background(card_w: int = 350, card_h: int = 490) -> Image.Image:
    bg_w, bg_h = card_w + 200, card_h + 200
    bg = np.full((bg_h, bg_w, 3), 30, dtype=np.uint8)
    x0 = (bg_w - card_w) // 2
    y0 = (bg_h - card_h) // 2
    bg[y0 : y0 + card_h, x0 : x0 + card_w] = 240
    return Image.fromarray(bg, mode="RGB")


def test_detect_rectifies_clean_card() -> None:
    img = _synthetic_card_on_background()
    out, detected = detect_and_rectify(img)
    assert detected is True
    w, h = out.size
    aspect = w / h
    assert 0.65 <= aspect <= 0.75, f"aspect {aspect} not portrait card"


def test_detect_falls_back_on_pure_noise() -> None:
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, size=(400, 300, 3), dtype=np.uint8)
    img = Image.fromarray(noise, mode="RGB")
    out, detected = detect_and_rectify(img)
    assert detected is False
    assert out.size == img.size


def test_detect_falls_back_on_tiny_image() -> None:
    img = Image.new("RGB", (40, 40), (255, 255, 255))
    out, detected = detect_and_rectify(img)
    assert detected is False
    assert out.size == (40, 40)
```

- [ ] **Step 2: Run, confirm ImportError failure**

Run: `.venv/bin/pytest tests/test_card_detect.py -v`
Expected: FAIL — `app.card_detect` module does not exist.

- [ ] **Step 3: Create `app/card_detect.py`**

```python
"""Detect the card quadrilateral in a photo and perspective-correct it.

If detection fails (busy background, low contrast, fewer than 4 corners),
fall back to the input image unchanged. Downstream OCR / symbol matching
then behaves exactly as before this module was added.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger("pokemon_scanner.card_detect")

_CANONICAL_W = 750
_CANONICAL_H = 1050
_WORK_LONG_SIDE = 1200
_MIN_INPUT_SIDE = 200
_MIN_AREA_FRAC = 0.25


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Return 4 points ordered TL, TR, BR, BL."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    return np.array(
        [
            pts[np.argmin(s)],      # TL
            pts[np.argmin(diff)],   # TR
            pts[np.argmax(s)],      # BR
            pts[np.argmax(diff)],   # BL
        ],
        dtype=np.float32,
    )


def _scale_for_detection(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = rgb.shape[:2]
    long_side = max(h, w)
    if long_side <= _WORK_LONG_SIDE:
        return rgb, 1.0
    scale = _WORK_LONG_SIDE / long_side
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    return cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA), scale


def detect_and_rectify(image: Image.Image) -> tuple[Image.Image, bool]:
    """Return (rectified_card_image, True) or (input_image, False) on fallback."""
    if image is None:
        return image, False
    w0, h0 = image.size
    if min(w0, h0) < _MIN_INPUT_SIDE:
        log.info("card_detect.fallback reason=too_small size=%sx%s", w0, h0)
        return image, False

    rgb = np.asarray(image.convert("RGB"))
    work, scale = _scale_for_detection(rgb)
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        log.info("card_detect.fallback reason=no_contours")
        return image, False

    work_area = work.shape[0] * work.shape[1]
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:6]
    quad: np.ndarray | None = None
    for c in contours:
        area = cv2.contourArea(c)
        if area < work_area * _MIN_AREA_FRAC:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        quad = approx
        break

    if quad is None:
        log.info("card_detect.fallback reason=no_quadrilateral")
        return image, False

    corners = _order_corners(quad) / scale
    dst = np.array(
        [
            [0, 0],
            [_CANONICAL_W - 1, 0],
            [_CANONICAL_W - 1, _CANONICAL_H - 1],
            [0, _CANONICAL_H - 1],
        ],
        dtype=np.float32,
    )

    side_a = float(np.linalg.norm(corners[0] - corners[1]))
    side_b = float(np.linalg.norm(corners[1] - corners[2]))
    if side_a > side_b:
        corners = np.roll(corners, -1, axis=0)

    M = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    warped = cv2.warpPerspective(
        rgb, M, (_CANONICAL_W, _CANONICAL_H), flags=cv2.INTER_LINEAR
    )
    log.info("card_detect.ok input=%sx%s output=%sx%s", w0, h0, _CANONICAL_W, _CANONICAL_H)
    return Image.fromarray(warped, mode="RGB"), True
```

- [ ] **Step 4: Re-run tests**

Run: `.venv/bin/pytest tests/test_card_detect.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/card_detect.py tests/test_card_detect.py
git commit -m "feat(card-detect): perspective-rectify cards with fail-soft fallback"
```

---

## Task 15: Wire `detect_and_rectify` into `extract_card_signals`

**Files:**
- Modify: `app/ocr_extract.py`

- [ ] **Step 1: Import and call the new module**

Near the existing imports in `app/ocr_extract.py`, add:

```python
from app.card_detect import detect_and_rectify
```

Then update the head of `extract_card_signals`. Find:

```python
    _check_tesseract()
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    gray = _scale_for_ocr(ImageOps.grayscale(img))
```

Replace with:

```python
    _check_tesseract()
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if not os.environ.get("CARD_DETECT_DISABLE"):
        img, _detected = detect_and_rectify(img)
    gray = _scale_for_ocr(ImageOps.grayscale(img))
```

- [ ] **Step 2: Run full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green. The smoke test exercises imports — confirms no circular-import regressions.

- [ ] **Step 3: Commit**

```bash
git add app/ocr_extract.py
git commit -m "feat(ocr): rectify card quadrilateral before OCR + symbol matching"
```

---

## Task 16: Confirm opt-out env vars + add explicit regression tests

**Files:**
- Modify: `tests/test_card_detect.py`
- Modify: `tests/test_set_code_resolution.py`

The env vars `CARD_DETECT_DISABLE` and `SYMBOL_OCR_TIEBREAKER_DISABLE` were wired in Tasks 13 and 15. This task locks them with regression tests so a future refactor can't silently remove them.

- [ ] **Step 1: Append env-var test to `tests/test_card_detect.py`**

```python
import io

import pytest


def test_card_detect_disable_env_bypasses_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARD_DETECT_DISABLE", "1")

    from app import ocr_extract

    img = _synthetic_card_on_background()
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    calls: list[int] = []

    def _fake_detect(image):  # type: ignore[no-untyped-def]
        calls.append(1)
        return image, True

    monkeypatch.setattr(ocr_extract, "detect_and_rectify", _fake_detect)
    monkeypatch.setattr(ocr_extract, "_check_tesseract", lambda: None)
    monkeypatch.setattr(ocr_extract, "_ocr_string", lambda im: "")
    monkeypatch.setattr(
        ocr_extract,
        "match_set_symbol_best_of_crops",
        lambda variants, boxes: None,
    )

    ocr_extract.extract_card_signals(buf.getvalue())
    assert calls == [], "detect_and_rectify should be skipped when CARD_DETECT_DISABLE is set"
```

- [ ] **Step 2: Append env-var test to `tests/test_set_code_resolution.py`**

At the top of `tests/test_set_code_resolution.py`, make sure these imports exist (add any that are missing):

```python
import io

import pytest
from PIL import Image
```

Then append at the end of the file:

```python
def test_tiebreaker_disable_env_skips_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import ocr_extract

    monkeypatch.setenv("SYMBOL_OCR_TIEBREAKER_DISABLE", "1")
    monkeypatch.setattr(ocr_extract, "_check_tesseract", lambda: None)
    monkeypatch.setattr(ocr_extract, "_ocr_string", lambda im: "SVI 132/200")
    monkeypatch.setattr(ocr_extract, "detect_and_rectify", lambda im: (im, False))

    refs = [_ref("100", "AAA"), _ref("200", "BBB")]

    monkeypatch.setattr(
        ocr_extract,
        "match_set_symbol_best_of_crops",
        lambda variants, boxes: (refs[0], 18, (0, 0, 10, 10)),
    )

    swapped: list[int] = []
    real_resolve = ocr_extract.resolve_set_with_tiebreaker

    def _spy(phash_hit, ocr_set_codes, **kw):  # type: ignore[no-untyped-def]
        swapped.append(1)
        return real_resolve(phash_hit, ocr_set_codes, **kw)

    monkeypatch.setattr(ocr_extract, "resolve_set_with_tiebreaker", _spy)

    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (255, 255, 255)).save(buf, format="PNG")
    ocr_extract.extract_card_signals(buf.getvalue())

    assert swapped == [], "tiebreaker should be skipped when SYMBOL_OCR_TIEBREAKER_DISABLE is set"
```

The `_ref` helper used here was defined earlier in the same file inside Task 12.

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_card_detect.py tests/test_set_code_resolution.py
git commit -m "test: lock opt-out env vars for card detect + tiebreaker"
```

---

## Task 17: Debug CLI for visual validation

**Files:**
- Create: `scripts/debug_set_symbol.py`

This task adds a manual diagnostic tool for the workflow we promised in the earlier troubleshooting thread. It is not pytest-tested — its value is producing on-disk artifacts a human inspects.

- [ ] **Step 1: Write `scripts/debug_set_symbol.py`**

```python
#!/usr/bin/env python3
"""Dump every symbol crop + top-K reference matches for a single image.

Usage:
    python scripts/debug_set_symbol.py path/to/card.jpg [--top-k 5]

Writes:
    scripts/output/debug_<image-stem>/
        00_input.png
        01_rectified.png            (only if card detection succeeded)
        crop_<box_index>.png        per emitted crop box
        glyph_<box_index>.png       isolated glyph if extraction succeeded
        report.txt                  hamming distances per crop × top-K refs
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

from app.card_detect import detect_and_rectify
from app.ocr_extract import _ocr_variants, _scale_for_ocr, _symbol_crop_boxes
from app.set_symbol_index import (
    _candidate_hashes_for_crop,
    _hamming,
    _isolate_glyph_crop,
    load_symbol_index,
)


def _format_top_k(hashes: list[int], top_k: int) -> list[tuple[str, int]]:
    refs = load_symbol_index()
    scored: list[tuple[str, int]] = []
    for ref in refs:
        d = min(_hamming(h, ref.hash_int) for h in hashes)
        scored.append((f"{ref.set_id}:{ref.set_code or '-'}", d))
    scored.sort(key=lambda x: x[1])
    return scored[:top_k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("scripts/output"),
    )
    args = ap.parse_args()

    if not args.image.is_file():
        print(f"Not a file: {args.image}", file=sys.stderr)
        sys.exit(2)

    out_dir = args.out_dir / f"debug_{args.image.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)

    img = Image.open(args.image)
    img = ImageOps.exif_transpose(img)
    img.save(out_dir / "00_input.png")

    rectified, detected = detect_and_rectify(img)
    if detected:
        rectified.save(out_dir / "01_rectified.png")
    work = rectified if detected else img

    gray = _scale_for_ocr(ImageOps.grayscale(work))
    variants = _ocr_variants(gray)
    proc = variants[0]
    w, h = proc.size
    boxes = _symbol_crop_boxes(w, h)

    report_lines: list[str] = [
        f"input: {args.image}",
        f"detected_card: {detected}",
        f"processed_px: {w}x{h}",
        f"boxes: {len(boxes)}",
        "",
    ]

    for idx, box in enumerate(boxes):
        crop = proc.crop(box)
        crop.save(out_dir / f"crop_{idx:02d}.png")
        glyph = _isolate_glyph_crop(crop)
        if glyph is not None:
            glyph.save(out_dir / f"glyph_{idx:02d}.png")
        hashes = _candidate_hashes_for_crop(crop)
        top = _format_top_k(hashes, args.top_k)
        report_lines.append(
            f"crop[{idx:02d}] box={box} size={crop.size} → "
            + ", ".join(f"{name}={dist}" for name, dist in top)
        )

    report = "\n".join(report_lines) + "\n"
    (out_dir / "report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"Wrote artifacts to {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the script manually**

Pick any reference PNG and run:

```
.venv/bin/python scripts/debug_set_symbol.py app/data/set_symbols/charizard-evolutions.png
```

(or any image you have handy.) Expected: script exits 0, writes `scripts/output/debug_<stem>/report.txt` and per-crop PNGs. The first crop's top match should be `charizard-evolutions` or a visually similar set.

- [ ] **Step 3: Commit**

```bash
git add scripts/debug_set_symbol.py
git commit -m "tools: debug_set_symbol.py CLI dumps crops + top-K matches"
```

---

## Verification

After the final task, run the full pipeline end-to-end:

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all green across `test_smoke`, `test_card_signals`, `test_name_parser`, `test_symbol_crop_boxes`, `test_set_code_resolution`, `test_card_detect`, `test_build_search_queries`.

- [ ] **Step 2: Start the API locally**

Run: `LOG_LEVEL=DEBUG .venv/bin/uvicorn app.main:app --reload`
Expected: startup logs include `set_symbol.index loaded refs=N` with N > 0, no traceback.

- [ ] **Step 3: Submit a known-good card image (manual)**

Use the existing frontend or `curl -F image=@…` against `POST /v1/cards/analyze-image`. Inspect logs for the new signals:
- `card_detect.ok …` or `card_detect.fallback reason=…`
- `set_symbol.tiebreaker_swap …` (only when triggered)
- `name_parser` lines indirectly reflected in `ocr.final_candidates`

Expected: response shape matches `CardAnalyzeResponse` schema (unchanged), but `suggested_search_queries` now contains broader name variants when applicable.

---

## Open follow-ups (out of scope for this plan)

- Spec self-review flagged that `pytest` is added at the top level rather than as a dev-only extra. If the project later adopts `requirements-dev.txt`, move `pytest` there.
- The fetch script for Pokémon names hits PokéAPI per-species (slow). A bulk endpoint would be faster but isn't currently available; revisit if the helper becomes annoying to run.
- Sub-project 2 (Pokédex/collection/valuation) is unblocked once this plan lands.
