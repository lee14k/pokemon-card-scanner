import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  lookupPricesFromImage,
  type CardMatch,
  type PriceLookupResponse,
} from "./api";
import "./App.css";

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const eur = new Intl.NumberFormat("de-DE", {
  style: "currency",
  currency: "EUR",
  maximumFractionDigits: 2,
});

function formatUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return usd.format(n);
}

function formatEur(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return eur.format(n);
}

function CardResult({ card }: { card: CardMatch }) {
  const tcg = card.tcgplayer;
  const cm = card.cardmarket;

  return (
    <article className="result-card">
      <h3>{card.name}</h3>
      <div className="result-meta">
        {[card.set_name, card.number, card.rarity].filter(Boolean).join(" · ")}
      </div>
      <span className="score-pill">Match {card.match_score}%</span>

      {tcg?.prices && tcg.prices.length > 0 ? (
        <div className="price-block">
          <h4>TCGPlayer (USD)</h4>
          {tcg.prices.map((p, i) => (
            <div key={i} className="price-row">
              <span className="price-tag">
                <b>{p.sub_type_name ?? "Price"}</b>
              </span>
              <span className="price-tag">
                Market <b>{formatUsd(p.market_price)}</b>
              </span>
              <span className="price-tag">
                Low <b>{formatUsd(p.low_price)}</b>
              </span>
              <span className="price-tag">
                Mid <b>{formatUsd(p.mid_price)}</b>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {cm?.prices && cm.prices.length > 0 ? (
        <div className="price-block">
          <h4>Cardmarket (EUR)</h4>
          {cm.prices.map((p, i) => (
            <div key={i} className="price-row">
              <span className="price-tag">
                <b>{p.variant_type ?? "variant"}</b>
              </span>
              <span className="price-tag">
                Trend <b>{formatEur(p.trend)}</b>
              </span>
              <span className="price-tag">
                Low <b>{formatEur(p.low)}</b>
              </span>
              <span className="price-tag">
                Avg <b>{formatEur(p.avg)}</b>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {!tcg?.prices?.length && !cm?.prices?.length ? (
        <div className="price-block">
          <p className="result-meta" style={{ margin: 0 }}>
            No price rows returned for this listing.
          </p>
        </div>
      ) : null}

      <div className="external-links">
        {tcg?.url ? (
          <a href={tcg.url} target="_blank" rel="noopener noreferrer">
            TCGPlayer
          </a>
        ) : null}
        {cm?.product_url ? (
          <a href={cm.product_url} target="_blank" rel="noopener noreferrer">
            Cardmarket
          </a>
        ) : null}
      </div>
    </article>
  );
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const fileId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [hint, setHint] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<PriceLookupResponse | null>(null);

  useEffect(() => {
    if (!file) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const openPicker = useCallback((capture: boolean) => {
    const el = fileInputRef.current;
    if (!el) return;
    el.value = "";
    el.removeAttribute("capture");
    if (capture) {
      el.setAttribute("capture", "environment");
    }
    el.click();
  }, []);

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    setFile(f ?? null);
    setError(null);
    setData(null);
  };

  const onSubmit = async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const res = await lookupPricesFromImage(file, {
        cardNameHint: hint || undefined,
        maxResults: 10,
      });
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>Card price check</h1>
        <p>Photo of your card → live TCGPlayer &amp; Cardmarket prices</p>
      </header>

      <input
        ref={fileInputRef}
        id={fileId}
        type="file"
        accept="image/*"
        className="sr-only"
        onChange={onFileChange}
      />

      <div className="preview-wrap">
        {previewUrl ? (
          <img src={previewUrl} alt="Selected card" />
        ) : (
          <div className="preview-placeholder">
            <span>Add a clear photo of the card front</span>
          </div>
        )}
      </div>

      <div className="capture-row">
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => openPicker(true)}
        >
          Camera
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => openPicker(false)}
        >
          Library
        </button>
      </div>

      {file ? (
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => openPicker(false)}
        >
          Choose different photo
        </button>
      ) : null}

      <div>
        <label className="field-label" htmlFor="hint">
          Name hint (optional)
        </label>
        <input
          id="hint"
          className="hint-input"
          placeholder="e.g. Charizard ex — skips OCR if filled"
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          autoComplete="off"
          enterKeyHint="done"
        />
      </div>

      <button
        type="button"
        className="btn btn-primary"
        disabled={!file || loading}
        onClick={onSubmit}
      >
        {loading ? "Checking…" : "Get prices"}
      </button>

      {error ? <div className="error-box">{error}</div> : null}

      {loading ? (
        <div className="loading-row">
          <div className="spinner" aria-hidden />
          <span>Looking up prices…</span>
        </div>
      ) : null}

      {data ? (
        <section className="results-section">
          {data.ocr_text_sample ? (
            <div className="meta-strip">
              <strong>OCR</strong> {data.ocr_text_sample}
            </div>
          ) : null}
          <h2 style={{ marginTop: "1rem" }}>Results</h2>
          {data.matches.map((card) => (
            <CardResult key={card.id} card={card} />
          ))}
        </section>
      ) : null}
    </div>
  );
}
