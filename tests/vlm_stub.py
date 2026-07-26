"""Local stand-in for the RunPod Qwen2.5-VL worker — mimics the serverless
runsync API so the app-side VLM fallback can be smoke-tested without a GPU.

Run: uvicorn tests.vlm_stub:app --port 9192
Point the app at it: VLM_ENDPOINT=http://127.0.0.1:9192/v2/test VLM_API_KEY=x

Returns a canned identification per requested card. Override the number/set it
answers with via env VLM_STUB_NUMBER / VLM_STUB_DEN / VLM_STUB_SET, and the
answer latency via VLM_STUB_DELAY_S — the real worker's cold start is seconds,
and a stub that answers instantly cannot show whether a scan is waiting on it.

``REQUESTS`` records what each call actually asked for (never the image bytes),
so an in-process probe can assert the request CONTRACT — per-card ``kind``, the
hints, and the empty-``cards`` warm-up ping — and not just the answer. A real
worker ignores unknown fields, so nothing here is load-bearing for the app.
"""
import asyncio
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="VLM stub")

# One entry per runsync call: {"cards": [{row_index, kind, hint_set,
# hint_denominator, bytes}]}. Image b64 is REPLACED by its length — a probe that
# drives several multi-megapixel pages must not accumulate them in memory.
REQUESTS: list[dict] = []


class RunInput(BaseModel):
    input: dict


@app.post("/v2/{endpoint_id}/runsync")
async def runsync(endpoint_id: str, body: RunInput,
                  authorization: str | None = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    REQUESTS.append({"cards": [
        {"row_index": c.get("row_index"), "kind": c.get("kind"),
         "hint_set": c.get("hint_set"), "hint_denominator": c.get("hint_denominator"),
         "bytes": len(c.get("image_b64") or "")}
        for c in body.input.get("cards", [])]})
    delay = float(os.environ.get("VLM_STUB_DELAY_S", "0") or 0)
    if delay > 0:
        await asyncio.sleep(delay)
    num = os.environ.get("VLM_STUB_NUMBER", "126")
    den = os.environ.get("VLM_STUB_DEN", "167")
    setname = os.environ.get("VLM_STUB_SET", "Twilight Masquerade")
    cards = []
    for c in body.input.get("cards", []):
        cards.append({
            "row_index": c.get("row_index"),
            "number": num, "denominator": den, "set_name": setname,
            "confidence": 0.95,
        })
    return {"id": "stub", "status": "COMPLETED", "output": {"cards": cards}}
