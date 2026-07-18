# matcher/Dockerfile — build context = repo root (Railway: root directory "/",
# dockerfile path matcher/Dockerfile), so COPY paths are repo-relative.
FROM python:3.12-slim AS model
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
ARG MODEL_URL=https://huggingface.co/Qdrant/clip-ViT-B-32-vision/resolve/main/model.onnx
ARG MODEL_SHA256=c68d3d9a200ddd2a8c8a5510b576d4c94d1ae383bf8b36dd8c084f94e1fb4d63
RUN curl -L -o /model.onnx "$MODEL_URL" && echo "$MODEL_SHA256  /model.onnx" | sha256sum -c -

FROM python:3.12-slim
WORKDIR /srv
COPY matcher/requirements.txt matcher/requirements.txt
RUN pip install --no-cache-dir -r matcher/requirements.txt
COPY matcher/ matcher/
COPY --from=model /model.onnx matcher/model/model.onnx
ENV INDEX_DIR=/data MODEL_PATH=matcher/model/model.onnx
CMD ["sh", "-c", "uvicorn matcher.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
