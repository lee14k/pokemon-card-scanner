import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  getBinderFollowup,
  type BinderCard,
  type BinderScan,
  type CollectionSaveCard,
} from "../api";
import FixCardForm from "./FixCardForm";
import { priceRange, reasonText } from "@shared/format";
import { FOLLOWUP_POLL_MS, hasIdentity, isFlagged, keepBinderGeometry, mergeLate, settlePending } from "@shared/review";
import PixelIcon from "../ui/PixelIcon";
import { Loader, ScreenTitle } from "../ui/kit";

interface Props {
  scan: BinderScan;
  photo: Blob;
  onConfirm: (cards: CollectionSaveCard[]) => void;
  onRetake: () => void;
}

// A cell is drawable only if its geometry is a finite [x, y, w, h] tuple.
function hasCell(c: BinderCard): boolean {
  return (
    Array.isArray(c.cell) &&
    c.cell.length === 4 &&
    c.cell.every((n) => typeof n === "number" && Number.isFinite(n))
  );
}

export default function BinderReview({ scan, photo, onConfirm, onRetake }: Props) {
  const [cards, setCards] = useState<BinderCard[]>(scan.cards);
  const [fixing, setFixing] = useState<number | null>(null);
  // Rows the USER has settled — by applying a fix, or by keeping a flagged cell
  // as it is. Two jobs, and they are the same set on purpose:
  //   1. a late VLM answer must never overwrite one (the human looked at the
  //      card, the model didn't);
  //   2. a still-flagged row is sent up `confirmed: true` ONLY if it is in here,
  //      and the server persists a flagged card only when it is confirmed.
  const [settledRows, setSettledRows] = useState<Set<number>>(new Set());

  // ── Background-VLM follow-up poll ────────────────────────────────────────
  // The scan answered before the VLM did (see binder._finish); the flagged
  // cells came back `pending_vlm` and resolve here. Mirrors the interval shape
  // ReviewScreen/LiveScanScreen use: ONE interval for the whole review, keyed
  // only on the scan id so a poll-driven patch can't restart (and starve) it —
  // pending-ness and the user's fixes are read from refs inside the tick.
  const scanId = scan.scan_id ?? null;
  const cardsRef = useRef<BinderCard[]>(cards);
  cardsRef.current = cards;
  const settledRef = useRef<Set<number>>(settledRows);
  settledRef.current = settledRows;

  useEffect(() => {
    if (!scanId) return;
    let cancelled = false;
    let id = 0;
    const stop = () => {
      cancelled = true;
      window.clearInterval(id);
    };
    id = window.setInterval(async () => {
      if (cancelled) return;
      if (!cardsRef.current.some((c) => c.state === "pending_vlm")) return;
      let st;
      try {
        st = await getBinderFollowup(scanId);
      } catch (e) {
        if (cancelled) return;
        // 404 is TERMINAL, not transient: the follow-up entry is gone for good
        // (expired, or the process that held it restarted), so no answer is
        // coming. Settle the rows and stop. Any other failure — offline, 5xx,
        // a proxy hiccup — is retried on the next tick.
        if (e instanceof ApiError && e.status === 404) {
          setCards(settlePending);
          stop();
        }
        return;
      }
      if (cancelled) return;
      // Eligible ONLY while a row is still locally pending and the user hasn't
      // settled it (see mergeLate); keep the locally-drawn geometry + thumbnail,
      // exactly like the fix apply below — they describe THIS photo, not the
      // identity.
      setCards((prev) => mergeLate(prev, st.cards, settledRef.current, keepBinderGeometry));
      if (st.done) {
        // The drain is finished, so this is the last useful poll: settle anything
        // it somehow left pending (it shouldn't) and drop the interval instead of
        // idling on it. React applies this after the merge above, so it only ever
        // sees rows the merge could not resolve.
        setCards(settlePending);
        stop();
      }
    }, FOLLOWUP_POLL_MS);
    return stop;
  }, [scanId]);

  // ── Card-finder overlay ──────────────────────────────────────────────────
  // Draw the captured page photo with each detected cell outlined so a bad
  // segmentation is visible rather than surfacing as mystery grid cells.
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const canvasWrapRef = useRef<HTMLDivElement | null>(null);
  // Grid cell nodes keyed by row_index, so a rect click can scroll to its cell.
  const cellRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  // Displayed rects (canvas-pixel coords) + row_index for click hit-testing.
  const rectsRef = useRef<{ row_index: number; x: number; y: number; w: number; h: number }[]>([]);
  const [overlayOpen, setOverlayOpen] = useState(true);
  const [overlayOk, setOverlayOk] = useState(true);
  const [highlight, setHighlight] = useState<number | null>(null);

  const drawableCells = cards.filter(hasCell);

  useEffect(() => {
    if (drawableCells.length === 0) {
      setOverlayOk(false);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    let bitmap: ImageBitmap | null = null;

    const draw = async () => {
      try {
        let source: CanvasImageSource;
        let natW: number;
        let natH: number;
        if (typeof createImageBitmap === "function") {
          bitmap = await createImageBitmap(photo);
          source = bitmap;
          natW = bitmap.width;
          natH = bitmap.height;
        } else {
          objectUrl = URL.createObjectURL(photo);
          const img = await new Promise<HTMLImageElement>((resolve, reject) => {
            const im = new Image();
            im.onload = () => resolve(im);
            im.onerror = () => reject(new Error("image decode failed"));
            im.src = objectUrl as string;
          });
          source = img;
          natW = img.naturalWidth;
          natH = img.naturalHeight;
        }
        if (cancelled) return;
        const canvas = canvasRef.current;
        const wrap = canvasWrapRef.current;
        if (!canvas || !wrap || !natW || !natH) throw new Error("overlay not ready");

        // Fit the natural photo into the container width and ~40vh, uniformly —
        // draw at that display scale so a 3px stroke reads as 3 on-screen px.
        const maxW = wrap.clientWidth || natW;
        const maxH = window.innerHeight * 0.4;
        const scale = Math.min(maxW / natW, maxH / natH);
        const dispW = Math.max(1, Math.round(natW * scale));
        const dispH = Math.max(1, Math.round(natH * scale));
        canvas.width = dispW;
        canvas.height = dispH;

        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("no 2d context");

        const root = getComputedStyle(document.documentElement);
        const success = root.getPropertyValue("--green").trim() || "#5ee37f";
        const danger = root.getPropertyValue("--danger").trim() || "#ff6b6b";
        const badgeText = root.getPropertyValue("--on-bright").trim() || "#1a1033";

        ctx.clearRect(0, 0, dispW, dispH);
        ctx.drawImage(source, 0, 0, dispW, dispH);

        const rects: typeof rectsRef.current = [];
        cards.forEach((c, i) => {
          if (!hasCell(c)) return;
          const [x, y, w, h] = c.cell;
          const rx = x * scale;
          const ry = y * scale;
          const rw = w * scale;
          const rh = h * scale;
          const tone = isFlagged(c) ? danger : success;

          ctx.lineWidth = 3;
          ctx.strokeStyle = tone;
          ctx.strokeRect(rx, ry, rw, rh);

          // Filled index badge at the cell's top-left, readable when scaled down.
          const label = String(i + 1);
          ctx.font = "900 14px Nunito, system-ui, -apple-system, sans-serif";
          const padX = 5;
          const badgeH = 20;
          const badgeW = ctx.measureText(label).width + padX * 2;
          ctx.fillStyle = tone;
          ctx.fillRect(rx, ry, badgeW, badgeH);
          ctx.fillStyle = badgeText;
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillText(label, rx + padX, ry + badgeH / 2 + 0.5);

          rects.push({ row_index: c.row_index, x: rx, y: ry, w: rw, h: rh });
        });
        rectsRef.current = rects;
        if (!cancelled) setOverlayOk(true);
      } catch {
        if (!cancelled) setOverlayOk(false);
      }
    };

    draw();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (bitmap) bitmap.close();
    };
    // Redraw when the photo changes or a fix flips a cell's flag/geometry.
  }, [photo, cards]); // eslint-disable-line react-hooks/exhaustive-deps

  const focusCell = (rowIndex: number) => {
    const el = cellRefs.current.get(rowIndex);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlight(rowIndex);
    window.setTimeout(() => {
      setHighlight((cur) => (cur === rowIndex ? null : cur));
    }, 1200);
  };

  const onCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const box = canvas.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) return;
    const cx = ((e.clientX - box.left) / box.width) * canvas.width;
    const cy = ((e.clientY - box.top) / box.height) * canvas.height;
    const hit = rectsRef.current.find(
      (r) => cx >= r.x && cx <= r.x + r.w && cy >= r.y && cy <= r.y + r.h
    );
    if (hit) focusCell(hit.row_index);
  };

  // scan_binder_page raises no_cards_found (→ scanBinder rejects) when nothing
  // is readable, and App routes that to an empty-cards scan so the retake state
  // lives here in the review flow.
  if (cards.length === 0) {
    return (
      <section className="stack-lg">
        <ScreenTitle kicker="Binder page" title="No cards found" />
        <div className="panel empty-state">
          <PixelIcon name="grid" size={48} />
          <p>
            We couldn&apos;t read any cards on that page. Lay the page flat, fill the frame, and avoid glare,
            then try again.
          </p>
          <button type="button" className="primary" onClick={onRetake}>
            Retake photo
          </button>
        </div>
      </section>
    );
  }

  const cols = scan.grid.cols || 1;
  const foundN = cards.length;
  const needReview = cards.filter(isFlagged).length;
  // Flagged AND untouched: the server will skip exactly these. Counted here so
  // the number on the button matches the number in the save summary.
  const unsettled = cards.filter((c) => isFlagged(c) && !settledRows.has(c.row_index));

  // The save payload: a flagged card carries the user's explicit go-ahead, and
  // nothing else carries the field at all. Untouched flagged cards are sent too
  // (rather than filtered out here) so the server is the single place that
  // decides what persists — and the skipped count comes back from the same pass
  // that made the decision.
  const confirmPayload = (): CollectionSaveCard[] =>
    cards.map((c) =>
      isFlagged(c) && settledRows.has(c.row_index) ? { ...c, confirmed: true } : c
    );

  // "I looked at it, save it anyway." Only reachable for a flagged cell that has
  // something to file under — see hasIdentity.
  const keepAsIs = (rowIndex: number) => {
    setSettledRows((prev) => new Set(prev).add(rowIndex));
  };

  return (
    <section className="stack-lg">
      <ScreenTitle
        kicker="Binder page"
        title="Review page"
        sub="Tap a card to fix it. A flagged card is saved only after you fix it or keep it."
      >
        <div className="tips">
          <span className="badge gold">
            {scan.grid.rows}×{scan.grid.cols} page
          </span>
          <span className="badge outline">{foundN} found</span>
          {needReview > 0 ? (
            <span className="badge red">{needReview} to review</span>
          ) : (
            <span className="badge green">All clear</span>
          )}
        </div>
      </ScreenTitle>

      {overlayOk && drawableCells.length > 0 && (
        <details
          className="panel finder"
          open={overlayOpen}
          onToggle={(e) => setOverlayOpen((e.target as HTMLDetailsElement).open)}
        >
          <summary>What the scanner found</summary>
          <p className="muted small-text" style={{ margin: "0.6rem 0" }}>
            Found {foundN} card{foundN === 1 ? "" : "s"}
            {needReview > 0 ? ` · ${needReview} need review` : ""} · tap an outline to jump to its card.
          </p>
          <div ref={canvasWrapRef}>
            <canvas ref={canvasRef} onClick={onCanvasClick} />
          </div>
        </details>
      )}

      <div className="binder-grid" style={{ "--cols": cols } as React.CSSProperties}>
        {cards.map((c) => {
          const flagged = isFlagged(c);
          // Still being identified in the background: the cell stays outlined as
          // flagged (it IS unconfirmed, and the overlay/count must agree with
          // that), but "needs review" would be the wrong ask — nothing to do yet.
          const pending = c.state === "pending_vlm";
          const price = priceRange(c.price_usd_low, c.price_usd_high);
          const highlighted = highlight === c.row_index;
          // A flagged cell the user has settled: still flagged (nothing about the
          // card got better), but it WILL be saved, so it must not read the same
          // as one that is about to be skipped.
          const kept = flagged && settledRows.has(c.row_index);
          return (
            <div
              key={c.row_index}
              ref={(el) => {
                if (el) cellRefs.current.set(c.row_index, el);
                else cellRefs.current.delete(c.row_index);
              }}
              className={`card-tile binder-cell${flagged && !kept ? " flagged" : ""}${kept ? " kept" : ""}${highlighted ? " highlighted" : ""}`}
              role="button"
              tabIndex={0}
              aria-label={`${c.name ?? "Unknown card"}${c.card_number ? ` ${c.card_number}` : ""} — edit`}
              onClick={() => setFixing(c.row_index)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setFixing(c.row_index);
                }
              }}
            >
              {c.thumb_b64 ? (
                <img src={`data:image/jpeg;base64,${c.thumb_b64}`} alt={c.name ?? "card"} className="art" />
              ) : (
                <div className="art placeholder">?</div>
              )}
              <span className="tile-name">{c.name ?? "Unknown card"}</span>
              <span className="tile-meta">
                {c.card_number ?? "—"} · {c.set_name ?? "Unknown set"}
              </span>
              {price && <span className="price">{price}</span>}
              {pending ? (
                <span className="live-chip-status" style={{ fontSize: "0.75rem" }}>
                  <Loader small /> identifying…
                </span>
              ) : (
                flagged && (
                  <>
                    <span className="reason" style={{ fontSize: "0.75rem" }}>
                      ! {reasonText(c.low_confidence_reason)}
                    </span>
                    {kept ? (
                      <span className="badge gold">Kept — will save</span>
                    ) : hasIdentity(c) ? (
                      // Escape hatch for a card that cannot be fixed (an unreadable
                      // number the user can still read off the card in their hand).
                      //
                      // BOTH handlers stop propagation, because the cell around this
                      // button is itself a role="button" that opens the fix form:
                      //   - onClick, or a pointer press would keep the card AND open
                      //     the form on top of it;
                      //   - onKeyDown, because Enter/Space on a focused <button>
                      //     dispatches keydown FIRST (which bubbles to the cell's
                      //     onKeyDown, where preventDefault + setFixing would open the
                      //     form) and only then synthesizes the click. Without this,
                      //     "Keep as is" was reachable by mouse only: a keyboard user
                      //     got the fix modal every time. Stopping the keydown leaves
                      //     the browser's own Enter->click on the button intact, so the
                      //     onClick above still runs and the keep still happens.
                      <button
                        type="button"
                        className="small"
                        onClick={(e) => {
                          e.stopPropagation();
                          keepAsIs(c.row_index);
                        }}
                        onKeyDown={(e) => e.stopPropagation()}
                      >
                        Keep as is
                      </button>
                    ) : (
                      <span className="muted" style={{ fontSize: "0.75rem" }}>
                        Nothing readable — fix it to add it
                      </span>
                    )}
                  </>
                )
              )}
            </div>
          );
        })}
      </div>

      {fixing !== null && (
        <FixCardForm
          initial={cards.find((c) => c.row_index === fixing)!}
          onApply={(fixed) => {
            // Reuse the pack FixCardForm, but keep the binder-only fields it
            // doesn't know about (cell geometry + thumbnail) and the row_index.
            // state "ok" + settledRows both retire the row from the VLM poll: the
            // user's answer is final, whatever the worker eventually says. The
            // looked-up card comes back unflagged, so it saves on its own merits;
            // settling it also covers the case where it does not.
            setCards((prev) =>
              prev.map((c) =>
                c.row_index === fixing
                  ? {
                      ...fixed,
                      row_index: c.row_index,
                      cell: c.cell,
                      thumb_b64: c.thumb_b64,
                      state: "ok",
                    }
                  : c
              )
            );
            setSettledRows((prev) => new Set(prev).add(fixing));
            setFixing(null);
          }}
          onCancel={() => setFixing(null)}
        />
      )}

      {unsettled.length > 0 && (
        <p className="danger-text small-text">
          {unsettled.length} card{unsettled.length === 1 ? "" : "s"} still need
          {unsettled.length === 1 ? "s" : ""} review and won&apos;t be saved — fix
          {unsettled.length === 1 ? " it" : " them"}, or tap “Keep as is”.
        </p>
      )}

      <div className="sticky-actions">
        <button type="button" onClick={onRetake}>
          Retake
        </button>
        <button
          type="button"
          className="primary"
          // Nothing on this page would be persisted: every cell is flagged and
          // none has been settled. "Save 0 to collection" is a button whose only
          // possible outcome is a summary saying nothing was saved.
          disabled={cards.length === unsettled.length}
          onClick={() => onConfirm(confirmPayload())}
        >
          {unsettled.length > 0
            ? `Save ${cards.length - unsettled.length} to collection`
            : "Save to collection"}
        </button>
      </div>
    </section>
  );
}
