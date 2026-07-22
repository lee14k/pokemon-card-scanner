import { useEffect, useRef, useState } from "react";

// frontend/src/capture/LiveCapture.tsx — live camera with auto-fire.
// Key numbers (from the design spec):
const METRICS_W = 160; // motion canvas width
const STRIP_SHARP_W = 500; // sharpness measured on the number-strip region
const STABLE_MS = 300; // steady+sharp duration before firing
const COOLDOWN_MS = 1500; // min gap between fires
const REARM_MOTION = 14; // mean-abs-diff (0-255) that re-arms after a fire
const FIRE_MOTION = 6; // below this = "stable"
const GUIDE = { x: 0.14, y: 0.08, w: 0.72, h: 0.8 }; // card fills ~70-80% of height
const STRIP_FRAC = 0.16; // bottom fraction of guide = number strip
const TARGET_CARD_H = 1400; // upload card crop height cap

// Presence/edge heuristics — not part of the numbered spec constants, tuned locally.
const PRESENCE_THRESHOLD = 0.05;
const EDGE_THRESHOLD = 24;
const FLASH_MS = 220;

const HQ_CONSTRAINTS: MediaTrackConstraints = {
  facingMode: "environment",
  width: { ideal: 3840 },
  height: { ideal: 2160 },
};
const LQ_CONSTRAINTS: MediaTrackConstraints = {
  facingMode: "environment",
  width: { ideal: 1920 },
  height: { ideal: 1080 },
};

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Geometry {
  vw: number;
  vh: number;
  metricsW: number;
  metricsH: number;
  guideMetrics: Rect;
  guideNative: Rect;
  stripNative: Rect;
}

type Phase = "searching" | "locking" | "fired";

interface CandidateEntry {
  blob: Blob;
  sharpness: number;
}

interface LiveCaptureProps {
  onFire: (card: Blob, strip: Blob, secondBest: Blob | null) => void; // called per capture event
  paused: boolean; // parent pauses firing while a POST is in flight + queue full
  autoFire: boolean; // toggle owned by parent
  onCameraInfo?: (settings: MediaTrackSettings) => void;
}

// --- pure helpers (module scope — no component state) ---

function computeGeometry(vw: number, vh: number): Geometry {
  const metricsW = METRICS_W;
  const metricsH = Math.max(1, Math.round(METRICS_W * (vh / vw)));
  const guideNative: Rect = {
    x: GUIDE.x * vw,
    y: GUIDE.y * vh,
    w: GUIDE.w * vw,
    h: GUIDE.h * vh,
  };
  const guideMetrics: Rect = {
    x: GUIDE.x * metricsW,
    y: GUIDE.y * metricsH,
    w: GUIDE.w * metricsW,
    h: GUIDE.h * metricsH,
  };
  const stripNative: Rect = {
    x: guideNative.x,
    y: guideNative.y + guideNative.h * (1 - STRIP_FRAC),
    w: guideNative.w,
    h: guideNative.h * STRIP_FRAC,
  };
  return { vw, vh, metricsW, metricsH, guideMetrics, guideNative, stripNative };
}

function toGray(data: Uint8ClampedArray, w: number, h: number): Uint8ClampedArray {
  const out = new Uint8ClampedArray(w * h);
  for (let i = 0, p = 0; i < out.length; i++, p += 4) {
    out[i] = (data[p] * 0.299 + data[p + 1] * 0.587 + data[p + 2] * 0.114) | 0;
  }
  return out;
}

// Fraction of simple neighbor-diff "edge" pixels inside `rect` exceeds PRESENCE_THRESHOLD.
function hasCardPresence(gray: Uint8ClampedArray, w: number, h: number, rect: Rect): boolean {
  const x0 = Math.max(1, Math.round(rect.x));
  const y0 = Math.max(1, Math.round(rect.y));
  const x1 = Math.min(w - 2, Math.round(rect.x + rect.w));
  const y1 = Math.min(h - 2, Math.round(rect.y + rect.h));
  if (x1 <= x0 || y1 <= y0) return false;

  let edgeCount = 0;
  let total = 0;
  for (let y = y0; y <= y1; y++) {
    const row = y * w;
    for (let x = x0; x <= x1; x++) {
      const idx = row + x;
      const dx = Math.abs(gray[idx + 1] - gray[idx - 1]);
      const dy = Math.abs(gray[idx + w] - gray[idx - w]);
      if (dx + dy > EDGE_THRESHOLD) edgeCount++;
      total++;
    }
  }
  return total > 0 && edgeCount / total > PRESENCE_THRESHOLD;
}

// Variance of a 3x3 (cross-shaped) Laplacian — standard "blur metric".
function laplacianVariance(gray: Uint8ClampedArray, w: number, h: number): number {
  if (w < 3 || h < 3) return 0;
  const n = (w - 2) * (h - 2);
  if (n <= 0) return 0;
  const lap = new Float32Array(n);
  let sum = 0;
  let i = 0;
  for (let y = 1; y < h - 1; y++) {
    const row = y * w;
    for (let x = 1; x < w - 1; x++) {
      const idx = row + x;
      const v = -4 * gray[idx] + gray[idx - 1] + gray[idx + 1] + gray[idx - w] + gray[idx + w];
      lap[i] = v;
      sum += v;
      i++;
    }
  }
  const mean = sum / n;
  let variance = 0;
  for (let j = 0; j < n; j++) {
    const d = lap[j] - mean;
    variance += d * d;
  }
  return variance / n;
}

export default function LiveCapture({ onFire, paused, autoFire, onCameraInfo }: LiveCaptureProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [interrupted, setInterrupted] = useState(false);

  // Two persistent, ref-scoped canvases (never allocated per-frame). metricsCanvas is
  // reused as scratch space for both the low-res motion/presence pass (METRICS_W wide)
  // and the higher-res strip-sharpness pass (STRIP_SHARP_W wide) — its dimensions are
  // resized in place each tick rather than allocating a third canvas.
  const metricsCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const metricsCtxRef = useRef<CanvasRenderingContext2D | null>(null);
  const captureCanvasRef = useRef<HTMLCanvasElement | null>(null);
  if (metricsCanvasRef.current === null) {
    const c = document.createElement("canvas");
    c.width = METRICS_W;
    c.height = METRICS_W;
    metricsCanvasRef.current = c;
    metricsCtxRef.current = c.getContext("2d", { willReadFrequently: true });
  }
  if (captureCanvasRef.current === null) {
    captureCanvasRef.current = document.createElement("canvas");
  }

  // Stream / lifecycle refs.
  const streamRef = useRef<MediaStream | null>(null);
  const trackRef = useRef<MediaStreamTrack | null>(null);
  const wakeLockRef = useRef<WakeLockSentinel | null>(null);
  const mountedRef = useRef(true);
  const interruptedRef = useRef(false);

  // Loop refs.
  const rvfcIdRef = useRef<number | null>(null);
  const rafIdRef = useRef<number | null>(null);
  const loopModeRef = useRef<"rvfc" | "raf" | null>(null);
  const geometryRef = useRef<Geometry | null>(null);
  const prevGrayRef = useRef<Uint8ClampedArray | null>(null);
  const phaseRef = useRef<Phase>("searching");

  // Stability / fire-gating refs.
  const armedRef = useRef(true);
  const stableSinceRef = useRef<number | null>(null);
  const windowIdRef = useRef(0);
  const ringRef = useRef<CandidateEntry[]>([]);
  const lastFireAtRef = useRef<number>(-Infinity);
  const flashUntilRef = useRef(0);

  // Latest-props refs — the loop is wired up once (mount effect) and must never read
  // stale closures for values the parent can change on every render.
  const pausedRef = useRef(paused);
  const autoFireRef = useRef(autoFire);
  const onFireRef = useRef(onFire);
  const onCameraInfoRef = useRef(onCameraInfo);
  pausedRef.current = paused;
  autoFireRef.current = autoFire;
  onFireRef.current = onFire;
  onCameraInfoRef.current = onCameraInfo;

  function paintOverlay(phase: Phase) {
    const canvas = overlayRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (w === 0 || h === 0) return;

    const gx = GUIDE.x * w;
    const gy = GUIDE.y * h;
    const gw = GUIDE.w * w;
    const gh = GUIDE.h * h;

    const tint =
      phase === "fired"
        ? "rgba(255, 255, 255, 0.5)"
        : phase === "locking"
          ? "rgba(52, 211, 153, 0.18)"
          : "rgba(59, 130, 246, 0.10)";
    ctx.fillStyle = tint;
    ctx.fillRect(gx, gy, gw, gh);

    ctx.strokeStyle =
      phase === "fired"
        ? "rgba(255, 255, 255, 0.95)"
        : phase === "locking"
          ? "rgba(52, 211, 153, 0.95)"
          : "rgba(59, 130, 246, 0.8)";
    ctx.lineWidth = 3;
    ctx.strokeRect(gx, gy, gw, gh);

    const stripY = gy + gh * (1 - STRIP_FRAC);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.45)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(gx, stripY);
    ctx.lineTo(gx + gw, stripY);
    ctx.stroke();
  }

  function layoutOverlay() {
    const canvas = overlayRef.current;
    const video = videoRef.current;
    if (!canvas || !video) return;
    canvas.width = video.clientWidth || video.videoWidth || 1;
    canvas.height = video.clientHeight || video.videoHeight || 1;
    paintOverlay(phaseRef.current);
  }

  function refreshGeometry() {
    const video = videoRef.current;
    if (!video || !video.videoWidth || !video.videoHeight) return;
    geometryRef.current = computeGeometry(video.videoWidth, video.videoHeight);
    prevGrayRef.current = null;
    stableSinceRef.current = null;
    windowIdRef.current += 1;
    ringRef.current = [];
    layoutOverlay();
  }

  function stopLoop() {
    const video = videoRef.current;
    if (loopModeRef.current === "rvfc" && video && rvfcIdRef.current !== null) {
      video.cancelVideoFrameCallback(rvfcIdRef.current);
    }
    if (loopModeRef.current === "raf" && rafIdRef.current !== null) {
      cancelAnimationFrame(rafIdRef.current);
    }
    rvfcIdRef.current = null;
    rafIdRef.current = null;
    loopModeRef.current = null;
  }

  function startLoop() {
    stopLoop();
    const video = videoRef.current;
    if (!video) return;

    if (typeof video.requestVideoFrameCallback === "function") {
      loopModeRef.current = "rvfc";
      const step: VideoFrameRequestCallback = (now) => {
        doTick(now);
        rvfcIdRef.current = video.requestVideoFrameCallback(step);
      };
      rvfcIdRef.current = video.requestVideoFrameCallback(step);
    } else {
      loopModeRef.current = "raf";
      let lastTick = 0;
      const minInterval = 1000 / 12;
      const step = (now: number) => {
        if (now - lastTick >= minInterval) {
          lastTick = now;
          doTick(now);
        }
        rafIdRef.current = requestAnimationFrame(step);
      };
      rafIdRef.current = requestAnimationFrame(step);
    }
  }

  // Draws only the strip band (guide bottom STRIP_FRAC) into the shared metrics
  // canvas at STRIP_SHARP_W wide, then measures Laplacian variance on it.
  function measureStripSharpness(
    video: HTMLVideoElement,
    ctx: CanvasRenderingContext2D,
    canvas: HTMLCanvasElement,
    geometry: Geometry
  ): number {
    const { x, y, w, h } = geometry.stripNative;
    const outW = STRIP_SHARP_W;
    const outH = Math.max(1, Math.round(STRIP_SHARP_W * (h / w)));
    canvas.width = outW;
    canvas.height = outH;
    ctx.drawImage(video, x, y, w, h, 0, 0, outW, outH);
    const data = ctx.getImageData(0, 0, outW, outH);
    const gray = toGray(data.data, outW, outH);
    return laplacianVariance(gray, outW, outH);
  }

  // Captures a candidate card crop during a stable window and keeps the top-2 by
  // sharpness (ring of at most 2). Guarded by windowId so a slow toBlob() resolving
  // after the window has ended/fired can't pollute a later window.
  function captureCandidate(sharpness: number, windowId: number) {
    const video = videoRef.current;
    const geometry = geometryRef.current;
    if (!video || !geometry) return;

    const ring = ringRef.current;
    if (ring.length >= 2 && sharpness <= ring[ring.length - 1].sharpness) return;

    const { x, y, w, h } = geometry.guideNative;
    const scale = h > TARGET_CARD_H ? TARGET_CARD_H / h : 1;
    const outW = Math.max(1, Math.round(w * scale));
    const outH = Math.max(1, Math.round(h * scale));
    const canvas = document.createElement("canvas");
    canvas.width = outW;
    canvas.height = outH;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, x, y, w, h, 0, 0, outW, outH);
    canvas.toBlob(
      (blob) => {
        if (!blob) return;
        if (windowIdRef.current !== windowId) return; // stale — window moved on
        const current = ringRef.current;
        current.push({ blob, sharpness });
        current.sort((a, b) => b.sharpness - a.sharpness);
        ringRef.current = current.slice(0, 2);
      },
      "image/jpeg",
      0.8
    );
  }

  // Fires from the CURRENT frame: draws it once to captureCanvas (native res) so both
  // crops come from the same frame, then crops card (scaled) + strip (native res).
  function fire() {
    const video = videoRef.current;
    const geometry = geometryRef.current;
    const captureCanvas = captureCanvasRef.current;
    if (!video || !geometry || !captureCanvas) return;
    const ctx = captureCanvas.getContext("2d");
    if (!ctx) return;

    captureCanvas.width = geometry.vw;
    captureCanvas.height = geometry.vh;
    ctx.drawImage(video, 0, 0, geometry.vw, geometry.vh);

    const { x: gx, y: gy, w: gw, h: gh } = geometry.guideNative;
    const scale = gh > TARGET_CARD_H ? TARGET_CARD_H / gh : 1;
    const cardW = Math.max(1, Math.round(gw * scale));
    const cardH = Math.max(1, Math.round(gh * scale));
    const cardCanvas = document.createElement("canvas");
    cardCanvas.width = cardW;
    cardCanvas.height = cardH;
    const cardCtx = cardCanvas.getContext("2d");

    const strip = geometry.stripNative;
    const stripW = Math.max(1, Math.round(strip.w));
    const stripH = Math.max(1, Math.round(strip.h));
    const stripCanvas = document.createElement("canvas");
    stripCanvas.width = stripW;
    stripCanvas.height = stripH;
    const stripCtx = stripCanvas.getContext("2d");

    if (!cardCtx || !stripCtx) return;

    cardCtx.drawImage(captureCanvas, gx, gy, gw, gh, 0, 0, cardW, cardH);
    stripCtx.drawImage(captureCanvas, strip.x, strip.y, strip.w, strip.h, 0, 0, stripW, stripH);

    const secondBest = ringRef.current[0]?.blob ?? null;

    cardCanvas.toBlob(
      (cardBlob) => {
        if (!cardBlob) return;
        stripCanvas.toBlob(
          (stripBlob) => {
            if (!stripBlob) return;
            onFireRef.current(cardBlob, stripBlob, secondBest);
          },
          "image/jpeg",
          0.8
        );
      },
      "image/jpeg",
      0.8
    );

    const firedAt = performance.now();
    lastFireAtRef.current = firedAt;
    armedRef.current = false; // require motion > REARM_MOTION before the next window
    stableSinceRef.current = null;
    windowIdRef.current += 1;
    ringRef.current = [];
    flashUntilRef.current = firedAt + FLASH_MS;
    navigator.vibrate?.(30);
  }

  function manualFire() {
    if (pausedRef.current || interruptedRef.current) return;
    fire();
  }

  function doTick(now: number) {
    if (interruptedRef.current) return;
    const video = videoRef.current;
    const geometry = geometryRef.current;
    const metricsCanvas = metricsCanvasRef.current;
    const mctx = metricsCtxRef.current;
    if (!video || !geometry || !metricsCanvas || !mctx) return;
    if (video.readyState < 2) return;

    // (a) + (b): low-res full-frame draw → motion vs previous frame, card presence.
    metricsCanvas.width = geometry.metricsW;
    metricsCanvas.height = geometry.metricsH;
    mctx.drawImage(video, 0, 0, geometry.metricsW, geometry.metricsH);
    const frame = mctx.getImageData(0, 0, geometry.metricsW, geometry.metricsH);
    const gray = toGray(frame.data, geometry.metricsW, geometry.metricsH);

    let motion = 0;
    const prev = prevGrayRef.current;
    if (prev && prev.length === gray.length) {
      let sum = 0;
      for (let i = 0; i < gray.length; i++) sum += Math.abs(gray[i] - prev[i]);
      motion = sum / gray.length;
    }
    prevGrayRef.current = gray;

    if (motion > REARM_MOTION) armedRef.current = true;

    const presence = hasCardPresence(gray, geometry.metricsW, geometry.metricsH, geometry.guideMetrics);

    let phase: Phase = "searching";

    if (armedRef.current && motion < FIRE_MOTION && presence) {
      // (c): strip sharpness, only computed when stable+present+armed.
      const sharpness = measureStripSharpness(video, mctx, metricsCanvas, geometry);

      if (stableSinceRef.current === null) {
        stableSinceRef.current = now;
        windowIdRef.current += 1;
        ringRef.current = [];
      }
      phase = "locking";

      const elapsed = now - stableSinceRef.current;
      if (elapsed >= STABLE_MS) {
        const cooldownOk = now - lastFireAtRef.current >= COOLDOWN_MS;
        if (cooldownOk && !pausedRef.current && autoFireRef.current) {
          fire();
        }
      } else {
        captureCandidate(sharpness, windowIdRef.current);
      }
    } else {
      stableSinceRef.current = null;
    }

    if (now < flashUntilRef.current) phase = "fired";
    phaseRef.current = phase;
    paintOverlay(phase);
  }

  function handleLoadedMetadata() {
    refreshGeometry();
    startLoop();
  }

  function handleTrackInterrupt() {
    interruptedRef.current = true;
    setInterrupted(true);
    stopLoop();
  }

  function handleVisibilityChange() {
    if (document.visibilityState === "hidden") {
      interruptedRef.current = true;
      setInterrupted(true);
      stopLoop();
    } else if (document.visibilityState === "visible") {
      requestWakeLock();
    }
  }

  function handleResize() {
    refreshGeometry();
  }

  async function requestWakeLock() {
    try {
      const sentinel = await navigator.wakeLock?.request("screen");
      wakeLockRef.current = sentinel ?? null;
    } catch {
      wakeLockRef.current = null; // non-fatal — no screen-lock guarantee on this device
    }
  }

  function releaseWakeLock() {
    wakeLockRef.current?.release().catch(() => {});
    wakeLockRef.current = null;
  }

  async function setupStream() {
    stopLoop();
    const oldTrack = trackRef.current;
    oldTrack?.removeEventListener("mute", handleTrackInterrupt);
    oldTrack?.removeEventListener("ended", handleTrackInterrupt);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    trackRef.current = null;

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: HQ_CONSTRAINTS });
    } catch {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: LQ_CONSTRAINTS });
      } catch {
        if (mountedRef.current) {
          setCameraError("Camera unavailable — check permissions and reload.");
        }
        return;
      }
    }

    if (!mountedRef.current) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    streamRef.current = stream;
    const track = stream.getVideoTracks()[0] ?? null;
    trackRef.current = track;
    if (track) {
      onCameraInfoRef.current?.(track.getSettings());
      track.addEventListener("mute", handleTrackInterrupt);
      track.addEventListener("ended", handleTrackInterrupt);
    }
    if (videoRef.current) {
      videoRef.current.srcObject = stream;
    }
    setCameraError(null);
    interruptedRef.current = false;
    setInterrupted(false);
  }

  function resume() {
    setupStream();
  }

  useEffect(() => {
    mountedRef.current = true;
    const video = videoRef.current;
    video?.addEventListener("loadedmetadata", handleLoadedMetadata);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("resize", handleResize);

    requestWakeLock();
    setupStream();

    return () => {
      mountedRef.current = false;
      stopLoop();
      video?.removeEventListener("loadedmetadata", handleLoadedMetadata);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("resize", handleResize);
      const track = trackRef.current;
      track?.removeEventListener("mute", handleTrackInterrupt);
      track?.removeEventListener("ended", handleTrackInterrupt);
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      releaseWakeLock();
    };
    // Mount-only: everything this loop needs from props/state is read via refs above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="camera-capture">
      {!cameraError && (
        <div className="camera-stage">
          <video ref={videoRef} autoPlay playsInline muted />
          <canvas ref={overlayRef} className="camera-overlay" />
          {interrupted && (
            <button type="button" className="live-resume" onClick={resume}>
              Camera paused — tap to resume
            </button>
          )}
        </div>
      )}
      {cameraError && <p className="camera-error">{cameraError}</p>}
      <div className="camera-actions">
        {!cameraError && (
          <button type="button" className="primary" onClick={manualFire} disabled={paused || interrupted}>
            Capture
          </button>
        )}
      </div>
    </div>
  );
}
