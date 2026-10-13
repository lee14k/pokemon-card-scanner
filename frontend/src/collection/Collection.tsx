import { useCallback, useEffect, useMemo, useState } from "react";
import { compactUsd, friendlyError, priceRange, shortDate } from "@shared/format";
import {
  getCollection,
  patchCollectionQty,
  deleteCollectionCard,
  type CollectionOut,
} from "../api";
import PixelIcon from "../ui/PixelIcon";
import { CardArt, EmptyState, ErrorBox, LoadingScreen, ScreenTitle, StatTile } from "../ui/kit";

export default function Collection({ onScanBinder }: { onScanBinder: () => void }) {
  const [data, setData] = useState<CollectionOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // id being mutated
  const [query, setQuery] = useState("");

  const load = useCallback(
    () => getCollection().then(setData).catch((e) => setError(friendlyError(e))),
    []
  );

  useEffect(() => {
    load();
  }, [load]);

  // Optimistic qty change: apply locally, PATCH, roll back + reload on failure.
  const changeQty = async (id: string, qty: number) => {
    if (!data || qty < 1) return;
    const prev = data;
    setData({ ...data, cards: data.cards.map((c) => (c.id === id ? { ...c, qty } : c)) });
    setBusy(id);
    try {
      await patchCollectionQty(id, qty);
    } catch {
      setData(prev);
      await load();
    } finally {
      setBusy(null);
    }
  };

  // Optimistic delete: drop the row, DELETE, restore + reload on failure.
  const remove = async (id: string) => {
    if (!data) return;
    if (!window.confirm("Remove this card from your collection?")) return;
    const prev = data;
    setData({ ...data, cards: data.cards.filter((c) => c.id !== id) });
    setBusy(id);
    try {
      await deleteCollectionCard(id);
    } catch {
      setData(prev);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!data || !q) return data?.cards ?? [];
    return data.cards.filter((c) =>
      [c.name, c.set_name, c.card_number].some((f) => f?.toLowerCase().includes(q))
    );
  }, [data, query]);

  if (error)
    return (
      <ErrorBox
        message={error}
        onRetry={() => {
          setError(null);
          load();
        }}
      />
    );
  if (!data) return <LoadingScreen label="Opening your collection…" />;

  // Distinct-card count and total qty track optimistic edits; the estimated
  // value is the server's priced snapshot (labeled "≈ · prices as of …").
  const distinct = data.cards.length;
  const totalQty = data.cards.reduce((sum, c) => sum + c.qty, 0);

  return (
    <section className="stack-lg">
      <ScreenTitle
        kicker="Card vault"
        title="My collection"
        sub={
          data.priced_as_of
            ? `Prices as of ${shortDate(data.priced_as_of)}.`
            : "Scan binder pages to fill it up."
        }
      />

      {distinct === 0 ? (
        <EmptyState icon="chest" title="Your vault is empty">
          <p className="muted">Snap a binder page and every card on it lands here.</p>
          <button type="button" className="primary" onClick={onScanBinder}>
            Scan a binder page
          </button>
        </EmptyState>
      ) : (
        <>
          <div className="stat-tiles">
            <StatTile value={distinct} label="Unique" />
            <StatTile value={totalQty} label="Total qty" tone="cyan" />
            {data.estimated_value != null && <StatTile value={compactUsd(data.estimated_value)} label="Est. value" tone="green" />}
          </div>

          <div className="row">
            <input
              type="search"
              style={{ width: "auto", flex: "1 1 160px", minWidth: 0 }}
              placeholder="Search name, set or number…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search your collection"
            />
            <button type="button" onClick={onScanBinder}>
              <PixelIcon name="plus" size={14} /> Add page
            </button>
          </div>

          {shown.length === 0 && <p className="muted">No cards match “{query}”.</p>}

          <div className="tile-grid">
            {shown.map((c) => {
              const price = priceRange(c.price_usd_low, c.price_usd_high);
              return (
                <div key={c.id} className={`card-tile${busy === c.id ? " busy" : ""}`}>
                  <CardArt src={c.image_url} alt={c.name ?? "card"} fallback={c.name ?? "?"} />
                  <span className="badge gold qty">×{c.qty}</span>
                  <span className="tile-name">{c.name ?? "Unknown card"}</span>
                  <span className="tile-meta">
                    {c.card_number ?? "—"} · {c.set_name ?? "Unknown set"}
                  </span>
                  <span className="price">{price ?? "—"}</span>
                  <div className="qty-stepper">
                    <button
                      type="button"
                      className="small icon-only"
                      aria-label="Decrease quantity"
                      disabled={c.qty <= 1 || busy === c.id}
                      onClick={() => changeQty(c.id, c.qty - 1)}
                    >
                      <PixelIcon name="minus" size={12} />
                    </button>
                    <button
                      type="button"
                      className="small icon-only"
                      aria-label="Increase quantity"
                      disabled={busy === c.id}
                      onClick={() => changeQty(c.id, c.qty + 1)}
                    >
                      <PixelIcon name="plus" size={12} />
                    </button>
                    <button
                      type="button"
                      className="small icon-only ghost remove"
                      aria-label="Remove card"
                      disabled={busy === c.id}
                      onClick={() => remove(c.id)}
                    >
                      <PixelIcon name="close" size={12} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}
