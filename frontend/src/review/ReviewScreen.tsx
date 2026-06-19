import { useState } from "react";
import type { PackScanResponse, PackCard } from "../api";
import CardRow from "./CardRow";
import FixCardForm from "./FixCardForm";

interface Props {
  scan: PackScanResponse;
  onConfirm: (cards: PackCard[]) => void;
  onRetake: () => void;
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

export default function ReviewScreen({ scan, onConfirm, onRetake }: Props) {
  const [cards, setCards] = useState<PackCard[]>(scan.cards);
  const [resolvedRows, setResolvedRows] = useState<Set<number>>(new Set());
  const [fixing, setFixing] = useState<number | null>(null);

  const markResolved = (row: number) =>
    setResolvedRows((prev) => new Set(prev).add(row));

  const unresolved = cards.filter(
    (c) => c.low_confidence_reason !== null && !resolvedRows.has(c.row_index)
  );

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
              prev.map((c) => (c.row_index === fixing ? fixed : c))
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
