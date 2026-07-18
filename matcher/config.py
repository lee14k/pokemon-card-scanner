"""Env config. The matcher is stateless besides INDEX_DIR (a mounted Volume)."""
import os

def token() -> str:
    t = os.environ.get("MATCHER_TOKEN", "").strip()
    if not t:
        raise RuntimeError("MATCHER_TOKEN is required")
    return t

def index_dir() -> str:
    return os.environ.get("INDEX_DIR", "./var/matcher-index")

def model_path() -> str:
    return os.environ.get("MODEL_PATH", "matcher/model/model.onnx")
