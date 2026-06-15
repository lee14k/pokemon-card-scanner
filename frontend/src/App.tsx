import { useState } from "react";
import "./App.css";
import {
  scanPack,
  type CaptureMeta,
  type PackCard,
  type PackScanResponse,
} from "./api";
import StaircaseCapture from "./capture/StaircaseCapture";
import CodeCardCapture from "./capture/CodeCardCapture";
import ReviewScreen from "./review/ReviewScreen";

type Step =
  | { name: "staircase" }
  | { name: "code"; staircase: Blob; meta?: CaptureMeta }
  | { name: "submitting" }
  | { name: "review"; scan: PackScanResponse }
  | { name: "summary"; cards: PackCard[]; code: string | null }
  | { name: "error"; message: string };

export default function App() {
  const [step, setStep] = useState<Step>({ name: "staircase" });
  const [pending, setPending] = useState<{ staircase: Blob; meta?: CaptureMeta } | null>(null);

  const submit = async (staircase: Blob, codeCard: Blob, meta?: CaptureMeta) => {
    setStep({ name: "submitting" });
    try {
      const scan = await scanPack(staircase, codeCard, meta);
      setStep({ name: "review", scan });
    } catch (e) {
      setStep({ name: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <main className="app">
      <h1>Pack Scanner</h1>
      {step.name === "staircase" && (
        <StaircaseCapture
          onDone={(photo, meta) => {
            setPending({ staircase: photo, meta });
            setStep({ name: "code", staircase: photo, meta });
          }}
        />
      )}
      {step.name === "code" && (
        <CodeCardCapture
          onDone={(codePhoto) => submit(step.staircase, codePhoto, step.meta)}
        />
      )}
      {step.name === "submitting" && <p className="status">Reading cards…</p>}
      {step.name === "review" && (
        <ReviewScreen
          scan={step.scan}
          onRetake={() => setStep({ name: "staircase" })}
          onConfirm={(cards) =>
            setStep({ name: "summary", cards, code: step.scan.code_card.code })
          }
        />
      )}
      {step.name === "summary" && (
        <section>
          <h2>Pack logged</h2>
          <p>
            {step.cards.length} cards confirmed
            {step.code ? ` · code ${step.code}` : ""}.
          </p>
          {/* Sub-project B: persistence + pull-stats submission happens here. */}
          <button
            type="button"
            className="primary"
            onClick={() => {
              setPending(null);
              setStep({ name: "staircase" });
            }}
          >
            Scan another pack
          </button>
        </section>
      )}
      {step.name === "error" && (
        <section>
          <p className="camera-error">Scan failed: {step.message}</p>
          <button
            type="button"
            onClick={() =>
              pending
                ? setStep({ name: "code", staircase: pending.staircase, meta: pending.meta })
                : setStep({ name: "staircase" })
            }
          >
            Try again
          </button>
        </section>
      )}
    </main>
  );
}
