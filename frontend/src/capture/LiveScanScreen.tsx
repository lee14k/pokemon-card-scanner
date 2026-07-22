import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  liveCardImageUrl,
  liveDuplicate,
  liveFinish,
  liveFrame,
  liveStart,
  liveState,
  type LiveCardState,
  type LiveFrameOut,
  type LiveState,
  type PackCard,
  type PackScanResponse,
  type CodeCardResult,
} from "../api";
import LiveCapture from "./LiveCapture";

// frontend/src/capture/LiveScanScreen.tsx — owns the live-scan session + tray.
// One card at a time: LiveCapture fires a (card, strip, secondBest) triple per
// hold-up, we queue it and drain the queue through liveFrame() one request at a
// time, patching an optimistic "tray" of chips as responses come back.

export const SESSION_STORAGE_KEY = "pokemon-scanner:live-session-id";
const COOLDOWN_MS = 1500; // mirrors LiveCapture's min gap between fires
const POLL_MS = 2000; // pending_vlm poll interval
const MAX_COMPOSITE_THUMBS = 100;
const COMPOSITE_CELL = 400;

type ChipState = LiveCardState | "capturing";

interface TrayChip {
  clientId: string;
  state: ChipState;
  thumbUrl: string;
  row: PackCard | null;
}

interface QueueEntry {
  clientId: string;
  card: Blob;
  strip?: Blob;
  secondBest: Blob | null;
  capturedAt: number;
  retriedSecondBest: boolean;
}

interface ArchivedFrame {
  clientId: string;
  card: Blob;
  strip?: Blob;
  capturedAt: number;
}

interface CapturedCodeCard {
  result: CodeCardResult;
  blob: Blob;
}

interface Props {
  onDone: (scan: PackScanResponse, sessionId: string, compositeBlob: Blob, codeBlob: Blob | null) => void;
  onCancel: () => void;
}

function statusOf(e: unknown): number | undefined {
  if (e instanceof ApiError) return e.status;
  if (e && typeof e === "object" && "status" in e) {
    const s = (e as { status?: unknown }).status;
    return typeof s === "number" ? s : undefined;
  }
  return undefined;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("image load failed"));
    img.src = src;
  });
}

// Draws up to MAX_COMPOSITE_THUMBS tray thumbnails into a square grid (cols =
// ceil(sqrt(n))) so a live-scanned pull still gets the same kind of single
// "staircase" photo the guided/upload paths produce.
async function buildComposite(chips: TrayChip[]): Promise<Blob> {
  const usable = chips.filter((c) => c.state !== "dup_prompt").slice(0, MAX_COMPOSITE_THUMBS);
  const n = Math.max(usable.length, 1);
  const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
  const rows = Math.max(1, Math.ceil(n / cols));
  const canvas = document.createElement("canvas");
  canvas.width = cols * COMPOSITE_CELL;
  canvas.height = rows * COMPOSITE_CELL;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas unavailable");
  ctx.fillStyle = "#111318";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  for (let i = 0; i < usable.length; i++) {
    try {
      const img = await loadImage(usable[i].thumbUrl);
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = col * COMPOSITE_CELL;
      const y = row * COMPOSITE_CELL;
      const scale = Math.min(COMPOSITE_CELL / img.width, COMPOSITE_CELL / img.height);
      const dw = img.width * scale;
      const dh = img.height * scale;
      ctx.drawImage(img, x + (COMPOSITE_CELL - dw) / 2, y + (COMPOSITE_CELL - dh) / 2, dw, dh);
    } catch {
      // one bad thumbnail shouldn't sink the whole composite — leave the cell blank.
    }
  }

  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("composite encode failed"))), "image/jpeg", 0.85);
  });
}

export default function LiveScanScreen({ onDone, onCancel }: Props) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [tray, setTray] = useState<TrayChip[]>([]);
  const [codeCard, setCodeCard] = useState<CapturedCodeCard | null>(null);
  const [autoFire, setAutoFire] = useState(true);
  const [cameraInfo, setCameraInfo] = useState<MediaTrackSettings | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [phase, setPhase] = useState<"init" | "scanning" | "code_choice" | "finishing">("init");
  const [recovering, setRecovering] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [queueLen, setQueueLen] = useState(0);

  // Refs mirror the state above for use inside long-running async chains (the
  // drain loop, session recovery, finish) that must always see the freshest
  // value regardless of which render's closure is currently executing.
  const sidRef = useRef<string | null>(null);
  sidRef.current = sessionId;
  const trayRef = useRef<TrayChip[]>([]);
  trayRef.current = tray;
  const codeCardRef = useRef<CapturedCodeCard | null>(null);
  codeCardRef.current = codeCard;

  const queueRef = useRef<QueueEntry[]>([]);
  const drainingRef = useRef(false);
  const recoveringRef = useRef(false);
  const capturedBlobsRef = useRef<ArchivedFrame[]>([]);
  const dupDecisionsRef = useRef<Map<string, boolean>>(new Map());
  const nextIdRef = useRef(0);
  const resolvedCountRef = useRef(0);
  const toastTimerRef = useRef<number | null>(null);

  function showToast(msg: string) {
    setToast(msg);
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 3500);
  }

  function dropChip(clientId: string, toastMsg: string | null) {
    setTray((prev) => {
      const found = prev.find((c) => c.clientId === clientId);
      if (found) URL.revokeObjectURL(found.thumbUrl);
      return prev.filter((c) => c.clientId !== clientId);
    });
    if (toastMsg) showToast(toastMsg);
  }

  function updateChip(clientId: string, card: PackCard, state: ChipState) {
    setTray((prev) => prev.map((c) => (c.clientId === clientId ? { ...c, state, row: card } : c)));
  }

  function announce(card: PackCard) {
    resolvedCountRef.current += 1;
    const name = card.name ?? "Card";
    let position = `card ${resolvedCountRef.current}`;
    if (card.card_number && card.card_number.includes("/")) {
      const [num, den] = card.card_number.split("/");
      position = `${num} of ${den}`;
    }
    setAnnouncement(`${name}, ${position}, added`);
  }

  function archiveAccepted(clientId: string, card: Blob, strip?: Blob) {
    capturedBlobsRef.current.push({ clientId, card, strip, capturedAt: performance.now() });
  }

  // Rebuilds the tray from authoritative server state — used both on a
  // stored-session RESUME at mount and after 404-recovery replay, so the
  // displayed tray can never silently diverge from what the server actually
  // holds. Chips still "capturing" (queued/in-flight, not yet archived) have
  // no server-side counterpart yet, so they're carried over untouched instead
  // of being dropped; every other chip's blob URL is revoked before being
  // replaced by a server-image-backed chip.
  function hydrateTrayFromServer(sid: string, st: LiveState) {
    setTray((prev) => {
      const inFlight = prev.filter((c) => c.state === "capturing");
      prev.forEach((c) => {
        if (c.state !== "capturing") URL.revokeObjectURL(c.thumbUrl);
      });
      const serverChips: TrayChip[] = st.cards.map((c) => ({
        clientId: `resumed-${c.row_index}`,
        state: c.state ?? "ok",
        thumbUrl: liveCardImageUrl(sid, c.row_index),
        row: c,
      }));
      return [...serverChips, ...inFlight];
    });
    resolvedCountRef.current = st.cards.length;
  }

  // --- mount: start (or resume) the session -------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const stored = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (stored) {
        try {
          const st = await liveState(stored);
          if (cancelled) return;
          sidRef.current = stored;
          setSessionId(stored);
          hydrateTrayFromServer(stored, st);
          setPhase("scanning");
          return;
        } catch {
          // stored session id is gone/expired — fall through and start fresh.
        }
      }
      try {
        const sid = await liveStart();
        if (cancelled) return;
        sidRef.current = sid;
        setSessionId(sid);
        sessionStorage.setItem(SESSION_STORAGE_KEY, sid);
        setPhase("scanning");
      } catch {
        if (!cancelled) showToast("Couldn't start the live session — check your connection.");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- unmount cleanup: revoke every outstanding blob URL + timer ----------
  useEffect(() => {
    return () => {
      trayRef.current.forEach((c) => URL.revokeObjectURL(c.thumbUrl));
      if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- poll while any card is pending_vlm, patching chips in place ---------
  // One persistent interval per session, NOT torn down/restarted on tray
  // mutations (auto-fire cooldown is 1500ms, shorter than POLL_MS's 2000ms,
  // so depending on `tray` here would keep resetting the interval before it
  // ever fires under continuous scanning). Pending-ness is checked from
  // trayRef inside the tick instead, and the network call is skipped
  // entirely when nothing is pending.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const id = window.setInterval(async () => {
      if (cancelled) return;
      const anyPending = trayRef.current.some((c) => c.state === "pending_vlm");
      if (!anyPending) return;
      const sid = sidRef.current;
      if (!sid) return;
      try {
        const st = await liveState(sid);
        if (cancelled) return;
        patchTrayFromState(st);
      } catch (e) {
        if (statusOf(e) === 404 && !cancelled) await recoverSession();
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  function patchTrayFromState(st: LiveState) {
    setTray((prev) =>
      prev.map((chip) => {
        if (!chip.row) return chip;
        const match = st.cards.find((c) => c.row_index === chip.row!.row_index);
        if (!match) return chip;
        return { ...chip, state: match.state ?? chip.state, row: match };
      })
    );
  }

  // Auto-dismiss the code-choice interstitial the moment a code card actually
  // lands (the camera stays live while it's up, so this can happen mid-prompt).
  useEffect(() => {
    if (phase === "code_choice" && codeCard) setPhase("scanning");
  }, [phase, codeCard]);

  // --- capture intake --------------------------------------------------------
  function onFire(card: Blob, strip: Blob, secondBest: Blob | null) {
    const clientId = `c${nextIdRef.current++}`;
    const thumbUrl = URL.createObjectURL(card);
    setTray((prev) => [...prev, { clientId, state: "capturing", thumbUrl, row: null }]);
    queueRef.current.push({ clientId, card, strip, secondBest, capturedAt: Date.now(), retriedSecondBest: false });
    setQueueLen(queueRef.current.length);
    void drain();
  }

  // Collapses a burst of near-simultaneous fires (same hold-up) down to just
  // the newest, so a jittery stability window doesn't send N near-identical
  // frames of the same card.
  function pruneStaleQueue() {
    const q = queueRef.current;
    if (q.length <= 1) return;
    const newest = q[q.length - 1];
    for (let i = q.length - 2; i >= 0; i--) {
      if (newest.capturedAt - q[i].capturedAt < COOLDOWN_MS) {
        const [dropped] = q.splice(i, 1);
        dropChip(dropped.clientId, null);
      }
    }
    setQueueLen(q.length);
  }

  // Single mutable queue drained by exactly one in-flight liveFrame at a time:
  // drainingRef makes re-entrant onFire calls no-ops while a drain is already
  // running, and every branch below (409 retry, secondBest retry, 404
  // recovery) is awaited before the while-loop advances to the next entry.
  async function drain() {
    if (drainingRef.current) return;
    drainingRef.current = true;
    try {
      while (true) {
        pruneStaleQueue();
        const entry = queueRef.current[0];
        if (!entry) break;
        queueRef.current.shift();
        setQueueLen(queueRef.current.length);
        setInFlight(true);
        try {
          await processEntry(entry);
        } finally {
          setInFlight(false);
        }
      }
    } finally {
      drainingRef.current = false;
    }
  }

  async function sendFrameWithRetry(sid: string, card: Blob, strip?: Blob): Promise<LiveFrameOut> {
    try {
      return await liveFrame(sid, card, strip);
    } catch (e) {
      if (statusOf(e) === 409) {
        await sleep(500);
        return await liveFrame(sid, card, strip); // one retry; 404/other bubbles to caller
      }
      throw e;
    }
  }

  async function processEntry(entry: QueueEntry): Promise<void> {
    const sid = sidRef.current;
    if (!sid) {
      dropChip(entry.clientId, null);
      return;
    }

    let res: LiveFrameOut;
    try {
      res = await sendFrameWithRetry(sid, entry.card, entry.strip);
    } catch (e) {
      if (statusOf(e) === 404) {
        await recoverSession(entry);
        return;
      }
      dropChip(entry.clientId, "Server was busy — that card didn't save, show it again");
      return;
    }

    switch (res.event) {
      case "card": {
        if (!res.card) {
          dropChip(entry.clientId, null);
          break;
        }
        archiveAccepted(entry.clientId, entry.card, entry.strip);
        const state: ChipState = res.pending_vlm ? "pending_vlm" : "ok";
        // A same-identity re-fire within the server's dedup window (e.g. a fast
        // manual double-tap on a card that hasn't been swapped out yet) refines
        // an already-shown row rather than creating a new one — patch that
        // chip in place instead of rendering a second, duplicate-looking chip.
        const existing = trayRef.current.find(
          (c) => c.clientId !== entry.clientId && c.row?.row_index === res.card!.row_index && c.state !== "capturing"
        );
        if (existing) {
          updateChip(existing.clientId, res.card, state);
          dropChip(entry.clientId, null);
        } else {
          updateChip(entry.clientId, res.card, state);
          announce(res.card);
        }
        break;
      }
      case "code_card": {
        archiveAccepted(entry.clientId, entry.card, entry.strip);
        if (res.code_card) setCodeCard({ result: res.code_card, blob: entry.card });
        dropChip(entry.clientId, null); // code frames don't get a tray chip
        break;
      }
      case "duplicate_prompt": {
        if (!res.card) {
          dropChip(entry.clientId, null);
          break;
        }
        archiveAccepted(entry.clientId, entry.card, entry.strip);
        updateChip(entry.clientId, res.card, "dup_prompt");
        break;
      }
      case "no_card":
      case "unreadable": {
        if (entry.secondBest && !entry.retriedSecondBest) {
          await processEntry({ ...entry, card: entry.secondBest, strip: undefined, secondBest: null, retriedSecondBest: true });
        } else {
          dropChip(entry.clientId, "Didn't catch that — show it again");
        }
        break;
      }
    }
  }

  // --- session-death recovery -------------------------------------------------
  async function recoverSession(requeueEntry?: QueueEntry) {
    if (recoveringRef.current) {
      if (requeueEntry) {
        queueRef.current.unshift(requeueEntry);
        setQueueLen(queueRef.current.length);
      }
      return;
    }
    recoveringRef.current = true;
    setRecovering(true);
    showToast("Session expired — restarting…");
    try {
      const newSid = await liveStart();
      sidRef.current = newSid;
      setSessionId(newSid);
      sessionStorage.setItem(SESSION_STORAGE_KEY, newSid);

      // Replay every previously-accepted frame against the new session, in
      // order. The backend dedups same-identity frames within a 2s window
      // (DUP_WINDOW_S) — treating a <2s-apart re-fire as a refinement of the
      // same card but a >2s-apart re-fire as a genuine second copy. Sending
      // the whole backlog back-to-back would collapse genuine duplicates
      // (originally shown seconds apart) into one card, so each send after
      // the first waits out the ORIGINAL gap between that frame and the
      // previous one, clamped to 2.5s: short gaps (accidental double-fires)
      // stay collapsed, long gaps (real re-shows) still clear the 2s window,
      // and we never stall more than ~2.5s per card.
      //
      // Duplicate prompts are auto-resolved with the decision the user
      // already made the first time, keyed by the originating chip.
      const frames = capturedBlobsRef.current;
      for (let i = 0; i < frames.length; i++) {
        const item = frames[i];
        if (i > 0) {
          const gapMs = Math.max(0, item.capturedAt - frames[i - 1].capturedAt);
          await sleep(Math.min(gapMs, 2500));
        }
        try {
          const res = await liveFrame(newSid, item.card, item.strip);
          if (res.event === "duplicate_prompt" && res.card) {
            const decision = dupDecisionsRef.current.get(item.clientId);
            if (decision !== undefined) {
              await liveDuplicate(newSid, res.card.row_index, decision);
            }
          }
        } catch (e) {
          console.warn("live-scan replay: one frame failed to replay", e);
        }
      }

      // Reconcile against server truth regardless of how the replay went —
      // this is what guarantees the tray never silently diverges even if a
      // frame still collapsed or failed to replay.
      try {
        const st = await liveState(newSid);
        hydrateTrayFromServer(newSid, st);
      } catch (e) {
        console.warn("live-scan replay: post-replay reconcile failed", e);
      }

      if (requeueEntry) {
        queueRef.current.unshift(requeueEntry);
      }
    } catch {
      showToast("Couldn't restart the session — check your connection and try again.");
    } finally {
      recoveringRef.current = false;
      setRecovering(false);
      setQueueLen(queueRef.current.length);
      void drain();
    }
  }

  // --- duplicate resolution ----------------------------------------------------
  async function resolveDuplicate(chip: TrayChip, add: boolean) {
    const sid = sidRef.current;
    if (!sid || !chip.row) return;
    dupDecisionsRef.current.set(chip.clientId, add);
    try {
      await liveDuplicate(sid, chip.row.row_index, add);
    } catch (e) {
      if (statusOf(e) === 404) await recoverSession();
      // otherwise best-effort: finish() drops unresolved dup_prompt rows anyway.
    }
    if (add) {
      setTray((prev) => prev.map((c) => (c.clientId === chip.clientId ? { ...c, state: "ok" } : c)));
    } else {
      dropChip(chip.clientId, null);
    }
  }

  // --- cancel ---------------------------------------------------------------
  // Cancelling mid-scan must discard the session client-side too, otherwise
  // sessionStorage still points at the abandoned session (TTL 30min) and
  // re-entering Live silently resumes it instead of starting fresh.
  function handleCancel() {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
    onCancel();
  }

  // --- finish -------------------------------------------------------------------
  function handleFinishClick() {
    if (phase !== "scanning") return;
    if (!codeCard) {
      setPhase("code_choice");
      return;
    }
    void doFinish();
  }

  async function doFinish() {
    const sid = sidRef.current;
    if (!sid) return;
    setPhase("finishing");
    try {
      const compositeBlob = await buildComposite(trayRef.current);
      const scan = await liveFinish(sid);
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
      trayRef.current.forEach((c) => URL.revokeObjectURL(c.thumbUrl));
      onDone(scan, sid, compositeBlob, codeCardRef.current?.blob ?? null);
    } catch (e) {
      if (statusOf(e) === 404) {
        await recoverSession();
        const newSid = sidRef.current;
        if (newSid) {
          try {
            const compositeBlob = await buildComposite(trayRef.current);
            const scan = await liveFinish(newSid);
            sessionStorage.removeItem(SESSION_STORAGE_KEY);
            trayRef.current.forEach((c) => URL.revokeObjectURL(c.thumbUrl));
            onDone(scan, newSid, compositeBlob, codeCardRef.current?.blob ?? null);
            return;
          } catch {
            // fall through to the error toast below
          }
        }
      }
      showToast("Couldn't finish the pack — try again.");
      setPhase("scanning");
    }
  }

  const busy = recovering || tray.some((c) => c.state === "capturing");
  const paused = recovering || phase === "finishing" || (inFlight && queueLen > 0);
  const cardCount = tray.filter((c) => c.state !== "dup_prompt").length;

  return (
    <section className="live-scan">
      <h2>Live scan</h2>
      <p className="sr-only" aria-live="polite">{announcement}</p>
      {toast && <p className="live-toast" role="status">{toast}</p>}

      {phase === "init" && <p className="status">Starting camera session…</p>}

      {phase !== "init" && sessionId && (
        <>
          <LiveCapture onFire={onFire} paused={paused} autoFire={autoFire} onCameraInfo={setCameraInfo} />

          {cameraInfo && (
            <p className="camera-hint">
              {cameraInfo.width ?? "?"}×{cameraInfo.height ?? "?"}
              {Math.min(cameraInfo.width ?? 0, cameraInfo.height ?? 0) > 0 &&
                Math.min(cameraInfo.width ?? 0, cameraInfo.height ?? 0) < 1080 &&
                " — fill the guide with the card for a sharper read"}
            </p>
          )}

          <div className="live-toolbar">
            <label>
              <input
                type="checkbox"
                checked={autoFire}
                onChange={(e) => setAutoFire(e.target.checked)}
              />{" "}
              Auto-fire
            </label>
            <span className="live-count">
              {cardCount} card{cardCount === 1 ? "" : "s"} · {codeCard ? "code ✓" : "no code yet"}
            </span>
          </div>

          {phase === "code_choice" && (
            <div className="warn-banner code-choice">
              <p>Scan code card (needed to battle &amp; count in stats)</p>
              <div className="code-choice-actions">
                <button type="button" className="primary" onClick={() => setPhase("scanning")}>
                  Keep scanning
                </button>
                <button type="button" onClick={() => void doFinish()}>
                  Save anyway — this pull can&apos;t battle
                </button>
              </div>
            </div>
          )}

          <ul className="card-rows live-tray">
            {tray.map((chip) => (
              <ChipRow
                key={chip.clientId}
                chip={chip}
                onAdd={() => void resolveDuplicate(chip, true)}
                onIgnore={() => void resolveDuplicate(chip, false)}
              />
            ))}
          </ul>

          <div className="camera-actions">
            <button
              type="button"
              className="primary"
              onClick={handleFinishClick}
              disabled={phase !== "scanning" || busy}
            >
              Finish scanning
            </button>
            <button type="button" onClick={handleCancel}>Cancel</button>
          </div>
        </>
      )}

      {phase === "finishing" && <p className="status">Building your pull…</p>}
    </section>
  );
}

function ChipRow({ chip, onAdd, onIgnore }: { chip: TrayChip; onAdd: () => void; onIgnore: () => void }) {
  const row = chip.row;
  const isDup = chip.state === "dup_prompt";
  return (
    <li className={`card-row live-chip${isDup ? " flagged" : ""}`}>
      <img src={chip.thumbUrl} alt={row?.name ?? "captured card"} className="card-thumb" />
      <div className="card-row-body">
        {chip.state === "capturing" ? (
          <span className="live-chip-status">
            <span className="spinner" /> Reading…
          </span>
        ) : (
          <>
            <strong>{row?.name ?? (chip.state === "pending_vlm" ? "Identifying…" : "Unknown card")}</strong>
            <span>
              {row?.card_number ?? "—"} · {row?.set_name ?? "Unknown set"}
              {chip.state === "pending_vlm" && " · ❓"}
              {chip.state === "vlm_failed" && " · needs review"}
              {row?.price_usd_low != null && ` · $${row.price_usd_low.toFixed(2)}`}
            </span>
          </>
        )}
        {isDup && row && (
          <div className="card-row-flag">
            <em>Another copy of {row.name ?? "this card"}?</em>
            <button type="button" onClick={onAdd}>Add</button>
            <button type="button" onClick={onIgnore}>Ignore</button>
          </div>
        )}
      </div>
    </li>
  );
}
