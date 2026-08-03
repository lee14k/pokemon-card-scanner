"""RunPod serverless handler: Qwen2.5-VL-7B identifies Pokemon cards from either
a bottom number strip or a whole-card photo (see ``kind``). Loaded once at cold
start; one generate() per card.

Contract (matches app/pack/vlm_client.py):
  input:  {"cards": [{"row_index": int, "image_b64": str,
                      "strip_b64": str|null (optional),
                      "hint_set": str|null, "hint_denominator": str|null,
                      "kind": "strip"|"full_card"|null}]}
  output: {"cards": [{"row_index": int, "number": str|null,
                      "denominator": str|null, "set_name": str|null,
                      "name": str|null, "confidence": float}]}

``kind`` says what the image actually IS, so the prompt can stop claiming every
image is a bottom strip (binder cells and live frames are whole cards). It is
OPTIONAL in both directions: absent/unknown ⇒ "strip", today's prompt, so an old
app driving this worker is unchanged. The prompt text itself lives in the
dependency-free ``prompts`` module so it is reviewable/testable without a GPU.

``strip_b64`` is an equally optional SECOND image: a magnified crop of the same
card's bottom strip, sent because the collector number is ~10px tall in a
full-card crop. Absent (an old app) ⇒ one image and today's prompt exactly.

An empty ``cards`` list is a legitimate request: the app's warm-up ping
(vlm_client.warmup) sends one to trigger the ``_load()`` below and nothing else.
Keep that load unconditional and ahead of the loop or the ping stops working.

Deploy: build this dir as a RunPod Serverless endpoint (see Dockerfile header),
GPU >= 24GB (7B in bf16). MODEL overridable via env VLM_MODEL.
"""
import base64
import io
import json
import os
import re
import time

import runpod
import torch
from PIL import Image
from prompts import build_prompt   # /prompts.py — copied by the Dockerfile
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

MODEL = os.environ.get("VLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")

_model = None
_processor = None


def _load():
    global _model, _processor
    if _model is None:
        _model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL, torch_dtype=torch.bfloat16, device_map="auto")
        _processor = AutoProcessor.from_pretrained(MODEL)
    return _model, _processor


def _identify(model, processor, img: Image.Image, hint_set, hint_den,
              kind=None, strip_img: Image.Image | None = None) -> dict:
    content = [{"type": "image", "image": img}]
    # The second image ships only when the prompt will actually mention it:
    # prompts.build_prompt appends the strip sentence under `with_strip and
    # k == "full_card"`, resolving every other/unknown kind to "strip". Gate the
    # image on the SAME condition or the two halves desync — an unrecognized
    # kind would get a strip prompt with two images, and the model is told the
    # first image IS the strip. Mirrors prompts.py's resolution rule; this file
    # cannot import _LEADS, so keep the literal in step with it.
    with_strip = strip_img is not None and kind == "full_card"
    if with_strip:
        content.append({"type": "image", "image": strip_img})
    content.append({"type": "text",
                    "text": build_prompt(kind, hint_set, hint_den,
                                         with_strip=with_strip)})
    messages = [{"role": "user", "content": content}]
    text = processor.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=True)
    from qwen_vl_utils import process_vision_info

    image_inputs, _ = process_vision_info(messages)
    inputs = processor(text=[text], images=image_inputs, padding=True,
                       return_tensors="pt").to(model.device)
    # Per-card generate seconds — the single dominant cost of a VLM batch, and the
    # number that decides whether batching or a smaller model is worth it. RunPod
    # captures stdout, so a plain print is the log here.
    _t0 = time.perf_counter()
    gen = model.generate(**inputs, max_new_tokens=128, do_sample=False)
    print(f"timing.worker.generate s={time.perf_counter() - _t0:.2f}", flush=True)
    reply = processor.batch_decode(
        [g[len(i):] for i, g in zip(inputs.input_ids, gen)],
        skip_special_tokens=True)[0]
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return {"number": None, "denominator": None, "set_name": None,
                "name": None, "confidence": 0.0}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"number": None, "denominator": None, "set_name": None,
                "name": None, "confidence": 0.0}
    return {"number": d.get("number"), "denominator": d.get("denominator"),
            "set_name": d.get("set_name"), "name": d.get("name"),
            "confidence": float(d.get("confidence") or 0.0)}


def handler(job):
    # Batch wall clock includes the cold-start model load, which is exactly what the
    # caller's timeout has to cover — so it is timed from the very first statement.
    t_batch = time.perf_counter()
    model, processor = _load()
    print(f"timing.worker.load s={time.perf_counter() - t_batch:.2f}", flush=True)
    out = []
    for c in (job.get("input") or {}).get("cards") or []:
        t_card = time.perf_counter()
        try:
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
        except Exception as e:  # one bad card never fails the batch
            res = {"number": None, "denominator": None, "set_name": None,
                   "name": None, "confidence": 0.0, "error": str(e)}
        res["row_index"] = c.get("row_index")
        out.append(res)
        print(f"timing.worker.card row={res['row_index']} "
              f"s={time.perf_counter() - t_card:.2f}", flush=True)
    print(f"timing.worker.batch cards={len(out)} "
          f"s={time.perf_counter() - t_batch:.2f}", flush=True)
    return {"cards": out}


runpod.serverless.start({"handler": handler})
