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
    throw new Error(msg || `Request failed (${res.status})`);
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
  return parse(
    await fetch(`${base}/pulls`, { method: "POST", credentials: "include", body: form })
  );
}

export async function listPulls(): Promise<SavedPull[]> {
  return parse(await fetch(`${base}/pulls`, { credentials: "include" }));
}
