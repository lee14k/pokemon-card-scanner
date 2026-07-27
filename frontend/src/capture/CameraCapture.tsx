import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Longest side, in pixels, of anything this component uploads.
 *
 * A modern phone camera hands us 12-48MP (4032-8064px long side) and the whole
 * file crosses a mobile uplink before any scanning starts. The scanner does not
 * use that: the binder page's whole-photo detection caps at 2800px
 * (``binder._CAP``), its quad finder works at 1600 (``binder._QUAD_LONG``), and
 * the pack/live paths cap at 2600 (``rapidocr_reader.detect_lines*``). 2800 is
 * the largest of those, so it is the size above which extra pixels are
 * uploaded, decoded and then thrown away.
 *
 * CAVEAT, measured, and the reason this is a ceiling rather than a target: the
 * binder's PER-CELL band OCR crops out of the FULL-resolution page, so a card's
 * name/number band is read at a resolution proportional to the page's, not to
 * 2800. Halving the page really does halve that band. Downscaling to 2800 was
 * scored against every committed binder fixture and held the accuracy line
 * (18 correct, zero confident-wrong) — but it is not a free operation, and
 * lowering this constant further has not been measured.
 */
const MAX_LONG_SIDE = 2800;

/**
 * JPEG quality for anything re-encoded here — one constant for both paths so
 * the camera and upload flows cannot drift apart.
 *
 * 0.85, 0.92 and 0.95 were each scored at 2800px against every committed binder
 * fixture. All three held zero confident-wrong; they differed only in which
 * marginal card rectangle the quad finder happened to keep (see ``uploadFile``),
 * which is noise, not signal — 0.85 was the joint best and is the smallest
 * upload, so it is the one that ships. This does lower the camera path from the
 * 0.92 it used before; that payload is a ~1080p frame either way, so the change
 * is a few tens of KB and no measured accuracy.
 * (LiveCapture has its own pipeline at 0.8 and is untouched.)
 */
const JPEG_QUALITY = 0.85;

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
   * "Upload instead": shrink an oversized photo before it crosses the network.
   *
   * THREE PATHS, and the two that skip the canvas are the point:
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
