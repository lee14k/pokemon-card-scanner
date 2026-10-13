// Wire types for the FastAPI backend. Shared by the web app (frontend/) and the
// mobile app (mobile/) — both talk to the same endpoints, so both read the same
// shapes. Keep this file free of platform APIs (no DOM, no React Native).

// Per-card background-identification state, shared by the live flow and the
// batch (pack/binder) VLM follow-up. Absent means "no follow-up applies to this
// card" — treat it exactly like "ok". "dup_prompt" is live-only.
export type CardState = "ok" | "pending_vlm" | "vlm_failed" | "dup_prompt";

export interface PackCard {
  row_index: number;
  card_number: string | null;
  set_id: string | null;
  set_code: string | null;
  set_name: string | null;
  name: string | null;
  rarity: string | null;
  image_url: string | null;
  match_id: string | null; // PokéWallet card id — reserved for sub-project B persistence
  confidence: number;
  low_confidence_reason: string | null;
  needs_review?: boolean;
  price_usd_low?: number | null;
  price_usd_high?: number | null;
  state?: CardState;
}

export interface CodeCardResult {
  code: string | null;
  confidence: number;
  format_ok: boolean;
}

export interface PackScanResponse {
  cards: PackCard[];
  code_card: CodeCardResult;
  pack_confidence: number;
  segmentation_warning: string | null;
  // Present only while a background VLM pass is still identifying the flagged
  // rows (which carry state: "pending_vlm"). Poll it with getPackFollowup().
  scan_id?: string | null;
}

export interface SetInfo {
  set_id: string;
  set_code: string | null;
  set_name: string;
  denominators: string[];
  era: string;
}

export interface CaptureMeta {
  guide_positions: number[];
  image_dims: [number, number];
  declared_count: number;
}

export interface ScanProgressEvent {
  stage: string; // "decoded" | "cards_found" | "identifying" | "done" | ...
  count?: number;
  done?: number;
  total?: number;
}

export interface Trainer {
  id: string;
  email: string;
  handle: string;
  role: string;
  is_active: boolean;
}

export interface Encounter {
  species: string;
  count: number;
  new: boolean;
}

export interface SavedPull {
  id: string;
  created_at: string;
  capture_path: string;
  pack_confidence: number;
  segmentation_warning: string | null;
  code: string | null;
  code_format_ok: boolean;
  verified: boolean;
  cards: PackCard[];
  encounters: Encounter[];
  estimated_value?: number | null;
  priced_as_of?: string | null;
}

export interface SavePullMeta {
  capture_path: string;
  pack_confidence: number;
  segmentation_warning: string | null;
  capture_meta?: CaptureMeta | null;
  live_session_id?: string;
}

// ── Live scan ────────────────────────────────────────────────────────────────
export type LiveCardState = CardState;
export type LiveEventKind = "card" | "code_card" | "duplicate_prompt" | "no_card" | "unreadable";
export interface LiveCard extends PackCard {
  state?: LiveCardState;
}
export interface LiveFrameOut {
  event: LiveEventKind;
  card: PackCard | null;
  pending_vlm: boolean;
  code_card: CodeCardResult | null;
  cards_count: number;
}
export interface LiveState {
  cards: LiveCard[];
  code_card: CodeCardResult | null;
  any_pending: boolean;
}

// ── Stats / admin ────────────────────────────────────────────────────────────
export interface SetSummary {
  set_id: string;
  verified_pack_count: number;
}
export interface SetDetail {
  set_id: string;
  verified_pack_count: number;
  cards: {
    match_id: string;
    card_number: string | null;
    name: string | null;
    hits: number;
    packs: number;
    raw_rate: number;
    blended_rate: number;
  }[];
  rarities: { rarity: string; packs_with_rarity: number; raw_rate: number; blended_rate: number }[];
}
export interface AnomalyRow {
  id: string;
  detector: string;
  target_type: string;
  set_id: string;
  card_match_id: string | null;
  severity: number;
  detail: Record<string, unknown>;
  status: string;
}
export interface AdminTrainer {
  id: string;
  email: string;
  handle: string;
  role: string;
}

// ── Pokédex ──────────────────────────────────────────────────────────────────
export interface DexEntry {
  species: string;
  count: number;
  first_seen: string;
  image_url: string | null;
}
export interface DexOut {
  seen_count: number;
  entries: DexEntry[];
}

// ── Binder scan → Collection ─────────────────────────────────────────────────
export interface BinderCard extends PackCard {
  cell: [number, number, number, number];
  thumb_b64: string | null;
  price_usd_low?: number | null;
  price_usd_high?: number | null;
}
export interface BinderScan {
  cards: BinderCard[];
  grid: { rows: number; cols: number };
  page_confidence: number;
  // Same contract as PackScanResponse.scan_id — poll it with getBinderFollowup().
  scan_id?: string | null;
}

// Background-VLM follow-up poll (pack + binder). Both batch scans answer
// immediately and finish identifying their flagged rows in the background; the
// scan response's `scan_id` addresses that work. Poll until `done` (or until no
// card is `pending_vlm` any more) and merge the patched rows in. A 404 means the
// entry expired (30 min TTL) or never existed — the cards on screen are then
// final as far as the client is concerned.
export interface ScanFollowup<C extends PackCard = PackCard> {
  cards: C[];
  any_pending: boolean;
  done: boolean;
}

export interface CollectionSaveOut {
  added: number;
  incremented: number;
  total_cards: number;
  encounters: Encounter[];
  // Cards the server refused to save: a flagged cell the user never confirmed,
  // or a cell with no identity at all. Optional so a client that is newer than
  // the server it is talking to still type-checks — read them with `?? 0`.
  skipped?: number;
  skipped_rows?: number[];
}

// What POST /collection accepts: the reviewed card plus the user's verdict.
// `confirmed` is meaningful only for a FLAGGED card — it says the user fixed it
// or explicitly kept it. Absent/false on a flagged card means the server skips
// it (nothing the scanner was unsure about lands in the collection by default).
// Unflagged cards ignore the field entirely.
export interface CollectionSaveCard extends BinderCard {
  confirmed?: boolean;
}

export interface CollectionCardOut {
  id: string;
  set_code: string | null;
  set_name: string | null;
  card_number: string | null;
  name: string | null;
  image_url: string | null;
  qty: number;
  price_usd_low?: number | null;
  price_usd_high?: number | null;
}
export interface CollectionOut {
  cards: CollectionCardOut[];
  total_qty: number;
  estimated_value: number | null;
  priced_as_of: string | null;
}

// ── Battles ──────────────────────────────────────────────────────────────────
export interface BattleCard {
  name: string | null;
  price: number | null;
}
export interface BattleSide {
  label: string;
  score: number | null;
  cards: BattleCard[];
}
export interface Battle {
  id: string;
  mode: string;
  status: string;
  created_at: string;
  resolved_at: string | null;
  outcome: string;
  me: BattleSide;
  opponent: BattleSide;
}
export interface BattleList {
  wins: number;
  losses: number;
  ties: number;
  battles: Battle[];
}
