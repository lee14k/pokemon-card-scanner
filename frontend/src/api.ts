const base =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, "") ||
  (import.meta.env.DEV ? "/api" : "");

export interface TcgPriceRow {
  sub_type_name?: string;
  low_price?: number | null;
  mid_price?: number | null;
  high_price?: number | null;
  market_price?: number | null;
  direct_low_price?: number | null;
  updated_at?: string;
}

export interface CardMarketPriceRow {
  variant_type?: string;
  low?: number | null;
  avg?: number | null;
  trend?: number | null;
  avg1?: number | null;
  avg7?: number | null;
  avg30?: number | null;
  updated_at?: string;
}

export interface CardMatch {
  id: string;
  name: string;
  set_name: string | null;
  number: string | null;
  rarity: string | null;
  images: Record<string, string | null> | null;
  tcgplayer: {
    url?: string;
    prices?: TcgPriceRow[];
  } | null;
  cardmarket: {
    product_url?: string;
    product_name?: string;
    prices?: CardMarketPriceRow[];
  } | null;
  match_score: number;
}

export interface PriceLookupResponse {
  ocr_text_sample: string | null;
  query_fragments: string[];
  matches: CardMatch[];
}

export async function lookupPricesFromImage(
  file: File,
  options?: { cardNameHint?: string; maxResults?: number }
): Promise<PriceLookupResponse> {
  const form = new FormData();
  form.append("image", file);
  if (options?.cardNameHint?.trim()) {
    form.append("card_name_hint", options.cardNameHint.trim());
  }
  if (options?.maxResults != null) {
    form.append("max_results", String(options.maxResults));
  }

  const url = `${base}/v1/cards/price-from-image`;
  const res = await fetch(url, {
    method: "POST",
    body: form,
  });

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

  return body as PriceLookupResponse;
}
