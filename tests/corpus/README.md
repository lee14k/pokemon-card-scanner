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
