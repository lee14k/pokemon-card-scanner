import { useCallback, useEffect, useRef, useState } from "react";

interface Props {
  /** Draw the alignment overlay; called on each layout change. */
  drawOverlay: (ctx: CanvasRenderingContext2D, w: number, h: number) => void;
  /** Called with the captured JPEG and the intrinsic video dimensions. */
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

  const capture = () => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = video.videoWidth;
    c.height = video.videoHeight;
    c.getContext("2d")!.drawImage(video, 0, 0);
    c.toBlob(
      (blob) => blob && onCapture(blob, [video.videoWidth, video.videoHeight]),
      "image/jpeg",
      0.92
    );
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
          <button type="button" className="primary" onClick={capture}>
            {captureLabel}
          </button>
        )}
        <label className="upload-fallback">
          Upload instead
          <input
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => e.target.files?.[0] && onUploadFile(e.target.files[0])}
          />
        </label>
      </div>
    </div>
  );
}
