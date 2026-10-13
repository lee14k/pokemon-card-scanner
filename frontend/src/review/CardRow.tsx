import { priceRange, reasonText } from "@shared/format";
import { isFlagged } from "@shared/review";
import { liveCardImageUrl, type LiveCard } from "../api";
import { Loader } from "../ui/kit";

interface Props {
  card: LiveCard;
  resolved: boolean; // user has fixed or accepted a flagged row
  liveSessionId?: string; // when present, render the live captured-frame thumbnail
  onFix: () => void;
  onKeep: () => void;
}

export default function CardRow({ card, resolved, liveSessionId, onFix, onKeep }: Props) {
  // Pending VLM identification pre-empts the normal flagged/reason display —
  // it's neither "ok" nor a user-actionable flag yet, just "still working".
  const pending = card.state === "pending_vlm" && !resolved;
  const flagged = isFlagged(card) && !resolved && !pending;
  const kept = isFlagged(card) && resolved;
  const price = priceRange(card.price_usd_low, card.price_usd_high);

  return (
    <li
      className={`card-row${flagged ? " flagged" : ""}${pending ? " pending" : ""}${kept ? " kept" : ""}`}
      onClick={onFix}
      role="button"
      tabIndex={0}
      // Without a label the row would be named after ALL its text, including
      // the nested Fix / Keep buttons, so "Keep anyway" would name two controls.
      aria-label={`${card.name ?? "Unknown card"}${card.card_number ? ` ${card.card_number}` : ""} — edit`}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onFix();
        }
      }}
    >
      {liveSessionId ? (
        <img
          src={liveCardImageUrl(liveSessionId, card.row_index)}
          alt=""
          className="card-thumb review-thumb"
        />
      ) : card.image_url ? (
        <img src={card.image_url} alt={card.name ?? "card"} className="card-thumb review-thumb" />
      ) : (
        <div className="card-thumb review-thumb placeholder">?</div>
      )}
      <div className="card-row-body">
        <span className="card-name">{card.name ?? "Unknown card"}</span>
        {pending ? (
          <span className="live-chip-status">
            <Loader small /> Still identifying — wait or fix it
          </span>
        ) : (
          <span className="card-meta">
            {card.card_number ?? "—"} · {card.set_name ?? "Unknown set"}
            {card.rarity ? ` · ${card.rarity}` : ""}
          </span>
        )}
        {!pending && (price || kept) && (
          <span className="row" style={{ gap: 6 }}>
            {price && <span className="price">{price}</span>}
            {kept && <span className="badge gold">Kept</span>}
          </span>
        )}
        {flagged && (
          <div className="card-row-flag">
            <span className="reason">! {reasonText(card.low_confidence_reason)}</span>
            <button
              type="button"
              className="small primary"
              onClick={(e) => {
                e.stopPropagation();
                onFix();
              }}
            >
              Fix
            </button>
            <button
              type="button"
              className="small"
              onClick={(e) => {
                e.stopPropagation();
                onKeep();
              }}
            >
              Keep anyway
            </button>
          </div>
        )}
      </div>
      {!flagged && !pending && (
        <button
          type="button"
          className="small ghost row-edit"
          onClick={(e) => {
            e.stopPropagation();
            onFix();
          }}
        >
          Edit
        </button>
      )}
    </li>
  );
}
