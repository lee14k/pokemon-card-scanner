# Visual Card Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Identify pulled cards by matching strip images against per-set reference card art via a dedicated embedding service; OCR becomes a tiebreaker.

**Architecture:** New `matcher/` FastAPI service (own container/Volume) runs a CLIP ViT-B/32 image encoder on CPU (onnxruntime), owns per-set `.npz` embedding indexes built from reference images it fetches on request. The main app enumerates a set's cards from PokéWallet into the `card` table, asks the matcher to index them, and during scans sends all strips in one batched call, fusing art matches with OCR. Matcher unreachable/unindexed ⇒ exact current behavior.

**Tech Stack:** FastAPI, onnxruntime (CPU), numpy, Pillow, httpx; existing app stack unchanged.

**Repo rule: NO automated tests.** Verification is smokes + the corpus acceptance harness (Task 11). Machine care: at most the minimum servers needed per smoke; `pkill -f "uvicorn app.main"`, `pkill -f "uvicorn matcher"`, `pkill -f pokewallet_stub` before AND after every smoke; fresh ports.

Dev env for app commands: `DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false`.

## File map

```
matcher/__init__.py            # empty
matcher/config.py              # env: MATCHER_TOKEN, INDEX_DIR, MODEL_PATH
matcher/model.py               # onnx session, letterbox preprocess, embed()
matcher/index.py               # npz save/load, cosine top-k
matcher/app.py                 # FastAPI: /health, /index/{key}, /match/{key}
matcher/requirements.txt
matcher/Dockerfile             # multi-stage: model download + runtime
scripts/fetch_matcher_model.py # local-dev model download (same pinned URL)
scripts/measure_matcher.py     # corpus acceptance harness (Task 11)
app/enumeration.py             # PokéWallet whole-set enumeration → card table
app/matcher_client.py          # main-app client; disabled when MATCHER_URL unset
app/pack/pipeline.py           # fusion (modify)
app/cards.py                   # + get_cached_by_match_ids (modify)
app/admin.py                   # + POST /admin/matcher/index/{set_id} (modify)
.env.example, .gitignore, railway.toml comment  (modify)
```

Model artifact (pinned): `https://huggingface.co/Qdrant/clip-ViT-B-32-vision/resolve/main/model.onnx` (~340MB, output dim 512). On first download compute `shasum -a 256` and pin the digest in BOTH `matcher/Dockerfile` and `scripts/fetch_matcher_model.py` (replace `PINNED_SHA256` below).

---

### Task 1: Matcher skeleton + config + model runtime

**Files:** Create `matcher/__init__.py` (empty), `matcher/config.py`, `matcher/model.py`, `matcher/requirements.txt`, `scripts/fetch_matcher_model.py`. Modify `.gitignore` (add `matcher/model/`).

- [ ] **Step 1: config**

```python
# matcher/config.py
"""Env config. The matcher is stateless besides INDEX_DIR (a mounted Volume)."""
import os

def token() -> str:
    t = os.environ.get("MATCHER_TOKEN", "").strip()
    if not t:
        raise RuntimeError("MATCHER_TOKEN is required")
    return t

def index_dir() -> str:
    return os.environ.get("INDEX_DIR", "./var/matcher-index")

def model_path() -> str:
    return os.environ.get("MODEL_PATH", "matcher/model/model.onnx")
```

- [ ] **Step 2: model runtime**

```python
# matcher/model.py
"""CLIP ViT-B/32 image encoder via onnxruntime. Embeds letterboxed 224x224
RGB images to L2-normalized float32[512] vectors."""
from __future__ import annotations

import numpy as np
import onnxruntime as ort
from PIL import Image

_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
_SIZE = 224

_session: ort.InferenceSession | None = None

def load(model_path: str) -> None:
    global _session
    _session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])

def ready() -> bool:
    return _session is not None

def _letterbox(im: Image.Image) -> np.ndarray:
    im = im.convert("RGB")
    w, h = im.size
    s = _SIZE / max(w, h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    im = im.resize((nw, nh), Image.BICUBIC)
    canvas = Image.new("RGB", (_SIZE, _SIZE), (128, 128, 128))
    canvas.paste(im, ((_SIZE - nw) // 2, (_SIZE - nh) // 2))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    arr = (arr - _MEAN) / _STD
    return arr.transpose(2, 0, 1)  # CHW

def embed(images: list[Image.Image], batch: int = 16) -> np.ndarray:
    """float32 [N, 512], L2-normalized."""
    assert _session is not None, "model not loaded"
    out: list[np.ndarray] = []
    name = _session.get_inputs()[0].name
    for i in range(0, len(images), batch):
        x = np.stack([_letterbox(im) for im in images[i:i + batch]])
        (y,) = _session.run(None, {name: x})
        out.append(y.astype(np.float32))
    v = np.concatenate(out) if out else np.zeros((0, 512), np.float32)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-8)
```

- [ ] **Step 3: requirements + model fetch script**

```
# matcher/requirements.txt
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
python-multipart>=0.0.9
onnxruntime>=1.17
numpy>=1.24.0
pillow>=10.0.0
httpx>=0.27.0
```

```python
# scripts/fetch_matcher_model.py
"""Download the pinned matcher model for local dev (Docker does its own copy)."""
import hashlib, pathlib, sys, urllib.request

URL = "https://huggingface.co/Qdrant/clip-ViT-B-32-vision/resolve/main/model.onnx"
PINNED_SHA256 = "PINNED_SHA256"  # fill in on first download (see plan header)
DEST = pathlib.Path("matcher/model/model.onnx")

def main() -> None:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        print("already present:", DEST); return
    print("downloading", URL)
    urllib.request.urlretrieve(URL, DEST)
    digest = hashlib.sha256(DEST.read_bytes()).hexdigest()
    print("sha256:", digest)
    if PINNED_SHA256 != "PINNED_SHA256" and digest != PINNED_SHA256:
        DEST.unlink(); sys.exit("sha256 mismatch — refusing model")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: verify** — `python scripts/fetch_matcher_model.py` (record the sha256 it prints; pin it in this script and Task 5's Dockerfile). Then:

```bash
.venv/bin/pip install -r matcher/requirements.txt
.venv/bin/python -c "
from PIL import Image
from matcher import model
model.load('matcher/model/model.onnx')
import numpy as np
v = model.embed([Image.new('RGB',(400,60),(200,10,10)), Image.new('RGB',(400,60),(10,200,10))])
print(v.shape, float(np.linalg.norm(v[0])), float(v[0] @ v[1]))"
```
Expected: `(2, 512) 1.0 <cosine well below 0.99>` (distinct colors ⇒ distinct vectors).

- [ ] **Step 5: commit** `feat(matcher): model runtime + config + pinned model fetch`

### Task 2: Index store

**Files:** Create `matcher/index.py`.

- [ ] **Step 1:**

```python
# matcher/index.py
"""Per-set reference index: {set_key}.npz (ids, vectors) + {set_key}.meta.json."""
from __future__ import annotations

import json, os, pathlib, time
import numpy as np

def _paths(index_dir: str, set_key: str) -> tuple[pathlib.Path, pathlib.Path]:
    safe = "".join(c for c in set_key if c.isalnum() or c in "-_")
    base = pathlib.Path(index_dir)
    return base / f"{safe}.npz", base / f"{safe}.meta.json"

def save(index_dir: str, set_key: str, ids: list[str], vectors: np.ndarray,
         source: str, failures: int) -> dict:
    npz, meta = _paths(index_dir, set_key)
    npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez(npz, ids=np.array(ids), vectors=vectors.astype(np.float32))
    info = {"set_key": set_key, "count": len(ids), "failures": failures,
            "source": source, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    meta.write_text(json.dumps(info))
    return info

def load(index_dir: str, set_key: str) -> tuple[list[str], np.ndarray] | None:
    npz, _ = _paths(index_dir, set_key)
    if not npz.exists():
        return None
    d = np.load(npz, allow_pickle=False)
    return [str(x) for x in d["ids"]], d["vectors"]

def status(index_dir: str, set_key: str) -> dict | None:
    _, meta = _paths(index_dir, set_key)
    return json.loads(meta.read_text()) if meta.exists() else None

def top_k(vectors: np.ndarray, ids: list[str], query: np.ndarray, k: int = 5) -> list[dict]:
    scores = vectors @ query  # both L2-normalized ⇒ cosine
    order = np.argsort(-scores)[:k]
    return [{"id": ids[i], "score": round(float(scores[i]), 4)} for i in order]
```

- [ ] **Step 2: verify** — `.venv/bin/python -c "import numpy as np; from matcher import index as I; v=np.eye(3,512,dtype=np.float32); I.save('/tmp/mi','sv6',['a','b','c'],v,'test',0); ids,vs=I.load('/tmp/mi','sv6'); print(I.top_k(vs,ids,v[1]))"` → top hit `{'id': 'b', 'score': 1.0}`.

- [ ] **Step 3: commit** `feat(matcher): npz index store + cosine top-k`

### Task 3: Matcher API

**Files:** Create `matcher/app.py`.

- [ ] **Step 1:**

```python
# matcher/app.py
"""Matcher service: builds per-set reference indexes and matches strip images.
Stateless besides INDEX_DIR; no database. All routes need the bearer token."""
from __future__ import annotations

import asyncio, io, logging
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from matcher import config, index, model

log = logging.getLogger("matcher")
REF_BOTTOM_FRAC = 0.14  # reference crop = bottom 14% of card art (spec)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    try:
        model.load(config.model_path())
        log.info("model loaded")
    except Exception as e:  # health reports not-ready; app treats as down
        log.error("model load failed: %r", e)
    yield

app = FastAPI(title="Card Matcher", lifespan=_lifespan)

def _auth(request: Request) -> None:
    if request.headers.get("authorization") != f"Bearer {config.token()}":
        raise HTTPException(401, "bad token")

class IndexCard(BaseModel):
    id: str
    image_url: str

class IndexRequest(BaseModel):
    cards: list[IndexCard]

@app.get("/health")
async def health() -> dict:
    return {"status": "ok" if model.ready() else "model_not_loaded"}

@app.get("/index/{set_key}", dependencies=[Depends(_auth)])
async def index_status(set_key: str) -> dict:
    info = index.status(config.index_dir(), set_key)
    if info is None:
        raise HTTPException(404, "no index")
    return info

@app.post("/index/{set_key}", dependencies=[Depends(_auth)])
async def build_index(set_key: str, req: IndexRequest) -> dict:
    if not model.ready():
        raise HTTPException(503, "model not loaded")
    if not req.cards:
        raise HTTPException(400, "no cards")
    ids: list[str] = []
    crops: list[Image.Image] = []
    failures = 0
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for c in req.cards:
            try:
                r = await client.get(c.image_url)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
                w, h = im.size
                crops.append(im.crop((0, int(h * (1 - REF_BOTTOM_FRAC)), w, h)))
                ids.append(c.id)
            except (httpx.HTTPError, UnidentifiedImageError, OSError) as e:
                failures += 1
                log.warning("index.fetch_failed id=%s err=%r", c.id, e)
            await asyncio.sleep(0.1)  # be polite to the image host
    if not ids:
        raise HTTPException(502, f"no reference images fetched ({failures} failures)")
    vectors = model.embed(crops)
    info = index.save(config.index_dir(), set_key, ids, vectors, "api", failures)
    log.info("index.built %s", info)
    return info

@app.post("/match/{set_key}", dependencies=[Depends(_auth)])
async def match(set_key: str, strips: list[UploadFile] = File(...)) -> list[list[dict]]:
    if not model.ready():
        raise HTTPException(503, "model not loaded")
    loaded = index.load(config.index_dir(), set_key)
    if loaded is None:
        raise HTTPException(404, "no index")
    ids, vectors = loaded
    images: list[Image.Image] = []
    for s in strips:
        data = await s.read()
        try:
            images.append(Image.open(io.BytesIO(data)).convert("RGB"))
        except (UnidentifiedImageError, OSError):
            images.append(Image.new("RGB", (32, 32)))  # garbage in ⇒ low scores out
    queries = model.embed(images)
    return [index.top_k(vectors, ids, q) for q in queries]
```

- [ ] **Step 2: smoke** (matcher alone, port 8181):

```bash
pkill -f "uvicorn matcher" 2>/dev/null; MATCHER_TOKEN=dev-matcher-token INDEX_DIR=./var/matcher-index nohup .venv/bin/uvicorn matcher.app:app --port 8181 >/tmp/matcher-smoke.log 2>&1 &
curl -s --retry 15 --retry-connrefused --retry-delay 1 http://127.0.0.1:8181/health          # {"status":"ok"}
curl -s http://127.0.0.1:8181/index/sv6 -H "Authorization: Bearer dev-matcher-token"          # 404 no index
curl -s -X POST http://127.0.0.1:8181/index/sv6 -H "Authorization: Bearer dev-matcher-token" -H "content-type: application/json" -d '{"cards":[{"id":"sv6-1","image_url":"https://images.pokemontcg.io/sv6/1.png"},{"id":"sv6-2","image_url":"https://images.pokemontcg.io/sv6/2.png"}]}'
# → {"set_key":"sv6","count":2,...}
curl -s http://127.0.0.1:8181/index/sv6 -H "Authorization: Bearer dev-matcher-token"          # meta json
pkill -f "uvicorn matcher"
```

- [ ] **Step 3: commit** `feat(matcher): index + match API`

### Task 4 (folded into 3): —

### Task 5: Dockerfile

**Files:** Create `matcher/Dockerfile`.

- [ ] **Step 1:**

```dockerfile
# matcher/Dockerfile — build context = repo root (Railway: root directory "/",
# dockerfile path matcher/Dockerfile), so COPY paths are repo-relative.
FROM python:3.12-slim AS model
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
ARG MODEL_URL=https://huggingface.co/Qdrant/clip-ViT-B-32-vision/resolve/main/model.onnx
ARG MODEL_SHA256=PINNED_SHA256
RUN curl -L -o /model.onnx "$MODEL_URL" && echo "$MODEL_SHA256  /model.onnx" | sha256sum -c -

FROM python:3.12-slim
WORKDIR /srv
COPY matcher/requirements.txt matcher/requirements.txt
RUN pip install --no-cache-dir -r matcher/requirements.txt
COPY matcher/ matcher/
COPY --from=model /model.onnx matcher/model/model.onnx
ENV INDEX_DIR=/data MODEL_PATH=matcher/model/model.onnx
CMD ["sh", "-c", "uvicorn matcher.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
```

Replace `PINNED_SHA256` with the digest recorded in Task 1.

- [ ] **Step 2: verify** — if Docker is available locally: `docker build -f matcher/Dockerfile -t matcher-test . && docker run --rm -e MATCHER_TOKEN=t -p 8182:8080 matcher-test` then `curl -s localhost:8182/health`; otherwise verification is Railway's build (note it in the commit message).

- [ ] **Step 3: commit** `feat(matcher): container build with pinned model`

### Task 6: Main app — set enumeration

**Files:** Create `app/enumeration.py`. Modify `app/cards.py` (add helper).

- [ ] **Step 1:**

```python
# app/enumeration.py
"""Whole-set card enumeration from PokéWallet into the card table
(source='enumerate'). Primary form: paginated search q=<set_id>. If that
returns nothing (query form unsupported), falls back to iterating numerators
1..denominator+40 through lookup_card_exact, throttled."""
from __future__ import annotations

import asyncio, logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Card
from app.db.session import async_session_maker
from app.pack.set_resolution import load_denominator_table
from app.pokewallet import (get_api_key, lookup_card_exact, make_async_client,
                            pokewallet_image_url, search_cards)

log = logging.getLogger("pokemon_scanner.enumeration")


def _norm_num(card_number: str | None) -> str | None:
    if not card_number:
        return None
    head = str(card_number).split("/")[0].strip().upper()
    return (head.lstrip("0") or "0") if head else None


async def _upsert(set_id: str, results: list[dict[str, Any]]) -> int:
    rows = []
    for c in results:
        info = c.get("card_info") or {}
        cid, num = c.get("id"), _norm_num(info.get("card_number"))
        if not cid or not num:
            continue
        rows.append(dict(
            match_id=str(cid), set_id=set_id, numerator=num,
            set_name=info.get("set_name"), name=info.get("name") or info.get("clean_name"),
            rarity=info.get("rarity"), image_url=pokewallet_image_url(cid),
            payload=c, source="enumerate",
        ))
    if not rows:
        return 0
    async with async_session_maker() as session:
        for r in rows:  # per-row upsert; enumerate never clobbers richer sources
            stmt = pg_insert(Card).values(**r).on_conflict_do_nothing(index_elements=["match_id"])
            await session.execute(stmt)
        await session.commit()
    return len(rows)


async def enumerate_set(set_id: str) -> dict:
    """Returns {"set_id", "cards": n, "method"} — cards upserted into `card`."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("POKEWALLET_API_KEY not configured")
    total, page, method = 0, 1, "search"
    async with make_async_client() as client:
        while True:  # paginated q=<set_id>
            data = await search_cards(str(set_id), limit=100, page=page,
                                      api_key=api_key, client=client)
            results = [c for c in (data.get("results") or [])
                       if str((c.get("card_info") or {}).get("set_id") or set_id) == str(set_id)]
            if not results:
                break
            total += await _upsert(set_id, results)
            pag = data.get("pagination") or {}
            if page >= int(pag.get("total_pages") or page):
                break
            page += 1
            await asyncio.sleep(0.15)
        if total == 0:  # fallback: iterate numerators
            method = "iterate"
            table = load_denominator_table()
            entry = next((s for s in table.sets if s.set_id == str(set_id)), None)
            denoms = [int(d) for d in (entry.denominators if entry else []) if str(d).isdigit()]
            top = (max(denoms) if denoms else 200) + 40
            for n in range(1, top + 1):
                try:
                    m = await lookup_card_exact(str(set_id), str(n), api_key=api_key, client=client)
                except Exception as e:
                    log.warning("enumeration.iterate_failed n=%s err=%r", n, e)
                    m = None
                if m is not None:
                    total += await _upsert(str(set_id), [m])
                await asyncio.sleep(0.15)
    log.info("enumeration.done set=%s cards=%s method=%s", set_id, total, method)
    return {"set_id": str(set_id), "cards": total, "method": method}
```

- [ ] **Step 2:** add to `app/cards.py` (below `cached_lookup_card`):

```python
async def get_cached_by_match_ids(match_ids: list[str]) -> dict[str, dict]:
    """match_id → payload for known cards; missing ids simply absent.
    DB failure degrades to {} (matching philosophy: never break a scan)."""
    if not match_ids:
        return {}
    try:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(Card.match_id, Card.payload).where(Card.match_id.in_(match_ids))
            )).all()
        return {m: p for m, p in rows}
    except Exception as e:
        log.warning("cards.by_match_ids_failed err=%r", e)
        return {}
```

- [ ] **Step 3: smoke** — extend is unnecessary; verify against the stub:
`pkill -f pokewallet_stub; PORT=9181 nohup .venv/bin/python tests/pokewallet_stub.py >/tmp/stub.log 2>&1 &` then with dev env + `POKEWALLET_BASE_URL=http://127.0.0.1:9181 POKEWALLET_API_KEY=x`:
`.venv/bin/python -c "import asyncio; from app.enumeration import enumerate_set; print(asyncio.run(enumerate_set('23876')))"` → cards ≥1 (stub set), then `psql`: `select match_id, source from card where set_id='23876';` shows `enumerate` rows (existing `lookup`/`seed` rows untouched). `pkill -f pokewallet_stub`.
NOTE: the stub's search likely already answers `q=<set_id>` (that's the app's normal query prefix); if it doesn't, extend the stub minimally to return its fixture cards for a bare set-id query — do not change its existing behaviors.

- [ ] **Step 4: commit** `feat(matcher): PokéWallet whole-set enumeration into card table`

### Task 7: Main app — matcher client

**Files:** Create `app/matcher_client.py`.

- [ ] **Step 1:**

```python
# app/matcher_client.py
"""Client for the matcher service. MATCHER_URL unset ⇒ feature off entirely.
Every failure degrades to None/False — the matcher is never load-bearing."""
from __future__ import annotations

import asyncio, logging, os
from typing import Any

import httpx

log = logging.getLogger("pokemon_scanner.matcher")


def _base() -> str | None:
    return os.environ.get("MATCHER_URL", "").strip().rstrip("/") or None


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ.get('MATCHER_TOKEN', '')}"}


def enabled() -> bool:
    return _base() is not None


async def match_strips(set_key: str, strip_jpegs: list[bytes],
                       timeout: float = 8.0) -> list[list[dict[str, Any]]] | None:
    """Top-5 [{'id','score'}] per strip, or None (disabled/unindexed/error)."""
    base = _base()
    if base is None or not strip_jpegs:
        return None
    files = [("strips", (f"s{i}.jpg", b, "image/jpeg")) for i, b in enumerate(strip_jpegs)]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/match/{set_key}", files=files, headers=_headers())
        if r.status_code == 404:
            return None  # no index yet — caller may trigger a build
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("matcher.match_failed set=%s err=%r", set_key, e)
        return None


async def build_index(set_key: str, cards: list[dict[str, str]],
                      timeout: float = 600.0) -> dict | None:
    """cards: [{'id','image_url'}]. Returns build report or None."""
    base = _base()
    if base is None or not cards:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/index/{set_key}", json={"cards": cards},
                                  headers=_headers())
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("matcher.index_failed set=%s err=%r", set_key, e)
        return None


_inflight: set[str] = set()


def kick_index_build(set_key: str) -> None:
    """Fire-and-forget: enumerate the set and build its index once."""
    if not enabled() or set_key in _inflight:
        return
    _inflight.add(set_key)

    async def _run() -> None:
        try:
            from app.cards import enumerated_cards_for_index
            cards = await enumerated_cards_for_index(set_key)
            if cards:
                await build_index(set_key, cards)
        except Exception as e:
            log.warning("matcher.kick_failed set=%s err=%r", set_key, e)
        finally:
            _inflight.discard(set_key)

    asyncio.get_running_loop().create_task(_run())
```

- [ ] **Step 2:** add to `app/cards.py`:

```python
async def enumerated_cards_for_index(set_id: str) -> list[dict]:
    """[{'id','image_url'}] for a set — enumerating from PokéWallet if the
    card table doesn't already hold the set. Used by matcher index builds."""
    from app.pokewallet import get_api_key

    async def _rows() -> list[dict]:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(Card.match_id, Card.image_url).where(Card.set_id == str(set_id))
            )).all()
        return [{"id": m, "image_url": u} for m, u in rows if u]

    try:
        rows = await _rows()
        # A handful of cache rows isn't a set: enumerate when clearly partial.
        if len(rows) < 40 and get_api_key():
            from app.enumeration import enumerate_set
            await enumerate_set(str(set_id))
            rows = await _rows()
        return rows
    except Exception as e:
        log.warning("cards.enumerated_for_index_failed set=%s err=%r", set_id, e)
        return []
```

- [ ] **Step 3: commit** `feat(matcher): main-app matcher client + index kick`

### Task 8: Pipeline fusion

**Files:** Modify `app/pack/pipeline.py`.

- [ ] **Step 1:** add imports and fusion. After the `resolutions` gather and BEFORE the PokéWallet lookups, insert:

```python
    art = await _match_art(seg, resolutions)  # None when disabled/unavailable
```

and change the card-assembly loop to consult art results. Full new/changed pipeline code:

```python
from collections import Counter

from app.matcher_client import enabled as matcher_enabled, kick_index_build, match_strips

_MATCH_ACCEPT = float(os.environ.get("PACK_MATCH_ACCEPT", "0.85"))
_MATCH_MARGIN = float(os.environ.get("PACK_MATCH_MARGIN", "0.02"))


async def _match_art(seg, resolutions) -> list[dict | None] | None:
    """Batched art match for all strips against the pack's modal set.
    Returns per-strip accepted {'id','score'} or None; None overall when the
    matcher is disabled, unindexed (build kicked), or errored."""
    if not matcher_enabled():
        return None
    set_ids = [r.set_id for r in resolutions if r.set_id]
    if not set_ids:
        return None
    modal_set = Counter(set_ids).most_common(1)[0][0]
    jpegs = []
    for s in seg.strips:
        ok, buf = cv2.imencode(".jpg", s.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        jpegs.append(buf.tobytes() if ok else b"")
    results = await match_strips(str(modal_set), jpegs)
    if results is None:
        kick_index_build(str(modal_set))
        return None
    out: list[dict | None] = []
    for ranked in results:
        if (ranked and ranked[0]["score"] >= _MATCH_ACCEPT
                and (len(ranked) < 2 or ranked[0]["score"] - ranked[1]["score"] >= _MATCH_MARGIN)):
            out.append(ranked[0])
        else:
            out.append(None)
    log.info("pipeline.art_match set=%s accepted=%s/%s", modal_set,
             sum(1 for a in out if a), len(out))
    return out
```

In `scan_pack`, after `resolutions = ...`:

```python
    art = await _match_art(seg, resolutions)
    art_ids = [a["id"] for a in (art or []) if a]
    from app.cards import get_cached_by_match_ids
    art_payloads = await get_cached_by_match_ids(art_ids) if art_ids else {}
```

Change the assembly loop: for each row, if `art` has an accepted match with a known payload, the art result is authoritative:

```python
    for i, (strip, reading, res, match) in enumerate(zip(seg.strips, readings, resolutions, matches)):
        art_hit = art[i] if art else None
        payload = art_payloads.get(art_hit["id"]) if art_hit else None
        if art_hit and payload:
            info = payload.get("card_info") or {}
            art_num = str(info.get("card_number") or "")
            ocr_num = _display_number(reading.numerator, reading.denominator, reading.prefix)
            agrees = bool(ocr_num) and ocr_num.split("/")[0].lstrip("0") == art_num.split("/")[0].lstrip("0")
            conf = 0.97 if agrees else max(0.9 * art_hit["score"], 0.75)
            reason = None if agrees or not ocr_num else "art_ocr_disagree"
            cards.append(PackCard(
                row_index=strip.row_index,
                card_number=art_num or ocr_num,
                set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
                confidence=round(conf, 3), low_confidence_reason=reason,
                **card_fields_from_match(payload),
            ))
            continue
        conf, reason = score_card(reading, res, match is not None)
        cards.append(PackCard(  # unchanged OCR-first path
            row_index=strip.row_index,
            card_number=_display_number(reading.numerator, reading.denominator, reading.prefix),
            set_id=res.set_id, set_code=res.set_code, set_name=res.set_name,
            confidence=conf, low_confidence_reason=reason,
            **card_fields_from_match(match),
        ))
```

(`import os` is already present in pipeline.py via existing code? It is NOT — add it to the imports.)

- [ ] **Step 2: verify** — with `MATCHER_URL` unset, the full fixture + corpus scans behave EXACTLY as before (run both, compare rows/nums/code to current outputs). Suite: `pytest tests/ -x -q` still green.

- [ ] **Step 3: commit** `feat(scanner): art-primary fusion with OCR tiebreak`

### Task 9: Admin pre-warm endpoint

**Files:** Modify `app/admin.py`.

- [ ] **Step 1:** add (following the existing admin route style + `CurrentAdmin` dep):

```python
@router.post("/admin/matcher/index/{set_id}")
async def build_matcher_index(set_id: str, admin: CurrentAdmin) -> dict:
    """Enumerate a set and (re)build its matcher index. Synchronous; admin-only."""
    from app.cards import enumerated_cards_for_index
    from app.matcher_client import build_index, enabled

    if not enabled():
        raise HTTPException(503, "MATCHER_URL not configured")
    cards = await enumerated_cards_for_index(set_id)
    if not cards:
        raise HTTPException(502, "no reference cards available for set")
    report = await build_index(set_id, cards)
    if report is None:
        raise HTTPException(502, "matcher index build failed")
    return report
```

(Match the existing router's actual prefix — if the router mounts at `/admin`, the path here is `/matcher/index/{set_id}`. Check `app/admin.py` first.)

- [ ] **Step 2: commit** `feat(matcher): admin index pre-warm endpoint`

### Task 10: Env, docs, deploy notes

**Files:** Modify `.env.example`; `.gitignore` (`matcher/model/`, `var/matcher-index/` if not covered).

- [ ] **Step 1:** append to `.env.example`:

```
# --- Visual matcher (sub-project H) ---
# Main app: URL of the matcher service; UNSET disables art matching entirely.
# Railway: second service from this repo (Dockerfile matcher/Dockerfile, root "/"),
# Volume mounted at /data, private networking → http://<service>.railway.internal:8080
MATCHER_URL=
MATCHER_TOKEN=change-me-shared-bearer-token
# Fusion thresholds (cosine): accept art match at/above ACCEPT with top1-top2 margin
PACK_MATCH_ACCEPT=0.85
PACK_MATCH_MARGIN=0.02
```

- [ ] **Step 2: commit** `docs(matcher): env + second-service deploy notes`

### Task 11: Corpus acceptance harness (FOREGROUND — do not delegate)

**Files:** Create `scripts/measure_matcher.py`.

- [ ] **Step 1:** harness — builds a TWM index in a running local matcher from public reference images (`images.pokemontcg.io/sv6/{1..226}.png` — acceptance-only source; the app path still uses the PokéWallet seam), extracts the 11 corpus strips via the real pipeline, matches, and scores against ground truth:

```python
# scripts/measure_matcher.py
"""Acceptance: corpus pack vs a locally built TWM reference index.
Usage: MATCHER_TOKEN=... python scripts/measure_matcher.py http://127.0.0.1:8181
Requires: matcher running, tests/corpus/IMG_7102.heic present."""
import asyncio, sys

import httpx

TRUTH = ["10", "126", "101", "45", "143", "122", "79", "66", "78", "96", None]  # None = energy card (not in sv6)

async def main(base: str) -> None:
    import cv2
    from app.pack.pipeline import _decode
    from app.pack.segmentation import find_strips
    import os
    headers = {"Authorization": f"Bearer {os.environ['MATCHER_TOKEN']}"}
    async with httpx.AsyncClient(timeout=900.0) as client:
        cards = [{"id": f"sv6-{n}", "image_url": f"https://images.pokemontcg.io/sv6/{n}.png"}
                 for n in range(1, 227)]
        r = await client.post(f"{base}/index/sv6", json={"cards": cards}, headers=headers)
        print("index build:", r.status_code, r.text[:200])
        img = _decode(open("tests/corpus/IMG_7102.heic", "rb").read())
        seg = find_strips(img, None)
        files = []
        for i, s in enumerate(seg.strips):
            ok, buf = cv2.imencode(".jpg", s.image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            files.append(("strips", (f"s{i}.jpg", buf.tobytes(), "image/jpeg")))
        r = await client.post(f"{base}/match/sv6", files=files, headers=headers)
        r.raise_for_status()
        correct = 0
        for i, ranked in enumerate(r.json()):
            want = TRUTH[i] if i < len(TRUTH) else None
            got = ranked[0]["id"].split("-")[1] if ranked else "?"
            hit = want is not None and got == want
            correct += hit
            print(f"row {i}: want={want} top1={ranked[0]['id']}@{ranked[0]['score']}"
                  f" top2={ranked[1]['id']}@{ranked[1]['score']} {'✓' if hit else '✗'}")
        print(f"top-1 accuracy: {correct}/{sum(1 for t in TRUTH if t)}")

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: run** (matcher on 8181, machine-care pkills around it). **Gate: ≥9/10 TWM strips correct top-1.** If below: tune `REF_BOTTOM_FRAC` (try 0.12/0.16), strip jpeg quality, and thresholds; record final numbers in the commit message. Verify TRUTH row order against the actual `find_strips` output order before judging (rows sort top→bottom; adjust TRUTH if segmentation orders differently).

- [ ] **Step 3: full-stack smoke** — app (8172) + matcher (8181) + pokewallet stub (9172), guided fixture scan and corpus upload through `POST /scan/pack` with `MATCHER_URL=http://127.0.0.1:8181` set; confirm art-matched rows carry names + high confidence and `MATCHER_URL` unset reproduces current behavior. pkill all three after.

- [ ] **Step 4: commit** `feat(matcher): corpus acceptance harness + calibration results`

## Self-review notes

- Spec coverage: service+model (T1–5), enumeration/seam (T6), client+lazy build (T7), fusion+thresholds+reasons (T8), admin (T9), env/deploy (T10), acceptance+regression+memory-by-inspection (T11). Failure modes are embedded in each component's error paths.
- Type consistency: `match_strips` returns `list[list[{'id','score'}]]`; fusion consumes `art[i]['id']/['score']`; `enumerated_cards_for_index` returns the exact `{'id','image_url'}` shape `build_index` posts.
- Known deferred item: real-API validation of `q=<set_id>` enumeration happens on prod (no local key); the iterate fallback covers failure.
