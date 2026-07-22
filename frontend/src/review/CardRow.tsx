import { liveCardImageUrl, type LiveCard } from "../api";

const REASON_TEXT: Record<string, string> = {
  unreadable_strip: "Couldn't read this row",
  number_ambiguous: "Couldn't read the card number",
  set_ambiguous: "Couldn't tell which set this is from",
  no_db_match: "Card not found in the database",
};

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
  const flagged = (card.needs_review ?? card.low_confidence_reason !== null) && !resolved && !pending;

  return (
    <li
      className={`card-row${flagged ? " flagged" : ""}${pending ? " pending" : ""}`}
      onClick={onFix}
      role="button"
      tabIndex={0}
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
        <img src={card.image_url} alt={card.name ?? "card"} className="card-thumb" />
      ) : (
        <div className="card-thumb placeholder">?</div>
      )}
      <div className="card-row-body">
        <strong>{card.name ?? "Unknown card"}</strong>
        {pending ? (
          <span className="live-chip-status">
            <span className="spinner" /> still identifying — wait or fix manually
          </span>
        ) : (
          <span>
            {card.card_number ?? "—"} · {card.set_name ?? "Unknown set"}
            {card.rarity ? ` · ${card.rarity}` : ""}
          </span>
        )}
        {flagged && (
          <div className="card-row-flag">
            <em>{REASON_TEXT[card.low_confidence_reason!] ?? "Needs review"}</em>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onFix();
              }}
            >
              Fix
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onKeep();
              }}
            >
              Keep anyway
            </button>
          </div>
        )}
        {!flagged && !pending && (
          <button
            type="button"
            className="card-row-edit"
            onClick={(e) => {
              e.stopPropagation();
              onFix();
            }}
          >
            Edit
          </button>
        )}
      </div>
    </li>
  );
}
