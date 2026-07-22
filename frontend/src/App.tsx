import { useState } from "react";
import "./App.css";
import {
  savePull,
  scanPack,
  scanPackStream,
  type CaptureMeta,
  type Encounter,
  type PackCard,
  type PackScanResponse,
  type ScanProgressEvent,
} from "./api";
import StaircaseCapture from "./capture/StaircaseCapture";
import CodeCardCapture from "./capture/CodeCardCapture";
import LiveScanScreen, { SESSION_STORAGE_KEY as LIVE_SESSION_STORAGE_KEY } from "./capture/LiveScanScreen";
import ReviewScreen from "./review/ReviewScreen";
import { useAuth } from "./auth/AuthContext";
import AuthForms from "./auth/AuthForms";
import MyPulls from "./pulls/MyPulls";
import Dashboard from "./dashboard/Dashboard";
import Dex from "./dex/Dex";
import Battles from "./battles/Battles";
import Landing from "./landing/Landing";

type Step =
  | { name: "mode" }
  | { name: "staircase" }
  | { name: "live" }
  | { name: "code"; staircase: Blob; meta?: CaptureMeta }
  | { name: "submitting"; stage?: string; count?: number; done?: number; total?: number }
  | { name: "review"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; liveSessionId?: string }
  | { name: "saving"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; cards: PackCard[]; liveSessionId?: string }
  | { name: "summary"; verified: boolean; count: number; encounters: Encounter[]; pullId: string }
  | { name: "error"; message: string };

function submittingStageText(step: Extract<Step, { name: "submitting" }>): string {
  switch (step.stage) {
    case "decoded":
      return "Reading photo…";
    case "cards_found":
      return `Found ${step.count ?? "…"} cards — identifying…`;
    case "identifying":
      return `Identifying cards… ${step.done ?? 0}/${step.total ?? step.count ?? "?"}`;
    case "done":
      return "Finishing up…";
    default:
      return "Reading cards…";
  }
}

// Feature-detect fetch+ReadableStream support (unavailable in some older/embedded
// webviews) — those fall straight through to the non-streaming scanPack().
function supportsScanStream(): boolean {
  return typeof window !== "undefined" && "ReadableStream" in window
    && typeof window.ReadableStream === "function";
}

export default function App() {
  const { trainer, loading, logout } = useAuth();
  const [step, setStep] = useState<Step>({ name: "mode" });
  const [view, setView] = useState<"home" | "scan" | "pulls" | "dashboard" | "dex" | "battles">("home");
  const [battlePull, setBattlePull] = useState<string | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const canViewStats = trainer?.role === "analyst" || trainer?.role === "admin";

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
      setStep({ name: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  const doSave = async (s: Extract<Step, { name: "review" }>, cards: PackCard[]) => {
    if (!trainer) {
      setAuthOpen(true);
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
      setStep({ name: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <main className="app">
      <header className="app-header">
        <h1>
          <button type="button" className="home-link" onClick={() => setView("home")}>
            Pack Scanner
          </button>
        </h1>
        <nav>
          <button type="button" onClick={() => { setStep({ name: "mode" }); setView("scan"); }}>Scan</button>
          <button type="button" onClick={() => setView("pulls")} disabled={!trainer}>My Pulls</button>
          <button type="button" onClick={() => setView("dex")} disabled={!trainer}>Pokédex</button>
          <button type="button" onClick={() => setView("battles")} disabled={!trainer}>Battles</button>
          {canViewStats && (
            <button type="button" onClick={() => setView("dashboard")}>Dashboard</button>
          )}
          {!loading && (trainer
            ? <button type="button" onClick={logout}>@{trainer.handle} · log out</button>
            : <button type="button" onClick={() => setAuthOpen(true)}>Log in</button>)}
        </nav>
      </header>

      {authOpen && !trainer && (
        <div className="auth-modal">
          <AuthForms onDone={() => setAuthOpen(false)} />
          <button type="button" onClick={() => setAuthOpen(false)}>Cancel</button>
        </div>
      )}

      {view === "home" && (
        <Landing onStart={() => { setStep({ name: "mode" }); setView("scan"); }} />
      )}

      {view === "pulls" && trainer && <MyPulls />}

      {view === "dashboard" && canViewStats && <Dashboard />}

      {view === "dex" && trainer && <Dex />}

      {view === "battles" && trainer && <Battles preselectPullId={battlePull} />}

      {view === "scan" && (
        <>
          {step.name === "mode" && (
            <section className="mode-choice">
              <h2>How do you want to scan?</h2>
              <button type="button" className="primary" onClick={() => setStep({ name: "staircase" })}>
                One photo
              </button>
              <button type="button" onClick={() => setStep({ name: "live" })}>
                Live
              </button>
            </section>
          )}
          {step.name === "staircase" && (
            <StaircaseCapture onDone={(photo) => setStep({ name: "code", staircase: photo })} />
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
            <CodeCardCapture onDone={(codePhoto) => submit(step.staircase, codePhoto, step.meta)} />
          )}
          {step.name === "submitting" && (
            <section className="status">
              <span className="spinner" />
              <p>{submittingStageText(step)}</p>
              {typeof step.count === "number" && step.count > 0 && (
                <ul className="card-rows">
                  {Array.from({ length: step.count }).map((_, i) => (
                    <li
                      key={i}
                      className={`card-row skeleton-row${
                        typeof step.done === "number" && i < step.done ? " skeleton-done" : ""
                      }`}
                    >
                      <div className="card-thumb placeholder skeleton-block" />
                      <div className="card-row-body">
                        <div className="skeleton-line skeleton-line-wide" />
                        <div className="skeleton-line" />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
          {step.name === "saving" && <p className="status">Saving your pull…</p>}
          {step.name === "review" && (
            <ReviewScreen
              scan={step.scan}
              liveSessionId={step.liveSessionId}
              onRetake={() => setStep({ name: "staircase" })}
              onConfirm={(cards) => doSave(step, cards)}
            />
          )}
          {step.name === "summary" && (
            <section>
              <h2>Pack logged</h2>
              <p>{step.count} cards saved · {step.verified ? "verified ✓" : "unverified (duplicate or unreadable code)"}.</p>
              {step.encounters.length > 0 && (
                <ul className="card-rows">
                  {step.encounters.map((e) => (
                    <li key={e.species} className="card-row">
                      <div className="card-row-body">
                        {e.new
                          ? <strong>✨ NEW! {e.species} registered to your Pokédex!</strong>
                          : <span>You saw a wild {e.species} again (×{e.count})</span>}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <button type="button" className="primary" onClick={() => setStep({ name: "mode" })}>
                Scan another pack
              </button>
              {step.verified && (
                <button type="button" onClick={() => { setBattlePull(step.pullId); setView("battles"); }}>
                  ⚔️ Battle this pack
                </button>
              )}
            </section>
          )}
          {step.name === "error" && (
            <section>
              <p className="camera-error">Something went wrong: {step.message}</p>
              <button type="button" onClick={() => setStep({ name: "mode" })}>Start over</button>
            </section>
          )}
        </>
      )}
    </main>
  );
}
