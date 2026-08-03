# Binder VLM Merge Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the binder VLM merge from displaying a rejected identity (production case: Mega Latias ex shown as "Skitty · 130/162 · Temporal Forces"), give the VLM a magnified number strip so it stops fabricating collector numbers, and move thumbnail encoding off the event loop.

**Architecture:** All changes ride the existing seams: `apply_vlm_answer` in [app/pack/vlm_merge.py](../../app/pack/vlm_merge.py) gains an evidence-polarity fix (an exact catalog name hit outweighs a fabrication-prone VLM denominator) and a rollback (guard-rejected number-first merges restore the pass-1 card). The binder VLM payload gains an optional `strip_b64` second image (app → `vlm_client` → RunPod handler → prompt), backward compatible in both directions. `_finish` encodes thumbnails in one worker thread. The acceptance gate learns to score what flagged cells *display*.

**Tech Stack:** Python 3.11 / FastAPI app, pydantic `PackCard`, OpenCV, RapidOCR, rapidfuzz, Postgres (tcgdex catalog), RunPod serverless worker (Qwen2.5-VL-7B).

## Global Constraints

- **No pytest.** This project verifies with committed acceptance scripts only (`docs/acceptance/`). New regression coverage goes in `docs/acceptance/probes/` following the existing probe conventions (exit 0 = pass, exit 2 = BLOCKED when env/DB unusable, like `binder_gate.py`).
- **Verification env** (local catalog DB required):
  `export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false`
  Interpreter: `.venv/bin/python`.
- **The binder gate must pass after every task:** `.venv/bin/python docs/acceptance/binder_gate.py` → exit 0 (`GATE PASS`). Exit 2 (BLOCKED) means your env is wrong — fix env, never the gate.
- **Contract compatibility both directions** for the app↔worker change: an old worker receiving the new field must behave exactly as today; a new worker receiving old payloads must behave exactly as today.
- **Comment style:** comments state constraints and evidence ("why this value / why not the obvious alternative"), never narrate the next line. Match the density and voice of the surrounding file.
- **Commit style:** `fix(scope): <lowercase narrative>` matching `git log` (e.g. `fix(scan): keep the narrow strip, and stop re-encoding what we cannot re-verify`).
- Never touch `NUMDIFF_BASELINE` values or gate thresholds except where Task 2 explicitly adds a new, separate ratchet.

---

### Task 1: vlm_merge — exact-name evidence polarity + guard-rejected merge rollback

The production failure: the VLM read the name "Mega Latias ex" correctly but fabricated denominator 162. The veto at `vlm_merge.py:136-142` dropped the *entire exact-name candidate list* because the denominator contradicted it — backwards, because the codebase's own comment (line 114) documents that the VLM fabricates numbers, not names. The card then fell to number-first resolution (162 → Temporal Forces, #130 → Skitty), the name cross-check *detected* the mismatch, but the wrong fields stayed on the card for display.

There is also a latent sibling bug: the `name_display_only` branch (line 156-164) sets name/set fields, but then the number-first block *still* overwrites `card_number` with the claimed number and the keyed re-lookup (line 219-224) *still* overwrites `name` with whatever card sits at the claimed numerator in the display-only set. Display-only must be terminal for identity fields.

**Files:**
- Modify: `app/pack/vlm_merge.py` (only inside `apply_vlm_answer`, lines ~114-274)
- Create: `docs/acceptance/probes/vlm-merge-name-first-probe.py`

**Interfaces:**
- Consumes: `apply_vlm_answer(card, ans, table, *, ocr_texts)` — signature unchanged.
- Produces: unchanged signature/return; new behavior only. Task 3's worker change is independent of this.

- [ ] **Step 1: Write the failing probe**

Create `docs/acceptance/probes/vlm-merge-name-first-probe.py`. Look at an existing probe in `docs/acceptance/probes/` first and match its header/BLOCKED conventions. Content:

```python
"""Probe: apply_vlm_answer must never display an identity its own guards rejected.

Production case (2026-08-02, prod screenshot == tests/corpus/binder/real/page_1.jpeg
cell 2): the VLM read the printed name "Mega Latias ex" correctly and fabricated
the collector number "130/162". The old merge dropped the exact name hit because
the fabricated denominator contradicted it, resolved 162->Temporal Forces #130
("Skitty"), and left those fields on the card for display even after the name
cross-check refused to accept them.

Three assertions:
  1. Exact-name hit + contradicting denominator -> the NAME wins: card shows
     "Mega Latias ex" / Mega Evolution, stays flagged, claimed number is not
     merged, and the answer is not accepted (returns False).
  2. Fuzzy/garbage name + fabricated number -> number-first runs, the name
     cross-check fails, and the merge ROLLS BACK: the card is byte-identical
     to its pre-merge state.
  3. Exact name + correct denominator + variant-picking numerator (181/132)
     still resolves confidently (returns True) — the fix must not break the
     path that works.

Needs the local catalog DB (me01 must be ingested). Exit 0 = pass, 2 = BLOCKED.
"""
import asyncio
import os
import sys

EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED = 0, 1, 2


async def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("BLOCKED: DATABASE_URL unset")
        return EXIT_BLOCKED
    from sqlalchemy import select

    from app.db.models import TcgdexCard
    from app.db.session import async_session_maker
    from app.pack.set_resolution import load_denominator_table
    from app.pack.vlm_merge import apply_vlm_answer
    from app.schemas import PackCard

    try:
        async with async_session_maker() as session:
            row = (await session.execute(
                select(TcgdexCard.name).where(TcgdexCard.set_id == "me01")
                .limit(1))).first()
    except Exception as e:
        print(f"BLOCKED: catalog DB unreachable: {e!r}")
        return EXIT_BLOCKED
    if row is None:
        print("BLOCKED: me01 not ingested (run scripts/ingest_tcgdex.py)")
        return EXIT_BLOCKED

    table = load_denominator_table()
    failures: list[str] = []

    def fresh_card() -> PackCard:
        # The pass-1 state of the production cell: number read, nothing resolved.
        return PackCard(row_index=2, card_number="130/162", needs_review=True,
                        low_confidence_reason="set_ambiguous", confidence=0.2)

    # 1. Exact name + fabricated denominator: name wins, no Skitty.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "130/162",
               "denominator": 162, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if ok:
        failures.append("case1: accepted an uncorroborated variant-less identity")
    if card.name != "Mega Latias ex":
        failures.append(f"case1: name={card.name!r}, want 'Mega Latias ex'")
    if (card.set_name or "") != "Mega Evolution":
        failures.append(f"case1: set_name={card.set_name!r}, want 'Mega Evolution'")
    if card.card_number != "130/162":
        failures.append(f"case1: card_number={card.card_number!r} — the claimed "
                        "number must not be merged on a display-only name hit")
    if not card.needs_review:
        failures.append("case1: needs_review cleared")

    # 2. Garbage name + fabricated number: number-first merge must roll back.
    card = fresh_card()
    before = card.model_dump()
    ok = await apply_vlm_answer(
        card, {"name": "Xyzzy Prime ex", "number": "130/162",
               "denominator": 162, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if ok:
        failures.append("case2: accepted a name-mismatched identity")
    if card.model_dump() != before:
        diff = {k: (before[k], v) for k, v in card.model_dump().items()
                if before[k] != v}
        failures.append(f"case2: guard-rejected merge left fields behind: {diff}")

    # 3. Exact name + true denominator + variant numerator: still resolves.
    card = fresh_card()
    ok = await apply_vlm_answer(
        card, {"name": "Mega Latias ex", "number": "181/132",
               "denominator": 132, "set_name": None, "confidence": 0.9},
        table, ocr_texts=None)
    if not ok or card.needs_review:
        failures.append("case3: exact name + correct den + variant numerator "
                        f"failed to resolve (ok={ok} flagged={card.needs_review})")
    if card.name != "Mega Latias ex" or card.card_number != "181/132":
        failures.append(f"case3: got {card.name!r} {card.card_number!r}")

    for f in failures:
        print(f"FAIL: {f}")
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return EXIT_PASS if not failures else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

Before writing, confirm `PackCard`'s constructor accepts those keyword names (check `app/schemas.py`); adjust the probe's `fresh_card` to the real field names if they differ. If an existing probe file uses a different header or arg convention, match it.

- [ ] **Step 2: Run the probe, verify it fails for the right reasons**

Run: `.venv/bin/python docs/acceptance/probes/vlm-merge-name-first-probe.py` (with the Global Constraints env).
Expected: case1 fails (name comes back "Skitty"-adjacent or claimed number merged), case2 fails (fields left behind), case3 passes. If it exits BLOCKED, fix env before proceeding.

- [ ] **Step 3: Implement the three changes in `apply_vlm_answer`**

All inside `app/pack/vlm_merge.py`. Keep the surrounding comment voice; update the big comment block at lines 114-120 to document the new polarity.

**(a) Veto polarity.** Track whether the candidates came from the exact normalized-key lookup, and when the claimed denominator contradicts *every* exact-hit candidate, discard the denominator instead of the name:

```python
            cands = list(idx._entries.get(normalize_name(vlm_name)) or [])
            exact_hit = bool(cands)
            if not cands:
                fz = idx.match(vlm_name, denominator=str(den) if den else None)
                if fz is not None and not fz.ambiguous:
                    cands = list(idx._entries.get(normalize_name(fz.card_name)) or [])
            # Denominator filter — with a POLARITY rule. When the claimed
            # denominator contradicts every candidate, one of the two claims is
            # fabricated, and which one depends on how the name was hit:
            #   * EXACT normalized-key hit: a multi-token printed name read
            #     verbatim is the strong evidence; the denominator is the field
            #     this worker demonstrably fabricates (the Mega Latias ex /
            #     "130/162" -> Skitty production case). Discard the denominator,
            #     keep the name.
            #   * FUZZY hit: the name itself is a guess about a guess — the
            #     original veto stands and the name path is abandoned.
            if cands and den is not None and str(den).isdigit():
                den_i = int(str(den))
                with_den = [c for c in cands if c[4] == den_i]
                if with_den:
                    cands = with_den
                elif all(c[4] is not None and c[4] != den_i for c in cands):
                    if exact_hit:
                        log.info("vlm.den_discarded name=%r den=%s row=%s "
                                 "(exact name hit outweighs claimed denominator)",
                                 vlm_name, den, getattr(card, "row_index", None))
                    else:
                        cands = []
```

**(b) Display-only is terminal for identity fields.** The `name_display_only` branch already wrote honest name/set fields; nothing after it may overwrite them with number-keyed lookups. Gate the number-first block, the set_name/denominator pinning, the keyed re-lookup, and the TCGdex name fallback on `not name_display_only`:

```python
    if not name_resolved:
        if not num:
            return False
        if not name_display_only:
            card.card_number = f"{num}/{den}" if den else num
```

and change the three later conditions:
- `if not name_resolved and ans.get("set_name"):` → `if not name_resolved and not name_display_only and ans.get("set_name"):`
- `if not name_resolved and (set_id is None or card.set_name is None) and den is not None:` → add `and not name_display_only`
- `if set_id and num.isdigit():` (the `cached_lookup_card` re-lookup) → `if set_id and num.isdigit() and not name_display_only:`

(The TCGdex fallback at `card.name is None` is already unreachable when display-only set a name — leave it.)

**(c) Rollback on a failed name cross-check.** Snapshot the card before any number-first write, and restore it when the cross-check refuses the merged identity. Place the snapshot immediately before the `if not name_resolved:` block:

```python
    # Number-first merges are provisional: if the worker ALSO read a printed
    # name and that name disagrees with the card the number resolves to, the
    # merge is self-contradictory — displaying it anyway is how a flagged cell
    # showed "Skitty" over a Mega Latias ex. Snapshot here; the cross-check
    # below decides whether the writes stand.
    before = card.model_copy() if not name_resolved else None
```

Then, after the existing `name_ok` computation (keep its log line), add the restore before the accept gate:

```python
    if before is not None and not name_ok:
        for f in type(card).model_fields:
            setattr(card, f, getattr(before, f))
        log.info("vlm.merge_rolled_back vlm=%r row=%s (number-first identity "
                 "contradicted the worker's own name read)",
                 vlm_name, getattr(card, "row_index", None))
        return False
```

Note `name_display_only` merges are *not* rolled back — their fields came from the name, which is the evidence that survived.

- [ ] **Step 4: Run the probe, verify all three cases pass**

Run: `.venv/bin/python docs/acceptance/probes/vlm-merge-name-first-probe.py`
Expected: `PASS`, exit 0.

- [ ] **Step 5: Run the binder gate**

Run: `.venv/bin/python docs/acceptance/binder_gate.py`
Expected: `GATE PASS` (the gate runs without a VLM endpoint, so it exercises none of the changed lines — this is the no-import-breakage check; the probe is the behavioral coverage).

- [ ] **Step 6: Commit**

```bash
git add app/pack/vlm_merge.py docs/acceptance/probes/vlm-merge-name-first-probe.py
git commit -m "fix(vlm-merge): an exact name read outweighs a fabricated denominator, and a rejected merge leaves no fingerprints"
```

---

### Task 2: binder_gate — score what flagged cells display

`binder_gate.py` scores a flagged cell as `flagged` and stops looking (`_score_real`, line ~196-198), so a flagged row displaying a completely wrong merged identity passes the gate. Add a `misdisplay` bucket: a flagged cell on a real page whose displayed *name* matches no truth entry for that page. Ratcheted per page like `NUMDIFF_BASELINE`, default pin 0.

**Files:**
- Modify: `docs/acceptance/binder_gate.py`

**Interfaces:**
- Consumes: nothing from other tasks (independent).
- Produces: gate output line format gains `misdisplay=<n>/<pin>`; no code interface.

- [ ] **Step 1: Add the ratchet table and scoring**

Next to `NUMDIFF_BASELINE` add:

```python
# Pinned per-page MISDISPLAY baseline: a FLAGGED cell whose displayed name is not
# on the page at all. A flagged cell is allowed to be uncertain — it is NOT
# allowed to advertise a card that isn't there (production case: a flagged cell
# displaying "Skitty" over a Mega Latias ex after a VLM merge its own guards had
# rejected). A displayed name of None/"" is fine — honest uncertainty. Ratcheted
# exactly like NUMDIFF_BASELINE; no pin means zero.
MISDISPLAY_BASELINE: dict[str, int] = {
    "page_1.jpeg": 0,
    "page_2.jpeg": 0,
    "page_3.jpeg": 0,
    "page_4.jpeg": 0,
    "page_5.jpeg": 0,
}
_MISDISPLAY_DEFAULT = 0
```

In `_score_real`, change the flagged branch (keep everything else identical):

```python
    truth_names = {e[0] for e in _real_keys(page_truth)}
    remaining = _real_keys(page_truth)
    correct = numdiff = wrong = flagged = misdisplay = 0
    lines: list[str] = []
    for c in cards:
        got_name, got_num = _real_key(c)
        if c.get("needs_review"):
            flagged += 1
            if got_name and got_name not in truth_names:
                misdisplay += 1
                verdict = "flagged(MISDISPLAY)"
            else:
                verdict = "flagged"
```

and return `misdisplay` alongside the other counts: `return correct, numdiff, wrong, flagged, misdisplay, lines`.

- [ ] **Step 2: Wire the ratchet into `main`**

At the real-page branch, unpack the new count and ratchet it exactly like numdiff:

```python
            correct, numdiff, wrong, flagged, misdisplay, lines = _score_real(cards, page_truth)
```
```python
            mpin = MISDISPLAY_BASELINE.get(name, _MISDISPLAY_DEFAULT)
            if misdisplay > mpin:
                page_fail.append(
                    f"{name}: {misdisplay} flagged cell(s) DISPLAY a card that is "
                    f"not on the page (pin {mpin}) — a flagged row must not "
                    f"advertise a wrong identity")
            elif misdisplay < mpin:
                improvements.append(
                    f"{name}: misdisplay {mpin} -> {misdisplay}; lower its pin in "
                    f"MISDISPLAY_BASELINE (docs/acceptance/binder_gate.py)")
```

Add `misdisplay` to the per-page summary line (`misdisplay={misdisplay}/{mpin}`) and a `tot_misdisplay` in the totals. Synthetic pages: pass `misdisplay, mpin = 0, None` (synthetic truth has no names — the bucket does not apply; do not touch `_score`).

- [ ] **Step 3: Run the gate, verify pass and pins**

Run: `.venv/bin/python docs/acceptance/binder_gate.py`
Expected: `GATE PASS` with `misdisplay=0/0` on every real page (the gate runs VLM-less; pass-1 flagged cells display their own OCR'd title or nothing). If any page shows a nonzero count, STOP and report DONE_WITH_CONCERNS with the cell's report line — do not silently raise a pin.

- [ ] **Step 4: Commit**

```bash
git add docs/acceptance/binder_gate.py
git commit -m "test(acceptance): a flagged cell may be uncertain, but it may not advertise a card that is not there"
```

---

### Task 3: give the VLM the number strip, magnified

The worker fabricates collector numbers because the number is ~10px tall in a full-card crop. The binder already knows exactly where the strip is (`_NUM_STRIP`, bottom 20%) and already has a proven working size for reading it (`_STRIP_RETRY_LONG = 1400`, see `_retry_strip_number`'s comment). Send that crop as an optional second image. Compatibility: an old worker ignores unknown payload fields; a new worker without the field builds today's prompt byte-identically.

**Files:**
- Modify: `app/pack/binder.py` (`_vlm_payload` only)
- Modify: `app/pack/vlm_client.py` (`identify` passthrough)
- Modify: `runpod_worker/prompts.py` (`build_prompt` gains `with_strip`)
- Modify: `runpod_worker/handler.py` (`_identify` gains the second image)

**Interfaces:**
- Consumes: `_NUM_STRIP`, `_STRIP_RETRY_LONG`, `_scale_long` (all module-level in `binder.py`), `vlm_client.jpeg_b64`.
- Produces: payload cards may carry `"strip_b64": str | None`; `build_prompt(kind, hint_set=None, hint_den=None, with_strip=False)`. Deploy note: **the RunPod worker image must be rebuilt** for the worker half to ship (existing deploy-checklist item; the halves are independently safe).

- [ ] **Step 1: Extend `build_prompt` and assert the strings**

In `runpod_worker/prompts.py`:

```python
_STRIP_ATTACH = (
    "The second image is a magnified crop of the same card's bottom strip — "
    "read the collector number from the second image, and the name and set "
    "from the first. "
)
```

```python
def build_prompt(kind: str | None,
                 hint_set: str | None = None,
                 hint_den: str | None = None,
                 with_strip: bool = False) -> str:
    """The full prompt string for one card. ``hint_set``/``hint_den`` are HINTS
    (the caller's modal-set guess) and only ever appear as trailing context —
    never as an instruction to answer with them. ``with_strip`` says a second,
    magnified bottom-strip image accompanies the card (binder cells; see
    app/pack/binder._vlm_payload) — with it, the number instruction points at
    the image the number is actually legible in."""
    k = kind if kind in _LEADS else "strip"
    hint = ""
    if hint_set or hint_den:
        hint = _HINT_LEAD[k] + \
            (f"the set '{hint_set}'. " if hint_set else "") + \
            (f"denominator {hint_den}. " if hint_den else "")
    attach = _STRIP_ATTACH if (with_strip and k == "full_card") else ""
    return _LEADS[k] + attach + _REPLY + hint
```

Update the module docstring's KINDS note to mention the optional second image. Verify from the repo root (the module's own advertised property):

Run: `.venv/bin/python -c "
from runpod_worker.prompts import build_prompt
a = build_prompt('full_card'); b = build_prompt('full_card', with_strip=True)
assert a == build_prompt('full_card', with_strip=False), 'default changed'
assert 'second image' in b and 'second image' not in a
assert build_prompt('strip', with_strip=True) == build_prompt('strip'), 'strip kind must ignore with_strip'
print('prompts ok')
"`
Expected: `prompts ok`.

- [ ] **Step 2: Handler — decode and attach the second image**

In `runpod_worker/handler.py`, change `_identify` to accept the strip and build the content list with both images:

```python
def _identify(model, processor, img: Image.Image, hint_set, hint_den,
              kind=None, strip_img: Image.Image | None = None) -> dict:
    content = [{"type": "image", "image": img}]
    if strip_img is not None:
        content.append({"type": "image", "image": strip_img})
    content.append({"type": "text",
                    "text": build_prompt(kind, hint_set, hint_den,
                                         with_strip=strip_img is not None)})
    messages = [{"role": "user", "content": content}]
```

(rest of `_identify` unchanged). In `handler`, decode the optional field so a bad strip never fails the card — the strip is an assist, not a dependency:

```python
            img = Image.open(io.BytesIO(base64.b64decode(c["image_b64"]))).convert("RGB")
            strip_img = None
            if c.get("strip_b64"):
                try:
                    strip_img = Image.open(
                        io.BytesIO(base64.b64decode(c["strip_b64"]))).convert("RGB")
                except Exception:
                    strip_img = None   # a corrupt assist must not cost the card
            res = _identify(model, processor, img, c.get("hint_set"),
                            c.get("hint_denominator"), c.get("kind"), strip_img)
```

Update the handler docstring's input contract to include `"strip_b64": str|null (optional)`.

- [ ] **Step 3: Client passthrough**

In `app/pack/vlm_client.py` `identify`, pass the field through and count its weight:

```python
        payload_cards.append({
            "row_index": c["row_index"], "image_b64": b,
            "hint_set": c.get("hint_set"), "hint_denominator": c.get("hint_denominator"),
            "kind": c.get("kind") or "strip",
            "strip_b64": c.get("strip_b64"),
        })
```

and change the byte accounting line to include it:

```python
    payload_bytes = sum(len(c["image_b64"]) + len(c.get("strip_b64") or "")
                        for c in payload_cards)
```

Update `identify`'s docstring card-shape line to mention the optional `strip_b64`.

- [ ] **Step 4: Binder payload — encode the strip up front**

In `app/pack/binder.py` `_vlm_payload`, inside the per-cell loop after the existing `b64` encode succeeds:

```python
        # The magnified bottom strip rides along as a SECOND image: the collector
        # number is ~10px tall in the full-card crop — the field the worker
        # demonstrably fabricates — while the strip at _STRIP_RETRY_LONG is the
        # geometry _retry_strip_number already proved legible. Encoded here, up
        # front, for the same reason as the card crop (see docstring).
        ch = r.crop.shape[0]
        strip = r.crop[max(0, int(ch * (1.0 - _NUM_STRIP))):]
        strip_b64 = (vlm_client.jpeg_b64(_scale_long(strip, _STRIP_RETRY_LONG))
                     if strip.size else None)
        payload.append({"row_index": c.card.row_index, "image_b64": b64,
                        "strip_b64": strip_b64,
                        "hint_set": hint.set_name if hint else None,
                        "hint_denominator": hint.denominator if hint else None,
                        "kind": "full_card"})
```

Also extend `_vlm_payload`'s docstring with one sentence about the second image. Add a line to the docstring noting the worker half requires the rebuilt image but an old worker safely ignores the field.

- [ ] **Step 5: Run the gate**

Run: `.venv/bin/python docs/acceptance/binder_gate.py`
Expected: `GATE PASS` (VLM disabled locally, so this is the no-breakage check; the prompt assertion in Step 1 is the behavioral one).

- [ ] **Step 6: Commit**

```bash
git add app/pack/binder.py app/pack/vlm_client.py runpod_worker/prompts.py runpod_worker/handler.py
git commit -m "feat(vlm): send the number strip the size we already know it reads at"
```

---

### Task 4: thumbnails off the event loop

`_finish` (`app/pack/binder.py:1237-1243`) calls `_thumb(r.crop)` synchronously per cell while building `BinderCell`s — a `cv2.resize` + JPEG encode + base64 per cell, on the event loop, untimed. Move all of them into one worker thread with a timing stage.

**Files:**
- Modify: `app/pack/binder.py` (`_finish` only)

**Interfaces:**
- Consumes: `_thumb`, `stage` (both already imported/defined in the module).
- Produces: new log line `timing.binder.thumbs` (observability only).

- [ ] **Step 1: Implement**

Replace the cell-building loop's start in `_finish`:

```python
    cells: list[BinderCell] = []
    texts_by_row: dict[int, list[str]] = {}
    # One thread hop for all cells: a thumb is cv2.resize + JPEG + base64 of a
    # full-res crop — real CPU that was silently blocking the event loop once
    # per cell, in the same request that is already OCR-bound.
    with stage("binder", "thumbs", scan_id):
        thumbs = await asyncio.to_thread(
            lambda: [_thumb(r.crop) for r in reads])
    for idx, (r, tb) in enumerate(zip(reads, thumbs)):
        texts_by_row[idx] = r.texts
        cells.append(BinderCell(cell=r.box, card=_pack_card(idx, r.res),
                                thumb_b64=tb,
                                needs_vlm=not r.res.confident))
```

- [ ] **Step 2: Run the gate with timing, verify the stage line and pass**

Run: `.venv/bin/python docs/acceptance/binder_gate.py --only page_1 --timing`
Expected: a `timing.binder.thumbs` line appears among the `timing.binder.*` output; page passes.

Run: `.venv/bin/python docs/acceptance/binder_gate.py`
Expected: `GATE PASS`.

- [ ] **Step 3: Commit**

```bash
git add app/pack/binder.py
git commit -m "perf(binder): thumbnails are CPU work, so give them a thread"
```
