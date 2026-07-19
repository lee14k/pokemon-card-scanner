"""Fetch labeled training strips from a running app into training/data/uploads/.

Usage:
  PYTHONPATH=. python training/fetch_uploads.py \
    --base https://<app> --email admin@x --password ...

Logs in (cookie auth), downloads /admin/training/export, and replaces
training/data/uploads/ with the unpacked strips + manifest.json. Idempotent:
each run wipes and rewrites uploads/."""
import argparse
import io
import pathlib
import shutil
import sys
import tarfile
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

from training.config import UPLOADS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="app base URL")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        r = c.post(f"{base}/auth/cookie/login",
                   data={"username": args.email, "password": args.password})
        if r.status_code not in (200, 204):
            sys.exit(f"login failed: {r.status_code} {r.text[:200]}")
        r = c.get(f"{base}/admin/training/export")
        r.raise_for_status()

    if UPLOADS.exists():
        shutil.rmtree(UPLOADS)
    (UPLOADS / "strips").mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        tar.extractall(UPLOADS, filter="data")

    import json
    manifest = json.loads((UPLOADS / "manifest.json").read_text())
    strips = manifest["strips"]
    by = Counter((s["split"], s["tier"]) for s in strips)
    print(f"fetched {len(strips)} labeled strips -> {UPLOADS}")
    for (split, tier), n in sorted(by.items()):
        print(f"  {split}/{tier}: {n}")


if __name__ == "__main__":
    main()
