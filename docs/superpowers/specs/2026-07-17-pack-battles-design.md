# Pack Battles — Design Spec

**Date:** 2026-07-17
**Status:** Approved pending user review
**Sub-project:** F (sixth and final; A scanner, B auth/DB, C pull-rate stats, D Pokédex, E pricing — all shipped on `main`)

## Product context

The vision's competitive piece: *"E-pack battle — has to be packs you truly opened — tell you how great your pack is — random pack battles or 'friend request' pack battles … you could even build in 'fake' randomly generated pack battles."* Battles pit **verified** pulls (the code-card "truly opened" guarantee) against each other, scored by market value (sub-project E's prices). Three modes: instant anonymized random, consensual friend challenges, and clearly-labeled bot opponents.

## Decisions log

| Decision | Choice |
|---|---|
| Pack score | **Monetary value** — sum of price midpoints of the pull's client-confirmed cards against the latest done price snapshot (the same number My Pulls shows). Rarity-luck scoring deferred. |
| Eligibility | **Verified pulls only**, all modes, enforced server-side ("packs you truly opened") |
| Random mode privacy | **Anonymized**: opponent's cards + score visible as "a wild trainer's pack"; handle/identity never serialized; opponent not notified, unaffected, result records for the initiator only. (B's owner-only pull privacy is preserved in spirit: exposure is real but unlinkable.) |
| Friend mode | Challenge by handle → pending in opponent's inbox → accept (pick own verified pull) or decline; full mutual consent; handles shown; counts for both |
| Bot mode | Instant, clearly labeled; synthetic pack sampled from the challenger's set's priced cards, weighted by blended pull rates when available |
| Records | **Personal battle history + W-L-T tally only**; no leaderboards/profiles in v1 |
| Score immutability | Scores + winner computed once at resolution and stored; later price changes never alter past battles |
| Tests | **None** (standing directive); manual smoke only |

## Scope

**In:** migration `0005` (`battle` table); `app/battles.py` (six endpoints + serialization guard); pack-score helper shared with E's enrichment; bot-pack generator; Battles page (new battle / inbox / history / tally) + save-summary "Battle this pack" nudge; `api.ts` client.

**Out (explicitly):** automated tests; leaderboards, public profiles, notifications beyond the in-app inbox; wagers/rewards; live/real-time battles; rarity-luck scoring; battle chat; rematch flows; bot difficulty settings; any new infra/env.

## Data model (migration `0005`)

**`battle`**: `id` UUID PK; `mode` (`random｜friend｜bot`); `status` (`pending｜resolved｜declined` — pending only for friend); `challenger_id` FK→trainer (indexed); `challenger_pull_id` FK→pull; `opponent_id` FK→trainer null (indexed; set internally for random but never serialized); `opponent_pull_id` FK→pull null; `bot_pack` JSONB null (`[{name, match_id, price_usd_low, price_usd_high}]`); `challenger_score` / `opponent_score` float null; `winner` (`challenger｜opponent｜tie`) null; `created_at`, `resolved_at` timestamptz.

## Modes & resolution

- **Random (instant):** challenger's verified pull vs a random *other* trainer's verified pull (`ORDER BY random()`, excluding own). Inserted already-resolved. Empty pool → 409 "no opponents yet — try a bot battle."
- **Friend (async):** `pending` row visible in the opponent's inbox; accept (with own verified pull) → resolved; decline → `declined` (challenger sees it). Guards: no self-challenge; unknown handle → 404; only the named opponent may accept/decline; the challenger's pack locks at creation.
- **Bot (instant):** fabricate N cards (N = challenger's card count) sampled from the latest price snapshot's cards of the challenger's pull's set — weighted by C's `card_stat` blended rates when present, else uniform; empty set-universe → sample across all priced cards (logged). Bot prices come from the snapshot, so bot scores are honest.
- **Resolution (all modes):** `pack_score` both sides → higher wins, equal → tie; scores + winner stored, immutable. Random/bot never touch the opponent's history; friend counts for both.
- **No price snapshot exists** → 409 "prices not available yet" (never silent 0–0 battles).

**Scoring:** `pack_score(pull)` = sum of `(low+high)/2` for the pull's `pull_card` rows priced in the latest done snapshot (unpriced contribute 0) — computed via the same price-map helper E added (extracted for shared use, not duplicated). Bot packs scored from stored `bot_pack` prices identically.

## API (`app/battles.py`, all CurrentTrainer)

`POST /battles/random {pull_id}`; `POST /battles/bot {pull_id}`; `POST /battles/friend {pull_id, opponent_handle}`; `POST /battles/{id}/accept {pull_id}`; `POST /battles/{id}/decline`; `GET /battles` (history newest-first + `{wins, losses, ties}` tally); `GET /battles/inbox` (pending challenges addressed to me).

Serialization: one `battle_to_out(battle, viewer)` shapes every response — sides render as `{label: handle | "wild trainer" | "bot", score, cards: [{name, price}]}`; random mode strips opponent identity; other parties' pull IDs are never exposed; ownership checks 404 unrelated battles. Verified-only + ownership enforced on `pull_id` inputs (400 on unverified, 404 on not-yours).

## Frontend

Battles page (nav next to Pokédex, logged-in only): W-L-T tally header; New Battle panel (verified-pull picker + Random / Bot / Challenge-by-handle); inbox with accept (pull picker) / decline; expandable history rows (both packs' cards + values, outcome badge 🏆/💀/🤝, "vs @handle" / "vs a wild trainer's pack" / "vs BOT"). Save-summary adds "⚔️ Battle this pack" (verified saves only) jumping to Battles with that pull preselected. `api.ts` gains battle types + six calls.

## Verification (manual smoke — no automated tests)

1. `alembic upgrade head` applies `0005`.
2. Seed two trainers with verified pulls + a price snapshot (existing stub flow): random battle resolves with correct scores; response JSON contains **no opponent identity**; opponent's history unaffected.
3. Friend: challenge → appears in opponent inbox → accept resolves in **both** histories with handles; decline path shows declined.
4. Bot: generated pack matches the challenger's set, priced from the snapshot; battle labeled bot.
5. Guards: unverified pull → 400; someone else's pull_id → 404; self-challenge → 400; empty random pool → 409; no snapshot → 409.
6. Tally arithmetic matches history; scanner suite + frontend build green; no stray processes.

## Success criteria

- All three modes work end-to-end with value-based scoring and immutable results.
- Verified-only eligibility and the anonymity guarantee hold at the API layer (not just the UI).
- Friend consent flow (pending/accept/decline) works both directions.
- No regression to prior sub-projects; no new infra.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Random-pool cold start (few trainers) | Graceful 409 nudging to bot battles; pool grows with adoption |
| Anonymized packs theoretically re-identifiable (unique card combos) | Accepted for v1 at indie scale; no handle/id ever serialized; revisit if the pool grows |
| Value scores 0–0 when cards unpriced | 409 when no snapshot exists; unpriced cards contribute 0 otherwise (same semantics trainers already see in My Pulls) |
| Friend-challenge spam | Inbox-only (no push), decline is one tap; rate limiting deferred until real abuse |
| Bot packs feel unfair (too good/bad) | Sampled from real set universe weighted by real pull rates; tuning knobs deferred |
