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
