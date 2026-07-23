const base =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, "") ||
  (import.meta.env.DEV ? "/api" : "");

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

// Carries the HTTP status alongside the message so callers that need to branch
// on it (e.g. live-scan session recovery on 404) don't have to string-match
// error text. Still a plain Error for everyone else's `e instanceof Error`.
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const msg =
      typeof body === "object" && body !== null && "detail" in body
        ? JSON.stringify((body as { detail: unknown }).detail)
        : text || res.statusText;
    throw new ApiError(msg || `Request failed (${res.status})`, res.status);
  }
  return body as T;
}

export async function scanPack(
  staircase: Blob,
  codeCard: Blob,
  meta?: CaptureMeta
): Promise<PackScanResponse> {
  const form = new FormData();
  form.append("staircase", staircase, "staircase.jpg");
  form.append("code_card", codeCard, "code.jpg");
  if (meta) form.append("capture_meta", JSON.stringify(meta));
  return parse(await fetch(`${base}/scan/pack`, { method: "POST", body: form }));
}

export interface ScanProgressEvent {
  stage: string; // "decoded" | "cards_found" | "identifying" | "done" | ...
  count?: number;
  done?: number;
  total?: number;
}

// Progressive variant of scanPack(): same request, but reads a
// text/event-stream response so the caller can render stage-by-stage
// progress (skeleton rows, "identifying 3/9", etc) instead of a bare
// spinner. EventSource can't POST a body, so this hand-rolls SSE parsing
// over a fetch() ReadableStream. Any parse/network/stream error rejects —
// callers should catch and fall back to scanPack().
export async function scanPackStream(
  staircase: Blob,
  codeCard: Blob,
  meta: CaptureMeta | undefined,
  onProgress: (ev: ScanProgressEvent) => void
): Promise<PackScanResponse> {
  const form = new FormData();
  form.append("staircase", staircase, "staircase.jpg");
  form.append("code_card", codeCard, "code.jpg");
  if (meta) form.append("capture_meta", JSON.stringify(meta));

  const res = await fetch(`${base}/scan/pack/stream`, { method: "POST", body: form });
  if (!res.ok) {
    // Mirror parse()'s error shape (throws ApiError) so callers see a
    // consistent failure regardless of which scan path they took.
    return parse(res);
  }
  if (!res.body) {
    // 200 with no readable body (should not happen for a fetch Response once
    // ReadableStream is feature-detected, but guard rather than risk
    // silently mis-parsing raw SSE text as a PackScanResponse below).
    throw new Error("scan stream: response has no body");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  // Parses one complete "event:\ndata:\n\n" frame (blank-line separated, per
  // SSE). Lines starting with ":" are comments (our heartbeats) — ignored.
  const handleFrame = (frame: string): PackScanResponse | undefined => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (dataLines.length === 0) return undefined;
    const data = JSON.parse(dataLines.join("\n"));
    if (event === "progress") {
      onProgress(data as ScanProgressEvent);
      return undefined;
    }
    if (event === "error") {
      throw new Error(data?.message || "scan failed");
    }
    if (event === "result") {
      return data as PackScanResponse;
    }
    return undefined;
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (value) buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const result = handleFrame(frame);
      if (result) return result;
    }
    if (done) break;
  }
  throw new Error("scan stream ended without a result");
}

export async function lookupCard(
  setId: string,
  number: string
): Promise<{ found: boolean; card: PackCard | null }> {
  const params = new URLSearchParams({ set_id: setId, number });
  return parse(await fetch(`${base}/cards/lookup?${params}`));
}

export async function getSets(): Promise<SetInfo[]> {
  return parse(await fetch(`${base}/sets`));
}

export interface Trainer {
  id: string;
  email: string;
  handle: string;
  role: string;
  is_active: boolean;
}

export async function register(email: string, password: string, handle: string): Promise<Trainer> {
  return parse(
    await fetch(`${base}/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, password, handle }),
    })
  );
}

export async function login(email: string, password: string): Promise<void> {
  const form = new URLSearchParams({ username: email, password });
  const res = await fetch(`${base}/auth/cookie/login`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    credentials: "include",
    body: form,
  });
  if (!res.ok) throw new Error((await res.text()) || `login failed (${res.status})`);
}

export async function logout(): Promise<void> {
  await fetch(`${base}/auth/cookie/logout`, { method: "POST", credentials: "include" });
}

export async function me(): Promise<Trainer | null> {
  const res = await fetch(`${base}/users/me`, { credentials: "include" });
  if (res.status === 401) return null;
  return parse(res);
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

export async function savePull(
  staircase: Blob,
  codeCard: Blob,
  cards: PackCard[],
  meta: {
    capture_path: string;
    pack_confidence: number;
    segmentation_warning: string | null;
    capture_meta?: CaptureMeta | null;
    live_session_id?: string;
  }
): Promise<SavedPull> {
  const form = new FormData();
  form.append("staircase", staircase, "staircase.jpg");
  form.append("code_card", codeCard, "code.jpg");
  form.append("cards", JSON.stringify(cards));
  form.append("capture_path", meta.capture_path);
  form.append("pack_confidence", String(meta.pack_confidence));
  if (meta.segmentation_warning) form.append("segmentation_warning", meta.segmentation_warning);
  if (meta.capture_meta) form.append("capture_meta", JSON.stringify(meta.capture_meta));
  if (meta.live_session_id) form.append("live_session_id", meta.live_session_id);
  return parse(
    await fetch(`${base}/pulls`, { method: "POST", credentials: "include", body: form })
  );
}

export async function listPulls(): Promise<SavedPull[]> {
  return parse(await fetch(`${base}/pulls`, { credentials: "include" }));
}

export async function patchPullCode(
  pullId: string,
  code: Blob
): Promise<{ verified: boolean; code: string | null }> {
  const form = new FormData();
  form.append("code_card", code, "code.jpg");
  return parse(
    await fetch(`${base}/pulls/${pullId}/code`, {
      method: "PATCH",
      credentials: "include",
      body: form,
    })
  );
}

export type LiveCardState = "ok" | "pending_vlm" | "vlm_failed" | "dup_prompt";
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

export async function liveStart(): Promise<string> {
  const { session_id } = await parse<{ session_id: string }>(
    await fetch(`${base}/scan/live/start`, { method: "POST", credentials: "include" })
  );
  return session_id;
}

export async function liveFrame(sid: string, card: Blob, strip?: Blob): Promise<LiveFrameOut> {
  const form = new FormData();
  form.append("card", card, "card.jpg");
  if (strip) form.append("strip", strip, "strip.jpg");
  const res = await fetch(`${base}/scan/live/${sid}/frame`, {
    method: "POST",
    credentials: "include",
    body: form,
  });
  if (res.status === 409) throw { status: 409 };
  return parse(res);
}

export async function liveState(sid: string): Promise<LiveState> {
  return parse(await fetch(`${base}/scan/live/${sid}`, { credentials: "include" }));
}

export function liveCardImageUrl(sid: string, row: number): string {
  return `${base}/scan/live/${sid}/card/${row}/image`;
}

export async function liveDuplicate(sid: string, row: number, add: boolean): Promise<void> {
  await parse(
    await fetch(`${base}/scan/live/${sid}/card/${row}/duplicate`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ add }),
    })
  );
}

export async function liveReplace(sid: string, row: number): Promise<void> {
  await parse(
    await fetch(`${base}/scan/live/${sid}/card/${row}/replace`, {
      method: "POST",
      credentials: "include",
    })
  );
}

export async function liveFinish(sid: string): Promise<PackScanResponse> {
  return parse(
    await fetch(`${base}/scan/live/${sid}/finish`, {
      method: "POST",
      credentials: "include",
    })
  );
}

export interface SetSummary { set_id: string; verified_pack_count: number; }
export interface SetDetail {
  set_id: string;
  verified_pack_count: number;
  cards: { match_id: string; card_number: string | null; name: string | null; hits: number; packs: number; raw_rate: number; blended_rate: number; }[];
  rarities: { rarity: string; packs_with_rarity: number; raw_rate: number; blended_rate: number; }[];
}
export interface AnomalyRow {
  id: string; detector: string; target_type: string; set_id: string;
  card_match_id: string | null; severity: number; detail: Record<string, unknown>; status: string;
}
export interface AdminTrainer { id: string; email: string; handle: string; role: string; }

export async function statsSets(): Promise<SetSummary[]> {
  return parse(await fetch(`${base}/stats/sets`, { credentials: "include" }));
}
export async function statsSetDetail(setId: string): Promise<SetDetail> {
  return parse(await fetch(`${base}/stats/sets/${encodeURIComponent(setId)}`, { credentials: "include" }));
}
export async function statsAnomalies(status = "open"): Promise<AnomalyRow[]> {
  return parse(await fetch(`${base}/stats/anomalies?status=${status}`, { credentials: "include" }));
}
export async function updateAnomaly(id: string, status: string): Promise<AnomalyRow> {
  return parse(await fetch(`${base}/stats/anomalies/${id}`, {
    method: "PATCH", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ status }),
  }));
}
export async function recomputeStats(): Promise<void> {
  const res = await fetch(`${base}/admin/stats/recompute`, { method: "POST", credentials: "include" });
  if (!res.ok) throw new Error(`recompute failed (${res.status})`);
}
export async function adminTrainers(query = ""): Promise<AdminTrainer[]> {
  return parse(await fetch(`${base}/admin/trainers?query=${encodeURIComponent(query)}`, { credentials: "include" }));
}
export async function setTrainerRole(id: string, role: string): Promise<AdminTrainer> {
  return parse(await fetch(`${base}/admin/trainers/${id}/role`, {
    method: "PATCH", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ role }),
  }));
}

export interface Encounter { species: string; count: number; new: boolean; }
export interface DexEntry { species: string; count: number; first_seen: string; image_url: string | null; }
export interface DexOut { seen_count: number; entries: DexEntry[]; }

export async function getDex(): Promise<DexOut> {
  return parse(await fetch(`${base}/dex`, { credentials: "include" }));
}

// ── Binder scan → Collection ──────────────────────────────────────────────────
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
}
export interface CollectionSaveOut {
  added: number;
  incremented: number;
  total_cards: number;
  encounters: Encounter[];
}

// Scan one binder page into a grid of PackCard-shaped cells (with thumbnails).
// A decode failure or a page with no readable cards comes back as 422 — that
// case rejects with `{ code: "no_cards_found" }` so the caller can show the
// retake state without string-matching the error body.
export async function scanBinder(page: Blob): Promise<BinderScan> {
  const form = new FormData();
  form.append("page", page, "page.jpg");
  const res = await fetch(`${base}/scan/binder`, {
    method: "POST",
    credentials: "include",
    body: form,
  });
  if (res.status === 422) throw { code: "no_cards_found" };
  return parse<BinderScan>(res);
}

export async function saveToCollection(cards: BinderCard[]): Promise<CollectionSaveOut> {
  return parse(
    await fetch(`${base}/collection`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ cards }),
    })
  );
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

export async function getCollection(): Promise<CollectionOut> {
  return parse(await fetch(`${base}/collection`, { credentials: "include" }));
}

export async function patchCollectionQty(id: string, qty: number): Promise<void> {
  await parse(
    await fetch(`${base}/collection/${id}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ qty }),
    })
  );
}

export async function deleteCollectionCard(id: string): Promise<void> {
  await parse(
    await fetch(`${base}/collection/${id}`, { method: "DELETE", credentials: "include" })
  );
}

export interface BattleCard { name: string | null; price: number | null; }
export interface BattleSide { label: string; score: number | null; cards: BattleCard[]; }
export interface Battle {
  id: string; mode: string; status: string; created_at: string; resolved_at: string | null;
  outcome: string; me: BattleSide; opponent: BattleSide;
}
export interface BattleList { wins: number; losses: number; ties: number; battles: Battle[]; }

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return parse(await fetch(`${base}${path}`, {
    method: "POST", credentials: "include",
    headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }));
}
export const randomBattle = (pullId: string) => postJson<Battle>("/battles/random", { pull_id: pullId });
export const botBattle = (pullId: string) => postJson<Battle>("/battles/bot", { pull_id: pullId });
export const friendBattle = (pullId: string, handle: string) =>
  postJson<Battle>("/battles/friend", { pull_id: pullId, opponent_handle: handle });
export const acceptBattle = (id: string, pullId: string) =>
  postJson<Battle>(`/battles/${id}/accept`, { pull_id: pullId });
export const declineBattle = (id: string) => postJson<Battle>(`/battles/${id}/decline`, {});
export async function listBattles(): Promise<BattleList> {
  return parse(await fetch(`${base}/battles`, { credentials: "include" }));
}
export async function battleInbox(): Promise<Battle[]> {
  return parse(await fetch(`${base}/battles/inbox`, { credentials: "include" }));
}
