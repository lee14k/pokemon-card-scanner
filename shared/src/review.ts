// Review-screen rules shared by the web and mobile apps: which card is flagged,
// how late VLM answers fold into the rows on screen, and how rows that can never
// get an answer are settled.

import type { BinderCard, PackCard } from "./types";

/** POLL interval for pending_vlm rows (live session, pack and binder follow-ups). */
export const FOLLOWUP_POLL_MS = 2000;

/** A card the scanner wasn't sure about (and that the user hasn't settled). */
export function isFlagged(c: PackCard): boolean {
  return c.needs_review ?? c.low_confidence_reason !== null;
}

/**
 * Does this binder cell carry an identity the server can file it under? The set
 * alone is NOT one: the key's right-hand side is the number, else the name, and
 * an empty right-hand side ("SVI:" as much as "?:") is a bucket every unreadable
 * card of that set would share, so the server refuses it
 * (app/collection.py::_is_degenerate_key). "Keep as is" is therefore not on
 * offer for such a cell — keeping it would promise a save that is not coming.
 */
export function hasIdentity(c: PackCard): boolean {
  return Boolean(c.card_number || c.name);
}

/**
 * Fold late per-row identities (VLM answers) into the rows on screen.
 *
 * A row is eligible ONLY while it is still locally `pending_vlm` and the user
 * hasn't settled it. That single rule makes the merge idempotent, makes a manual
 * fix permanently authoritative (applying a fix sets state "ok" and marks the
 * row resolved, so no later poll can undo the human's answer), and lets an
 * unchanged poll return `prev` so nothing re-renders.
 *
 * `keep` copies fields from the local row onto the merged one — the binder uses
 * it to hold onto the cell geometry + thumbnail, which describe THIS photo
 * rather than the identity.
 */
export function mergeLate<C extends PackCard>(
  prev: C[],
  late: C[],
  resolved: Set<number>,
  keep?: (local: C) => Partial<C>
): C[] {
  let changed = false;
  const next = prev.map((c) => {
    if (c.state !== "pending_vlm" || resolved.has(c.row_index)) return c;
    const match = late.find((m) => m.row_index === c.row_index);
    if (!match || match.state === "pending_vlm") return c;
    changed = true;
    return { ...match, ...(keep ? keep(c) : {}) };
  });
  return changed ? next : prev;
}

export const keepBinderGeometry = (local: BinderCard): Partial<BinderCard> => ({
  cell: local.cell,
  thumb_b64: local.thumb_b64,
});

/**
 * Force every still-pending row terminal, for the cases where no answer can
 * ever arrive: the follow-up 404s (TTL expired, or the single-process store died
 * with a restart; a live session that was swept), or the follow-up reports
 * `done` with a row somehow still pending. Leaving those rows `pending_vlm`
 * would spin the "still identifying" spinner forever AND suppress the row's real
 * flag reason and its Fix/Keep buttons.
 */
export function settlePending<C extends PackCard>(prev: C[]): C[] {
  let changed = false;
  const next = prev.map((c) => {
    if (c.state !== "pending_vlm") return c;
    changed = true;
    return { ...c, state: "vlm_failed" as const };
  });
  return changed ? next : prev;
}
