"""RunPod serverless handler: Qwen2.5-VL-7B identifies Pokemon card bottom
strips. Loaded once at cold start; one generate() per card.

Contract (matches app/pack/vlm_client.py):
  input:  {"cards": [{"row_index": int, "image_b64": str,
                      "hint_set": str|null, "hint_denominator": str|null}]}
  output: {"cards": [{"row_index": int, "number": str|null,
                      "denominator": str|null, "set_name": str|null,
                      "confidence": float}]}

Deploy: build this dir as a RunPod Serverless endpoint (see Dockerfile header),
GPU >= 24GB (7B in bf16). MODEL overridable via env VLM_MODEL.
"""
import base64
import io
import json
import os
import re

import runpod
import torch
from PIL import Image
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


_PROMPT = (
    "This image is the bottom strip of a Pokemon trading card. Read the collector "
    "number exactly as printed (formats like 126/167, 12/198, or TG12/TG30). "
    "If the set symbol or name is legible, identify the set. "
    'Reply with ONLY a JSON object: '
    '{{"number": "<numerator>", "denominator": "<denominator or null>", '
    '"set_name": "<set or null>", "confidence": <0..1>}}. {hint}'
)


def _identify(model, processor, img: Image.Image, hint_set, hint_den) -> dict:
    hint = ""
    if hint_set or hint_den:
        hint = "Context: this pack is likely " + \
            (f"the set '{hint_set}'. " if hint_set else "") + \
            (f"denominator {hint_den}. " if hint_den else "")
    messages = [{"role": "user", "content": [
        {"type": "image", "image": img},
        {"type": "text", "text": _PROMPT.format(hint=hint)},
    ]}]
    text = processor.apply_chat_template(messages, tokenize=False,
                                         add_generation_prompt=True)
    from qwen_vl_utils import process_vision_info

    image_inputs, _ = process_vision_info(messages)
    inputs = processor(text=[text], images=image_inputs, padding=True,
                       return_tensors="pt").to(model.device)
    gen = model.generate(**inputs, max_new_tokens=128, do_sample=False)
    reply = processor.batch_decode(
        [g[len(i):] for i, g in zip(inputs.input_ids, gen)],
        skip_special_tokens=True)[0]
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return {"number": None, "denominator": None, "set_name": None, "confidence": 0.0}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"number": None, "denominator": None, "set_name": None, "confidence": 0.0}
    return {"number": d.get("number"), "denominator": d.get("denominator"),
            "set_name": d.get("set_name"),
            "confidence": float(d.get("confidence") or 0.0)}


def handler(job):
    model, processor = _load()
    out = []
    for c in (job.get("input") or {}).get("cards") or []:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(c["image_b64"]))).convert("RGB")
            res = _identify(model, processor, img, c.get("hint_set"), c.get("hint_denominator"))
        except Exception as e:  # one bad card never fails the batch
            res = {"number": None, "denominator": None, "set_name": None,
                   "confidence": 0.0, "error": str(e)}
        res["row_index"] = c.get("row_index")
        out.append(res)
    return {"cards": out}


runpod.serverless.start({"handler": handler})
