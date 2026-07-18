# In-House PokéWallet Replacement — Assessment (Sub-project G)

Scoping assessment for building the card catalog + prices + images "PokéWallet API"
on our side. Research verified live on 2026-07-18 (three lenses: catalog sources,
price feeds, image strategy); code inventory from the current repo.

## What we use PokéWallet for today

All PokéWallet traffic goes through one seam — `app/pokewallet.py`, overridable via
`POKEWALLET_BASE_URL` — with exactly four touchpoints:

1. **Keyed card lookup** — `lookup_card_exact(set_id, numerator, set_name)`; used by
   the scan pipeline (`app/pack/matching.py`) and the `/cards/lookup` manual-fix flow.
2. **Card images** — `pokewallet_image_url(card_id)` → `{base}/images/{id}?size=`.
3. **Price blobs** — tcgplayer/cardmarket blocks on card payloads feed the weekly
   price-snapshot stage (sub-project E).
4. **Sets listing** — scraped once by `scripts/build_denominator_table.py`.

The hidden coupling is identifiers: PokéWallet's `set_id` (e.g. `"23876"`) and card
`match_id` are persisted across ~7 places — `pull_card`, `pull_card_derived`,
`card_stat`, `anomaly`, `card_price`, `battle.bot_pack` JSONB — plus
`data/` denominator table and the symbol index. **ID migration is the real cost of
switching, not the API client.**

## What a replacement looks like

- A `card_set` + `card` catalog in our Postgres, ingested from an open dataset;
  `lookup_card_exact` becomes a local DB query (no external API, no key, no rate
  limits, no latency during scans).
- Weekly price refresh reads from the same ingest and feeds the existing
  `PriceSnapshot`/`CardPrice` pipeline unchanged.
- Images mirrored to storage we control and served from our origin.
- API response shapes (`PackCard`, pulls, battles) unchanged — frontend untouched.

## Source options (all claims live-verified 2026-07-18)

### Catalog

| | TCGdex | pokemontcg.io / pokemon-tcg-data |
|---|---|---|
| License | **MIT (verified LICENSE file)** | **None** (no LICENSE; ToS covers API conduct only) |
| Coverage | SWSH+SV same-day updates, 43 set entries | Same span, 43 entries, 7,164 cards counted |
| Bulk access | Self-hostable Docker image of the API | `git clone` of the data repo |
| ID scheme | Zero-padded (`sv01-001`, `sv03.5`) | Unpadded (`sv1-1`, `sv3pt5`) — de facto community standard |
| Health signal | Active community project (v2.46.2, Jun 2026) | Homepage now redirects: "Now part of Scrydex" (paid pivot; free tier future uncertain) |
| Known issues | Self-disclosed price-matching bugs (fix `variants_detailed` in development) | Cardmarket prices can lag new sets |

The two ID schemes are incompatible; pick one canonical, don't merge.

### Prices

Both catalogs embed TCGplayer + Cardmarket price blocks directly on the card object —
the same shape our snapshot pipeline already consumes. The **official TCGplayer and
Cardmarket APIs are both closed to new applicants** in 2026. tcgcsv.com (nightly
TCGplayer mirror) is fresh and free but directly conflicts with TCGplayer's API terms.
Paid fallbacks with cleaner terms if free sources degrade: PokemonPriceTracker
$9.99/mo (commercial license at $99/mo), JustTCG $19/mo, Scrydex $29/mo.

### Images

- **TCGdex CDN** (`assets.tcgdex.net`): low (245×337) + high (600×825) in
  png/webp/jpg; **webp low+high ≈ 79 KB/card → ~0.5 GB for the full catalog**;
  CORS `*`, 1-year cache headers, no auth.
- **images.pokemontcg.io**: stable legacy bucket, but only covers sets through
  ~late 2025 — sets from 2026-01-30 onward serve from `images.scrydex.com`, an
  **undocumented open endpoint on a commercial product's CDN** (could be gated
  any time). Hotlinking (status quo) inherits this risk directly.
- Storage/serving: Cloudflare R2 free tier (10 GB, zero egress) fits the whole
  mirror for $0; Railway Volume ≈ $0.08/mo at webp sizes (+$0.05/GB egress).
- Legal: **no source holds any license to the artwork** (copyright TPCi/Nintendo/
  Creatures/Game Freak); all community mirrors run on "not affiliated" disclaimers
  with no documented takedown precedent. A hobby app mirroring at the smallest
  usable footprint (webp) matches community practice; it is tolerance, not clearance.

## Recommendation

**TCGdex as the canonical source** (unambiguous MIT license, self-hostable for the
bulk crawl, smallest images, active maintenance), with the pokemontcg.io data repo
as a cross-check during ingest. Bulk-mirror webp images (~0.5 GB) to Cloudflare R2
(or the Railway Volume if avoiding a new account matters more than egress), served
through our origin with lazy fallback to the CDN. Prices ride the weekly ingest into
the existing snapshot pipeline; cards from sets released <2 weeks ago tolerate null
prices (already the pipeline's behavior); our existing anomaly detectors are the
sanity layer over TCGdex's known price-matching bugs.

Alternative worth naming: **do nothing** — keep PokéWallet with a working API key.
Zero effort, but keeps the external dependency, its rate limits, and the key
requirement in production.

## Effort & phasing

Roughly sub-project-C-sized (~10–12 tasks):

1. **Schema + ingest** — `card_set`/`card` tables + Alembic migration; ingest script
   (TCGdex → Postgres); weekly refresh stage appended to the existing batch.
2. **Lookup swap** — `lookup_card_exact` reimplemented as a DB query behind the same
   interface; `/cards/lookup` and scan matching unchanged externally.
3. **Images** — mirror job + serving route; `image_url` now points at our origin.
4. **ID migration** — mapping table (PokéWallet set_id/match_id → canonical IDs by
   set + collector number), backfill of the ~7 tables, regenerate the denominator
   table and symbol index keyed to canonical set IDs. *Riskiest step; do last, after
   the new catalog is proven.*

Phase order: catalog + lookup swap first (immediately removes the production
`POKEWALLET_API_KEY` dependency and all rate limits), then images, then price
cutover, then historical ID backfill.

## Key risks

- Artwork copyright is never licensed by any source (mitigate: smallest-footprint
  webp mirror, hobby scale, purgeable).
- TCGdex price-matching bugs until `variants_detailed` ships (mitigate: sanity
  checks + existing anomaly detectors).
- Set-granularity mismatch: sources count 43 SWSH+SV entries vs our 36-set
  denominator table (subsets like Trainer Gallery counted separately) — the spec
  must decide subset merging before ingest.
- Bulk-crawl politeness: self-host TCGdex's Docker image for the one-time crawl
  rather than hammering their hosted API.
