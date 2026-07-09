import { useEffect, useState } from "react";
import { getDex, type DexOut } from "../api";

export default function Dex() {
  const [dex, setDex] = useState<DexOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDex().then(setDex).catch((e) => setError(String(e)));
  }, []);

  if (error) return <p className="camera-error">{error}</p>;
  if (!dex) return <p className="status">Loading your Pokédex…</p>;
  if (dex.entries.length === 0) return <p>No Pokémon seen yet — scan a pack!</p>;

  return (
    <section>
      <h2>Pokédex — Seen: {dex.seen_count} species</h2>
      <ul className="card-rows">
        {dex.entries.map((e) => (
          <li key={e.species} className="card-row">
            {e.image_url ? (
              <img src={e.image_url} alt={e.species} className="card-thumb" />
            ) : (
              <div className="card-thumb placeholder">?</div>
            )}
            <div className="card-row-body">
              <strong>{e.species}</strong>
              <span>seen ×{e.count} · first {new Date(e.first_seen).toLocaleDateString()}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
