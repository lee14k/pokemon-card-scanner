import { useEffect, useState } from "react";
import { getSets, lookupCard, type PackCard, type SetInfo } from "../api";
import { Loader, Modal } from "../ui/kit";

interface Props {
  initial: PackCard;
  onApply: (fixed: PackCard) => void;
  onCancel: () => void;
}

export default function FixCardForm({ initial, onApply, onCancel }: Props) {
  const [sets, setSets] = useState<SetInfo[]>([]);
  const [setId, setSetId] = useState(initial.set_id ?? "");
  const [number, setNumber] = useState(initial.card_number ?? "");
  const [preview, setPreview] = useState<PackCard | null>(null);
  const [status, setStatus] = useState<"idle" | "looking" | "miss" | "error">("idle");

  useEffect(() => {
    getSets().then(setSets).catch(() => setSets([]));
  }, []);

  const look = async () => {
    if (!setId || !number.trim()) return;
    setStatus("looking");
    setPreview(null);
    try {
      const res = await lookupCard(setId, number.trim());
      if (res.found && res.card) {
        setPreview({ ...res.card, row_index: initial.row_index });
        setStatus("idle");
      } else {
        setStatus("miss");
      }
    } catch {
      setStatus("error");
    }
  };

  return (
    <Modal title="Fix card" onClose={onCancel}>
      <form
        className="form fix-form"
        onSubmit={(e) => {
          e.preventDefault();
          void look();
        }}
      >
        <p className="muted small-text">
          Pick the set and type the number printed at the bottom of the card.
        </p>
        <label>
          Set
          <select value={setId} onChange={(e) => setSetId(e.target.value)}>
            <option value="">Pick a set…</option>
            {sets.map((s) => (
              <option key={s.set_id} value={s.set_id}>
                {s.set_name} {s.set_code ? `(${s.set_code})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Card number
          <input value={number} placeholder="e.g. 123/198" onChange={(e) => setNumber(e.target.value)} />
        </label>
        <button type="submit" disabled={!setId || !number.trim() || status === "looking"}>
          {status === "looking" ? <Loader small label="Looking up" /> : "Look up"}
        </button>
        {status === "miss" && <p className="camera-error">No card found for that set + number.</p>}
        {status === "error" && <p className="camera-error">Lookup failed — try again.</p>}
        {preview && (
          <div className="panel tight row fix-preview" style={{ alignItems: "flex-start" }}>
            {preview.image_url && (
              <img src={preview.image_url} alt={preview.name ?? ""} className="card-thumb review-thumb" />
            )}
            <div className="stack grow" style={{ gap: 6 }}>
              <span className="card-name">{preview.name}</span>
              <span className="card-meta">
                {preview.card_number} · {preview.rarity ?? "?"}
              </span>
              <button type="button" className="primary small" onClick={() => onApply(preview)}>
                That&apos;s it!
              </button>
            </div>
          </div>
        )}
        <button type="button" className="ghost" onClick={onCancel}>
          Cancel
        </button>
      </form>
    </Modal>
  );
}
