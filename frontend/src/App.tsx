import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  analyzeCardImage,
  lookupPricesFromImage,
  type CardAnalyzeResponse,
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

function applyAnalyzeToReview(a: CardAnalyzeResponse) {
  return {
    reviewName: a.pokemon_name ?? "",
    reviewSetId: a.set_id ?? "",
    reviewNumber: a.collection_number ?? "",
    reviewSetCode: a.set_code ?? "",
  };
}

type Phase = "pick" | "review" | "results";

export default function App() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const fileId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [preHint, setPreHint] = useState("");
  const [phase, setPhase] = useState<Phase>("pick");
  const [analyzeLoading, setAnalyzeLoading] = useState(false);
  const [priceLoading, setPriceLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [analyzeMeta, setAnalyzeMeta] = useState<CardAnalyzeResponse | null>(
    null
  );
  const [reviewName, setReviewName] = useState("");
  const [reviewSetId, setReviewSetId] = useState("");
  const [reviewNumber, setReviewNumber] = useState("");
  const [reviewSetCode, setReviewSetCode] = useState("");
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

  const resetFlow = useCallback(() => {
    setPhase("pick");
    setAnalyzeMeta(null);
    setData(null);
    setError(null);
    setReviewName("");
    setReviewSetId("");
    setReviewNumber("");
    setReviewSetCode("");
  }, []);

  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    setFile(f ?? null);
    setError(null);
    setData(null);
    setAnalyzeMeta(null);
    setPhase(f ? "pick" : "pick");
    setReviewName("");
    setReviewSetId("");
    setReviewNumber("");
    setReviewSetCode("");
  };

  const onReadCard = async () => {
    if (!file) return;
    setAnalyzeLoading(true);
    setError(null);
    setAnalyzeMeta(null);
    setData(null);
    try {
      const res = await analyzeCardImage(file, {
        cardNameHint: preHint || undefined,
      });
      setAnalyzeMeta(res);
      const r = applyAnalyzeToReview(res);
      setReviewName(r.reviewName);
      setReviewSetId(r.reviewSetId);
      setReviewNumber(r.reviewNumber);
      setReviewSetCode(r.reviewSetCode);
      setPhase("review");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Read card failed");
    } finally {
      setAnalyzeLoading(false);
    }
  };

  const onFindPrice = async () => {
    if (!file) return;
    setPriceLoading(true);
    setError(null);
    setData(null);
    try {
      const res = await lookupPricesFromImage(file, {
        cardNameHint: reviewName || preHint || undefined,
        maxResults: 10,
        useReviewedFields: true,
        collectionNumber: reviewNumber,
        setId: reviewSetId,
        setCode: reviewSetCode,
      });
      setData(res);
      setPhase("results");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Price lookup failed");
    } finally {
      setPriceLoading(false);
    }
  };

  const busy = analyzeLoading || priceLoading;

  return (
    <div className="app">
      <header className="app-header">
        <h1>Card price check</h1>
        <p>Read the card, confirm details, then look up prices</p>
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
          onClick={() => {
            openPicker(false);
          }}
        >
          Choose different photo
        </button>
      ) : null}

      {phase === "pick" ? (
        <div>
          <label className="field-label" htmlFor="prehint">
            Name hint (optional, before read)
          </label>
          <input
            id="prehint"
            className="hint-input"
            placeholder="Helps OCR if the name is hard to read"
            value={preHint}
            onChange={(e) => setPreHint(e.target.value)}
            autoComplete="off"
            enterKeyHint="done"
          />
        </div>
      ) : null}

      {phase === "pick" ? (
        <button
          type="button"
          className="btn btn-primary"
          disabled={!file || busy}
          onClick={onReadCard}
        >
          {analyzeLoading ? "Reading card…" : "Read card"}
        </button>
      ) : null}

      {phase === "review" ? (
        <section className="review-panel">
          <h2 className="review-title">Check detected info</h2>
          <p className="review-sub">
            Edit anything that looks wrong, then run the price lookup.
          </p>
          {analyzeMeta?.symbol_match_distance != null ? (
            <p className="review-hint">
              Set symbol match distance: {analyzeMeta.symbol_match_distance}{" "}
              (lower is closer to your reference PNG)
            </p>
          ) : null}

          <div className="review-fields">
            <div>
              <label className="field-label" htmlFor="rev-name">
                Pokémon name
              </label>
              <input
                id="rev-name"
                className="hint-input"
                value={reviewName}
                onChange={(e) => setReviewName(e.target.value)}
                autoComplete="off"
                enterKeyHint="next"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="rev-setid">
                Set ID (PokéWallet)
              </label>
              <input
                id="rev-setid"
                className="hint-input"
                placeholder="From symbol table or API"
                value={reviewSetId}
                onChange={(e) => setReviewSetId(e.target.value)}
                autoComplete="off"
                enterKeyHint="next"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="rev-num">
                Card number
              </label>
              <input
                id="rev-num"
                className="hint-input"
                placeholder="e.g. 15/198"
                value={reviewNumber}
                onChange={(e) => setReviewNumber(e.target.value)}
                autoComplete="off"
                enterKeyHint="next"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="rev-code">
                Set code (optional)
              </label>
              <input
                id="rev-code"
                className="hint-input"
                placeholder="e.g. SV1"
                value={reviewSetCode}
                onChange={(e) => setReviewSetCode(e.target.value)}
                autoComplete="off"
                enterKeyHint="done"
              />
            </div>
          </div>

          {analyzeMeta?.suggested_search_queries?.length ? (
            <div className="meta-strip review-queries">
              <strong>Planned searches</strong>
              <ul className="query-chips">
                {analyzeMeta.suggested_search_queries.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="review-actions">
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => {
                resetFlow();
                setFile(null);
              }}
            >
              New photo
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={onReadCard}
            >
              Re-read card
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={onFindPrice}
            >
              {priceLoading ? "Looking up…" : "Find the price"}
            </button>
          </div>
        </section>
      ) : null}

      {phase === "results" ? (
        <section className="review-panel">
          <div className="review-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setPhase("review");
                setData(null);
              }}
            >
              Back to edit
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => {
                resetFlow();
                setFile(null);
              }}
            >
              Start over
            </button>
          </div>
        </section>
      ) : null}

      {error ? <div className="error-box">{error}</div> : null}

      {analyzeLoading ? (
        <div className="loading-row">
          <div className="spinner" aria-hidden />
          <span>Reading text from card…</span>
        </div>
      ) : null}

      {priceLoading ? (
        <div className="loading-row">
          <div className="spinner" aria-hidden />
          <span>Calling PokéWallet…</span>
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
