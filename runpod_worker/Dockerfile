# RunPod Serverless worker for Qwen2.5-VL-7B card identification.
#
# IMPORTANT interpreter note: runpod/base ships SIX pythons (3.7-3.11). `pip`
# installs into python3.11, there is NO bare `python`, and `python3` is 3.10 —
# so the CMD must be python3.11 explicitly or every worker exits at start
# ("exec: python: not found" -> RunPod shows "All workers are unhealthy").
#
# Deploy (use a VERSIONED tag so RunPod actually pulls the new image):
#   1. Build + push (from repo root, cross-built for RunPod's x86 hosts):
#        docker buildx build --platform linux/amd64 -t <you>/pcs-vlm:v2 --push runpod_worker/
#   2. RunPod → Serverless → your endpoint → Manage/Edit → set image to the
#      new tag (<you>/pcs-vlm:v2) and save; workers restart on the new image.
#      (New endpoint: GPU 24GB+ e.g. A5000/L4/3090, container disk 40GB+,
#      FlashBoot on.)
#   3. App env stays: VLM_ENDPOINT=https://api.runpod.ai/v2/<endpoint-id>,
#      VLM_API_KEY=<runpod-api-key>
#   The model (~16GB) downloads from HuggingFace on first cold start and is
#   cached on the worker volume thereafter. Set VLM_MODEL to override.
FROM runpod/base:0.6.2-cuda12.1.0

# torch + torchvision must be installed together from the cu121 index —
# transformers' Qwen2.5-VL image processor imports torchvision at load time.
RUN pip install --no-cache-dir \
    torch==2.5.1 torchvision==0.20.1 \
    --index-url https://download.pytorch.org/whl/cu121
COPY requirements.txt /req.txt
RUN pip install --no-cache-dir -r /req.txt

COPY handler.py /handler.py
CMD ["python3.11", "-u", "/handler.py"]
