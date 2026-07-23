# Binder Page Scan → Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Under fable-opus mode, the controller dispatches each task to an Opus implementer with this plan's task text as the zero-decision brief.

**Goal:** Scan a binder page (many cards, one photo) into a new per-trainer Collection with qty-aware upserts, grid review, and prices.

**Architecture:** One whole-photo RapidOCR pass → geometric gap-clustering into cells → the live-identify ladder (extracted to a shared core) per cell with no page prior → contour-refined cell crops for thumbnails/VLM → authed scan + collection CRUD APIs → grid review UI + Collection view.

**Tech Stack:** existing FastAPI/SQLAlchemy async/Alembic backend, RapidOCR + OpenCV, React/TS frontend. No new dependencies.

## Global Constraints

- **NO automated test additions** (standing rule). Verification = the smoke commands per task. Existing suite must stay green: `.venv/bin/python -m pytest tests/ -q` → `7 passed, 1 skipped`.
- Backend env (plain exports): `export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false`. Dev user `tduser@x.io` / `trainerpass1`.
- opencv pin `opencv-python>=4.8,<5`; never install another opencv distribution.
- Machine care: `pkill -f "uvicorn app.main"` before/after server smokes; one server at a time; `caffeinate -i` for long jobs.
- Commit to local `main`; the USER pushes. Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Frontend: `cd frontend && npx tsc --noEmit` must be clean; Vite dev proxies `/api`.
- Staircase and live flows must be byte-identical when binder mode is unused.

---

### Task 1: `detect_lines_xy` (x-aware whole-photo OCR)

**Files:**
- Modify: `app/pack/rapidocr_reader.py`

**Interfaces:**
- Produces: `detect_lines_xy(img_bgr: np.ndarray, cap: int = 2600) -> list[tuple[float, float, str, float, float, float]]` — per detected line `(x_center, y_center, TEXT_UPPER, conf, box_w, box_h)`, all coords scaled back to SOURCE pixels. `[]` on failure.
- `detect_lines` becomes a wrapper: `[(y, t, c) for _x, y, t, c, _w, _h in detect_lines_xy(img_bgr, cap)]` — existing callers see identical behavior.

- [ ] **Step 1:** Implement by copying the current `detect_lines` body: same engine call, same cap/scale logic; from each result `box` (4 points) compute `x = mean(p[0])/scale`, `y = mean(p[1])/scale`, `w = (max(p[0])-min(p[0]))/scale`, `h = (max(p[1])-min(p[1]))/scale`. Keep the try/except + logging idiom exactly (`rapidocr.detect_failed`).
- [ ] **Step 2:** Verify equivalence + new fields:

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs
.venv/bin/python - <<'EOF'
import warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
from app.pack.pipeline import _decode
from app.pack.rapidocr_reader import detect_lines, detect_lines_xy
img = _decode(open("tests/corpus/IMG_7102.heic","rb").read())
a = detect_lines(img); b = detect_lines_xy(img)
assert [(y,t,c) for y,t,c in a] == [(y,t,c) for x,y,t,c,w,h in b], "wrapper drift"
print("lines:", len(b), "| first with x:", b[0][:4], "| all boxes have w>0:", all(r[4]>0 for r in b))
EOF
```
Expected: same line count as before, x/w populated, no drift assertion.
- [ ] **Step 3:** Suite green; commit `feat(ocr): detect_lines_xy — expose x/box geometry from the whole-photo pass`.

---

### Task 2: Extract the identify ladder to `identify_core`

**Files:**
- Create: `app/pack/identify_core.py`
- Modify: `app/pack/live_identify.py` (delegate to the core; behavior identical)

**Interfaces:**
- Produces:
  - `@dataclass SessionPrior(set_id: str | None, set_name: str | None, denominator: str | None)` — MOVED here from live_identify; live_identify re-exports it (`from app.pack.identify_core import SessionPrior`) so `app/pack/live_api.py` and `app/pack/live_session.py` imports keep working unchanged.
  - `@dataclass IdentityResult(confident: bool, numerator: str | None, display_number: str | None, set_id: str | None, set_code: str | None, set_name: str | None, fields: dict, low_confidence_reason: str | None, identity_key: str, name_match_score: float | None)` — `fields` = `{name, rarity, image_url, match_id}`.
  - `async resolve_identity(name_texts: list[tuple[str, float]], reading: NumberReading | None, prior: SessionPrior | None) -> IdentityResult` — `name_texts` = candidate title-band lines as `(text, conf)`, best-first.
- Consumes: `get_name_index()` + `match`/`match_in_set` (name_index.py), `get_set_numerators`, `cached_lookup_card`, `card_fields_from_match`, `get_api_key`, `load_denominator_table`.

- [ ] **Step 1:** Move the ladder VERBATIM from `live_identify.identify_frame` (everything between the OCR calls and the PackCard construction — name match loop incl. the set-scoped fallback from commit dec10a2, den derivation, confident ladder, `_pw_set_id_for`, lookup + tcgdex-name fill, display_number, stage-accurate reason from commit a27e566, identity key) into `resolve_identity`. `name_texts` replaces the `name_lines` loop input (already `(text, conf)` sorted best-first — the y sort stays in the caller). `normalize_key` moves too.
- [ ] **Step 2:** Rewrite `identify_frame` to: QR check (unchanged) → two `detect_lines` band passes (unchanged) → build `name_texts = [(t, c) for _y, t, c in sorted(name_lines, key=lambda t: -t[2])]` and the best `pattern_ok` reading (unchanged) → `res = await resolve_identity(name_texts, reading, prior)` → construct `PackCard`/`FrameResult` from `res` exactly as today (confidence 0.9/0.3, needs_review=not confident, kind/needs_vlm logic, `no_card`/`unreadable` short-circuits BEFORE calling the core, as today).
- [ ] **Step 3:** Behavior-identical gate — run the reel fixtures and diff against the pre-change output:

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs PHOTO_STORAGE_DIR=./var/pulls
caffeinate -i .venv/bin/python - <<'EOF'
import asyncio, cv2, glob, warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)
from app.pack.live_identify import identify_frame, SessionPrior
async def main():
    for p in sorted(glob.glob("tests/corpus/reel/*.png")):
        im=cv2.imread(p); h=im.shape[0]
        r=await identify_frame(im, im[int(h*0.75):], SessionPrior(None,None,"86"))
        print(p.split('/')[-1], "->", r.kind, r.card and (r.card.card_number, r.card.name, r.card.low_confidence_reason))
asyncio.run(main())
EOF
```
Expected EXACTLY (from the current code's output): steady_1 unreadable, steady_2 unreadable, steady_3 card with name Bronzong, steady_4 unreadable. Any drift = refactor bug, fix before proceeding.
- [ ] **Step 4:** Suite green; commit `refactor(identify): extract shared resolve_identity core (live behavior identical)`.

---

### Task 3: Collection model + migration 0009

**Files:**
- Create: `alembic/versions/0009_collection.py`
- Modify: `app/db/models.py` (append `CollectionCard`)

**Interfaces:**
- Produces model `CollectionCard` (table `collection_card`): `id` (UUID pk, `uuid.uuid4` default, same idiom as existing models) · `trainer_id` (UUID FK `trainer.id`, ondelete CASCADE, indexed) · `tcgdex_card_id` Text nullable · `set_id` Text nullable · `set_code` Text nullable · `set_name` Text nullable · `card_number` Text nullable · `numerator` Text nullable · `name` Text nullable · `image_url` Text nullable · `match_id` Text nullable · `identity_key` Text NOT NULL · `qty` Integer NOT NULL server_default "1" · `created_at`/`updated_at` timestamptz (existing model idiom, updated_at onupdate) · UniqueConstraint `("trainer_id", "identity_key", name="uq_collection_trainer_identity")` · Index `ix_collection_card_trainer_id`.

- [ ] **Step 1:** Read `alembic/versions/0008_training_data.py` + an existing model class for the exact idiom (revision chaining, server defaults, timestamptz type used in this repo); write model + migration to match it precisely.
- [ ] **Step 2:** `alembic upgrade head` against dev DB; verify `\d collection_card` shape via a one-liner query; `alembic downgrade -1` then `upgrade head` round-trip works.
- [ ] **Step 3:** Suite green; commit `feat(collection): CollectionCard model + migration 0009`.

---

### Task 4: `card_crop.refine_card_box`

**Files:**
- Create: `app/pack/card_crop.py`

**Interfaces:**
- Produces: `refine_card_box(img_bgr: np.ndarray, coarse: tuple[int, int, int, int]) -> tuple[int, int, int, int]` — `(x, y, w, h)` in source pixels. Searches within the coarse box expanded 1.15× (clamped to image); Canny(50, 150) on grayscale → `cv2.findContours(RETR_EXTERNAL, CHAIN_APPROX_SIMPLE)` → keep contours whose `cv2.approxPolyDP(peri*0.02)` has 4–6 points, whose `cv2.minAreaRect` box has aspect `min(w,h)/max(w,h)` in `[0.63, 0.80]` and area ≥ 40% of the search region → pick largest; return its upright bounding rect intersected with the image. ANY failure/no candidate → return `coarse` unchanged. Pure function, no logging above debug, no exceptions escape.

- [ ] **Step 1:** Implement (~45 lines, module docstring notes: first quad-detection code in the repo, deliberately local-scope + hard fallback per spec).
- [ ] **Step 2:** Verify on a synthetic: draw a white 630×880 rectangle at (200, 150) on a 1600×1200 gray canvas, call with coarse `(150, 100, 750, 1000)` → returned box within ±15px of the true rect on every edge; and with a blank canvas → returns coarse verbatim. Print both.
- [ ] **Step 3:** Suite green; commit `feat(binder): local contour refinement of card crops (glare-safe fallback)`.

---

### Task 5: Binder pipeline + synthetic fixture + gate

**Files:**
- Create: `app/pack/binder.py`
- Create: `scripts/make_binder_fixture.py`
- Create: `tests/corpus/binder/` (synthetic fixture output, committed)

**Interfaces:**
- Consumes: `_decode` (pipeline), `detect_lines_xy` (Task 1), `resolve_identity`/`SessionPrior` (Task 2), `parse_number` (ocr), `refine_card_box` (Task 4), `vlm_client` + `apply_vlm_answer` + `load_denominator_table` (existing), `latest_price_map` (prices).
- Produces:
  - `@dataclass BinderCell(cell: tuple[int, int, int, int], card: PackCard, thumb_b64: str | None, needs_vlm: bool)`
  - `async scan_binder_page(page_bytes: bytes) -> dict` returning `{"cards": [BinderCell-shaped dicts with PackCard fields inlined + "cell" + "thumb_b64"], "grid": {"rows": int, "cols": int}, "page_confidence": float}` — raises `ValueError("no_cards_found")` when zero text lines survive.

**Algorithm (implement exactly):**
1. `img = _decode(page_bytes)`; None → `ValueError("no_cards_found")`. Detection cap 2800.
2. `lines = detect_lines_xy(img, cap=2800)`; drop lines with `conf < 0.5` or fewer than 2 characters; empty → `ValueError("no_cards_found")`.
3. Columns: sort lines by x; walk sorted x-centers, new column when `x - prev_x > 0.08 * img_width`. Cells: within each column sort by y; new cell when `y - prev_y > 0.12 * img_height`. `cols = n_columns`, `rows = max(len(cells_in_col) for col)`.
4. Per cell: `reading` = highest-conf `pattern_ok` `parse_number(text, conf)` over member lines; `name_texts` = the OTHER member lines as `(text, conf)` sorted by y ascending THEN conf descending (title is the top line of a card). `res = await resolve_identity(name_texts, reading, prior=None)`.
5. Cell box: bbox of member line boxes (using x, y, w, h) → expand to `0.92 *` median column pitch wide and `0.92 *` median row pitch tall, centered on the bbox centroid (single column/row: expand width to `bbox_w * 1.6`, height to `width * 88/63`), clamp to image → `refine_card_box(img, coarse)`.
6. Thumb: crop refined box, resize to width 240 (aspect preserved), `cv2.imencode(".jpg", crop, [IMWRITE_JPEG_QUALITY, 70])` → base64 str; encode failure → None.
7. PackCard per cell (same construction values as live: row_index = reading order left-to-right top-to-bottom re-assigned 0..n-1, confidence 0.9/0.3, needs_review=not confident, stage-accurate reason from `res`).
8. VLM pass (synchronous): if `vlm_client.enabled()` and any needs_review: one `vlm_client.identify` batch with `{"row_index", "image": refined crop ndarray, "hint_set": None, "hint_denominator": None}` → `apply_vlm_answer(card, ans, table)` per answered card (table = `load_denominator_table()`).
9. Prices: `latest_price_map` once; fill `price_usd_low/high` on cards with match_id.
10. `page_confidence = pack_confidence([c.confidence for c in cards])` (reuse from confidence.py).

- [ ] **Step 1:** `scripts/make_binder_fixture.py` — builds `tests/corpus/binder/synthetic_3x3.jpg`: downloads 9 TCGdex card images spanning ≥3 sets (hardcode this exact list of ids: sv06 126, sv06 101, me05 004, me05 010, me02.5 001, me02.5 003, sv03.5 025, swsh12 040, me01 020; URL pattern `https://assets.tcgdex.net/en/<series>/<set>/<lid3>/high.png` — derive series dir from the set id prefix, e.g. sv06→sv, me05→me, swsh12→swsh; skip cleanly with a message if offline), pastes them in a 3×3 grid on a dark-gray 2400×3200 canvas with 60px gutters, saves JPEG q88. Also writes `tests/corpus/binder/truth.json` `{"cards": [{"set": ..., "local_id": ...} × 9]}`.
- [ ] **Step 2:** Implement `binder.py` per the algorithm.
- [ ] **Step 3:** Gate:

```bash
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs PHOTO_STORAGE_DIR=./var/pulls
.venv/bin/python scripts/make_binder_fixture.py
caffeinate -i .venv/bin/python - <<'EOF'
import asyncio, json, warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.WARNING)
from app.pack.binder import scan_binder_page
async def main():
    r = await scan_binder_page(open("tests/corpus/binder/synthetic_3x3.jpg","rb").read())
    print("grid:", r["grid"], "cards:", len(r["cards"]))
    for c in r["cards"]:
        print(f"  [{c['row_index']}] {c['card_number']} | {c['set_name']} | {c['name']} | review={c['needs_review']} thumb={'y' if c['thumb_b64'] else 'n'}")
asyncio.run(main())
EOF
```
Gate (spec acceptance 1–2 interim): grid == 3×3; ≥7/9 cells identified confidently AND correctly vs truth.json; the remainder flagged (never silently wrong); all cells have thumbs. If below gate: STOP and debug (systematic-debugging) — clustering thresholds and name-band ordering are the likely knobs; do not lower the gate.
- [ ] **Step 4:** Suite green; commit `feat(binder): page pipeline — cluster, identify, refine, thumbs, VLM, prices + synthetic fixture`.

---

### Task 6: API — `/scan/binder` + `/collection` CRUD

**Files:**
- Create: `app/collection.py` (router: scan + CRUD; schemas inline per repo idiom or in `app/schemas.py` — follow where PullOut lives)
- Modify: `app/main.py` (include router)

**Interfaces:**
- Consumes: `scan_binder_page` (Task 5), `CollectionCard` (Task 3), `CurrentTrainer` (app/db/users.py), `latest_price_map` + `midpoint` (app/prices.py), `_compute_encounters`-equivalent species helper (read `app/pulls.py` and reuse its species normalizer exactly — import, don't copy), `load_denominator_table` (SetEntry.tcgdex_id for derivation).
- Produces routes:
  - `POST /scan/binder` — authed; multipart `page: UploadFile`; decode errors/`ValueError("no_cards_found")` → 422 `{"detail": "no_cards_found"}` → else `scan_binder_page` result as JSON.
  - `POST /collection` — authed; JSON body `{"cards": [PackCard-shaped dicts]}`; per card derive server-side: `identity_key = f"{set_code or set_name or '?'}:{numerator or normalized_name}"` (import `normalize_name` from name_index for the name arm; numerator = card_number's numerator part lstripped of zeros) and `tcgdex_card_id = f"{tdx}-{numerator.zfill(3)}"` where `tdx = entry.tcgdex_id or entry.set_code` for the denominator-table entry matching the card's set_id/set_code (None when unresolvable). Upsert: `INSERT ... ON CONFLICT (trainer_id, identity_key) DO UPDATE SET qty = collection_card.qty + 1, updated_at = now()` (use `sqlalchemy.dialects.postgresql.insert`). Response `{"added": n_new, "incremented": n_bumped, "total_cards": total_rows_for_trainer, "encounters": [...]}` — encounters from the card names via the pulls species helper.
  - `GET /collection` — authed → `{"cards": [row fields + price_usd_low/high + estimated_value_each (midpoint)], "total_qty": sum(qty), "estimated_value": sum(midpoint*qty, skipping unpriced) or None, "priced_as_of": iso|None}`, sorted `(set_code, numerator-as-int-when-digit)`.
  - `PATCH /collection/{id}` — authed owner; body `{"qty": int}`; 422 if qty < 1; 404 foreign/missing.
  - `DELETE /collection/{id}` — authed owner; 204; 404 foreign/missing.

- [ ] **Step 1:** Implement router + register in main.py (before StaticFiles mount, same block as other routers).
- [ ] **Step 2:** curl smoke:

```bash
pkill -f "uvicorn app.main"; sleep 1
export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false
nohup .venv/bin/uvicorn app.main:app --port 8000 >/tmp/binder.log 2>&1 & sleep 4
curl -s -c /tmp/cj -X POST http://127.0.0.1:8000/auth/jwt/login -d "username=tduser@x.io&password=trainerpass1" -H "content-type: application/x-www-form-urlencoded" >/dev/null
curl -s -b /tmp/cj -F "page=@tests/corpus/binder/synthetic_3x3.jpg" http://127.0.0.1:8000/scan/binder | python3 -c "import sys,json; d=json.load(sys.stdin); print('scan grid', d['grid'], 'cards', len(d['cards']))"
CARDS=$(curl -s -b /tmp/cj -F "page=@tests/corpus/binder/synthetic_3x3.jpg" http://127.0.0.1:8000/scan/binder | python3 -c "import sys,json; print(json.dumps({'cards': json.load(sys.stdin)['cards']}))")
curl -s -b /tmp/cj -X POST http://127.0.0.1:8000/collection -H "content-type: application/json" -d "$CARDS" | python3 -m json.tool
curl -s -b /tmp/cj -X POST http://127.0.0.1:8000/collection -H "content-type: application/json" -d "$CARDS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['added']==0 and d['incremented']>0, d; print('re-save increments qty OK')"
curl -s -b /tmp/cj http://127.0.0.1:8000/collection | python3 -c "import sys,json; d=json.load(sys.stdin); print('collection rows', len(d['cards']), 'total_qty', d['total_qty'], 'value', d['estimated_value'])"
curl -s -o /dev/null -w "no-auth scan -> %{http_code}\n" -X POST http://127.0.0.1:8000/scan/binder
pkill -f "uvicorn app.main"
```
Expected: scan 3×3; first save adds rows; second save `added=0, incremented>0` and qty=2 visible; GET sorted with totals; unauth 401.
- [ ] **Step 3:** Suite green; commit `feat(collection): binder scan + collection CRUD endpoints`.

---

### Task 7: Frontend — binder capture, grid review, mode chooser

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/capture/BinderCapture.tsx`
- Create: `frontend/src/review/BinderReview.tsx`
- Modify: `frontend/src/App.tsx` (mode chooser + binder flow steps)

**Interfaces:**
- api.ts adds (follow existing fetch conventions; all `credentials: "include"`):

```typescript
export interface BinderCard extends PackCard { cell: [number, number, number, number]; thumb_b64: string | null;
  price_usd_low?: number | null; price_usd_high?: number | null }
export interface BinderScan { cards: BinderCard[]; grid: { rows: number; cols: number }; page_confidence: number }
export interface CollectionSaveOut { added: number; incremented: number; total_cards: number; encounters: Encounter[] }
export async function scanBinder(page: Blob): Promise<BinderScan>;           // 422 -> throw {code:"no_cards_found"}
export async function saveToCollection(cards: BinderCard[]): Promise<CollectionSaveOut>;
```
- `BinderCapture` props `{ onDone: (photo: Blob) => void }` — clone StaircaseCapture's structure (CameraCapture + upload fallback), copy: "Lay the page flat, fill the frame, avoid glare."
- `BinderReview` props `{ scan: BinderScan; onConfirm: (cards: BinderCard[]) => void; onRetake: () => void }` — CSS grid `grid-template-columns: repeat(cols, 1fr)`; each cell: `<img src={"data:image/jpeg;base64," + thumb_b64}>` (placeholder div when null), name/card_number/set_name, price when present, flag styling reusing CardRow's REASON_TEXT patterns; tap any cell → existing `FixCardForm` (preserve row_index + cell + thumb on apply); confirm enabled always (flags don't block — same semantics as live); "no_cards_found" state on scan error with Retake.
- App.tsx: the scan mode chooser (from sub-project L) gains third option **"Binder page"** → steps `{name:"binder_capture"}` → scanBinder → `{name:"binder_review", scan}` → saveToCollection → `{name:"binder_summary", out: CollectionSaveOut}` (shows added/incremented, new dex species, link to Collection view) → done. Auth required before save (reuse the AuthForms modal pattern doSave uses).

- [ ] **Step 1:** Implement all four files. `npx tsc --noEmit` clean.
- [ ] **Step 2:** Browser click-through (desktop, Vite + backend running): chooser shows three modes; Binder → upload the synthetic fixture → grid review 3×3 with thumbs → fix one cell via FixCardForm → confirm → summary shows counts. Screenshot-level verification acceptable (no Playwright additions).
- [ ] **Step 3:** Commit `feat(binder): capture + grid review + mode chooser wiring`.

---

### Task 8: Frontend — Collection view + acceptance + docs

**Files:**
- Create: `frontend/src/collection/Collection.tsx`
- Modify: `frontend/src/App.tsx` (nav + view), `frontend/src/api.ts` (getCollection, patchCollectionQty, deleteCollectionCard)

**Interfaces:**
- api.ts adds:

```typescript
export interface CollectionCardOut { id: string; set_code: string | null; set_name: string | null;
  card_number: string | null; name: string | null; image_url: string | null; qty: number;
  price_usd_low?: number | null; price_usd_high?: number | null }
export interface CollectionOut { cards: CollectionCardOut[]; total_qty: number;
  estimated_value: number | null; priced_as_of: string | null }
export async function getCollection(): Promise<CollectionOut>;
export async function patchCollectionQty(id: string, qty: number): Promise<void>;
export async function deleteCollectionCard(id: string): Promise<void>;
```
- `Collection.tsx`: header totals (distinct cards, total qty, estimated value + priced_as_of); card grid (image_url img with name fallback, name, card_number · set_name, qty badge, price or "—"); per-card qty stepper (− disabled at 1 → PATCH; ✕ with confirm → DELETE, optimistic update with reload on error). Nav gains "Collection" in App.tsx's view union + nav buttons (match existing nav idiom).

- [ ] **Step 1:** Implement; `npx tsc --noEmit` clean; browser click-through: Collection shows saved cards with qty=2 from Task 6's double-save, stepper and delete work.
- [ ] **Step 2:** Full acceptance sweep (spec §Acceptance): re-run Task 5's gate script (fixture identify ≥7/9 + flags); Task 6's re-save-increments smoke; suite `7 passed, 1 skipped`; `npx tsc --noEmit` clean. Record all outputs in the commit message.
- [ ] **Step 3:** Docs: append a "Binder scan → Collection" paragraph to `docs/training-runbook.md`'s data-sources section noting binder scans do NOT feed training harvest or pull stats (Collection ≠ pulls) and that `tests/corpus/binder/` holds the synthetic gate fixture until real binder photos land (then: drop them in, re-run the Task 5 gate against each, expected = every visible card identified-or-flagged).
- [ ] **Step 4:** Commit `feat(collection): collection view + acceptance sweep + docs`.

---

## Execution notes

- Order: 1 → 2 → (3, 4 parallelizable) → 5 → 6 → 7 → 8. Tasks 3/4 touch disjoint files and may run concurrently under fable-opus's ≤3-agent cap; everything else is sequential.
- Task 14 of sub-project L (RunPod e2e verify) remains open separately; binder's VLM pass inherits whatever endpoint state exists — binder tasks must not block on it.
- If the synthetic-fixture gate keeps failing on clustering thresholds, the knobs are the 0.08/0.12 gap fractions — tune ONLY with evidence from printed cluster geometry, and record the final values in the commit.
