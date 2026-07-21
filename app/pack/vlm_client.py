"""Client for the RunPod-hosted Qwen2.5-VL card-identification worker.

VLM_ENDPOINT unset ⇒ feature off entirely. Every failure returns None — the VLM
is a fallback, never load-bearing: a disabled/slow/errored worker leaves the
Phase-1 result untouched."""
from __future__ import annotations

import base64
import logging
import os

import cv2
import httpx

log = logging.getLogger("pokemon_scanner.pack.vlm")


def _endpoint() -> str | None:
    # e.g. https://api.runpod.ai/v2/<endpoint-id>
    return os.environ.get("VLM_ENDPOINT", "").strip().rstrip("/") or None


def enabled() -> bool:
    return _endpoint() is not None


def _b64_jpeg(img) -> str | None:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


async def identify(cards: list[dict], timeout: float = 90.0) -> dict[int, dict] | None:
    """cards: [{"row_index": int, "image": bgr ndarray, "hint_set": str|None,
    "hint_denominator": str|None}]. Returns {row_index: {number, denominator,
    set_name, confidence}} or None (disabled / no cards / error). Timeout is
    generous for serverless cold start."""
    base = _endpoint()
    if base is None or not cards:
        return None
    payload_cards = []
    for c in cards:
        b = _b64_jpeg(c["image"])
        if b is None:
            continue
        payload_cards.append({
            "row_index": c["row_index"], "image_b64": b,
            "hint_set": c.get("hint_set"), "hint_denominator": c.get("hint_denominator"),
        })
    if not payload_cards:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/runsync", json={"input": {"cards": payload_cards}},
                headers={"Authorization": f"Bearer {os.environ.get('VLM_API_KEY', '')}"})
        if r.status_code == 401:
            log.warning("vlm.unauthorized (check VLM_API_KEY)")
            return None
        r.raise_for_status()
        out = (r.json().get("output") or {}).get("cards") or []
        result = {c["row_index"]: c for c in out if c.get("row_index") is not None}
        log.info("vlm.identify cards=%s answered=%s", len(payload_cards), len(result))
        return result or None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("vlm.identify_failed err=%r", e)
        return None
