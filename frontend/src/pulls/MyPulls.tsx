import { useCallback, useEffect, useState } from "react";
import { compactUsd, friendlyError, plural, priceRange, shortDate, shortDateTime, usd } from "@shared/format";
import { listPulls, type SavedPull } from "../api";
import PixelIcon from "../ui/PixelIcon";
import { EmptyState, ErrorBox, LoadingScreen, ScreenTitle, StatTile } from "../ui/kit";

export default function MyPulls({ onScan }: { onScan: () => void }) {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    listPulls().then(setPulls).catch((e) => setError(friendlyError(e)));
  }, []);

  useEffect(load, [load]);

  if (error) return <ErrorBox message={error} onRetry={load} />;
  if (!pulls) return <LoadingScreen label="Loading your pulls…" />;

  const verified = pulls.filter((p) => p.verified).length;
  const totalCards = pulls.reduce((n, p) => n + p.cards.length, 0);
  const value = pulls.reduce((n, p) => n + (p.estimated_value ?? 0), 0);

  return (
    <section className="stack-lg">
      <ScreenTitle kicker="Trainer log" title="My pulls" sub="Every pack you've opened and logged." />

      {pulls.length === 0 ? (
        <EmptyState icon="pack" title="No pulls yet">
          <p className="muted">Rip open a pack and scan it — it'll show up here.</p>
          <button type="button" className="primary" onClick={onScan}>
            Scan a pack
          </button>
        </EmptyState>
      ) : (
        <>
          <div className="stat-tiles">
            <StatTile value={pulls.length} label="Packs" />
            <StatTile value={totalCards} label="Cards" tone="cyan" />
            <StatTile value={verified} label="Verified" tone="green" />
            {value > 0 && <StatTile value={compactUsd(value)} label="Est. value" />}
          </div>

          <ul className="card-rows">
            {pulls.map((p) => {
              const isOpen = open === p.id;
              return (
                <li key={p.id} className="panel pull-card">
                  <button
                    type="button"
                    className="pull-head"
                    aria-expanded={isOpen}
                    onClick={() => setOpen(isOpen ? null : p.id)}
                  >
                    <span className={`icon-box ${p.verified ? "green" : "purple"}`}>
                      <PixelIcon name={p.verified ? "check" : "pack"} size={20} />
                    </span>
                    <span className="stack" style={{ gap: 4, minWidth: 0 }}>
                      <span className="pull-title">{shortDateTime(p.created_at)}</span>
                      <span className="row" style={{ gap: 6 }}>
                        <span className="badge outline">{plural(p.cards.length, "card")}</span>
                        {p.verified ? (
                          <span className="badge green">Verified</span>
                        ) : (
                          <span className="badge outline">Unverified</span>
                        )}
                        {p.estimated_value != null && p.priced_as_of != null && (
                          <span className="price">≈ {usd(p.estimated_value)}</span>
                        )}
                      </span>
                    </span>
                    <span className="chev">▶</span>
                  </button>
                  {isOpen && (
                    <>
                      <p className="muted small-text">
                        Code <code>{p.code ?? "—"}</code>
                        {p.priced_as_of && <> · prices as of {shortDate(p.priced_as_of)}</>}
                      </p>
                      <div className="pull-cards">
                        {p.cards.map((c) => (
                          <div key={c.row_index} className="mini-card">
                            {c.image_url ? (
                              <img src={c.image_url} alt={c.name ?? "card"} loading="lazy" />
                            ) : (
                              <div className="art">?</div>
                            )}
                            <span title={c.name ?? undefined}>{c.name ?? c.card_number ?? "?"}</span>
                            <span className="price">{priceRange(c.price_usd_low, c.price_usd_high) ?? "—"}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
