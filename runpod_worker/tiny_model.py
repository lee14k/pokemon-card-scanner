"""Build a tiny, randomly initialized Qwen2.5-VL (model + tokenizer + processor)
entirely offline, for verify_batching.py:

    python runpod_worker/tiny_model.py /tmp/tiny-qwen25vl
    python runpod_worker/verify_batching.py /tmp/tiny-qwen25vl

Saved so the worker loads it exactly as it loads the real model
(AutoProcessor / Qwen2_5_VLForConditionalGeneration.from_pretrained(<dir>)), with
no download — useful anywhere the Hub is unreachable or a GPU is unavailable.

Same architecture as Qwen2.5-VL-7B (window-attention vision tower, 2x2 patch
merger, M-RoPE text model), just tiny dims — so batching mechanics (left
padding, per-row image-token placement, M-RoPE position ids, reply slicing)
run through exactly the code paths the real model uses.
"""
import sys
from pathlib import Path

import torch
from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
from transformers import (Qwen2TokenizerFast, Qwen2_5_VLConfig,
                          Qwen2_5_VLForConditionalGeneration, Qwen2_5_VLProcessor,
                          Qwen2VLImageProcessor)

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

SPECIAL = ["<|endoftext|>", "<|im_start|>", "<|im_end|>", "<|vision_start|>",
           "<|vision_end|>", "<|vision_pad|>", "<|image_pad|>", "<|video_pad|>"]
corpus = [
    "This image is a photo of a whole Pokemon trading card. Read the card's printed "
    "name. Reply with ONLY a JSON object: {\"number\": \"126\", \"denominator\": "
    "\"167\", \"set_name\": \"Twilight Masquerade\", \"name\": \"Pikachu\", "
    "\"confidence\": 0.9}. system user assistant You are a helpful assistant.",
] * 50
tok = Tokenizer(models.BPE())
tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
tok.decoder = decoders.ByteLevel()
tok.train_from_iterator(corpus, trainers.BpeTrainer(
    vocab_size=700, special_tokens=SPECIAL,
    initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
hf_tok = Qwen2TokenizerFast(
    tokenizer_object=tok, vocab_file=None, merges_file=None,
    eos_token="<|im_end|>", pad_token="<|endoftext|>",
    additional_special_tokens=SPECIAL[1:])

# Qwen2-VL's chat template (image/text content parts; system prompt injected).
# initializer_range=0.5 below gives the random logits real margins, so the
# replies are long, deterministic noise rather than a near-tie on every token.
TEMPLATE = (
    "{% for message in messages %}"
    "{% if loop.first and message['role'] != 'system' %}"
    "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n{% endif %}"
    "<|im_start|>{{ message['role'] }}\n"
    "{% if message['content'] is string %}{{ message['content'] }}<|im_end|>\n"
    "{% else %}{% for content in message['content'] %}"
    "{% if content['type'] == 'image' %}<|vision_start|><|image_pad|><|vision_end|>"
    "{% elif content['type'] == 'text' %}{{ content['text'] }}{% endif %}"
    "{% endfor %}<|im_end|>\n{% endif %}{% endfor %}"
    "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")

ids = {t: hf_tok.convert_tokens_to_ids(t) for t in SPECIAL}
V = len(hf_tok)
cfg = Qwen2_5_VLConfig(
    text_config=dict(
        vocab_size=V, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=32768,
        rope_scaling={"type": "mrope", "mrope_section": [2, 3, 3]},
        bos_token_id=ids["<|endoftext|>"], eos_token_id=ids["<|im_end|>"],
        pad_token_id=ids["<|endoftext|>"], initializer_range=0.5),
    vision_config=dict(
        depth=2, hidden_size=32, intermediate_size=64, num_heads=2,
        out_hidden_size=64, patch_size=14, spatial_merge_size=2,
        temporal_patch_size=2, window_size=112, fullatt_block_indexes=[1],
        in_channels=3, initializer_range=0.5),
    image_token_id=ids["<|image_pad|>"], video_token_id=ids["<|video_pad|>"],
    vision_start_token_id=ids["<|vision_start|>"],
    vision_end_token_id=ids["<|vision_end|>"],
    bos_token_id=ids["<|endoftext|>"], eos_token_id=ids["<|im_end|>"],
    pad_token_id=ids["<|endoftext|>"], initializer_range=0.5)
torch.manual_seed(0)
model = Qwen2_5_VLForConditionalGeneration(cfg)
model.generation_config.eos_token_id = ids["<|im_end|>"]
model.generation_config.pad_token_id = ids["<|endoftext|>"]
model.save_pretrained(out)

proc_kwargs = dict(image_processor=Qwen2VLImageProcessor(), tokenizer=hf_tok,
                   chat_template=TEMPLATE)
try:
    from transformers import Qwen2VLVideoProcessor
    proc_kwargs["video_processor"] = Qwen2VLVideoProcessor()
except ImportError:
    pass
Qwen2_5_VLProcessor(**proc_kwargs).save_pretrained(out)
n = sum(p.numel() for p in model.parameters())
print(f"saved tiny Qwen2.5-VL to {out}: vocab={V} params={n:,} ids={ids}")
