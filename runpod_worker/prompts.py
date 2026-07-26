"""The worker's prompt text, per image KIND — deliberately a separate,
DEPENDENCY-FREE module (stdlib only, no runpod/torch/transformers).

handler.py can't be imported without a GPU image, so the prompt it builds could
never be exercised or reviewed outside a deployed worker. Everything about the
prompt therefore lives here, where `python -c "from runpod_worker.prompts import
build_prompt"` works from the repo root and the exact string sent to the model is
directly assertable.

Deploy note: the Dockerfile MUST copy this file next to handler.py (it does —
`COPY prompts.py /prompts.py`), or the worker dies at import with "No module
named prompts" and RunPod reports every worker unhealthy.

KINDS (the app's per-card `kind` field, app/pack/vlm_client.identify):
  "strip"      a pack scan's bottom number-row band — the ONLY thing the worker
               used to be told it was getting.
  "full_card"  a whole card photo: a binder-page cell or a live-scan frame. Both
               flows have always sent full cards while the prompt insisted they
               were bottom strips; telling the model "this is a strip" while
               handing it a full art card is a documented contributor to
               hallucinated collector numbers.
Anything else (including None, and a kind a future app invents) falls back to
"strip" — the pre-existing behavior, so an old app talking to a new worker is
byte-identical to today.
"""

_STRIP_LEAD = (
    "This image is the bottom strip of a Pokemon trading card. Read the collector "
    "number exactly as printed (formats like 126/167, 12/198, or TG12/TG30). "
    "If the set symbol or name is legible, identify the set. "
    "If the card's printed name is legible, read it. "
)

_FULL_CARD_LEAD = (
    "This image is a photo of a whole Pokemon trading card. Read the card's "
    "printed name from the title at the top. The collector number is printed "
    "small in the bottom strip of the card (formats like 126/167, TG12/TG30, or "
    "a promo code such as SWSH123 or MEP 037). Read it exactly as printed, and do "
    "not guess a number you cannot actually see. If the set name or set symbol is "
    "legible, identify the set. "
)

# The reply contract is IDENTICAL for every kind: same JSON object, same fields,
# so the worker's output shape (and app-side merge) is kind-independent.
_REPLY = (
    "Reply with ONLY a JSON object: "
    '{"number": "<numerator>", "denominator": "<denominator or null>", '
    '"set_name": "<set or null>", "name": "<the card\'s printed name or null>", '
    '"confidence": <0..1>}. '
)

_LEADS = {"strip": _STRIP_LEAD, "full_card": _FULL_CARD_LEAD}

# The strip hint kept its original "this pack is likely" wording verbatim: pack
# scans are the only flow that has ever sent hints, and their accuracy is
# measured against that exact string. A full-card hint says "this card" instead —
# a binder page or a live frame is not a pack.
_HINT_LEAD = {"strip": "Context: this pack is likely ",
              "full_card": "Context: this card is likely from "}


def build_prompt(kind: str | None,
                 hint_set: str | None = None,
                 hint_den: str | None = None) -> str:
    """The full prompt string for one card. ``hint_set``/``hint_den`` are HINTS
    (the caller's modal-set guess) and only ever appear as trailing context —
    never as an instruction to answer with them."""
    k = kind if kind in _LEADS else "strip"
    hint = ""
    if hint_set or hint_den:
        hint = _HINT_LEAD[k] + \
            (f"the set '{hint_set}'. " if hint_set else "") + \
            (f"denominator {hint_den}. " if hint_den else "")
    return _LEADS[k] + _REPLY + hint
