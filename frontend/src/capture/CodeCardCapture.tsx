import { useCallback } from "react";
import CameraCapture from "./CameraCapture";

interface Props {
  onDone: (photo: Blob) => void;
}

export default function CodeCardCapture({ onDone }: Props) {
  const drawOverlay = useCallback(
    (ctx: CanvasRenderingContext2D, w: number, h: number) => {
      ctx.strokeStyle = "rgba(255, 220, 0, 0.9)";
      ctx.lineWidth = 3;
      ctx.strokeRect(w * 0.08, h * 0.25, w * 0.84, h * 0.5);
    },
    []
  );

  return (
    <section>
      <h2>Step 2 — Code card</h2>
      <p>Fill the frame with the pack&apos;s code card. Hold steady.</p>
      <CameraCapture
        drawOverlay={drawOverlay}
        captureLabel="Capture code card"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
