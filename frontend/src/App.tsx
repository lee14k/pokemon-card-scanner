import { useCallback, useRef, useState } from "react";
import "./App.css";
import { friendlyError, plural, scanStageText } from "@shared/format";
import {
  isNoCardsFound,
  savePull,
  scanBinder,
  saveToCollection,
  scanPack,
  scanPackStream,
  type BinderScan,
  type CaptureMeta,
  type CollectionSaveCard,
  type CollectionSaveOut,
  type Encounter,
  type PackCard,
  type PackScanResponse,
  type ScanProgressEvent,
} from "./api";
import StaircaseCapture from "./capture/StaircaseCapture";
import BinderCapture from "./capture/BinderCapture";
import CodeCardCapture from "./capture/CodeCardCapture";
import LiveScanScreen, { SESSION_STORAGE_KEY as LIVE_SESSION_STORAGE_KEY } from "./capture/LiveScanScreen";
import ReviewScreen from "./review/ReviewScreen";
import BinderReview from "./review/BinderReview";
import { useAuth } from "./auth/AuthContext";
import AuthForms from "./auth/AuthForms";
import MyPulls from "./pulls/MyPulls";
import Collection from "./collection/Collection";
import Dashboard from "./dashboard/Dashboard";
import Dex from "./dex/Dex";
import Battles from "./battles/Battles";
import Landing from "./landing/Landing";
import { PROTECTED, TabBar, Topbar, type View } from "./shell/Nav";
import PixelIcon from "./ui/PixelIcon";
import { Confetti, ErrorBox, HpBar, Loader, LoadingScreen, Modal, ScreenTitle, StatTile } from "./ui/kit";

type Step =
  | { name: "mode" }
  | { name: "staircase" }
  | { name: "live" }
  | { name: "code"; staircase: Blob; meta?: CaptureMeta }
  | { name: "submitting"; stage?: string; count?: number; done?: number; total?: number }
  | { name: "review"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; liveSessionId?: string }
  | { name: "saving"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; cards: PackCard[]; liveSessionId?: string }
  | { name: "summary"; verified: boolean; count: number; encounters: Encounter[]; pullId: string }
  | { name: "binder_capture" }
  | { name: "binder_review"; scan: BinderScan; photo: Blob }
  | { name: "binder_summary"; out: CollectionSaveOut }
  | { name: "error"; message: string };

// Feature-detect fetch+ReadableStream support (unavailable in some older/embedded
// webviews) — those fall straight through to the non-streaming scanPack().
function supportsScanStream(): boolean {
  return typeof window !== "undefined" && "ReadableStream" in window
    && typeof window.ReadableStream === "function";
}

const LOGIN_REASONS: Partial<Record<View, string>> = {
  pulls: "Log in to see the packs you've logged.",
  dex: "Log in to fill your Pokédex.",
  collection: "Log in to keep a collection.",
  battles: "Log in to battle other trainers.",
  dashboard: "Log in to view the dashboard.",
};

export default function App() {
  const { trainer, loading, logout } = useAuth();
  const [step, setStep] = useState<Step>({ name: "mode" });
  const [view, setView] = useState<View>("home");
  const [battlePull, setBattlePull] = useState<string | null>(null);
  const [authPrompt, setAuthPrompt] = useState<string | null>(null);
  // Runs once the login modal succeeds — e.g. open the tab the user tapped, or
  // start the live/binder scan they picked. Cleared on cancel.
  const afterLogin = useRef<(() => void) | null>(null);
  const canViewStats = trainer?.role === "analyst" || trainer?.role === "admin";

  const askLogin = useCallback((reason: string, then?: () => void) => {
    afterLogin.current = then ?? null;
    setAuthPrompt(reason);
  }, []);

  const navigate = useCallback(
    (v: View) => {
      if (PROTECTED.has(v) && !trainer) {
        askLogin(LOGIN_REASONS[v] ?? "Log in to continue.", () => setView(v));
        return;
      }
      // Coming back to Scan resumes an in-progress flow (a review survives a
      // peek at the Dex), but a finished or failed one starts fresh.
      if (v === "scan") {
        setStep((s) => (s.name === "summary" || s.name === "binder_summary" || s.name === "error" ? { name: "mode" } : s));
      }
      setView(v);
      window.scrollTo({ top: 0 });
    },
    [trainer, askLogin]
  );

  const startScan = () => {
    setStep({ name: "mode" });
    setView("scan");
    window.scrollTo({ top: 0 });
  };

  const submit = async (staircase: Blob, code: Blob, meta?: CaptureMeta) => {
    setStep({ name: "submitting" });
    const onProgress = (ev: ScanProgressEvent) => {
      setStep((prev) => (prev.name === "submitting" ? { ...prev, ...ev } : prev));
    };
    try {
      let scan: PackScanResponse;
      if (supportsScanStream()) {
        try {
          scan = await scanPackStream(staircase, code, meta, onProgress);
        } catch {
          // Any stream failure (network hiccup, parse error, mid-stream error
          // event) falls back to the plain non-streaming scan — reset the
          // stage/skeleton state first so the fallback shows a plain spinner.
          setStep({ name: "submitting" });
          scan = await scanPack(staircase, code, meta);
        }
      } else {
        scan = await scanPack(staircase, code, meta);
      }
      setStep({ name: "review", scan, staircase, code, meta });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  const doSave = async (s: Extract<Step, { name: "review" }>, cards: PackCard[]) => {
    if (!trainer) {
      // ReviewScreen keeps its edits, so tapping save again after login saves.
      askLogin("Log in to save this pack to your pulls.");
      return;
    }
    setStep({ name: "saving", scan: s.scan, staircase: s.staircase, code: s.code, meta: s.meta, cards, liveSessionId: s.liveSessionId });
    try {
      const saved = await savePull(s.staircase, s.code, cards, {
        capture_path: s.liveSessionId ? "live" : s.meta ? "guided" : "upload",
        pack_confidence: s.scan.pack_confidence,
        segmentation_warning: s.scan.segmentation_warning,
        capture_meta: s.meta ?? null,
        live_session_id: s.liveSessionId,
      });
      setStep({ name: "summary", verified: saved.verified, count: saved.cards.length, encounters: saved.encounters ?? [], pullId: saved.id });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  const submitBinder = async (page: Blob) => {
    setStep({ name: "submitting" });
    try {
      const scan = await scanBinder(page);
      setStep({ name: "binder_review", scan, photo: page });
    } catch (e) {
      // scanBinder rejects with `{ code: "no_cards_found" }` for a decode
      // failure or an unreadable page — route that to an empty-cards review so
      // BinderReview shows its retake state; anything else is a real error.
      if (isNoCardsFound(e)) {
        setStep({ name: "binder_review", scan: { cards: [], grid: { rows: 0, cols: 0 }, page_confidence: 0 }, photo: page });
      } else {
        setStep({ name: "error", message: friendlyError(e) });
      }
    }
  };

  const doSaveCollection = async (cards: CollectionSaveCard[]) => {
    // Auth gate mirrors doSave: BinderReview keeps the (possibly edited) cards,
    // so re-clicking Save after login saves.
    if (!trainer) {
      askLogin("Log in to save these cards to your collection.");
      return;
    }
    setStep({ name: "submitting" });
    try {
      const out = await saveToCollection(cards);
      setStep({ name: "binder_summary", out });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  // Live + binder scans run against the trainer's session server-side.
  const needsTrainer = (reason: string, next: Step) => {
    if (trainer) setStep(next);
    else askLogin(reason, () => setStep(next));
  };

  const backToModes = (
    <button type="button" className="ghost small" style={{ alignSelf: "flex-start" }} onClick={() => setStep({ name: "mode" })}>
      ◀ Modes
    </button>
  );

  return (
    <div className="shell">
      <Topbar
        view={view}
        trainer={trainer}
        loading={loading}
        canViewStats={canViewStats}
        onNavigate={navigate}
        onLogin={() => askLogin("Log in or sign up to save your pulls.")}
        onLogout={async () => {
          await logout();
          if (PROTECTED.has(view)) setView("home");
        }}
      />

      {authPrompt && !trainer && (
        <Modal
          title="Trainer login"
          onClose={() => {
            afterLogin.current = null;
            setAuthPrompt(null);
          }}
        >
          <AuthForms
            reason={authPrompt}
            onDone={() => {
              setAuthPrompt(null);
              const next = afterLogin.current;
              afterLogin.current = null;
              next?.();
            }}
          />
        </Modal>
      )}

      <main className="screen">
        {view === "home" && <Landing onStart={startScan} />}

        {view === "pulls" && trainer && <MyPulls onScan={startScan} />}

        {view === "collection" && trainer && (
          <Collection onScanBinder={() => { setView("scan"); setStep({ name: "binder_capture" }); }} />
        )}

        {view === "dashboard" && canViewStats && <Dashboard />}

        {view === "dex" && trainer && <Dex onScan={startScan} />}

        {view === "battles" && trainer && <Battles preselectPullId={battlePull} onScan={startScan} />}

        {view === "scan" && (
          <>
            {step.name === "mode" && (
              <section className="stack-lg">
                <ScreenTitle kicker="Player 1 ready" title="Select mode" sub="How do you want to scan today?" />
                <div className="mode-list">
                  <button type="button" className="mode-option" onClick={() => setStep({ name: "staircase" })}>
                    <span className="icon-box red"><PixelIcon name="camera" size={24} /></span>
                    <span>
                      <h3>One photo <span className="badge gold">Fastest</span></h3>
                      <p>Fan a whole pack in a staircase and snap it once.</p>
                    </span>
                    <span className="chev">▶</span>
                  </button>
                  <button
                    type="button"
                    className="mode-option"
                    onClick={() => needsTrainer("Log in to use live scan.", { name: "live" })}
                  >
                    <span className="icon-box cyan"><PixelIcon name="bolt" size={24} /></span>
                    <span>
                      <h3>Live {!trainer && <span className="badge outline">Login</span>}</h3>
                      <p>Hold cards up one at a time — the camera grabs each one.</p>
                    </span>
                    <span className="chev">▶</span>
                  </button>
                  <button
                    type="button"
                    className="mode-option"
                    onClick={() => needsTrainer("Log in to scan binder pages into your collection.", { name: "binder_capture" })}
                  >
                    <span className="icon-box green"><PixelIcon name="grid" size={24} /></span>
                    <span>
                      <h3>Binder page {!trainer && <span className="badge outline">Login</span>}</h3>
                      <p>Snap a full binder page straight into your collection.</p>
                    </span>
                    <span className="chev">▶</span>
                  </button>
                </div>
              </section>
            )}
            {step.name === "binder_capture" && (
              <>
                {backToModes}
                <BinderCapture onDone={(photo) => submitBinder(photo)} />
              </>
            )}
            {step.name === "binder_review" && (
              <BinderReview
                scan={step.scan}
                photo={step.photo}
                onConfirm={(cards) => doSaveCollection(cards)}
                onRetake={() => setStep({ name: "binder_capture" })}
              />
            )}
            {step.name === "binder_summary" && (
              <section className="stack-lg">
                <div className="panel celebrate">
                  <Confetti />
                  <span className="kicker">Binder page</span>
                  <h2>Saved!</h2>
                  <p className="muted">Your collection just got bigger.</p>
                </div>
                <div className="stat-tiles">
                  <StatTile value={step.out.added} label="New cards" tone="green" />
                  <StatTile value={step.out.incremented} label="Qty bumped" tone="cyan" />
                  <StatTile value={step.out.total_cards} label="In collection" />
                </div>
                {(step.out.skipped ?? 0) > 0 && (
                  // The server skipped these: a flagged cell nobody confirmed, or one
                  // with no identity to file under. Say so — the alternative is the
                  // user counting the collection and finding cards missing.
                  <div className="panel warn row">
                    <PixelIcon name="warn" size={24} className="danger-text" />
                    <p className="grow">
                      {plural(step.out.skipped ?? 0, "card")} skipped because{" "}
                      {step.out.skipped === 1 ? "it" : "they"} still needed review. Scan the page again and
                      fix (or keep) {step.out.skipped === 1 ? "it" : "them"} to add {step.out.skipped === 1 ? "it" : "them"}.
                    </p>
                  </div>
                )}
                <Encounters encounters={step.out.encounters} />
                <div className="stack">
                  <button type="button" className="primary big block" onClick={() => setStep({ name: "binder_capture" })}>
                    <PixelIcon name="grid" size={18} /> Scan another page
                  </button>
                  <button type="button" className="block" onClick={() => setView("collection")}>
                    <PixelIcon name="chest" size={18} /> View collection
                  </button>
                </div>
              </section>
            )}
            {step.name === "staircase" && (
              <>
                {backToModes}
                <StaircaseCapture onDone={(photo) => setStep({ name: "code", staircase: photo })} />
              </>
            )}
            {step.name === "live" && (
              <LiveScanScreen
                onDone={(scan, sid, composite, code) =>
                  setStep({ name: "review", scan, staircase: composite, code: code ?? composite, meta: undefined, liveSessionId: sid })
                }
                onCancel={() => { sessionStorage.removeItem(LIVE_SESSION_STORAGE_KEY); setStep({ name: "mode" }); }}
              />
            )}
            {step.name === "code" && (
              <>
                <button type="button" className="ghost small" style={{ alignSelf: "flex-start" }} onClick={() => setStep({ name: "staircase" })}>
                  ◀ Retake pack photo
                </button>
                <CodeCardCapture onDone={(codePhoto) => submit(step.staircase, codePhoto, step.meta)} />
              </>
            )}
            {step.name === "submitting" && <ScanProgress step={step} />}
            {step.name === "saving" && <LoadingScreen label="Saving your pull…" />}
            {step.name === "review" && (
              <ReviewScreen
                scan={step.scan}
                liveSessionId={step.liveSessionId}
                onRetake={() => setStep({ name: "staircase" })}
                onConfirm={(cards) => doSave(step, cards)}
              />
            )}
            {step.name === "summary" && (
              <section className="stack-lg">
                <div className="panel celebrate">
                  <Confetti />
                  <span className="kicker">Pack opened</span>
                  <h2>Pack logged!</h2>
                  <p className="muted">
                    {plural(step.count, "card")} saved to your pulls.
                  </p>
                </div>
                <div className="stat-tiles">
                  <StatTile value={step.count} label="Cards" />
                  <StatTile
                    value={step.verified ? "YES" : "NO"}
                    label="Verified"
                    tone={step.verified ? "green" : "red"}
                  />
                </div>
                {!step.verified && (
                  <p className="muted small-text">
                    Unverified — the code card was a duplicate or unreadable, so this pull can&apos;t battle or
                    count toward pull-rate stats.
                  </p>
                )}
                <Encounters encounters={step.encounters} />
                <div className="stack">
                  <button type="button" className="primary big block" onClick={() => setStep({ name: "mode" })}>
                    <PixelIcon name="camera" size={18} /> Scan another pack
                  </button>
                  {step.verified && (
                    <button type="button" className="gold block" onClick={() => { setBattlePull(step.pullId); setView("battles"); }}>
                      <PixelIcon name="swords" size={18} /> Battle this pack
                    </button>
                  )}
                  <button type="button" className="block" onClick={() => setView("pulls")}>
                    <PixelIcon name="pack" size={18} /> My pulls
                  </button>
                </div>
              </section>
            )}
            {step.name === "error" && (
              <section className="stack-lg">
                <ScreenTitle kicker="Uh oh" title="It fled!" sub="Something went wrong with that scan." />
                <ErrorBox message={step.message} />
                <button type="button" className="primary block" onClick={() => setStep({ name: "mode" })}>
                  Start over
                </button>
              </section>
            )}
          </>
        )}
      </main>

      <TabBar view={view} onNavigate={navigate} />
    </div>
  );
}

function ScanProgress({ step }: { step: Extract<Step, { name: "submitting" }> }) {
  const total = step.total ?? step.count;
  return (
    <section className="scan-progress" aria-live="polite">
      <ScreenTitle kicker="Please wait" title="Identifying…" sub={scanStageText(step)} />
      {typeof total === "number" && total > 0 ? (
        <HpBar value={step.done ?? 0} max={total} label="ID" tone="cyan" />
      ) : (
        <div className="status">
          <Loader label="Scanning" />
        </div>
      )}
      {typeof step.count === "number" && step.count > 0 && (
        <ul className="card-rows">
          {Array.from({ length: step.count }).map((_, i) => {
            const done = typeof step.done === "number" && i < step.done;
            return (
              <li key={i} className={`card-row${done ? "" : " pending"}`} style={{ opacity: done ? 1 : 0.6 }}>
                <div className="card-thumb placeholder">{done ? "✓" : "?"}</div>
                <div className="card-row-body">
                  <span className="card-name">{done ? "Identified" : "Card " + (i + 1)}</span>
                  <span className="card-meta">{done ? "Ready for review" : "Reading…"}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Encounters({ encounters }: { encounters: Encounter[] }) {
  if (encounters.length === 0) return null;
  return (
    <ul className="encounters">
      {encounters.map((e) => (
        <li key={e.species} className="textbox encounter">
          {e.new ? (
            <>
              <span className="badge gold new">NEW!</span>
              <strong>{e.species}</strong> registered to your Pokédex!
            </>
          ) : (
            <>
              You saw a wild <strong>{e.species}</strong> again! <span className="badge outline">×{e.count}</span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}
