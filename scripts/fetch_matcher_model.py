"""Download the pinned matcher model for local dev (Docker does its own copy)."""
import hashlib, pathlib, sys, urllib.request

URL = "https://huggingface.co/Qdrant/clip-ViT-B-32-vision/resolve/main/model.onnx"
PINNED_SHA256 = "c68d3d9a200ddd2a8c8a5510b576d4c94d1ae383bf8b36dd8c084f94e1fb4d63"
DEST = pathlib.Path("matcher/model/model.onnx")

def main() -> None:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        print("already present:", DEST); return
    print("downloading", URL)
    urllib.request.urlretrieve(URL, DEST)
    digest = hashlib.sha256(DEST.read_bytes()).hexdigest()
    print("sha256:", digest)
    if PINNED_SHA256 != "PINNED_SHA256" and digest != PINNED_SHA256:
        DEST.unlink(); sys.exit("sha256 mismatch — refusing model")

if __name__ == "__main__":
    main()
