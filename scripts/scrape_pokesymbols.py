#!/usr/bin/env python3
"""
Download Pokémon TCG set metadata and symbol PNGs from pokesymbols.com.

Source listing: https://pokesymbols.com/tcg/sets

This is an unofficial helper for building local reference images (e.g. for
app/data/set_symbols). Respect the site’s terms, robots.txt, and rate limits;
use reasonable delays if you extend this script.

Dependencies:
    pip install -r scripts/requirements-scrape.txt

Examples:
    python scripts/scrape_pokesymbols.py \\
        --json-out app/data/set_symbols/pokesymbols_sets.json \\
        --download-dir app/data/set_symbols
    # Full scrape + PokéWallet index (needs POKEWALLET_API_KEY):
    bash scripts/run_set_symbol_pipeline.sh
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    import httpx
    from bs4 import BeautifulSoup
except ImportError:
    print("Install dependencies: pip install httpx beautifulsoup4", file=sys.stderr)
    sys.exit(1)

BASE = "https://pokesymbols.com"
LIST_URL = f"{BASE}/tcg/sets"
USER_AGENT = (
    "pokemon-card-scanner-scraper/1.0 (+local; contact: your-email; "
    "reads public set listing for offline symbol index)"
)


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=60.0,
    )


def fetch_html(client: httpx.Client, url: str) -> str:
    r = client.get(url)
    r.raise_for_status()
    return r.text


def parse_set_cards(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    root = main or soup

    # <a href="sets/slug-here"> ... <img src="/images/tcg/sets/symbols/slug.png" />
    slug_re = re.compile(r"^sets/[a-z0-9][a-z0-9\-]*$", re.I)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for a in root.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not slug_re.match(href):
            continue
        slug = href.split("/")[-1].strip().lower()
        if not slug or slug in seen:
            continue

        img = a.find("img", src=re.compile(r"/images/tcg/sets/symbols/.+\.png", re.I))
        if not img:
            continue

        symbol_path = (img.get("src") or "").strip()
        if not symbol_path:
            continue

        name = ""
        released = ""
        for p in a.find_all("p"):
            classes = p.get("class") or []
            text = p.get_text(strip=True)
            if not text or text == "View Details":
                continue
            if "Released:" in text:
                released = text.replace("Released:", "").strip()
            elif "text-lg" in classes:
                name = text
            elif not name and "font-semibold" in classes:
                name = text

        if not name:
            alt = (img.get("alt") or "").strip()
            name = re.sub(r"\s+symbol\s*$", "", alt, flags=re.I).strip()

        seen.add(slug)
        symbol_url = urljoin(BASE, symbol_path)
        detail_path = f"/tcg/{href}" if not href.startswith("/") else href
        detail_url = urljoin(BASE, detail_path)

        rows.append(
            {
                "slug": slug,
                "name": name,
                "released": released,
                "symbol_png_url": symbol_url,
                "detail_url": detail_url,
            }
        )

    return rows


def download_png(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = client.get(url)
    r.raise_for_status()
    dest.write_bytes(r.content)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape pokesymbols.com TCG set list + icons.")
    ap.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write set metadata JSON to this path",
    )
    ap.add_argument(
        "--download-dir",
        type=Path,
        default=None,
        help="If set, download each symbol PNG into this directory as {slug}.png",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.35,
        help="Seconds between icon downloads (default: 0.35)",
    )
    args = ap.parse_args()

    if not args.json_out and not args.download_dir:
        ap.error("Provide at least one of --json-out or --download-dir")

    with _client() as client:
        print(f"Fetching {LIST_URL} …", file=sys.stderr)
        html = fetch_html(client, LIST_URL)
        sets_list = parse_set_cards(html)
        print(f"Parsed {len(sets_list)} sets.", file=sys.stderr)

        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(
                json.dumps(sets_list, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"Wrote {args.json_out}", file=sys.stderr)

        if args.download_dir:
            for i, row in enumerate(sets_list):
                url = row["symbol_png_url"]
                slug = row["slug"]
                path = urlparse(url).path
                ext = Path(path).suffix or ".png"
                dest = args.download_dir / f"{slug}{ext}"
                try:
                    download_png(client, url, dest)
                    print(f"[{i + 1}/{len(sets_list)}] {dest.name}", file=sys.stderr)
                except httpx.HTTPError as e:
                    print(f"FAIL {slug}: {e}", file=sys.stderr)
                if args.sleep > 0 and i + 1 < len(sets_list):
                    time.sleep(args.sleep)


if __name__ == "__main__":
    main()
