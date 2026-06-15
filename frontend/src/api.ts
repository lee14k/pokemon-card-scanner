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
  match_id: string | null;
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
