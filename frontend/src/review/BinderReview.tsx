import { useEffect, useRef, useState } from "react";
import { ApiError, getBinderFollowup, type BinderCard, type BinderScan } from "../api";
import FixCardForm from "./FixCardForm";

const POLL_MS = 2000; // mirrors ReviewScreen / LiveScanScreen's pending_vlm poll

// Force every still-pending row terminal, for the cases where no answer can ever
// arrive: the follow-up 404s (its 30-min TTL expired, or the single-process store
// died with a server restart), or the server reports `done` with a row somehow
// still pending. Leaving those rows `pending_vlm` would spin the "identifying…"
// badge forever AND hide the cell's real flag reason, which is strictly worse
// than admitting the VLM never answered.
function settlePending(prev: BinderCard[]): BinderCard[] {
  let changed = false;
  const next = prev.map((c) => {
    if (c.state !== "pending_vlm") return c;
    changed = true;
    return { ...c, state: "vlm_failed" as const };
  });
  return changed ? next : prev;
}

// Same machine-reason → friendly-copy map CardRow uses, so a flagged binder
// cell reads identically to a flagged pack row.
const REASON_TEXT: Record<string, string> = {
  unreadable_strip: "Couldn't read this row",
  number_ambiguous: "Couldn't read the card number",
  set_ambiguous: "Couldn't tell which set this is from",
  no_db_match: "Card not found in the database",
};

interface Props {
  scan: BinderScan;
  photo: Blob;
  onConfirm: (cards: BinderCard[]) => void;
  onRetake: () => void;
}

// A cell is flagged the same way the grid decides it — keep the overlay's
// outline colour and the "needs review" count in lockstep with the cells below.
function isFlagged(c: BinderCard): boolean {
  return c.needs_review ?? c.low_confidence_reason !== null;
}

// A cell is drawable only if its geometry is a finite [x, y, w, h] tuple.
function hasCell(c: BinderCard): boolean {
  return (
    Array.isArray(c.cell) &&
    c.cell.length === 4 &&
    c.cell.every((n) => typeof n === "number" && Number.isFinite(n))
  );
}

function priceText(c: BinderCard): string | null {
  const lo = c.price_usd_low ?? null;
  const hi = c.price_usd_high ?? null;
  if (lo == null && hi == null) return null;
  if (lo != null && hi != null) {
    return lo === hi ? `$${lo.toFixed(2)}` : `$${lo.toFixed(2)}–$${hi.toFixed(2)}`;
  }
  return `$${((lo ?? hi) as number).toFixed(2)}`;
}

export default function BinderReview({ scan, photo, onConfirm, onRetake }: Props) {
  const [cards, setCards] = useState<BinderCard[]>(scan.cards);
  const [fixing, setFixing] = useState<number | null>(null);
  // Rows the USER has settled by applying a fix. A late VLM answer must never
  // overwrite one — the human looked at the card, the model didn't.
  const [fixedRows, setFixedRows] = useState<Set<number>>(new Set());

  // ── Background-VLM follow-up poll ────────────────────────────────────────
  // The scan answered before the VLM did (see binder._finish); the flagged
  // cells came back `pending_vlm` and resolve here. Mirrors the interval shape
  // ReviewScreen/LiveScanScreen use: ONE interval for the whole review, keyed
  // only on the scan id so a poll-driven patch can't restart (and starve) it —
  // pending-ness and the user's fixes are read from refs inside the tick.
  const scanId = scan.scan_id ?? null;
  const cardsRef = useRef<BinderCard[]>(cards);
  cardsRef.current = cards;
  const fixedRef = useRef<Set<number>>(fixedRows);
  fixedRef.current = fixedRows;

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
      setCards((prev) => {
        let changed = false;
        const next = prev.map((c) => {
          // Eligible ONLY while this row is still locally pending and the user
          // hasn't fixed it: that makes the merge idempotent and makes a manual
          // fix (which sets state "ok" below) permanently authoritative.
          if (c.state !== "pending_vlm" || fixedRef.current.has(c.row_index)) return c;
          const match = st.cards.find((m) => m.row_index === c.row_index);
          if (!match || match.state === "pending_vlm") return c;
          changed = true;
          // Keep the locally-drawn geometry + thumbnail, exactly like the fix
          // apply below: they describe THIS photo, not the identity.
          return { ...match, cell: c.cell, thumb_b64: c.thumb_b64 };
        });
        return changed ? next : prev;
      });
      if (st.done) {
        // The drain is finished, so this is the last useful poll: settle anything
        // it somehow left pending (it shouldn't) and drop the interval instead of
        // idling on it. React applies this after the merge above, so it only ever
        // sees rows the merge could not resolve.
        setCards(settlePending);
        stop();
      }
    }, POLL_MS);
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
        const success = root.getPropertyValue("--success").trim() || "#34d399";
        const danger = root.getPropertyValue("--danger").trim() || "#f87171";
        const badgeText = root.getPropertyValue("--bg").trim() || "#0f1419";

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
          ctx.font = "600 14px system-ui, -apple-system, sans-serif";
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
      <section>
        <h2>No cards found</h2>
        <p>
          We couldn&apos;t read any cards on that page. Lay the page flat, fill the
          frame, and avoid glare, then try again.
        </p>
        <button type="button" className="primary" onClick={onRetake}>
          Retake photo
        </button>
      </section>
    );
  }

  const cols = scan.grid.cols || 1;
  const foundN = cards.length;
  const needReview = cards.filter(isFlagged).length;

  return (
    <section>
      <h2>Review your binder page</h2>
      <p>
        {scan.grid.rows}×{scan.grid.cols} page · tap a card to fix it. Flags don&apos;t
        block saving.
      </p>

      {overlayOk && drawableCells.length > 0 && (
        <details
          open={overlayOpen}
          onToggle={(e) => setOverlayOpen((e.target as HTMLDetailsElement).open)}
          style={{
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            background: "var(--surface)",
            padding: "0.5rem 0.75rem",
            marginBottom: "1rem",
          }}
        >
          <summary style={{ cursor: "pointer", fontWeight: 600 }}>
            What the scanner found
          </summary>
          <p style={{ color: "var(--text-muted)", fontSize: "0.9rem", margin: "0.5rem 0" }}>
            Found {foundN} card{foundN === 1 ? "" : "s"}
            {needReview > 0 ? ` · ${needReview} need review` : ""} · tap an outline to
            jump to its card.
          </p>
          <div ref={canvasWrapRef}>
            <canvas
              ref={canvasRef}
              onClick={onCanvasClick}
              style={{
                display: "block",
                maxWidth: "100%",
                height: "auto",
                borderRadius: "var(--radius)",
                cursor: "pointer",
              }}
            />
          </div>
        </details>
      )}

      <div
        className="binder-grid"
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gap: "0.5rem",
        }}
      >
        {cards.map((c) => {
          const flagged = c.needs_review ?? c.low_confidence_reason !== null;
          // Still being identified in the background: the cell stays outlined as
          // flagged (it IS unconfirmed, and the overlay/count must agree with
          // that), but "needs review" would be the wrong ask — nothing to do yet.
          const pending = c.state === "pending_vlm";
          const price = priceText(c);
          const highlighted = highlight === c.row_index;
          return (
            <div
              key={c.row_index}
              ref={(el) => {
                if (el) cellRefs.current.set(c.row_index, el);
                else cellRefs.current.delete(c.row_index);
              }}
              className={`binder-cell${flagged ? " flagged" : ""}`}
              role="button"
              tabIndex={0}
              onClick={() => setFixing(c.row_index)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setFixing(c.row_index);
                }
              }}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                padding: "0.5rem",
                border: `1px solid ${flagged ? "var(--danger)" : "var(--border)"}`,
                background: flagged ? "rgba(248, 113, 113, 0.12)" : "var(--surface)",
                borderRadius: "var(--radius)",
                cursor: "pointer",
                outlineStyle: highlighted ? "solid" : "none",
                outlineWidth: highlighted ? "3px" : undefined,
                outlineColor: highlighted ? "var(--accent)" : undefined,
                outlineOffset: 2,
                boxShadow: highlighted ? "0 0 0 3px rgba(59, 130, 246, 0.35)" : "none",
                transition: "outline-color 0.15s, box-shadow 0.15s",
              }}
            >
              {c.thumb_b64 ? (
                <img
                  src={`data:image/jpeg;base64,${c.thumb_b64}`}
                  alt={c.name ?? "card"}
                  style={{ width: "100%", aspectRatio: "63 / 88", objectFit: "cover", borderRadius: 4 }}
                />
              ) : (
                <div
                  style={{
                    width: "100%",
                    aspectRatio: "63 / 88",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    background: "var(--surface)",
                    color: "var(--text-muted)",
                    borderRadius: 4,
                  }}
                >
                  ?
                </div>
              )}
              <strong>{c.name ?? "Unknown card"}</strong>
              <span style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>
                {c.card_number ?? "—"} · {c.set_name ?? "Unknown set"}
              </span>
              {price && <span style={{ fontSize: "0.85rem" }}>{price}</span>}
              {pending ? (
                <em className="live-chip-status" style={{ fontSize: "0.8rem" }}>
                  <span className="spinner" /> identifying…
                </em>
              ) : (
                flagged && (
                  <em style={{ color: "var(--danger)", fontSize: "0.8rem" }}>
                    {REASON_TEXT[c.low_confidence_reason!] ?? "Needs review"}
                  </em>
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
            // state "ok" + fixedRows both retire the row from the VLM poll: the
            // user's answer is final, whatever the worker eventually says.
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
            setFixedRows((prev) => new Set(prev).add(fixing));
            setFixing(null);
          }}
          onCancel={() => setFixing(null)}
        />
      )}

      <div style={{ display: "flex", gap: "0.5rem", marginTop: "1rem" }}>
        <button type="button" onClick={onRetake}>
          Retake photo
        </button>
        <button type="button" className="primary" onClick={() => onConfirm(cards)}>
          Save to collection
        </button>
      </div>
    </section>
  );
}
