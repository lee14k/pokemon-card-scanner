import { useEffect, useState } from "react";
import { getSets, lookupCard, type PackCard, type SetInfo } from "../api";

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
    <div className="fix-form">
      <h3>Fix card</h3>
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
        <input
          value={number}
          placeholder="e.g. 123/198"
          onChange={(e) => setNumber(e.target.value)}
        />
      </label>
      <button type="button" onClick={look} disabled={!setId || !number.trim()}>
        Look up
      </button>
      {status === "looking" && <p>Looking…</p>}
      {status === "miss" && <p>No card found for that set + number.</p>}
      {status === "error" && <p>Lookup failed — try again.</p>}
      {preview && (
        <div className="fix-preview">
          {preview.image_url && <img src={preview.image_url} alt={preview.name ?? ""} />}
          <p>
            {preview.name} · {preview.card_number} · {preview.rarity ?? "?"}
          </p>
          <button type="button" className="primary" onClick={() => onApply(preview)}>
            That&apos;s it
          </button>
        </div>
      )}
      <button type="button" onClick={onCancel}>Cancel</button>
    </div>
  );
}
