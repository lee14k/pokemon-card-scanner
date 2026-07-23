import { useState } from "react";
import type { BinderCard, BinderScan } from "../api";
import FixCardForm from "./FixCardForm";

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
  onConfirm: (cards: BinderCard[]) => void;
  onRetake: () => void;
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

export default function BinderReview({ scan, onConfirm, onRetake }: Props) {
  const [cards, setCards] = useState<BinderCard[]>(scan.cards);
  const [fixing, setFixing] = useState<number | null>(null);

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

  return (
    <section>
      <h2>Review your binder page</h2>
      <p>
        {scan.grid.rows}×{scan.grid.cols} page · tap a card to fix it. Flags don&apos;t
        block saving.
      </p>
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
          const price = priceText(c);
          return (
            <div
              key={c.row_index}
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
              {flagged && (
                <em style={{ color: "var(--danger)", fontSize: "0.8rem" }}>
                  {REASON_TEXT[c.low_confidence_reason!] ?? "Needs review"}
                </em>
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
            setCards((prev) =>
              prev.map((c) =>
                c.row_index === fixing
                  ? { ...fixed, row_index: c.row_index, cell: c.cell, thumb_b64: c.thumb_b64 }
                  : c
              )
            );
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
