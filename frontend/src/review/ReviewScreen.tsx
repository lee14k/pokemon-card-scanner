import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  getPackFollowup,
  liveState,
  type LiveCard,
  type PackCard,
  type PackScanResponse,
} from "../api";
import { friendlyWarning } from "@shared/format";
import { FOLLOWUP_POLL_MS, isFlagged, mergeLate, settlePending } from "@shared/review";
import CardRow from "./CardRow";
import FixCardForm from "./FixCardForm";
import PixelIcon from "../ui/PixelIcon";
import { ScreenTitle } from "../ui/kit";

interface Props {
  scan: PackScanResponse;
  liveSessionId?: string;
  onConfirm: (cards: PackCard[]) => void;
  onRetake: () => void;
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
  // from liveFinish, which never sets `state` (the field is serialized only when
  // a follow-up owns the card, and the live flow's late identities live in the
  // session store instead) -- only GET /scan/live/{sid} injects it per row.
  // Without this, every card.state is undefined at mount, the poll gate below
  // never sees "pending_vlm", and a still-identifying row never shows the spinner
  // or gets patched. This is a one-shot effect, separate from the recurring poll
  // effect, so it can't restart or duplicate the poll's interval. Defensive: if
  // the session is already gone (e.g. expired), swallow the error and leave
  // scan.cards as-is -- the feature just no-ops rather than crashing the review
  // screen.
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
      isFlagged(c) &&
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
    let id = 0;
    const stop = () => {
      cancelled = true;
      window.clearInterval(id);
    };
    id = window.setInterval(async () => {
      if (cancelled) return;
      const anyPending = cardsRef.current.some((c) => c.state === "pending_vlm");
      if (!anyPending) return;
      let late: LiveCard[];
      let done = false;   // only the pack follow-up has a terminal flag
      try {
        if (liveSessionId) {
          late = (await liveState(liveSessionId)).cards;
        } else {
          const st = await getPackFollowup(scanId as string);
          late = st.cards;
          done = st.done;
        }
      } catch (e) {
        if (cancelled) return;
        // 404 is TERMINAL, not transient: the follow-up entry (or live session)
        // is gone for good, so no answer is coming. Settle the rows and stop, or
        // they spin forever. Any other failure -- offline, 5xx, a proxy hiccup --
        // is retried on the next tick.
        if (e instanceof ApiError && e.status === 404) {
          setCards(settlePending);
          stop();
        }
        return;
      }
      if (cancelled) return;
      setCards((prev) => mergeLate(prev, late, resolvedRef.current));
      if (done) {
        // Last useful poll: settle anything the drain somehow left pending (it
        // shouldn't) and drop the interval instead of idling on it. React applies
        // this after the merge above, so it only sees rows the merge couldn't
        // resolve.
        setCards(settlePending);
        stop();
      }
    }, FOLLOWUP_POLL_MS);
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveSessionId, scanId]);

  const flaggedLeft = unresolved.length;

  return (
    <section className="stack-lg">
      <ScreenTitle
        kicker="Review"
        title="Your pulls"
        sub={
          flaggedLeft > 0
            ? `${flaggedLeft} card${flaggedLeft > 1 ? "s" : ""} need${flaggedLeft > 1 ? "" : "s"} a look. Fix ${flaggedLeft > 1 ? "them" : "it"} or keep ${flaggedLeft > 1 ? "them" : "it"} anyway.`
            : "Tap any card to correct it, then save the pack."
        }
      >
        <div className="tips">
          <span className="badge gold">{cards.length} cards</span>
          {flaggedLeft > 0 ? (
            <span className="badge red">{flaggedLeft} flagged</span>
          ) : (
            <span className="badge green">All clear</span>
          )}
        </div>
      </ScreenTitle>
      {scan.segmentation_warning && (
        <div className="panel warn stack">
          <div className="row">
            <PixelIcon name="warn" size={24} />
            <p className="grow">{friendlyWarning(scan.segmentation_warning)}</p>
          </div>
          <div className="row row-end">
            <button type="button" className="small" onClick={onRetake}>Retake photo</button>
          </div>
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
      <div className="code-ticket">
        <PixelIcon name="pack" size={24} className="muted" />
        <div className="stack grow" style={{ gap: 4 }}>
          <span className="stat-label">Code card</span>
          <code>{scan.code_card.code ?? "not read"}</code>
        </div>
        {scan.code_card.code && !scan.code_card.format_ok && <span className="badge outline">Unusual format</span>}
      </div>
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
      <div className="sticky-actions">
        <button
          type="button"
          className="primary big"
          disabled={unresolved.length > 0}
          onClick={() => onConfirm(cards)}
        >
          {unresolved.length > 0
            ? `Fix ${unresolved.length} flagged card${unresolved.length > 1 ? "s" : ""} first`
            : "Looks good — save pack"}
        </button>
      </div>
    </section>
  );
}
