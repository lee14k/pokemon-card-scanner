import { useEffect, useRef, useState } from "react";
import {
  getPackFollowup,
  liveState,
  type LiveCard,
  type PackCard,
  type PackScanResponse,
} from "../api";
import CardRow from "./CardRow";
import FixCardForm from "./FixCardForm";

interface Props {
  scan: PackScanResponse;
  liveSessionId?: string;
  onConfirm: (cards: PackCard[]) => void;
  onRetake: () => void;
}

const POLL_MS = 2000; // mirrors LiveScanScreen's pending_vlm poll interval

// Fold late per-row identities (VLM answers) into the rows on screen.
//
// A row is eligible ONLY while it is still locally `pending_vlm` and the user
// hasn't settled it. That single rule does three things: it makes the merge
// idempotent, it makes a manual fix permanently authoritative (FixCardForm's
// apply sets state "ok" and marks the row resolved, so no later poll can undo
// the human's answer), and it lets an unchanged poll return `prev` so nothing
// re-renders.
function mergeLate(
  prev: LiveCard[],
  late: LiveCard[],
  resolved: Set<number>
): LiveCard[] {
  let changed = false;
  const next = prev.map((c) => {
    if (c.state !== "pending_vlm" || resolved.has(c.row_index)) return c;
    const match = late.find((m) => m.row_index === c.row_index);
    if (!match || match.state === "pending_vlm") return c;
    changed = true;
    return { ...match };
  });
  return changed ? next : prev;
}

// Turn the backend's machine-readable segmentation_warning into a friendly sentence.
function friendlyWarning(warning: string): string {
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

export default function ReviewScreen({ scan, liveSessionId, onConfirm, onRetake }: Props) {
  // scan.cards is a PackCard[], structurally assignable to LiveCard[] (state is
  // optional) — this seeds each row's `state` from the initial scan when the
  // backend happened to include it, and leaves it undefined (treated as
  // settled, same as "ok") otherwise. No PackCard/LiveCard redefinition needed.
  const [cards, setCards] = useState<LiveCard[]>(scan.cards);
  const [resolvedRows, setResolvedRows] = useState<Set<number>>(new Set());
  const [fixing, setFixing] = useState<number | null>(null);

  // A one-photo/stream scan whose flagged rows are still being identified in the
  // background carries the follow-up handle on the scan itself (see
  // pipeline.scan_pack). Mutually exclusive with liveSessionId — a live scan's
  // late identities come from its session store, which liveFinish leaves intact.
  const scanId = scan.scan_id ?? null;

  // Mirror `cards`/`resolvedRows` for use inside the poll's interval tick, which
  // must always see the freshest value regardless of which render's closure is
  // running.
  const cardsRef = useRef<LiveCard[]>(cards);
  cardsRef.current = cards;
  const resolvedRef = useRef<Set<number>>(resolvedRows);
  resolvedRef.current = resolvedRows;

  const markResolved = (row: number) =>
    setResolvedRows((prev) => new Set(prev).add(row));

  // Bootstrap once on mount: seed real per-row `state` (and any VLM-refreshed
  // identity fields) from the live session. `scan.cards` is a PackScanResponse
  // from liveFinish, and PackCard has no `state` field -- only
  // GET /scan/live/{sid} injects it per row. Without this, every card.state is
  // undefined at mount, the poll gate below never sees "pending_vlm", and a
  // still-identifying row never shows the spinner or gets patched. This is a
  // one-shot effect, separate from the recurring poll effect, so it can't
  // restart or duplicate the poll's interval. Defensive: if the session is
  // already gone (e.g. expired), swallow the error and leave scan.cards as-is
  // -- the feature just no-ops rather than crashing the review screen.
  useEffect(() => {
    if (!liveSessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const st = await liveState(liveSessionId);
        if (cancelled) return;
        setCards((prev) =>
          prev.map((c) => {
            // Wholesale (not mergeLate): at mount every state is undefined, so
            // there is nothing "pending" to gate on yet — seeding the real states
            // is the entire point. Only a row the user already settled is spared.
            if (resolvedRef.current.has(c.row_index)) return c;
            const match = st.cards.find((m) => m.row_index === c.row_index);
            return match ? { ...match } : c;
          })
        );
      } catch {
        // best effort -- session may already be expired/gone; scan.cards stands
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveSessionId]);

  // Block confirmation only on genuinely uncertain cards. needs_review reflects
  // identity confidence (number + set + catalog), not whether the DB lookup ran,
  // so confidently-read cards never burden the user. pending_vlm rows are
  // excluded from blocking too — they're still being identified in the
  // background; the user can wait for the poll below to resolve them or fix
  // manually, but Finish must never stall on a still-pending row.
  const unresolved = cards.filter(
    (c) =>
      c.state !== "pending_vlm" &&
      (c.needs_review ?? c.low_confidence_reason !== null) &&
      !resolvedRows.has(c.row_index)
  );

  // Poll while any row is still pending_vlm, patching rows in place as VLM
  // answers land — from the live session store for a live scan, or from the pack
  // follow-up entry for a one-photo/stream scan (whose VLM pass no longer blocks
  // the scan response). One persistent interval per review, NOT torn down or
  // restarted on every card patch — depending on `cards` here would reset the
  // interval on every poll-driven update and it could starve. Pending-ness is
  // read from cardsRef inside the tick instead, and the network call is
  // skipped entirely when nothing is pending. Mirrors the pattern in
  // LiveScanScreen's own pending_vlm poll.
  useEffect(() => {
    if (!liveSessionId && !scanId) return;
    let cancelled = false;
    const id = window.setInterval(async () => {
      if (cancelled) return;
      const anyPending = cardsRef.current.some((c) => c.state === "pending_vlm");
      if (!anyPending) return;
      try {
        const late = liveSessionId
          ? (await liveState(liveSessionId)).cards
          : (await getPackFollowup(scanId as string)).cards;
        if (cancelled) return;
        setCards((prev) => mergeLate(prev, late, resolvedRef.current));
      } catch {
        // best effort -- the session may be mid-recovery elsewhere, or the
        // follow-up entry may have expired; try again next tick
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveSessionId, scanId]);

  return (
    <section>
      <h2>Step 3 — Review your pulls</h2>
      {scan.segmentation_warning && (
        <div className="warn-banner">
          {friendlyWarning(scan.segmentation_warning)}{" "}
          <button type="button" onClick={onRetake}>Retake photo</button>
        </div>
      )}
      <ul className="card-rows">
        {cards.map((c) => (
          <CardRow
            key={c.row_index}
            card={c}
            resolved={resolvedRows.has(c.row_index)}
            liveSessionId={liveSessionId}
            onFix={() => setFixing(c.row_index)}
            onKeep={() => markResolved(c.row_index)}
          />
        ))}
      </ul>
      <p>
        Code card: <code>{scan.code_card.code ?? "not read"}</code>
        {!scan.code_card.format_ok && scan.code_card.code && " (unusual format)"}
      </p>
      {fixing !== null && (
        <FixCardForm
          initial={cards.find((c) => c.row_index === fixing)!}
          onApply={(fixed) => {
            setCards((prev) =>
              prev.map((c) => (c.row_index === fixing ? { ...fixed, state: "ok" } : c))
            );
            markResolved(fixing);
            setFixing(null);
          }}
          onCancel={() => setFixing(null)}
        />
      )}
      <button
        type="button"
        className="primary"
        disabled={unresolved.length > 0}
        onClick={() => onConfirm(cards)}
      >
        {unresolved.length > 0
          ? `Fix ${unresolved.length} flagged card${unresolved.length > 1 ? "s" : ""} first`
          : "Looks good"}
      </button>
    </section>
  );
}
