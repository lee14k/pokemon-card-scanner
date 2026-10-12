import type { CSSProperties } from "react";
import PixelIcon from "../ui/PixelIcon";

const CARD_COUNT = 5;
// Demo staircase geometry, as fractions of the stage height (mirrors the
// capture-guide layout in StaircaseCapture, shifted to fit the demo frame).
const FIRST_GUIDE = 0.48;
const LAST_GUIDE = 0.94;
const CARD_HEIGHT = 0.42;

const fractions = Array.from(
  { length: CARD_COUNT },
  (_, i) => FIRST_GUIDE + ((LAST_GUIDE - FIRST_GUIDE) * i) / (CARD_COUNT - 1)
);

export default function Landing({ onStart }: { onStart: () => void }) {
  return (
    <section className="hero">
      <span className="hero-kicker">
        <span className="badge red">New game</span>
        <span className="badge outline">1 player</span>
      </span>
      <h2>
        Scan a whole pack in <em>one shot</em>
      </h2>
      <p className="hero-sub">
        Stack your cards in a staircase, snap a single photo, and we identify every card — then log
        it, price it, fill your Pokédex and battle it.
      </p>

      <div
        className="scan-demo"
        role="img"
        aria-label="Animated demo: cards fan out into a staircase, yellow guide lines appear under each card edge, a scan beam sweeps down, the camera snaps, and all cards are identified"
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
          <div key={`guide-${i}`} className="demo-guide" style={{ top: `${(f * 100).toFixed(1)}%` }} />
        ))}
        <div className="demo-beam" />
        <div className="demo-flash" />
        <div className="demo-check">
          <span className="badge green">✓ 5 cards found</span>
        </div>
      </div>

      <div className="demo-captions" aria-hidden="true">
        <span className="demo-cap-1">1 · Stack your pack</span>
        <span className="demo-cap-2">2 · Line up the edges</span>
        <span className="demo-cap-3">3 · One snap — done!</span>
      </div>

      <button type="button" className="primary press-start" onClick={onStart}>
        <span className="blink">▶</span> Press start
      </button>

      <div className="feature-tiles">
        <div className="panel feature-tile">
          <span className="icon-box red">
            <PixelIcon name="camera" size={24} />
          </span>
          <div>
            <h3>One photo</h3>
            <p>A whole fanned-out pack, identified from a single snap.</p>
          </div>
        </div>
        <div className="panel feature-tile">
          <span className="icon-box cyan">
            <PixelIcon name="bolt" size={24} />
          </span>
          <div>
            <h3>Live scan</h3>
            <p>Hold cards up one by one — the camera grabs each automatically.</p>
          </div>
        </div>
        <div className="panel feature-tile">
          <span className="icon-box green">
            <PixelIcon name="grid" size={24} />
          </span>
          <div>
            <h3>Binder pages</h3>
            <p>Photograph a binder page and file every card into your collection.</p>
          </div>
        </div>
        <div className="panel feature-tile">
          <span className="icon-box">
            <PixelIcon name="swords" size={24} />
          </span>
          <div>
            <h3>Pack battles</h3>
            <p>Pit a verified pack&apos;s value against friends, randoms or a bot.</p>
          </div>
        </div>
      </div>
    </section>
  );
}
