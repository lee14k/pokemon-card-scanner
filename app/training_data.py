"""Admin training-data API: staircase intake (any row count), paste-JSON labeling,
label templates, pool/reference/synthetic browsing, eval runs. Admin-only.

Storage mirrors app/storage.py: photos land under
<PHOTO_STORAGE_DIR>/training/<photo_id>/photo.jpg with per-row strip_<row>.jpg
siblings — server-generated UUID path segments only, no traversal surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path

import cv2
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.config import db_settings
from app.db.models import (Card, SetIdMap, TcgdexCard, TcgdexSet, TrainingPhoto,
                           TrainingStrip)
from app.db.session import async_session_maker
from app.db.users import CurrentAdmin
from app.pack.pipeline import _decode  # shared decode: EXIF transpose + HEIC support
from app.pack.segmentation import find_strips
from app.pack.set_resolution import SetEntry, load_denominator_table
from app.storage import open_photo

log = logging.getLogger("pokemon_scanner.training_data")
router = APIRouter(prefix="/admin/training", tags=["training"])

_MAX_UPLOAD = 15 * 1024 * 1024
_TIERS = ("standard", "stress")
_SPLITS = ("train", "test")


# ── Storage helpers (pattern: app/storage.py, training/ prefix) ──────────────
def _root() -> Path:
    return Path(db_settings().photo_storage_dir)


def _save_training_photo(
    photo_id: uuid.UUID, photo_jpg: bytes, strips: list[tuple[int, bytes]]
) -> tuple[str, list[tuple[int, str]]]:
    """Write photo + per-row strips; returns storage-root-relative paths."""
    d = _root() / "training" / str(photo_id)
    d.mkdir(parents=True, exist_ok=True)
    rel = Path("training") / str(photo_id)
    (d / "photo.jpg").write_bytes(photo_jpg)
    strip_paths: list[tuple[int, str]] = []
    for row, jpg in strips:
        (d / f"strip_{row}.jpg").write_bytes(jpg)
        strip_paths.append((row, str(rel / f"strip_{row}.jpg")))
    return str(rel / "photo.jpg"), strip_paths


def _jpg(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise HTTPException(500, "could not encode image")
    return buf.tobytes()


async def _read_image(upload: UploadFile, field: str) -> bytes:
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(400, f"{field}: upload an image file")
    data = await upload.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, f"{field}: image too large (max 15MB)")
    return data


# ── Intake ───────────────────────────────────────────────────────────────────
@router.post("/photos", status_code=201)
async def upload_training_photo(
    admin: CurrentAdmin,
    photo: UploadFile = File(...),
    tier: str = Form("standard"),
    split: str = Form("train"),
    set_hint: str | None = Form(None),
) -> dict:
    """Segment a staircase photo of ANY card count ≥1 (no min_rows constraint —
    training intake bypasses the scanner's pack-size rules) and store it unlabeled."""
    if tier not in _TIERS:
        raise HTTPException(422, f"tier must be one of {list(_TIERS)}")
    if split not in _SPLITS:
        raise HTTPException(422, f"split must be one of {list(_SPLITS)}")
    data = await _read_image(photo, "photo")
    img = _decode(data)
    if img is None:
        raise HTTPException(422, "photo could not be decoded")

    seg = await asyncio.to_thread(find_strips, img, None)
    if not seg.strips:
        raise HTTPException(422, "no rows detected")

    photo_id = uuid.uuid4()
    photo_path, strip_paths = _save_training_photo(
        photo_id, _jpg(img), [(s.row_index, _jpg(s.image)) for s in seg.strips]
    )

    async with async_session_maker() as session:
        session.add(TrainingPhoto(
            id=photo_id, path=photo_path, tier=tier, split=split,
            labeled=False, set_hint=(set_hint or None), uploaded_by=admin.id,
        ))
        rows = []
        for row, rel_path in strip_paths:
            strip = TrainingStrip(
                id=uuid.uuid4(), photo_id=photo_id, row_index=row,
                path=rel_path, source="upload",
            )
            session.add(strip)
            rows.append({"strip_id": str(strip.id), "row_index": row})
        await session.commit()

    log.info("training.intake photo=%s rows=%s tier=%s split=%s by=%s",
             photo_id, len(rows), tier, split, admin.id)
    return {"photo_id": str(photo_id), "rows": rows, "tier": tier, "split": split,
            "segmentation_warning": seg.warning}

# ── Set + number resolution (labeling, templates, references) ────────────────
def _norm_num(number: str) -> str:
    head = str(number).split("/")[0].strip().upper()
    return head.lstrip("0") or "0"


def _resolve_set(query: str) -> SetEntry:
    q = query.strip()
    table = load_denominator_table()
    for s in table.sets:
        if s.set_id == q or (s.set_code and s.set_code.upper() == q.upper()):
            return s
    folded = q.casefold()
    for s in table.sets:
        if s.set_name.casefold() == folded:
            return s
    close = [s.set_name for s in table.sets if folded in s.set_name.casefold()][:5]
    raise HTTPException(404, f"unknown set {query!r}" + (f"; close: {close}" if close else ""))


async def _tcgdex_set_id(session, set_id: str) -> str | None:
    row = (await session.execute(
        select(SetIdMap.tcgdex_set_id).where(SetIdMap.pokewallet_set_id == str(set_id))
    )).scalar_one_or_none()
    return row


class TrainingSet(BaseModel):
    set_id: str            # scanner set_id when known, else the tcgdex id
    set_code: str | None
    set_name: str
    tcgdex_set_id: str | None


async def _resolve_training_set(session, query: str) -> TrainingSet:
    """Training-side set resolution: scanner catalog first, then TCGdex
    directly — training data is not limited to sets the scanner supports
    (e.g. brand-new eras like "me05" / "Pitch Black")."""
    try:
        entry = _resolve_set(query)
        tdx = await _tcgdex_set_id(session, entry.set_id)
        return TrainingSet(set_id=entry.set_id, set_code=entry.set_code,
                           set_name=entry.set_name, tcgdex_set_id=tdx)
    except HTTPException:
        pass
    q = query.strip()
    rows = (await session.execute(select(TcgdexSet))).scalars().all()
    hits = [s for s in rows if s.id.lower() == q.lower() or s.name.casefold() == q.casefold()]
    if not hits:
        sub = [s for s in rows if q.casefold() in s.name.casefold()]
        if len(sub) == 1:
            hits = sub
        elif sub:
            raise HTTPException(404, f"ambiguous set {query!r}: {sorted(s.name for s in sub)[:5]}")
    if not hits:
        raise HTTPException(404, f"unknown set {query!r} — try the full set name "
                                 f"or a TCGdex id (e.g. me05 for Pitch Black)")
    s = hits[0]
    return TrainingSet(set_id=s.id, set_code=None, set_name=s.name, tcgdex_set_id=s.id)


async def _tcgdex_cards(session, tcgdex_set_id: str) -> list[TcgdexCard]:
    rows = (await session.execute(
        select(TcgdexCard).where(TcgdexCard.set_id == tcgdex_set_id)
    )).scalars().all()

    def sort_key(c: TcgdexCard):
        m = re.match(r"([A-Za-z]*)0*(\d+)", c.local_id)
        return (m.group(1).upper(), int(m.group(2))) if m else (c.local_id, 0)

    return sorted(rows, key=sort_key)


@router.get("/label-template/{query}")
async def label_template(query: str, admin: CurrentAdmin) -> dict:
    async with async_session_maker() as session:
        ts = await _resolve_training_set(session, query)
        cards = await _tcgdex_cards(session, ts.tcgdex_set_id) if ts.tcgdex_set_id else []
    return {
        "set_id": ts.set_id, "set_code": ts.set_code, "set_name": ts.set_name,
        "tcgdex_set_id": ts.tcgdex_set_id,
        "cards": [{"number": c.local_id, "name": c.name, "card_key": c.id} for c in cards],
    }


class LabelBody(BaseModel):
    set: str
    rows: list[str | None]


@router.patch("/photos/{photo_id}/labels")
async def label_photo(photo_id: uuid.UUID, body: LabelBody, admin: CurrentAdmin) -> dict:
    async with async_session_maker() as session:
        entry = await _resolve_training_set(session, body.set)
        photo = (await session.execute(
            select(TrainingPhoto).where(TrainingPhoto.id == photo_id)
            .options(selectinload(TrainingPhoto.strips))
        )).scalar_one_or_none()
        if photo is None:
            raise HTTPException(404, "photo not found")
        strips = sorted(photo.strips, key=lambda s: s.row_index)
        if len(body.rows) != len(strips):
            raise HTTPException(422, f"rows must have exactly {len(strips)} entries "
                                     f"(one per detected strip), got {len(body.rows)}")
        by_num: dict[str, TcgdexCard] = {}
        if entry.tcgdex_set_id:
            for c in await _tcgdex_cards(session, entry.tcgdex_set_id):
                by_num[_norm_num(c.local_id)] = c
        errors = []
        resolved: list[tuple[TrainingStrip, str | None, str | None]] = []
        for strip, number in zip(strips, body.rows):
            if number is None:
                resolved.append((strip, None, None))
                continue
            num = _norm_num(number)
            if by_num:
                card = by_num.get(num)
                if card is None:
                    errors.append({"row": strip.row_index, "number": number,
                                   "error": "not in set"})
                    continue
                resolved.append((strip, entry.set_id, card.id))
            else:  # no tcgdex mapping: store a synthetic key, still validated non-empty
                resolved.append((strip, entry.set_id, f"{entry.set_id}-{num}"))
        if errors:
            raise HTTPException(422, {"errors": errors})
        for strip, set_id, card_key in resolved:
            strip.set_id = set_id
            strip.card_key = card_key
        photo.labeled = True
        await session.commit()
    labeled = sum(1 for _, _, k in resolved if k)
    return {"photo_id": str(photo_id), "labeled_rows": labeled,
            "skipped_rows": len(resolved) - labeled}


# ── Export: bundle labeled strips for the training machine ───────────────────
@router.get("/export")
async def export_training_data(admin: CurrentAdmin) -> Response:
    """Stream a .tar.gz of every labeled strip + a manifest, for the local
    training pipeline (`training/fetch_uploads.py`). Includes ALL splits/tiers;
    the training side separates them."""
    import io
    import tarfile

    async with async_session_maker() as session:
        rows = (await session.execute(
            select(TrainingStrip, TrainingPhoto)
            .join(TrainingPhoto, TrainingStrip.photo_id == TrainingPhoto.id)
            .where(TrainingStrip.card_key.is_not(None), TrainingPhoto.labeled.is_(True))
        )).all()

    manifest = {"strips": []}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for strip, photo in rows:
            arc = f"strips/{strip.id}.jpg"
            try:
                data = open_photo(strip.path)
            except FileNotFoundError:
                log.warning("export.missing_strip id=%s path=%s", strip.id, strip.path)
                continue
            info = tarfile.TarInfo(arc)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            manifest["strips"].append({
                "file": arc, "card_key": strip.card_key, "set_id": strip.set_id,
                "split": photo.split, "tier": photo.tier, "source": strip.source,
                "photo_id": str(photo.id), "row_index": strip.row_index,
            })
        mbytes = json.dumps(manifest).encode()
        minfo = tarfile.TarInfo("manifest.json")
        minfo.size = len(mbytes)
        tar.addfile(minfo, io.BytesIO(mbytes))

    log.info("export.done strips=%s by=%s", len(manifest["strips"]), admin.id)
    return Response(
        buf.getvalue(), media_type="application/gzip",
        headers={"content-disposition": "attachment; filename=training-export.tar.gz"},
    )


# ── Browse: photos, pools, references, synthetic ─────────────────────────────
@router.get("/photos")
async def list_photos(admin: CurrentAdmin, split: str | None = None,
                      labeled: bool | None = None, tier: str | None = None) -> list[dict]:
    async with async_session_maker() as session:
        q = select(TrainingPhoto).options(selectinload(TrainingPhoto.strips))
        if split:
            q = q.where(TrainingPhoto.split == split)
        if labeled is not None:
            q = q.where(TrainingPhoto.labeled == labeled)
        if tier:
            q = q.where(TrainingPhoto.tier == tier)
        photos = (await session.execute(q.order_by(TrainingPhoto.created_at.desc()))
                  ).scalars().all()
    return [{
        "photo_id": str(p.id), "tier": p.tier, "split": p.split, "labeled": p.labeled,
        "set_hint": p.set_hint, "created_at": p.created_at.isoformat(),
        "rows": [{"strip_id": str(s.id), "row_index": s.row_index,
                  "card_key": s.card_key} for s in sorted(p.strips, key=lambda s: s.row_index)],
    } for p in photos]


def _serve_photo(rel_path: str) -> Response:
    try:
        return Response(open_photo(rel_path), media_type="image/jpeg")
    except FileNotFoundError:
        raise HTTPException(404, "image missing")


@router.get("/photos/{photo_id}/image")
async def photo_image(photo_id: uuid.UUID, admin: CurrentAdmin) -> Response:
    async with async_session_maker() as session:
        p = (await session.execute(select(TrainingPhoto.path)
                                   .where(TrainingPhoto.id == photo_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "photo not found")
    return _serve_photo(p)


@router.get("/strips/{strip_id}/image")
async def strip_image(strip_id: uuid.UUID, admin: CurrentAdmin) -> Response:
    async with async_session_maker() as session:
        p = (await session.execute(select(TrainingStrip.path)
                                   .where(TrainingStrip.id == strip_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "strip not found")
    return _serve_photo(p)


@router.get("/pools/summary")
async def pools_summary(admin: CurrentAdmin) -> dict:
    from sqlalchemy import func as sa_func
    async with async_session_maker() as session:
        photo_counts = (await session.execute(
            select(TrainingPhoto.tier, TrainingPhoto.split, TrainingPhoto.labeled,
                   sa_func.count()).group_by(TrainingPhoto.tier, TrainingPhoto.split,
                                             TrainingPhoto.labeled)
        )).all()
        strip_counts = (await session.execute(
            select(TrainingStrip.set_id, sa_func.count())
            .where(TrainingStrip.card_key.is_not(None)).group_by(TrainingStrip.set_id)
        )).all()
    return {
        "photos": [{"tier": t, "split": s, "labeled": lab, "count": c}
                   for t, s, lab, c in photo_counts],
        "labeled_strips_by_set": [{"set_id": s, "count": c} for s, c in strip_counts],
    }


@router.get("/references/{query}")
async def references(query: str, admin: CurrentAdmin) -> dict:
    async with async_session_maker() as session:
        entry = await _resolve_training_set(session, query)
        cached = (await session.execute(
            select(Card).where(Card.set_id == str(entry.set_id))
        )).scalars().all()
        tcgdex = (await _tcgdex_cards(session, entry.tcgdex_set_id)
                  if entry.tcgdex_set_id else [])
    by_key: dict[str, dict] = {}
    for c in tcgdex:
        by_key[c.id] = {"card_key": c.id, "number": c.local_id, "name": c.name,
                        "image_url": (c.image_base + "/high.png") if c.image_base else None}
    for c in cached:  # PokéWallet-cached rows override with their image_url
        by_key[f"pw-{c.match_id}"] = {"card_key": c.match_id, "number": c.numerator,
                                      "name": c.name, "image_url": c.image_url}
    return {"set_id": entry.set_id, "set_code": entry.set_code,
            "set_name": entry.set_name, "cards": list(by_key.values())}


def _training_data_dir() -> Path | None:
    import os
    d = Path(os.environ.get("TRAINING_DATA_DIR", "./training/data"))
    return d if d.is_dir() else None


@router.get("/synthetic")
async def synthetic(admin: CurrentAdmin, version: str | None = None,
                    sample: int = 24) -> dict:
    root = _training_data_dir()
    if root is None:
        return {"available": False, "datasets": []}
    datasets = sorted(p.parent.name for p in root.glob("*/manifest.jsonl"))
    out: dict = {"available": True, "datasets": datasets}
    if version:
        if version not in datasets:
            raise HTTPException(404, "unknown dataset version")
        import json as _json
        rows = []
        with open(root / version / "manifest.jsonl") as f:
            for line in f:
                rows.append(_json.loads(line))
        labeled = [r for r in rows if r.get("card_key")]
        out["version"] = version
        out["counts"] = {"strips": len(rows), "labeled": len(labeled)}
        step = max(1, len(labeled) // max(1, sample))
        out["samples"] = [{"path": r["path"], "card_key": r["card_key"],
                           "set": r["set"], "split": r["split"]}
                          for r in labeled[::step][:sample]]
    return out


@router.get("/synthetic/image")
async def synthetic_image(admin: CurrentAdmin, version: str, path: str) -> Response:
    root = _training_data_dir()
    if root is None:
        raise HTTPException(404, "no training data on this deployment")
    target = (root / version / path).resolve()
    if not str(target).startswith(str((root / version).resolve())):
        raise HTTPException(400, "bad path")
    if not target.is_file():
        raise HTTPException(404, "image missing")
    return Response(target.read_bytes(), media_type="image/jpeg")


# ── Matcher-backed predictions + eval runs ───────────────────────────────────
async def _strip_jpegs(strips: list[TrainingStrip]) -> list[bytes]:
    return [open_photo(s.path) for s in sorted(strips, key=lambda s: s.row_index)]


@router.get("/photos/{photo_id}/predictions")
async def photo_predictions(photo_id: uuid.UUID, admin: CurrentAdmin) -> dict:
    from app.matcher_client import enabled, match_strips
    if not enabled():
        raise HTTPException(503, "MATCHER_URL not configured")
    async with async_session_maker() as session:
        photo = (await session.execute(
            select(TrainingPhoto).where(TrainingPhoto.id == photo_id)
            .options(selectinload(TrainingPhoto.strips))
        )).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "photo not found")
    if not photo.set_hint:
        raise HTTPException(422, "photo has no set_hint to match against")
    async with async_session_maker() as session:
        entry = await _resolve_training_set(session, photo.set_hint)
    ranked = await match_strips(str(entry.set_id), await _strip_jpegs(photo.strips))
    if ranked is None:
        raise HTTPException(502, "matcher unavailable or set not indexed")
    return {"photo_id": str(photo_id), "set_id": entry.set_id,
            "rows": [{"row_index": i, "top": r[:3]} for i, r in enumerate(ranked)]}


class EvalBody(BaseModel):
    model_version: str | None = None


@router.get("/eval-runs")
async def eval_runs(admin: CurrentAdmin) -> list[dict]:
    from app.db.models import EvalRun
    async with async_session_maker() as session:
        runs = (await session.execute(
            select(EvalRun).order_by(EvalRun.created_at.desc()).limit(100)
        )).scalars().all()
    return [{"id": str(r.id), "model_version": r.model_version, "tier": r.tier,
             "top1": r.top1, "top3": r.top3, "total": r.total,
             "created_at": r.created_at.isoformat()} for r in runs]


@router.post("/eval-runs", status_code=201)
async def run_eval(body: EvalBody, admin: CurrentAdmin) -> list[dict]:
    from app.db.models import EvalRun
    from app.matcher_client import enabled, match_strips
    if not enabled():
        raise HTTPException(503, "MATCHER_URL not configured")
    async with async_session_maker() as session:
        photos = (await session.execute(
            select(TrainingPhoto)
            .where(TrainingPhoto.split == "test", TrainingPhoto.labeled.is_(True))
            .options(selectinload(TrainingPhoto.strips))
        )).scalars().all()
    tiers: dict[str, dict] = {}
    for photo in photos:
        strips = [s for s in sorted(photo.strips, key=lambda s: s.row_index)]
        set_ids = {s.set_id for s in strips if s.set_id}
        if len(set_ids) != 1:
            continue
        (set_id,) = set_ids
        ranked = await match_strips(str(set_id), await _strip_jpegs(photo.strips))
        if ranked is None:
            continue
        t = tiers.setdefault(photo.tier, {"top1": 0, "top3": 0, "total": 0, "detail": []})
        for strip, r in zip(strips, ranked):
            if strip.card_key is None:
                continue
            ids = [x["id"] for x in r[:3]]
            t["total"] += 1
            t["top1"] += bool(ids and ids[0] == strip.card_key)
            t["top3"] += strip.card_key in ids
            t["detail"].append({"photo": str(photo.id), "row": strip.row_index,
                                "want": strip.card_key, "got": ids})
    out = []
    async with async_session_maker() as session:
        for tier, agg in tiers.items():
            run = EvalRun(id=uuid.uuid4(), model_version=body.model_version or "current",
                          tier=tier, top1=agg["top1"], top3=agg["top3"],
                          total=agg["total"], detail={"rows": agg["detail"]})
            session.add(run)
            out.append({"id": str(run.id), "tier": tier, "top1": agg["top1"],
                        "top3": agg["top3"], "total": agg["total"]})
        await session.commit()
    if not out:
        raise HTTPException(422, "no labeled test-split photos with a single resolvable set")
    return out
