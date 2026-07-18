"""Per-set reference index: {set_key}.npz (ids, vectors) + {set_key}.meta.json."""
from __future__ import annotations

import json, os, pathlib, time
import numpy as np

def _paths(index_dir: str, set_key: str) -> tuple[pathlib.Path, pathlib.Path]:
    safe = "".join(c for c in set_key if c.isalnum() or c in "-_")
    base = pathlib.Path(index_dir)
    return base / f"{safe}.npz", base / f"{safe}.meta.json"

def save(index_dir: str, set_key: str, ids: list[str], vectors: np.ndarray,
         source: str, failures: int, extra: dict | None = None) -> dict:
    npz, meta = _paths(index_dir, set_key)
    npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez(npz, ids=np.array(ids), vectors=vectors.astype(np.float32))
    info = {"set_key": set_key, "count": len(ids), "failures": failures,
            "source": source, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(extra or {})}
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
