import { useCallback } from "react";
import CameraCapture from "./CameraCapture";
import { ScreenTitle } from "../ui/kit";

interface Props {
  onDone: (photo: Blob) => void;
}

export default function CodeCardCapture({ onDone }: Props) {
  const drawOverlay = useCallback(
    (ctx: CanvasRenderingContext2D, w: number, h: number) => {
      // Gold guide box, ink-shadowed so it reads on any background.
      const x = w * 0.08;
      const y = h * 0.25;
      const bw = w * 0.84;
      const bh = h * 0.5;
      ctx.lineWidth = 4;
      ctx.setLineDash([14, 8]);
      ctx.strokeStyle = "rgba(10, 7, 23, 0.85)";
      ctx.strokeRect(x + 2, y + 2, bw, bh);
      ctx.strokeStyle = "rgba(255, 210, 63, 0.95)";
      ctx.strokeRect(x, y, bw, bh);
    },
    []
  );

  return (
    <section className="stack-lg">
      <ScreenTitle
        kicker="One photo · Step 2/2"
        title="Code card"
        sub="Fill the gold box with the pack's code card and hold steady. It verifies the pull so it can battle and count in stats."
      />
      <CameraCapture
        drawOverlay={drawOverlay}
        captureLabel="Capture code card"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
