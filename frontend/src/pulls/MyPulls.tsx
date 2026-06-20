import { useEffect, useState } from "react";
import { listPulls, type SavedPull } from "../api";

export default function MyPulls() {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listPulls().then(setPulls).catch((e) => setError(String(e)));
  }, []);

  if (error) return <p className="camera-error">{error}</p>;
  if (!pulls) return <p className="status">Loading your pulls…</p>;
  if (pulls.length === 0) return <p>No pulls saved yet — scan a pack!</p>;

  return (
    <ul className="card-rows">
      {pulls.map((p) => (
        <li key={p.id} className="card-row">
          <div className="card-row-body">
            <strong>{new Date(p.created_at).toLocaleString()}</strong>
            <span>
              {p.cards.length} cards · code {p.code ?? "—"} ·{" "}
              {p.verified ? "✓ verified" : "unverified"}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
