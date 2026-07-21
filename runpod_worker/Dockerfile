# RunPod Serverless worker for Qwen2.5-VL-7B card identification.
#
# Deploy:
#   1. Build + push:  docker build -t <you>/pcs-vlm:latest runpod_worker/
#                     docker push <you>/pcs-vlm:latest
#   2. RunPod → Serverless → New Endpoint → this image, GPU 24GB+ (e.g. A5000/
#      L4/3090), container disk 40GB+ (model is ~16GB), FlashBoot on.
#   3. Copy the Endpoint ID + your RunPod API key into the app env:
#        VLM_ENDPOINT=https://api.runpod.ai/v2/<endpoint-id>
#        VLM_API_KEY=<runpod-api-key>
#   The model downloads from HuggingFace on first cold start (cached on the
#   worker volume thereafter). Set VLM_MODEL to override.
FROM runpod/base:0.6.2-cuda12.1.0

RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cu121 || true
COPY requirements.txt /req.txt
RUN pip install --no-cache-dir -r /req.txt

COPY handler.py /handler.py
CMD ["python", "-u", "/handler.py"]
