"""Probe for the API-resilience and photo-speed fixes (2026-10-12).

  A. POKEWALLET RETRY   search_cards retries ONCE on a 5xx/429 or a transport
     error (a read timeout here), honours a short Retry-After, never retries a
     4xx, and gives up after the second attempt. The shared client keeps its 30s
     default (task-9's contract); only each request carries the tighter budget.
  B. MISS CACHE         cached_lookup_card remembers a completed "no such card"
     for the set/number/set-name it asked about, and does NOT remember a request
     that failed.
  C. RUNPOD POLLING     identify() follows a /runsync that comes back IN_QUEUE /
     IN_PROGRESS through /status/{id} to its answer; reports a FAILED job as no
     answer; cancels a job still running at its deadline; and keeps runsync's
     own ?wait under the budget. A job answered inside runsync is never polled.
  D. CODE CARD          the QR-anchored read of the corpus code card
     (tests/corpus/IMG_7103.heic, code 2CM-2ZY7-WKD-DTM per tests/corpus/README.md)
     is right at native, 1/2 and 1/4 scale and prints its time; a Tesseract call
     past its timeout degrades to no tokens instead of raising; OMP_THREAD_LIMIT
     defaults to 1.
  E. PACK SCAN          the code card is read concurrently with the staircase
     (code_wait < code_ocr) and still read right; an upload that falls back to
     segmentation runs PP-OCR over the whole photo ONCE, not twice.

DB-free: the stubs below stand in for PokéWallet and RunPod, and a missing
Postgres only costs cached_lookup_card its (failing, logged) cache read — which
is the path B wants anyway. With a DB up, B's set ids do not exist there.

Not a pytest test (the suite stays small and VLM-free); a one-shot script that
exits 0 only when every check held.
Run: PYTHONPATH=. .venv/bin/python docs/acceptance/probes/api-speed-probe.py
"""
import asyncio
import logging
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://pcs:pcs@localhost:5432/pcs")
os.environ.setdefault("AUTH_SECRET", "dev-secret-not-for-prod-pad-0123456789")
os.environ.setdefault("PHOTO_STORAGE_DIR", "./var/pulls")
os.environ.setdefault("COOKIE_SECURE", "false")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

REPO = Path(__file__).resolve().parents[3]     # docs/acceptance/probes/<this> -> repo
CORPUS = REPO / "tests" / "corpus"
CODE = "2CM-2ZY7-WKD-DTM"

failures: list[str] = []


def check(cond: bool, msg: str, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(msg)


def serve(app: FastAPI) -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("stub did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}"


# --- scripted PokéWallet ------------------------------------------------------
pw_app = FastAPI()
pw_calls: dict[str, int] = {}
_CARD = {"id": "pw-1", "card_info": {"name": "Probe", "card_number": "001/100",
                                     "set_name": "Probe Set"}}


@pw_app.get("/search")
async def pw_search(q: str):
    n = pw_calls[q] = pw_calls.get(q, 0) + 1
    ok = {"results": [_CARD], "pagination": {}, "metadata": {}}
    if q == "flaky 1":                                  # 503, then fine
        return ok if n > 1 else JSONResponse({}, status_code=503)
    if q == "limited 1":                                # 429 + Retry-After: 1
        return ok if n > 1 else JSONResponse({}, status_code=429,
                                             headers={"Retry-After": "1"})
    if q == "down 1":                                   # never recovers
        return JSONResponse({}, status_code=500)
    if q == "missing 1":                                # a client error: no retry
        return JSONResponse({}, status_code=404)
    if q == "slow 1":                                   # first attempt times out
        if n == 1:
            await asyncio.sleep(3)
        return ok
    if q.startswith("absent "):                         # answered: no such card
        return {"results": [{"id": "x", "card_info": {"card_number": "999/100"}}]}
    if q == "broken 7":                                 # fails twice per lookup
        return JSONResponse({}, status_code=502)
    return ok


# --- scripted RunPod ----------------------------------------------------------
rp_app = FastAPI()
rp: dict = {"mode": "sync", "polls": 0, "cancels": [], "waits": []}


def _answer(cards):
    return {"cards": [{"row_index": c["row_index"], "number": "126",
                       "denominator": "167", "set_name": "Probe", "confidence": 0.9}
                      for c in cards]}


@rp_app.post("/v2/x/runsync")
async def rp_runsync(request: Request):
    body = await request.json()
    rp["waits"].append(request.query_params.get("wait"))
    rp["cards"] = body["input"]["cards"]
    if rp["mode"] == "sync":
        return {"id": "j", "status": "COMPLETED", "output": _answer(rp["cards"])}
    return {"id": "j", "status": "IN_QUEUE"}


@rp_app.get("/v2/x/status/{job_id}")
async def rp_status(job_id: str):
    rp["polls"] += 1
    if rp["mode"] == "pending" and rp["polls"] >= 3:
        return {"id": job_id, "status": "COMPLETED", "output": _answer(rp["cards"])}
    if rp["mode"] == "failed" and rp["polls"] >= 2:
        return {"id": job_id, "status": "FAILED", "error": "CUDA out of memory"}
    return {"id": job_id, "status": "IN_PROGRESS"}


@rp_app.post("/v2/x/cancel/{job_id}")
async def rp_cancel(job_id: str):
    rp["cancels"].append(job_id)
    return {"id": job_id, "status": "CANCELLED"}


async def section_a(pw) -> None:
    print("\n-- A. PokéWallet: one retry, for the failures worth one --")
    pw._RETRY_BACKOFF_S = 0.05
    data = await pw.search_cards("flaky 1", api_key="k")
    check(bool(data["results"]) and pw_calls["flaky 1"] == 2,
          "a 503 is retried once and the second answer is returned",
          f"calls={pw_calls['flaky 1']}")
    t = time.perf_counter()
    data = await pw.search_cards("limited 1", api_key="k")
    waited = time.perf_counter() - t
    check(bool(data["results"]) and pw_calls["limited 1"] == 2 and waited >= 0.9,
          "a 429 waits out a short Retry-After, then retries", f"waited={waited:.2f}s")
    try:
        await pw.search_cards("down 1", api_key="k")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    check(raised and pw_calls["down 1"] == 2,
          "a 5xx that persists raises after exactly two attempts",
          f"calls={pw_calls['down 1']}")
    try:
        await pw.search_cards("missing 1", api_key="k")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    check(raised and pw_calls["missing 1"] == 1, "a 4xx is not retried",
          f"calls={pw_calls['missing 1']}")
    real_timeout = pw._SEARCH_TIMEOUT
    pw._SEARCH_TIMEOUT = httpx.Timeout(1.0, connect=1.0)
    t = time.perf_counter()
    data = await pw.search_cards("slow 1", api_key="k")
    took = time.perf_counter() - t
    pw._SEARCH_TIMEOUT = real_timeout
    check(bool(data["results"]) and pw_calls["slow 1"] == 2 and took < 2.5,
          "a request past its per-attempt budget is abandoned and retried",
          f"took={took:.2f}s calls={pw_calls['slow 1']}")
    c = await pw.shared_client()
    check(c.timeout.read == 30.0 and pw._SEARCH_TIMEOUT.read == 8.0,
          "shared client keeps its 30s default; each search carries 8s",
          f"client={c.timeout.read} search={pw._SEARCH_TIMEOUT.read}")


async def section_b(cards) -> None:
    print("\n-- B. a completed miss is remembered; a failure is not --")
    cards._lookup_misses.clear()
    for _ in range(3):
        m = await cards.cached_lookup_card("absent", "12", set_name="Probe Set",
                                           api_key="k")
    check(m is None and pw_calls.get("absent 12") == 1,
          "three lookups of a card PokéWallet does not carry cost ONE request",
          f"calls={pw_calls.get('absent 12')}")
    await cards.cached_lookup_card("absent", "12", set_name="Other Set", api_key="k")
    check(pw_calls.get("absent 12") == 2,
          "the memo is per set name, like lookup_card_exact's own filter")
    for _ in range(2):
        try:
            await cards.cached_lookup_card("broken", "7", api_key="k")
        except httpx.HTTPStatusError:
            pass
    check(pw_calls.get("broken 7") == 4,
          "a lookup that FAILED is asked again next time (2 attempts x 2 lookups)",
          f"calls={pw_calls.get('broken 7')}")
    key = next(iter(cards._lookup_misses))
    cards._lookup_misses[key] = time.monotonic() - 1           # expire it
    await cards.cached_lookup_card("absent", "12", set_name="Probe Set", api_key="k")
    check(pw_calls.get("absent 12") == 3, "an expired miss is asked again")


async def section_c(vc) -> None:
    print("\n-- C. RunPod: a job that outlives /runsync is followed, not dropped --")
    vc._POLL_INTERVAL_S = 0.1
    card = [{"row_index": 4, "image_b64": "AAAA", "kind": "full_card"}]

    rp.update(mode="sync", polls=0)
    out = await vc.identify(card)
    check(out is not None and 4 in out and rp["polls"] == 0,
          "an answer inside runsync is used as-is, nothing polled")
    check(rp["waits"][-1] == "60000",
          "runsync's own wait stays under the 180s default budget",
          f"wait={rp['waits'][-1]}")

    rp.update(mode="pending", polls=0)
    out = await vc.identify(card)
    check(out is not None and out[4]["number"] == "126" and rp["polls"] == 3,
          "IN_QUEUE from runsync -> /status polled to COMPLETED -> answer returned",
          f"polls={rp['polls']}")

    rp.update(mode="failed", polls=0)
    out = await vc.identify(card)
    check(out is None and rp["polls"] == 2, "a FAILED job is no answer",
          f"polls={rp['polls']}")

    rp.update(mode="forever", polls=0)
    rp["cancels"].clear()
    t = time.perf_counter()
    out = await vc.identify(card, timeout=1.5)
    took = time.perf_counter() - t
    check(out is None and rp["cancels"] == ["j"] and took < 3.0,
          "a job still running at the deadline is cancelled, not left billing",
          f"took={took:.2f}s cancels={rp['cancels']}")
    check(rp["waits"][-1] == "1000", "a short budget clamps ?wait to RunPod's 1000ms floor",
          f"wait={rp['waits'][-1]}")


def section_d() -> None:
    print("\n-- D. code card read and Tesseract bounds --")
    import cv2
    import numpy as np

    from app.pack import ocr
    from app.pack.pipeline import _decode
    check(os.environ.get("OMP_THREAD_LIMIT") == "1",
          "OMP_THREAD_LIMIT defaults to 1 for Tesseract subprocesses")
    img = _decode((CORPUS / "IMG_7103.heic").read_bytes())
    for label, f in (("native", 1), ("1/2", 2), ("1/4", 4)):
        im = img if f == 1 else cv2.resize(img, None, fx=1 / f, fy=1 / f,
                                           interpolation=cv2.INTER_AREA)
        t = time.perf_counter()
        r = ocr.read_code_card(im)
        ms = (time.perf_counter() - t) * 1000
        check(r.code == CODE and r.format_ok,
              f"corpus code card read right at {label} ({im.shape[1]}x{im.shape[0]})",
              f"{ms:.0f}ms conf={r.confidence:.2f} code={r.code}")
    real = ocr._TESSERACT_TIMEOUT_S
    ocr._TESSERACT_TIMEOUT_S = 0.2
    noise = (np.random.default_rng(0).random((600, 7200)) * 255).astype(np.uint8)
    out = ocr._ocr_tokens(noise, ocr._NUM_CONFIG)
    ocr._TESSERACT_TIMEOUT_S = real
    check(out == [], "a Tesseract call past its timeout yields no tokens, no exception")


async def section_e() -> None:
    print("\n-- E. pack scan: overlapped code read, one whole-photo pass --")
    import cv2

    from app.pack import pipeline, rapidocr_reader
    from app.pack.ocr import NumberReading, _code_cache
    # Not under test here, and a flagged card would start a background follow-up
    # that is still connecting when the probe exits.
    os.environ.pop("VLM_ENDPOINT", None)
    rapidocr_reader.warmup()
    stages: dict[str, float] = {}

    class H(logging.Handler):
        def emit(self, rec):
            m = re.match(r"timing\.pack\.(\w+) scan=\w+ ms=([\d.]+)", rec.getMessage())
            if m:
                stages[m.group(1)] = float(m.group(2))
    logging.getLogger("pokemon_scanner.timing").addHandler(H())
    logging.getLogger("pokemon_scanner").setLevel(logging.INFO)   # timing lines are INFO

    _code_cache.clear()
    t = time.perf_counter()
    resp = await pipeline.scan_pack((CORPUS / "IMG_7102.heic").read_bytes(),
                                    (CORPUS / "IMG_7103.heic").read_bytes(), None)
    total = (time.perf_counter() - t) * 1000
    check(resp.code_card.code == CODE, "the code card still reads right in a scan")
    check(stages.get("code_wait", 1e9) < stages.get("code_ocr", 0),
          "the code read overlapped the staircase (code_wait < code_ocr)",
          f"total={total:.0f}ms code_ocr={stages.get('code_ocr', 0):.0f}ms "
          f"code_wait={stages.get('code_wait', 0):.0f}ms "
          f"detect_first={stages.get('detect_first', 0):.0f}ms")

    img = pipeline._decode((CORPUS / "IMG_7102.heic").read_bytes())
    ok, buf = cv2.imencode(".jpg", img[: int(img.shape[0] * 0.3)])
    whole = []
    real_detect = rapidocr_reader.detect_lines
    real_read = pipeline.read_card_number

    def counting(im, cap=2600):
        whole.append(im.shape)
        return real_detect(im, cap)
    rapidocr_reader.detect_lines = counting
    pipeline.read_card_number = lambda strip: NumberReading()   # strip OCR is not under test
    try:
        stages.clear()
        await pipeline.scan_pack(buf.tobytes(),
                                 (REPO / "tests/fixtures/e2e/code.jpg").read_bytes(), None)
    finally:
        rapidocr_reader.detect_lines = real_detect
        pipeline.read_card_number = real_read
    check("find_strips" in stages, "the cropped photo really took the fallback path")
    check(len(whole) == 1, "PP-OCR ran over the whole photo once, not twice",
          f"whole-photo passes={len(whole)}")


async def main() -> int:
    os.environ["POKEWALLET_BASE_URL"] = serve(pw_app)
    os.environ["VLM_ENDPOINT"] = serve(rp_app) + "/v2/x"
    os.environ["VLM_API_KEY"] = "probe"
    from app import cards
    from app import pokewallet as pw
    from app.pack import vlm_client as vc
    await section_a(pw)
    await section_b(cards)
    await section_c(vc)
    section_d()
    await section_e()
    await pw.close_shared_client()
    print(f"\n{'ALL CHECKS HELD' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
