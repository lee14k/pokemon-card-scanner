# Calibration corpus

Each pack = one directory: `tests/corpus/<pack_id>/`
- `staircase.jpg` — staircase photo (phone, portrait, 1080p+)
- `code.jpg` — close-up of the code card
- `truth.json` — ground truth (create with `scripts/label_corpus.py`)

## Photo protocol
- Stack the opened pack in a staircase: front card fully visible on top, each
  card behind shifted down ~1.5–2cm so every bottom strip shows.
- Dark, flat, non-reflective background. Fill the frame with the stack.
- Include the energy card row if the pack had one; label what's printed on it.
- Target 20–30 packs spanning your sets. Vary deliberately:
  - lighting: daylight / lamp / dim (≥4 packs each)
  - capture: guided overlay AND plain photo (mix)
  - stress: several with foil/holo glare in the bottom strip, several slightly
    rotated, one badly blurry (control — should be flagged, not misread)

## truth.json format
{
  "capture_meta": null,            // or the guided-capture metadata if used
  "cards": [{"row_index": 0, "number": "123/198", "set_id": "..."}, ...],
  "code": "XXX-XXXX-XXX"
}

## Loose photos at the corpus root — triage record (2026-07-27)

Ten raw phone photos sat untracked in this directory. They were triaged for the
binder gate; **none of them is a binder page** (all are staircase stacks except
one code card), so none became a `tests/corpus/binder/real/` fixture. They are
kept here, unlabeled and flat, as raw material for the pack corpus: nothing
globs the corpus root, so no gate or test picks them up. `scripts/measure_matcher.py`
already reads `IMG_7102.heic` from here.

Every collector number below was read off the pixels by eye and then
corroborated against the tcgdex catalog: the flavor text visible on each strip
matches the card that `(set, number)` resolves to. Printed set code → tcgdex id:
CRI → `me04` (Chaos Rising), MEG → `me01`, JTG → `sv09`, WHT → `sv10.5w`,
TWM → `sv06`, MEE → `mee`, SVE → `sve`.

| file | px | kind | set | legible collector numbers (top → bottom) |
|---|---|---|---|---|
| `225F57C5-…_4_5005_c.jpeg` | 360×480 | staircase (11 strips) | CRI + MEE | 030, 052, 014, 046, 061, 009, 074, 029, 070, 029, `MEE 001` |
| `5699EF12-…_4_5005_c.jpeg` | 360×480 | staircase (11 strips) | CRI + MEE | 001, 050, 023, 036, 079, 026, 047, 056, 014, 064, `MEE 001` |
| `7C04A219-….heic` | 3024×4032 | staircase (8 strips + top card face) | CRI | 012, 043, 024, 083, 050, 060, 023, 070 |
| `8EC9AC39-…_1_105_c.jpeg` | 768×1024 | staircase (8 strips) | JTG | 108, 106, 097, 041, 151, 060, 158, 106 |
| `C05EF4AE-….heic` | 3024×4032 | staircase (7 strips) | CRI | 082, 011, 080, 019, 036, 011, 018 |
| `D2B15416-….heic` | 3024×4032 | staircase (7 strips) | CRI | 004, 008, 047, 026, 079, 038, 006 |
| `D52489DA-….heic` | 3024×4032 | staircase, clipped (3 strips) | WHT | 048, 013, 015 |
| `E7809859-…_1_105_c.jpeg` | 768×1024 | staircase (8 strips) | MEG | 081, 119, 097, 011, 096, 028, 066, 102 |
| `IMG_7102.heic` | 3024×4032 | staircase (11 strips) | TWM + SVE | 010, 126, 101, 045, 143, 122, 079, 066, 078, 096, `SVE 007` |
| `IMG_7103.heic` | 4032×3024 | code card | TWM | code `2CM-2ZY7-WKD-DTM` (Twilight Masquerade) |

Notes for whoever labels these into packs:
- `IMG_7102.heic` + `IMG_7103.heic` are one pack (same session, TWM staircase +
  TWM code card) — the only complete staircase/code pair here, and the natural
  first `tests/corpus/<pack_id>/` entry. The other nine have no code card.
- Duplicated numbers within a stack are real (e.g. `029` twice in
  `225F57C5`, `106` twice in `8EC9AC39`, `011` twice in `C05EF4AE`) — these are
  sorted piles, not necessarily single-pack contents, so a pack `truth.json`
  built from one of them would be describing a pile, not a pack.
- The two 360×480 files are phone-export thumbnails; usable as a low-resolution
  stress case, not as a normal-quality fixture.
- `D52489DA` is clipped: only three strips are in frame and the stack continues
  past the top edge.
