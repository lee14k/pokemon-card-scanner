import { useState } from "react";
import "./App.css";
import {
  savePull,
  scanPack,
  type CaptureMeta,
  type Encounter,
  type PackCard,
  type PackScanResponse,
} from "./api";
import StaircaseCapture from "./capture/StaircaseCapture";
import CodeCardCapture from "./capture/CodeCardCapture";
import ReviewScreen from "./review/ReviewScreen";
import { useAuth } from "./auth/AuthContext";
import AuthForms from "./auth/AuthForms";
import MyPulls from "./pulls/MyPulls";
import Dashboard from "./dashboard/Dashboard";
import Dex from "./dex/Dex";
import Battles from "./battles/Battles";
import Landing from "./landing/Landing";

type Step =
  | { name: "staircase" }
  | { name: "code"; staircase: Blob; meta?: CaptureMeta }
  | { name: "submitting" }
  | { name: "review"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta }
  | { name: "saving"; scan: PackScanResponse; staircase: Blob; code: Blob; meta?: CaptureMeta; cards: PackCard[] }
  | { name: "summary"; verified: boolean; count: number; encounters: Encounter[]; pullId: string }
  | { name: "error"; message: string };

export default function App() {
  const { trainer, loading, logout } = useAuth();
  const [step, setStep] = useState<Step>({ name: "staircase" });
  const [view, setView] = useState<"home" | "scan" | "pulls" | "dashboard" | "dex" | "battles">("home");
  const [battlePull, setBattlePull] = useState<string | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const canViewStats = trainer?.role === "analyst" || trainer?.role === "admin";

  const submit = async (staircase: Blob, code: Blob, meta?: CaptureMeta) => {
    setStep({ name: "submitting" });
    try {
      const scan = await scanPack(staircase, code, meta);
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
    setStep({ name: "saving", scan: s.scan, staircase: s.staircase, code: s.code, meta: s.meta, cards });
    try {
      const saved = await savePull(s.staircase, s.code, cards, {
        capture_path: s.meta ? "guided" : "upload",
        pack_confidence: s.scan.pack_confidence,
        segmentation_warning: s.scan.segmentation_warning,
        capture_meta: s.meta ?? null,
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
          <button type="button" onClick={() => setView("scan")}>Scan</button>
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
        <Landing onStart={() => { setStep({ name: "staircase" }); setView("scan"); }} />
      )}

      {view === "pulls" && trainer && <MyPulls />}

      {view === "dashboard" && canViewStats && <Dashboard />}

      {view === "dex" && trainer && <Dex />}

      {view === "battles" && trainer && <Battles preselectPullId={battlePull} />}

      {view === "scan" && (
        <>
          {step.name === "staircase" && (
            <StaircaseCapture onDone={(photo, meta) => setStep({ name: "code", staircase: photo, meta })} />
          )}
          {step.name === "code" && (
            <CodeCardCapture onDone={(codePhoto) => submit(step.staircase, codePhoto, step.meta)} />
          )}
          {step.name === "submitting" && <p className="status">Reading cards…</p>}
          {step.name === "saving" && <p className="status">Saving your pull…</p>}
          {step.name === "review" && (
            <ReviewScreen
              scan={step.scan}
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
              <button type="button" className="primary" onClick={() => setStep({ name: "staircase" })}>
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
              <button type="button" onClick={() => setStep({ name: "staircase" })}>Start over</button>
            </section>
          )}
        </>
      )}
    </main>
  );
}
