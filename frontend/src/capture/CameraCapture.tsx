import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Longest side, in pixels, of anything this component encodes.
 *
 * The whole file crosses a mobile uplink before any scanning starts, and the
 * scanner does not use the top of it: the binder page's whole-photo detection
 * caps at 2800px (``binder._CAP``), its quad finder works at 1600
 * (``binder._QUAD_LONG``), and the pack/live paths cap at 2600
 * (``rapidocr_reader.detect_lines*``). 2800 is the largest of those, so it is
 * the size above which extra pixels are uploaded, decoded and thrown away.
 *
 * CAVEAT, measured, and the reason this is a ceiling rather than a target: the
 * binder's PER-CELL band OCR crops out of the FULL-resolution page, so a card's
 * name/number band is read at a resolution proportional to the page's, not to
 * 2800. Halving the page really does halve that band. Lowering this constant
 * further has not been measured and is not a free knob.
 *
 * In the shipped configuration this binds on the CAMERA path only — see
 * UPLOAD_DOWNSCALE for why an uploaded file is passed through untouched.
 */
const MAX_LONG_SIDE = 2800;

/**
 * JPEG quality for anything re-encoded here.
 *
 * 0.92 — unchanged from what this component has always used. The size win in
 * this change comes from the PIXEL COUNT (see MAX_LONG_SIDE), and moving quality
 * at the same time would add a second variable to a path the committed fixtures
 * cannot validate: they are raw camera FILES, so they exercise the upload path,
 * never this one. 0.85 and 0.95 were both scored against those fixtures and were
 * indistinguishable from 0.92 (zero confident-wrong in all three; they differed
 * only in which marginal card rectangle survived, which is noise) — no evidence
 * to move, so it does not move.
 * (LiveCapture has its own pipeline at 0.8 and is untouched.)
 */
const JPEG_QUALITY = 0.92;

/**
 * Downscale on the "Upload instead" path — currently OFF, and this is a
 * deliberate hold rather than dead code.
 *
 * Downscaling an upload requires decoding and re-encoding it, and that step
 * alone — with no resize and at quality 100 — is enough to change how many card
 * rectangles the binder's contour-based quad finder finds. On the committed
 * corpus it moves page_5 from 6 cells to 5, which is the binder gate's HARD
 * failure category (cell count != truth count), not a soft accuracy dip. The
 * effect is non-monotonic in both resolution and quality, so no cap or quality
 * setting tunes it away; a 2800px upload scored no confident-wrong, but "one
 * fixture crosses into the gate's fail bucket" is not something to ship for a
 * bandwidth win, and one clean cap value on a five-photo corpus is not evidence
 * that it generalises.
 *
 * TO RE-ENABLE, both must be true:
 *   1. the Phase-2 detection cascade has replaced the single contour pass that
 *      is sitting on this knife edge, and
 *   2. docs/acceptance/binder_gate.py scores a RE-ENCODED corpus, so the failure
 *      mode above is visible to CI instead of only to a one-off experiment.
 *
 * Until then an "Upload instead" file goes up byte-for-byte as the user picked
 * it, exactly as it always has. The machinery below is kept working and behind
 * this flag so re-enabling is a one-line change with a test to back it.
 */
const UPLOAD_DOWNSCALE: boolean = false;

/** (width, height) shrunk to fit MAX_LONG_SIDE. Never enlarges. */
function fitted(w: number, h: number): [number, number] {
  const scale = Math.min(1, MAX_LONG_SIDE / Math.max(w, h));
  return [Math.max(1, Math.round(w * scale)), Math.max(1, Math.round(h * scale))];
}

/** Draw `src` into a canvas at most MAX_LONG_SIDE on its long side, as JPEG. */
function toJpeg(
  src: CanvasImageSource,
  w: number,
  h: number
): Promise<{ blob: Blob; dims: [number, number] } | null> {
  const [dw, dh] = fitted(w, h);
  const canvas = document.createElement("canvas");
  canvas.width = dw;
  canvas.height = dh;
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.resolve(null);
  ctx.drawImage(src, 0, 0, dw, dh);
  return new Promise((resolve) =>
    canvas.toBlob(
      (blob) => resolve(blob ? { blob, dims: [dw, dh] } : null),
      "image/jpeg",
      JPEG_QUALITY
    )
  );
}

interface Props {
  /** Draw the alignment overlay; called on each layout change. */
  drawOverlay: (ctx: CanvasRenderingContext2D, w: number, h: number) => void;
  /** Called with the captured JPEG and ITS OWN dimensions (post-downscale). */
  onCapture: (blob: Blob, dims: [number, number]) => void;
  /** Fallback for devices/contexts without camera access. */
  onUploadFile: (file: File) => void;
  captureLabel: string;
}

export default function CameraCapture({
  drawOverlay,
  onCapture,
  onUploadFile,
  captureLabel,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const overlayRef = useRef<HTMLCanvasElement>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);

  useEffect(() => {
    let stream: MediaStream | null = null;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        if (videoRef.current) videoRef.current.srcObject = stream;
      } catch {
        setCameraError("Camera unavailable — use upload instead.");
      }
    })();
    return () => stream?.getTracks().forEach((t) => t.stop());
  }, []);

  const redraw = useCallback(() => {
    const canvas = overlayRef.current;
    const video = videoRef.current;
    if (!canvas || !video) return;
    canvas.width = video.clientWidth;
    canvas.height = video.clientHeight;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      drawOverlay(ctx, canvas.width, canvas.height);
    }
  }, [drawOverlay]);

  useEffect(() => {
    redraw();
    window.addEventListener("resize", redraw);
    return () => window.removeEventListener("resize", redraw);
  }, [redraw]);

  // A getUserMedia frame is upright by construction (no EXIF anywhere in a
  // MediaStream) and is typically 1080p-1440p, so the cap rarely binds here —
  // it is applied anyway so one constant governs everything we upload.
  const capture = async () => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const out = await toJpeg(video, video.videoWidth, video.videoHeight);
    if (out) onCapture(out.blob, out.dims);
  };

  /**
   * "Upload instead". With UPLOAD_DOWNSCALE off (the shipped state, see the
   * constant) this hands the picked file straight through, untouched.
   *
   * When it is on, THREE PATHS — and the two that skip the canvas are the point:
   *
   *  1. Already small enough -> the ORIGINAL FILE is sent, untouched. Decoding
   *     and re-encoding a photo that is not being resized buys nothing and is
   *     not free: it is a lossy generation step, and the binder's contour-based
   *     quad finder is measurably sensitive to one (re-encoding a fixture at
   *     native size and quality 100 is enough to change how many card
   *     rectangles it finds). So we only pay it when it actually removes
   *     pixels. This also leaves small HEICs alone — the server decodes HEIC
   *     itself (pillow_heif), and HEIC->JPEG usually makes a file BIGGER.
   *
   *  2. Oversized and decodable -> canvas downscale to MAX_LONG_SIDE, JPEG.
   *     `imageOrientation: "from-image"` makes createImageBitmap apply the
   *     file's EXIF rotation to the PIXELS, which matters because the JPEG this
   *     produces carries no EXIF at all: without it every portrait phone photo
   *     would reach the server sideways and the scan would collapse (verified
   *     against the fixtures — a sideways binder page scores 6 confident-wrong
   *     cells against 0 upright). "from-image" is also the current spec
   *     default, so this is belt and braces for engines that still default to
   *     "none"; an engine too old to know the value throws, and lands in 3.
   *
   *  3. Anything throws -> the ORIGINAL FILE, unchanged. The realistic case is
   *     HEIC outside Safari: Chrome/Firefox cannot decode it, createImageBitmap
   *     rejects, and the raw HEIC goes up exactly as it does today. Same for a
   *     tainted/oversized bitmap, a canvas the browser refuses to allocate, or
   *     a toBlob that returns null. Uploading is never blocked by this
   *     optimisation failing.
   */
  const uploadFile = async (file: File) => {
    if (!UPLOAD_DOWNSCALE) {
      onUploadFile(file);
      return;
    }
    let bitmap: ImageBitmap;
    try {
      bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      onUploadFile(file); // path 3 — HEIC on Chrome/Firefox is the common one
      return;
    }
    try {
      if (Math.max(bitmap.width, bitmap.height) <= MAX_LONG_SIDE) {
        onUploadFile(file); // path 1 — nothing to remove, so nothing to re-encode
        return;
      }
      const out = await toJpeg(bitmap, bitmap.width, bitmap.height);
      onUploadFile(
        out
          ? new File([out.blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", {
              type: "image/jpeg",
            })
          : file
      );
    } catch {
      onUploadFile(file);
    } finally {
      bitmap.close();
    }
  };

  return (
    <div className="camera-capture">
      {!cameraError && (
        <div className="camera-stage">
          <video ref={videoRef} autoPlay playsInline muted onLoadedMetadata={redraw} />
          <canvas ref={overlayRef} className="camera-overlay" />
        </div>
      )}
      {cameraError && <p className="camera-error">{cameraError}</p>}
      <div className="camera-actions">
        {!cameraError && (
          <button type="button" className="primary" onClick={() => void capture()}>
            {captureLabel}
          </button>
        )}
        <label className="upload-fallback">
          Upload instead
          <input
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void uploadFile(f);
            }}
          />
        </label>
      </div>
    </div>
  );
}
