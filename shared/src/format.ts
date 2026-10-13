// Copy and formatting shared by the web and mobile apps, so a flagged card, a
// price or a failed login reads the same on every screen.

import { ApiError } from "./client";
import type { ScanProgressEvent } from "./types";

/** Machine reason → friendly copy for a flagged card. */
export const REASON_TEXT: Record<string, string> = {
  unreadable_strip: "Couldn't read this row",
  number_ambiguous: "Couldn't read the card number",
  set_ambiguous: "Couldn't tell which set this is from",
  no_db_match: "Card not found in the database",
};

export function reasonText(reason: string | null | undefined): string {
  return (reason && REASON_TEXT[reason]) || "Needs review";
}

export function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

/** Short money for tight spots (stat tiles): "$41.75", "$1.2k", "$12k". */
export function compactUsd(n: number): string {
  if (n >= 10000) return `$${Math.round(n / 1000)}k`;
  if (n >= 1000) return `$${(n / 1000).toFixed(1)}k`;
  return usd(n);
}

/** "$1.00", "$1.00–$2.50", or null when the card is unpriced. */
export function priceRange(lo: number | null | undefined, hi: number | null | undefined): string | null {
  const l = lo ?? null;
  const h = hi ?? null;
  if (l == null && h == null) return null;
  if (l != null && h != null) return l === h ? usd(l) : `${usd(l)}–${usd(h)}`;
  return usd((l ?? h) as number);
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function shortDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Status line for the progressive pack scan. */
export function scanStageText(ev: Partial<ScanProgressEvent>): string {
  switch (ev.stage) {
    case "decoded":
      return "Reading photo…";
    case "cards_found":
      return `Found ${ev.count ?? "…"} cards — identifying…`;
    case "identifying":
      return `Identifying cards… ${ev.done ?? 0}/${ev.total ?? ev.count ?? "?"}`;
    case "done":
      return "Finishing up…";
    default:
      return "Reading cards…";
  }
}

/** Turn the backend's machine-readable segmentation_warning into a sentence. */
export function friendlyWarning(warning: string): string {
  const guided = warning.match(/detected (\d+) rows, declared (\d+)/);
  if (guided) {
    return `We found ${guided[1]} cards but you said ${guided[2]} — retake or continue?`;
  }
  const ungrided = warning.match(/detected (\d+) rows/);
  if (ungrided) {
    return `We found ${ungrided[1]} cards — double-check the list, then retake or continue.`;
  }
  return "We couldn't read the capture cleanly — retake or continue.";
}

export type BattleOutcome = "win" | "loss" | "tie" | string;

export function outcomeLabel(o: BattleOutcome): string {
  if (o === "win") return "WIN";
  if (o === "loss") return "LOSS";
  if (o === "tie") return "TIE";
  return o.toUpperCase();
}

// fastapi-users answers auth failures with bare codes; say what they mean.
const ERROR_CODES: Record<string, string> = {
  LOGIN_BAD_CREDENTIALS: "That email and password don't match a trainer.",
  LOGIN_USER_NOT_VERIFIED: "This account isn't verified yet.",
  REGISTER_USER_ALREADY_EXISTS: "A trainer with that email already exists — try logging in.",
  REGISTER_HANDLE_ALREADY_EXISTS: "That handle is taken — pick another.",
  REGISTER_INVALID_PASSWORD: "That password isn't allowed — try a longer one.",
};

/** Best human sentence for anything a request can throw. */
export function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (typeof d === "string") return ERROR_CODES[d] ?? d;
    if (d && typeof d === "object" && "code" in d && typeof (d as { code: unknown }).code === "string") {
      const code = (d as { code: string }).code;
      const reason = (d as { reason?: unknown }).reason;
      return ERROR_CODES[code] ?? (typeof reason === "string" ? reason : code);
    }
    if (Array.isArray(d) && d.length > 0) {
      // pydantic validation errors: [{ loc, msg, type }, …]
      const msgs = d
        .map((x) => (x && typeof x === "object" && "msg" in x ? String((x as { msg: unknown }).msg) : null))
        .filter(Boolean);
      if (msgs.length) return msgs.join(" · ").replace(/^Value error, /, "");
    }
    if (e.status === 401) return "Please log in first.";
    if (e.status >= 500) return "The server hit a snag — try again in a moment.";
    return e.message;
  }
  if (e instanceof Error) {
    if (/network request failed|failed to fetch|load failed/i.test(e.message)) {
      return "Couldn't reach the server — check your connection.";
    }
    return e.message;
  }
  return String(e);
}
