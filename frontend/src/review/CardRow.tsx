import type { PackCard } from "../api";

const REASON_TEXT: Record<string, string> = {
  unreadable_strip: "Couldn't read this row",
  number_ambiguous: "Couldn't read the card number",
  set_ambiguous: "Couldn't tell which set this is from",
  no_db_match: "Card not found in the database",
};

interface Props {
  card: PackCard;
  resolved: boolean; // user has fixed or accepted a flagged row
  onFix: () => void;
  onKeep: () => void;
}

export default function CardRow({ card, resolved, onFix, onKeep }: Props) {
  const flagged = (card.needs_review ?? card.low_confidence_reason !== null) && !resolved;
  return (
    <li className={`card-row${flagged ? " flagged" : ""}`}>
      {card.image_url ? (
        <img src={card.image_url} alt={card.name ?? "card"} className="card-thumb" />
      ) : (
        <div className="card-thumb placeholder">?</div>
      )}
      <div className="card-row-body">
        <strong>{card.name ?? "Unknown card"}</strong>
        <span>
          {card.card_number ?? "—"} · {card.set_name ?? "Unknown set"}
          {card.rarity ? ` · ${card.rarity}` : ""}
        </span>
        {flagged && (
          <div className="card-row-flag">
            <em>{REASON_TEXT[card.low_confidence_reason!] ?? "Needs review"}</em>
            <button type="button" onClick={onFix}>Fix</button>
            <button type="button" onClick={onKeep}>Keep anyway</button>
          </div>
        )}
      </div>
    </li>
  );
}
