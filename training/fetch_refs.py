"""Download hires reference images for set slugs (pokemontcg.io public CDN).
Usage: python training/fetch_refs.py sv6 sv1 swsh9"""
import sys, time
import httpx
from training.config import REFS_RAW

def fetch_set(slug: str) -> int:
    out = REFS_RAW / slug
    out.mkdir(parents=True, exist_ok=True)
    n, got = 1, 0
    with httpx.Client(timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": "pokemon-card-scanner-training/1.0"}) as c:
        misses = 0
        while misses < 3:  # sets end where consecutive 404s begin
            p = out / f"{slug}-{n}.png"
            if p.exists():
                got += 1; n += 1; continue
            r = c.get(f"https://images.pokemontcg.io/{slug}/{n}_hires.png")
            if r.status_code == 404:
                misses += 1; n += 1; continue
            r.raise_for_status()
            p.write_bytes(r.content)
            got += 1; misses = 0; n += 1
            time.sleep(0.05)
    print(f"{slug}: {got} images")
    return got

if __name__ == "__main__":
    for slug in sys.argv[1:] or ["sv6"]:
        fetch_set(slug)
