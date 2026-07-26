"""Local stand-in for the RunPod Qwen2.5-VL worker — mimics the serverless
runsync API so the app-side VLM fallback can be smoke-tested without a GPU.

Run: uvicorn tests.vlm_stub:app --port 9192
Point the app at it: VLM_ENDPOINT=http://127.0.0.1:9192/v2/test VLM_API_KEY=x

Returns a canned identification per requested card. Override the number/set it
answers with via env VLM_STUB_NUMBER / VLM_STUB_DEN / VLM_STUB_SET, and the
answer latency via VLM_STUB_DELAY_S — the real worker's cold start is seconds,
and a stub that answers instantly cannot show whether a scan is waiting on it.
"""
import asyncio
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="VLM stub")


class RunInput(BaseModel):
    input: dict


@app.post("/v2/{endpoint_id}/runsync")
async def runsync(endpoint_id: str, body: RunInput,
                  authorization: str | None = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
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
