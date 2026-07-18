# matcher/Dockerfile — build context = repo root (Railway: root directory "/",
# dockerfile path matcher/Dockerfile), so COPY paths are repo-relative.
# The model artifact is committed at matcher/model/ (small ONNX export).
FROM python:3.12-slim
WORKDIR /srv
COPY matcher/requirements.txt matcher/requirements.txt
RUN pip install --no-cache-dir -r matcher/requirements.txt
COPY matcher/ matcher/
ENV INDEX_DIR=/data MODEL_PATH=matcher/model/model.onnx
CMD ["sh", "-c", "uvicorn matcher.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
