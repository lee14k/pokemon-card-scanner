# Identification Accuracy — Design

**Date:** 2026-05-20
**Scope:** Sub-project 1 of 2 (the second sub-project — collection / Pokédex / valuation — is tracked separately and will be brainstormed after this lands).
**Status:** Approved design, pending spec review.

---

## Problem

Two failure modes on `POST /v1/cards/analyze-image` and `POST /v1/cards/price-from-image`:

1. **Name parsing is too narrow.** `app/ocr_extract.py::pick_primary_name_from_top_band` extracts a single name token using a regex (`[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}(?:\s+[a-z]{1,4})?`) that misses common card-name shapes:
   - **Possessives**: `Misty's Tentacool`, `N's Zoroark`, `Iono's Bellibolt` — the `'s` breaks the token boundary and the regex never matches the full name.
   - **Form/region prefixes**: `Alolan Raichu`, `Galarian Rapidash`, `Hisuian Zoroark`, `Paldean Tauros`, `Dark Charizard`, `Shining Magikarp`, `Radiant Charizard` — usually do match the current regex, but with no validation that the second word is actually a Pokémon, garbage adjective + capitalized noise can win.
   - **Modifiers**: trailing `V`, `VMAX`, `VSTAR`, `GX`, `EX`, `ex`, `BREAK`, `LV.X` are dropped because the trailing optional group only allows 1-4 lowercase letters.

2. **Set symbol localization is too narrow.** `app/ocr_extract.py::_symbol_crop_boxes` only emits crop boxes anchored to the image's bottom-LEFT corner. This breaks in two ways:
   - **Bottom-right layouts**: modern Scarlet & Violet, Sword & Shield Trainer Gallery, and several promo prints sit the set symbol bottom-right, immediately right of the card number. None of our crops contain it.
   - **Background / framing**: the function assumes the image is a tight card crop. When the user photographs the card with table or hand visible, "bottom-left of the image" is not "bottom-left of the card."

## Goals

- Recognize Pokémon names that follow the `<prefix> <pokemon>`, `<trainer>'s <pokemon>`, and `<pokemon> <suffix>` shapes.
- Reduce false-positive name extraction by validating tokens against a canonical Pokémon name list.
- When name extraction is ambiguous, emit multiple candidates and route disambiguation to the existing scorer in `app/matching.py` rather than guessing.
- Locate the set symbol on cards where it sits bottom-right (modern SV-era layouts).
- Tolerate photos with visible background by detecting the card quadrilateral and cropping relative to it.
- Use OCR'd set codes (`SVI`, `PAL`, `MEW`, `SV4PT5`, …) as a tiebreaker when pHash distances are close.

## Non-goals

- No persistent collection storage, valuation, or UI changes — those land in Sub-project 2.
- No new card-source coverage (we still index the ~192 sets already scraped from pokesymbols.com).
- No model-based name OCR. We stay with Tesseract + heuristics.
- No mobile / device-side changes. All logic remains server-side.

---

## Architecture

```
+----------------------+        +-----------------------+        +----------------------------+
|   raw upload bytes   |  -->   |  app/card_detect.py   |  -->   |  rectified card image      |
+----------------------+        |  (NEW)                |        |  (or raw fallback)         |
                                +-----------------------+        +-------------+--------------+
                                                                               |
                                                                               v
                            +---------------------------------------------------------------+
                            |  app/ocr_extract.py  (modified)                               |
                            |                                                               |
                            |  • _ocr_variants            (unchanged)                       |
                            |  • _symbol_crop_boxes       (bottom-LEFT  + bottom-RIGHT)     |
                            |  • _bottom_strip_set_code   (NEW: OCR small ASCII tokens)     |
                            |  • pick_primary_name_*      (multi-candidate, dex-validated)  |
                            +-------------------------------+-------------------------------+
                                                            |
                                                            v
                            +---------------------------------------------------------------+
                            |  app/set_symbol_index.py  (modified)                          |
                            |                                                               |
                            |  • match_set_symbol_best_of_crops (unchanged signature)       |
                            |  • set_code_to_set_id              (NEW: tiebreaker lookup)   |
                            +---------------------------------------------------------------+
                                                            |
                                                            v
                            +---------------------------------------------------------------+
                            |  CardSignals  (modified: name_candidates: list[str])          |
                            +---------------------------------------------------------------+
```

---

## Component 1 — Name parser (in `app/ocr_extract.py`)

### Data file: `app/data/pokemon_names.txt`

- One canonical English Pokémon name per line, e.g. `Bulbasaur`, `Mr. Mime`, `Farfetch'd`, `Type: Null`.
- Source: PokéAPI `GET /api/v2/pokemon-species?limit=2000` for the canonical species list (covers all 1,025+ species through current gen with headroom). Names are taken from each species' English (`language.name == "en"`) entry under `names`, falling back to the URL slug if the localized lookup fails. Fetched offline by a new helper script.
- Loaded once at import time into a `frozenset[str]` of lowercased names plus a `frozenset[str]` of ASCII-folded names (so `farfetch'd` and `farfetchd` both look up).
- Size: ~1,200 entries today, ~25 KB on disk. Tiny.

### New helper: `scripts/fetch_pokemon_names.py`

- Hits PokéAPI, writes `app/data/pokemon_names.txt`. Idempotent. One-line invocation. Not run in production — humans run it when new gens drop.

### Static enums (in `app/ocr_extract.py`)

```python
_FORM_PREFIXES = frozenset({
    "alolan", "galarian", "hisuian", "paldean",
    "dark", "light", "shining", "crystal", "shadow", "radiant",
    "ancient", "future",  # SV scarlet/violet treasure of ruin / paradox
})

_NAME_SUFFIXES = frozenset({
    "v", "vmax", "vstar", "gx", "ex",
    "break", "lv.x", "δ",
    # tag-team and other markers handled via the '&' splitter below
})
```

### New extraction functions

```python
def _is_known_pokemon(token: str) -> bool: ...
def _strip_possessive_prefix(line: str) -> tuple[str | None, str]:
    """('Misty', 'Tentacool') for 'Misty's Tentacool'; (None, line) otherwise."""
def _expand_with_modifiers(line: str, anchor_pokemon: str) -> str:
    """Given an anchor that is a real Pokémon name, return the full printed
    name including any form prefix and/or modifier suffix on the same line."""
def collect_name_candidates(raw_top: str, max_candidates: int = 4) -> list[str]: ...
```

Algorithm for `collect_name_candidates`:

1. Walk `_lines_from_raw_top_to_bottom(raw_top)` (existing helper).
2. Skip lines via `_is_junk_name_line` (existing).
3. For each surviving line:
    a. Strip a leading possessive (`Misty's Tentacool` → trainer=`Misty`, rest=`Tentacool`). The possessive apostrophe may be ASCII `'`, curly `’` (U+2019), or a prime `′` (U+2032) — all three are normalized before matching.
   b. Tokenize alphanumerically.
   c. Find the **first** token that is a known Pokémon. If none, fall back to the legacy `_extract_capitalized_name_token` for backward compatibility.
   d. Reassemble the canonical printed name as `<form_prefix?> <pokemon> <suffix?>`, then prepend the possessive if any (so the emitted candidate is `Misty's Tentacool`, not just `Tentacool`).
   e. Emit both the full canonical form AND the bare Pokémon name as two candidates. PokéWallet often catalogs the bare name with set metadata that already encodes the trainer.
4. Return de-duplicated candidates, capped at `max_candidates`.

### CardSignals contract change

`app/card_signals.py::CardSignals` gains a new field:

```python
name_candidates: list[str] = field(default_factory=list)
```

`primary_name_guess` stays for backward compatibility and is set to `name_candidates[0] if name_candidates else None`. Existing call sites in `app/main.py` keep working unchanged.

### Search query integration

`app/matching.py::build_search_queries` already accepts `primary_name_guess: str | None`. We extend it to also accept `name_candidates: list[str] | None = None` and, when present, expand its candidate set with each entry (de-duplicated, capped at `max_queries`).

This is the "route ambiguity to the scorer" play: instead of trying to be right in one shot, we let PokéWallet's text search and `score_card_against_blob` filter.

---

## Component 2 — Card detection (`app/card_detect.py`, NEW)

New module, single public function:

```python
def detect_and_rectify(image: Image.Image) -> tuple[Image.Image, bool]:
    """
    Returns (image, detected). When detected=True, image is a perspective-corrected
    card crop at canonical aspect (2.5 x 3.5 inches = 5:7). When detected=False,
    image is the input unchanged and downstream logic falls back to current behavior.
    """
```

Algorithm:

1. Resize working copy to long-side 1200px for speed.
2. Convert to grayscale, light Gaussian blur, `cv2.Canny`.
3. `cv2.findContours`, filter to closed contours with area ≥ 25% of image area.
4. For the top-K largest contours, attempt `cv2.approxPolyDP` to 4 vertices; accept if the polygon is convex and aspect ratio is in `[0.5, 0.85]` (portrait card) or `[1.18, 2.0]` (landscape — we'll un-rotate).
5. Apply `cv2.getPerspectiveTransform` + `cv2.warpPerspective` to a canonical 750×1050 (5:7) target.
6. If any step fails, return `(input, False)`.

Called once at the top of `extract_card_signals` before `_ocr_variants`. If detection succeeds, **everything downstream operates on the rectified card** — both the OCR and the symbol matching. If it fails, behavior is identical to today.

---

## Component 3 — Symbol localization (in `app/ocr_extract.py` + `app/set_symbol_index.py`)

### Expanded crop boxes

`_symbol_crop_boxes(w, h)` is rewritten to emit both **bottom-left** and **bottom-right** regions:

```
bottom-left:  current behavior (preserved verbatim)
bottom-right: x range = [w - max(w * frac, 24), w]  for frac in (0.11, 0.14, 0.18)
              y range = same y_fracs as bottom-left
              Plus the same "looser legacy" boxes mirrored to the right.
```

Crop count grows from ~21 to ~42. Inner loop cost is dominated by the 192-ref pHash compare, so end-to-end latency increase is small (rough estimate: +30-50ms per request based on the existing per-crop log volume).

### OCR set code tiebreaker

New function in `app/ocr_extract.py`:

```python
_SET_CODE_RE = re.compile(r"\b([A-Z]{2,5}\d*[A-Z]*)\b")

def _candidate_set_codes(raw_bottom: str) -> list[str]:
    """Pull short ASCII tokens like 'SVI', 'PAL', 'MEW', 'SV4PT5' from the bottom strip."""
```

New helper in `app/set_symbol_index.py`:

```python
@lru_cache(maxsize=1)
def set_code_to_set_id() -> dict[str, str]:
    """Build {SET_CODE: set_id} from index.json. Used as a pHash tiebreaker."""

def resolve_set_with_tiebreaker(
    phash_hit: tuple[SymbolRef, int, ...],
    ocr_set_codes: list[str],
) -> tuple[SymbolRef, int]:
    """
    If pHash margin is ambiguous (margin < min_margin) AND one of ocr_set_codes
    maps to a set_id in the index, prefer that set. Otherwise keep pHash decision.
    """
```

`extract_card_signals` is updated to:

1. Compute `_candidate_set_codes(raw_bottom)` after the existing OCR loop.
2. After `match_set_symbol_best_of_crops` returns, pass the hit through `resolve_set_with_tiebreaker`.
3. Log both the raw pHash result and the post-tiebreaker decision so we can see when the OCR code rescued an ambiguous match.

This is **additive only** — when no set code is OCR'd or no code maps to a known set, behavior is exactly as today.

---

## Data flow

```
upload bytes
  │
  ▼
PIL.Image.open + ExifTranspose
  │
  ▼
card_detect.detect_and_rectify   ← NEW (Component 2)
  │  (rectified image OR raw fallback)
  ▼
_scale_for_ocr + _ocr_variants    (unchanged)
  │
  ▼
+----------- top band OCR -----------+    +----------- bottom strip OCR -----------+
| pick_primary_name_from_top_band    |    | pick_collection_number                |
| collect_name_candidates  ← NEW     |    | _candidate_set_codes  ← NEW            |
+------------------+-----------------+    +-------------------+-------------------+
                   │                                          │
                   ▼                                          ▼
              CardSignals.name_candidates              ocr_set_codes
                   │                                          │
                   │                                          ▼
                   │                          _symbol_crop_boxes (BL + BR)
                   │                                          │
                   │                                          ▼
                   │                          match_set_symbol_best_of_crops
                   │                                          │
                   │                                          ▼
                   │                          resolve_set_with_tiebreaker  ← NEW
                   │                                          │
                   ▼                                          ▼
              build_search_queries  ←─────  CardSignals.set_id_from_symbol
                                                              │
                                                              ▼
                                                       /v1/cards/* response
```

---

## Error handling

- **PokéAPI fetch failure** (`fetch_pokemon_names.py`): script exits non-zero. Production never runs it. If the file is missing at import time, `_is_known_pokemon` returns `False` for every token and the name parser degrades gracefully to legacy regex extraction. Already-loaded users continue to work; quality regresses to today's baseline. Logged at WARN: `name_parser.pokemon_dict_missing path=...`.
- **Card detection failure** (low-contrast, busy background, holo glare): `detect_and_rectify` returns `(input_image, False)` and pipeline proceeds with raw bytes — identical to today. Logged at INFO: `card_detect.fallback reason=<…>`.
- **OCR set-code false positive** (e.g. `HP`, `OF`, two-letter junk): codes that don't map to any set in `set_code_to_set_id()` are silently dropped. No regression risk.
- **Tiebreaker conflict** (pHash says X, OCR says Y, both valid): rule is "OCR wins only when pHash margin < min_margin." This guards against tiny-text OCR overriding a confident pHash match.

---

## Testing strategy

A small `tests/` directory does not exist yet in this repo. We'll create one and seed with:

1. **`tests/test_name_parser.py`** — table-driven unit tests for `collect_name_candidates`:
   - `"Misty's Tentacool"` → contains `"Misty's Tentacool"` and `"Tentacool"`
   - `"Alolan Raichu"` → contains `"Alolan Raichu"` and `"Raichu"`
   - `"Iono's Bellibolt ex"` → contains `"Iono's Bellibolt ex"` and `"Bellibolt"`
   - `"Charizard VMAX"` → contains `"Charizard VMAX"` and `"Charizard"`
   - `"GAME FREAK"` → empty (junk filter)
   - `"Stes"` → empty (not in dex, length-tier rule)

2. **`tests/test_symbol_crop_boxes.py`** — confirm `_symbol_crop_boxes(750, 1050)` emits both bottom-left and bottom-right boxes; assert count and that no box has `x1 <= x0` or `y1 <= y0`.

3. **`tests/test_set_code_resolution.py`** — feed in a synthetic `index.json` and assert `set_code_to_set_id` maps codes correctly; assert `resolve_set_with_tiebreaker` keeps pHash decision when margin is healthy and switches to OCR code when margin is tight.

4. **No new image-based integration tests** (those need real card images and are flaky in CI). We will additionally land `scripts/debug_set_symbol.py` (a CLI that runs the same crop + match pipeline against a local image file and dumps every crop + per-crop top-K reference matches to `scripts/output/debug_<image>/`) for manual visual validation.

`pytest` will be added as a top-level dependency in `requirements.txt`. The project does not use extras today, so we don't introduce them now.

---

## Backward compatibility

- `CardSignals.primary_name_guess`: preserved (now derived from `name_candidates[0]`).
- `app/schemas.py::CardAnalyzeResponse`: unchanged. `name_candidates` is internal; we keep the response shape stable.
- `app/main.py`: only the `build_search_queries(...)` call site changes — adds `name_candidates=signals.name_candidates`. Everything else is untouched.
- Env vars `SET_SYMBOL_MAX_DISTANCE` and `SET_SYMBOL_MIN_MARGIN` keep their meaning.

A user who pulls these changes and does nothing else gets:
- Card detection enabled by default (fail-soft).
- Bottom-right symbol crops enabled by default (purely additive).
- OCR set-code tiebreaker enabled by default (purely additive).
- Multi-candidate name search enabled by default (search query count goes up by 1-3, still capped at `max_queries=8`).

Opt-outs (if needed during rollout):
- `CARD_DETECT_DISABLE=1` — bypass `detect_and_rectify`.
- `SYMBOL_OCR_TIEBREAKER_DISABLE=1` — bypass the OCR set-code tiebreaker.

---

## Open questions

None blocking. The two flagged trade-offs were accepted in design review:
1. ~25 KB names file in the repo, refreshed occasionally via `scripts/fetch_pokemon_names.py`.
2. Card detection falls back gracefully when it can't find a quadrilateral.

---

## Out of scope (deferred to Sub-project 2)

- Persistent storage of scanned cards.
- "Add to my collection" flow.
- Total-value rollup across owned cards.
- Variant / condition / quantity model.
- Authentication.
