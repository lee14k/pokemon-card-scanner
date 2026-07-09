# Pokédex — Design Spec

**Date:** 2026-07-08
**Status:** Approved pending user review
**Sub-project:** D (fourth of six; A = scanner, B = auth/DB, C = pull-rate stats — all shipped on `main`)

## Product context

The vision's "cute or fun piece": *"how many times have you seen this Pokémon? … like you saw a Pokémon in the wild."* A personal, per-trainer Pokédex built from the pulls they save: a dex page of species seen (with counts and first-seen dates) and a save-time moment that celebrates encounters ("✨ NEW! Bulbasaur registered to your Pokédex!"). Personal only — no sharing, no public surface, no role gating beyond login.

## Decisions log

| Decision | Choice |
|---|---|
| Identity | **Species-level** (any Pikachu card counts as "Pikachu"), not card-level |
| Source data | **All saved pulls** (verified or not), **trainer-confirmed cards (`pull_card`)** — corrections count; instant at save time; verification only gates public stats, not personal fun |
| v1 experience | **Dex page + save-time encounter callouts**; no generation filters/badges/sharing |
| Species computation | **`species` column on `pull_card`, computed at save time** by a normalizer against a **committed National-Dex name list** (no runtime network); migration `0003` + backfill script |
| Encounter semantics | count = total **cards** of that species ever saved (2 Pikachu in one pack = seen twice); NEW = first-ever card(s) of that species |
| Encounters storage | **Not stored** — computed in the save response only; the dex page is the persistent view |
| Tests | **None** (standing user directive); manual smoke only |

## Scope

**In:** migration `0003` (`pull_card.species` Text nullable indexed); `app/dex/` (species data + normalizer + routes); `save_pull` sets species + returns `encounters`; `GET /dex`; frontend Dex view + nav + summary-screen callouts; `scripts/build_species_list.py` (one-time generator, output committed) + `scripts/backfill_species.py` (dev-data backfill).

**Out (explicitly):** automated tests; sharing/public dexes; generation/type filters, badges, shiny tracking; multi-Pokémon (TAG TEAM) handling beyond first-species fallback (not in SWSH/SV packs); server-derived (`pull_card_derived`) as dex source; storing encounter rows; any Railway/env changes.

## Data model & normalizer

- **Migration `0003`:** `pull_card.species` — Text, nullable, indexed. Null = "not a Pokémon / unresolvable" (Trainers, Items, Energy). No other schema changes.
- **Species data:** `app/dex/data/species.json` — committed English National Dex names (Gen 1–9, ~1025), lowercase-keyed: `{"pikachu": "Pikachu", "mr. mime": "Mr. Mime", …}` including punctuation-tricky names (Farfetch'd, Ho-Oh, Nidoran♀/♂, Flabébé, Type: Null…). Generated once by `scripts/build_species_list.py` (PokéAPI, build-time only); runtime never touches the network.
- **Normalizer** `app/dex/species.py :: species_of(card_name) -> str | None`:
  1. iteratively strip right-side suffix tokens: `ex`, `EX`, `GX`, `V`, `VMAX`, `VSTAR`, `BREAK`, `Prism Star`, `◇`;
  2. strip leading form/regional markers: `Radiant`, `Tera`, `Alolan`, `Galarian`, `Hisuian`, `Paldean`, `Origin Forme`, `Therian Forme`;
  3. case-insensitive lookup; on miss retry after dropping a trailing parenthetical;
  4. miss ⇒ `None`. Names containing `&` take the first species (defensive; TAG TEAM isn't in scope's sets).
- **Write path:** `save_pull` sets `species=species_of(name)` per card row. **Backfill:** `scripts/backfill_species.py` for pre-existing rows (dev only; prod not yet deployed).

## Save-time encounters

In `save_pull`, after the pull + cards commit:
1. distinct species of this pull's rows (nulls ignored);
2. one `GROUP BY species` over **all** the trainer's `pull_card` rows for those species → totals (include this pull);
3. NEW iff total == count-within-this-pull;
4. respond with `encounters: [{species, count, new}]`, NEW-first then alphabetical.

`PullOut.encounters: list[EncounterOut] = []` — populated only by save; list/detail return it empty. Encounter computation failure must never fail the save (fall back to `[]`). Anonymous users can't reach this path (save already requires login).

## API & frontend

- `GET /dex` (CurrentTrainer) → `{seen_count, entries: [{species, count, first_seen, image_url}]}`: `GROUP BY species` over the trainer's non-null-species rows; `count(*)`; `first_seen = min(pull.created_at)` via join; `image_url` = art of the most recent card of that species; sorted first_seen desc. 401 anonymous.
- Frontend: `frontend/src/dex/Dex.tsx` (header "Seen: N species", grid of art/species/×count/first-seen, empty state "No Pokémon seen yet — scan a pack!"); "Pokédex" nav button (disabled anonymous, same pattern as My Pulls); summary screen renders `saved.encounters` ("✨ NEW! X registered to your Pokédex!" / "You saw a wild X again (×N)"); `api.ts` gains `getDex()` + `encounters` on `SavedPull`.

## Verification (manual smoke — no automated tests)

1. `alembic upgrade head` applies `0003`; species column present.
2. Normalizer spot-checks: "Pikachu ex"→Pikachu; "Hisuian Zoroark VSTAR"→Zoroark; "Radiant Charizard"→Charizard; "Iono"→None; "Basic Grass Energy"→None.
3. Save pull #1 with real-name cards in the confirmed payload (stub names "Test Mon A" resolve to None, so the smoke supplies real names) → summary shows ✨NEW encounters; `GET /dex` lists species with count/first_seen.
4. Save pull #2 overlapping a species → "seen again (×2)"; dex count increments; seen_count counts species, not cards.
5. Anonymous: no dex nav; direct `GET /dex` → 401. Scanner suite still green; frontend builds.

## Success criteria

- A trainer's saved pulls produce a dex page with correct species counts and first-seen dates, instantly after each save.
- First-time species produce NEW callouts; repeats show incremented counts.
- Non-Pokémon cards never appear in the dex.
- No regression to scanner/auth/pulls/stats; no new infra.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Name variants the normalizer misses (odd promos, future eras) | Miss ⇒ `None` (card silently absent from dex — degrades to "no fun", never wrong data); suffix/prefix lists are data-driven and easy to extend |
| Species list drift (new generations) | Regenerate `species.json` with the committed script when new gens matter |
| Encounter query cost on save | One indexed GROUP BY over a single trainer's rows — negligible; failure falls back to empty encounters |
| Backfill forgotten for old dev rows | Dex simply omits them until `backfill_species.py` runs; prod unaffected (not yet deployed) |
