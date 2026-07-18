import type { CSSProperties } from "react";

const CARD_COUNT = 5;
// Demo staircase geometry, as fractions of the stage height (mirrors the
// capture-guide layout in StaircaseCapture, shifted to fit the demo frame).
const FIRST_GUIDE = 0.46;
const LAST_GUIDE = 0.94;
const CARD_HEIGHT = 0.42;

const fractions = Array.from(
  { length: CARD_COUNT },
  (_, i) => FIRST_GUIDE + ((LAST_GUIDE - FIRST_GUIDE) * i) / (CARD_COUNT - 1)
);

export default function Landing({ onStart }: { onStart: () => void }) {
  return (
    <section className="landing">
      <h2>Scan a whole pack in one shot</h2>
      <p className="landing-sub">
        Stack your cards in a staircase, line the edges up with the guides, and
        snap a single photo — we identify every card in the pack.
      </p>

      <div
        className="scan-demo"
        role="img"
        aria-label="Animated demo: cards fan out into a staircase, yellow guide lines appear under each card edge, the camera snaps, and all cards are identified"
      >
        {fractions.map((f, i) => (
          <div
            key={`card-${i}`}
            className="demo-card"
            style={
              {
                "--ty": `${(((f - FIRST_GUIDE) / CARD_HEIGHT) * 100).toFixed(1)}%`,
                zIndex: CARD_COUNT - i,
              } as CSSProperties
            }
          />
        ))}
        {fractions.map((f, i) => (
          <div
            key={`guide-${i}`}
            className="demo-guide"
            style={{ top: `${(f * 100).toFixed(1)}%` }}
          />
        ))}
        <div className="demo-flash" />
        <div className="demo-check">✓ 5 cards found</div>
      </div>

      <div className="demo-captions" aria-hidden="true">
        <span className="demo-cap-1">1 · Stack your pack in a staircase</span>
        <span className="demo-cap-2">2 · Bottom edges on the yellow lines</span>
        <span className="demo-cap-3">3 · One snap — every card identified</span>
      </div>

      <button type="button" className="primary" onClick={onStart}>
        Get scanning!
      </button>
    </section>
  );
}
