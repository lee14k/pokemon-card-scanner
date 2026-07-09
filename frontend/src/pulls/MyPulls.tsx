import { useEffect, useState } from "react";
import { listPulls, type PackCard, type SavedPull } from "../api";

function cardPrice(c: PackCard): string {
  if (c.price_usd_low == null || c.price_usd_high == null) return "—";
  if (c.price_usd_low === c.price_usd_high) return `$${c.price_usd_low.toFixed(2)}`;
  return `$${c.price_usd_low.toFixed(2)}–$${c.price_usd_high.toFixed(2)}`;
}

export default function MyPulls() {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

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
            <button type="button" className="pull-row-toggle"
                    onClick={() => setOpen(open === p.id ? null : p.id)}>
              <strong>{new Date(p.created_at).toLocaleString()}</strong>
            </button>
            <span>
              {p.cards.length} cards · code {p.code ?? "—"} ·{" "}
              {p.verified ? "✓ verified" : "unverified"}
              {p.estimated_value != null && p.priced_as_of != null && (
                <> · ≈ ${p.estimated_value.toFixed(2)} (prices as of{" "}
                {new Date(p.priced_as_of).toLocaleDateString()})</>
              )}
            </span>
            {open === p.id && (
              <ul className="card-rows">
                {p.cards.map((c) => (
                  <li key={c.row_index} className="card-row">
                    <div className="card-row-body">
                      <span>{c.name ?? c.card_number ?? "?"} · {cardPrice(c)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
