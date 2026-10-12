import { useCallback, useEffect, useState } from "react";
import { friendlyError, shortDate } from "@shared/format";
import { getDex, type DexOut } from "../api";
import { CardArt, EmptyState, ErrorBox, LoadingScreen, ScreenTitle } from "../ui/kit";

export default function Dex({ onScan }: { onScan: () => void }) {
  const [dex, setDex] = useState<DexOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    getDex().then(setDex).catch((e) => setError(friendlyError(e)));
  }, []);

  useEffect(load, [load]);

  if (error) return <ErrorBox message={error} onRetry={load} />;
  if (!dex) return <LoadingScreen label="Booting Pokédex…" />;

  return (
    <section className="stack-lg">
      <ScreenTitle kicker="Pokédex" title={`Seen: ${String(dex.seen_count).padStart(3, "0")}`} sub="Every species you've pulled — newest discoveries first." />
      {dex.entries.length === 0 ? (
        <EmptyState icon="dex" title="No Pokémon seen yet">
          <p className="muted">Scan a pack and every Pokémon in it registers here.</p>
          <button type="button" className="primary" onClick={onScan}>
            Scan a pack
          </button>
        </EmptyState>
      ) : (
        <div className="dex-grid">
          {dex.entries.map((e, i) => (
            <div key={e.species} className="card-tile dex-entry">
              {/* entries arrive newest-first; number them in discovery order */}
              <span className="dex-no">No.{String(dex.entries.length - i).padStart(3, "0")}</span>
              <CardArt src={e.image_url} alt={e.species} />
              <span className="tile-name">{e.species}</span>
              <span className="row row-between" style={{ gap: 4 }}>
                <span className="badge outline">×{e.count}</span>
                <span className="tile-meta">{shortDate(e.first_seen)}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
