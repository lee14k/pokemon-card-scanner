"""RunPod serverless handler: Qwen2.5-VL-7B identifies Pokemon cards from either
a bottom number strip or a whole-card photo (see ``kind``). Loaded once at cold
start; the cards of a request are generated in batches of up to
``VLM_MAX_BATCH`` (see ``_answer``).

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
GPU >= 24GB (7B in bf16). MODEL overridable via env VLM_MODEL, batch size via
VLM_MAX_BATCH.
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
# Cards per generate() call. A binder page sends nine cells, and generating them
# one at a time paid nine full decode loops: decoding is bound by reading the
# 7B weights once per step, so a step costs about the same for one sequence as
# for four, and batching shares those reads. Bounded because a batch's prefill
# (every image's vision tokens at once) is what has to fit beside the weights on
# a 24GB card; a batch that does not fit falls back to one card at a time (see
# _answer), so too high a value costs time, never answers.
MAX_BATCH = max(1, int(os.environ.get("VLM_MAX_BATCH", "4")))
_DTYPE = torch.bfloat16

_model = None
_processor = None


def _load():
    global _model, _processor
    if _model is None:
        _model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL, dtype=_DTYPE, device_map="auto")
        _processor = AutoProcessor.from_pretrained(MODEL)
        # A batch pads every prompt to the longest one. Decoder-only generation
        # needs that padding on the LEFT, so each row's last position is its own
        # last prompt token and the reply starts right after it; right padding
        # would have every shorter row continue from pad tokens. It also makes the
        # reply slice in _generate (everything past the padded prompt length) the
        # same for every row.
        _processor.tokenizer.padding_side = "left"
    return _model, _processor


def _conversation(img: Image.Image, hint_set, hint_den, kind=None,
                  strip_img: Image.Image | None = None) -> list[dict]:
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
    return [{"role": "user", "content": content}]


def _generate(model, processor, conversations: list[list[dict]]) -> list[str]:
    """One generate() over several cards' conversations; one reply per card, in
    order. Rows may carry different numbers of images (a binder cell's strip
    rides as a second one): process_vision_info flattens them in conversation
    order, which is the order their image tokens appear in the batch."""
    from qwen_vl_utils import process_vision_info

    texts = [processor.apply_chat_template(m, tokenize=False, add_generation_prompt=True)
             for m in conversations]
    image_inputs, _ = process_vision_info(conversations)
    inputs = processor(text=texts, images=image_inputs, padding=True,
                       return_tensors="pt").to(model.device)
    # Seconds per generate() — the single dominant cost of a VLM request, and the
    # number that says whether VLM_MAX_BATCH is right for the GPU. RunPod
    # captures stdout, so a plain print is the log here.
    _t0 = time.perf_counter()
    gen = model.generate(**inputs, max_new_tokens=128, do_sample=False)
    print(f"timing.worker.generate cards={len(conversations)} "
          f"s={time.perf_counter() - _t0:.2f}", flush=True)
    return processor.batch_decode(
        [g[len(i):] for i, g in zip(inputs.input_ids, gen)],
        skip_special_tokens=True)


def _parse(reply: str) -> dict:
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


def _failed(e: Exception) -> dict:
    return {"number": None, "denominator": None, "set_name": None,
            "name": None, "confidence": 0.0, "error": str(e)}


def _answer(model, processor, conversations: list[list[dict]]) -> list[dict]:
    """Parsed answers for a batch of conversations, one per conversation — never
    raises. A batch that fails for ANY reason (the realistic one is CUDA running
    out of memory on a big prefill) is retried one card at a time, which is
    exactly how every card was generated before batching: a bad batch costs
    time, never an answer, and one bad card never fails its neighbours."""
    try:
        return [_parse(r) for r in _generate(model, processor, conversations)]
    except Exception as e:
        if len(conversations) == 1:
            return [_failed(e)]
        print(f"worker.batch_failed cards={len(conversations)} err={e!r} "
              f"— retrying one at a time", flush=True)
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return [r for c in conversations for r in _answer(model, processor, [c])]


def _image(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def handler(job):
    # Batch wall clock includes the cold-start model load, which is exactly what the
    # caller's timeout has to cover — so it is timed from the very first statement.
    t_batch = time.perf_counter()
    model, processor = _load()
    print(f"timing.worker.load s={time.perf_counter() - t_batch:.2f}", flush=True)
    cards = (job.get("input") or {}).get("cards") or []
    out: list[dict | None] = [None] * len(cards)
    ready: list[tuple[int, list[dict]]] = []      # (position in `cards`, conversation)
    for i, c in enumerate(cards):
        try:
            img = _image(c["image_b64"])
            strip_img = None
            if c.get("strip_b64"):
                try:
                    strip_img = _image(c["strip_b64"])
                except Exception:
                    strip_img = None   # a corrupt assist must not cost the card
            ready.append((i, _conversation(img, c.get("hint_set"),
                                           c.get("hint_denominator"), c.get("kind"),
                                           strip_img)))
        except Exception as e:  # one bad card never fails the batch
            out[i] = _failed(e)
    for s in range(0, len(ready), MAX_BATCH):
        chunk = ready[s:s + MAX_BATCH]
        for (i, _conv), res in zip(chunk, _answer(model, processor,
                                                  [conv for _i, conv in chunk])):
            out[i] = res
    for c, res in zip(cards, out):
        res["row_index"] = c.get("row_index")
    print(f"timing.worker.batch cards={len(out)} max_batch={MAX_BATCH} "
          f"s={time.perf_counter() - t_batch:.2f}", flush=True)
    return {"cards": out}


if __name__ == "__main__":   # the Dockerfile runs this file; a probe imports it
    runpod.serverless.start({"handler": handler})
