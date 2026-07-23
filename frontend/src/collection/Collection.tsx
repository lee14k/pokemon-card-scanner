import { useEffect, useState } from "react";
import {
  getCollection,
  patchCollectionQty,
  deleteCollectionCard,
  type CollectionCardOut,
  type CollectionOut,
} from "../api";

// Same low/high price formatting the pack + binder rows use, so a collection
// card's price reads identically to the rest of the app.
function priceText(c: CollectionCardOut): string {
  const lo = c.price_usd_low ?? null;
  const hi = c.price_usd_high ?? null;
  if (lo == null && hi == null) return "—";
  if (lo != null && hi != null) {
    return lo === hi ? `$${lo.toFixed(2)}` : `$${lo.toFixed(2)}–$${hi.toFixed(2)}`;
  }
  return `$${((lo ?? hi) as number).toFixed(2)}`;
}

export default function Collection() {
  const [data, setData] = useState<CollectionOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // id being mutated

  const load = () => getCollection().then(setData).catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, []);

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

  if (error) return <p className="camera-error">{error}</p>;
  if (!data) return <p className="status">Loading your collection…</p>;
  if (data.cards.length === 0)
    return <p>No cards in your collection yet — scan a binder page!</p>;

  // Distinct-card count and total qty track optimistic edits; the estimated
  // value is the server's priced snapshot (labeled "≈ · prices as of …").
  const distinct = data.cards.length;
  const totalQty = data.cards.reduce((sum, c) => sum + c.qty, 0);

  return (
    <section>
      <h2>My Collection</h2>
      <p style={{ color: "var(--text-muted)" }}>
        {distinct} card{distinct === 1 ? "" : "s"} · {totalQty} total
        {data.estimated_value != null && (
          <>
            {" · "}≈ ${data.estimated_value.toFixed(2)}
            {data.priced_as_of && (
              <> (prices as of {new Date(data.priced_as_of).toLocaleDateString()})</>
            )}
          </>
        )}
      </p>
      <div
        className="collection-grid"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
          gap: "0.5rem",
        }}
      >
        {data.cards.map((c) => (
          <div
            key={c.id}
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 4,
              padding: "0.5rem",
              border: "1px solid var(--border)",
              background: "var(--surface)",
              borderRadius: "var(--radius)",
              opacity: busy === c.id ? 0.55 : 1,
            }}
          >
            {c.image_url ? (
              <img
                src={c.image_url}
                alt={c.name ?? "card"}
                style={{ width: "100%", aspectRatio: "63 / 88", objectFit: "cover", borderRadius: 4 }}
              />
            ) : (
              <div
                style={{
                  width: "100%",
                  aspectRatio: "63 / 88",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  textAlign: "center",
                  padding: 4,
                  background: "var(--bg-elevated)",
                  color: "var(--text-muted)",
                  borderRadius: 4,
                  fontSize: "0.8rem",
                }}
              >
                {c.name ?? "?"}
              </div>
            )}
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 6 }}>
              <strong style={{ fontSize: "0.9rem" }}>{c.name ?? "Unknown card"}</strong>
              <span
                style={{
                  flexShrink: 0,
                  fontSize: "0.75rem",
                  fontWeight: 700,
                  padding: "0.05rem 0.4rem",
                  borderRadius: 999,
                  background: "var(--bg-elevated)",
                  border: "1px solid var(--border)",
                  color: "var(--text-muted)",
                }}
              >
                ×{c.qty}
              </span>
            </div>
            <span style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}>
              {c.card_number ?? "—"} · {c.set_name ?? "Unknown set"}
            </span>
            <span style={{ fontSize: "0.8rem" }}>{priceText(c)}</span>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
              <button
                type="button"
                aria-label="Decrease quantity"
                disabled={c.qty <= 1 || busy === c.id}
                onClick={() => changeQty(c.id, c.qty - 1)}
                style={{ minHeight: 32, minWidth: 32, padding: 0 }}
              >
                −
              </button>
              <button
                type="button"
                aria-label="Increase quantity"
                disabled={busy === c.id}
                onClick={() => changeQty(c.id, c.qty + 1)}
                style={{ minHeight: 32, minWidth: 32, padding: 0 }}
              >
                +
              </button>
              <button
                type="button"
                aria-label="Remove card"
                disabled={busy === c.id}
                onClick={() => remove(c.id)}
                style={{ minHeight: 32, minWidth: 32, padding: 0, marginLeft: "auto" }}
              >
                ✕
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
