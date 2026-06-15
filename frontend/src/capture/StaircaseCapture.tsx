import { useCallback, useState } from "react";
import type { CaptureMeta } from "../api";
import CameraCapture from "./CameraCapture";

// Guide geometry (fractions of frame height): the front card sits fully visible
// above the first guide; remaining bottom edges spread to near the frame bottom.
const FIRST_GUIDE = 0.40;
const LAST_GUIDE = 0.92;

export function guideFractions(count: number): number[] {
  if (count === 1) return [LAST_GUIDE];
  return Array.from(
    { length: count },
    (_, i) => FIRST_GUIDE + ((LAST_GUIDE - FIRST_GUIDE) * i) / (count - 1)
  );
}

interface Props {
  onDone: (photo: Blob, meta?: CaptureMeta) => void;
}

export default function StaircaseCapture({ onDone }: Props) {
  const [count, setCount] = useState(10);

  const drawOverlay = useCallback(
    (ctx: CanvasRenderingContext2D, w: number, h: number) => {
      ctx.strokeStyle = "rgba(255, 220, 0, 0.9)";
      ctx.fillStyle = "rgba(255, 220, 0, 0.9)";
      ctx.lineWidth = 2;
      ctx.font = "12px sans-serif";
      guideFractions(count).forEach((f, i) => {
        const y = f * h;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
        ctx.fillText(String(i + 1), 6, y - 4);
      });
    },
    [count]
  );

  return (
    <section>
      <h2>Step 1 — Stack your cards</h2>
      <p>
        Stack the pack in a staircase so each card&apos;s bottom edge lines up
        with a yellow line. Front card on top.
      </p>
      <div className="count-stepper">
        <button type="button" onClick={() => setCount((c) => Math.max(5, c - 1))}>−</button>
        <span>{count} cards</span>
        <button type="button" onClick={() => setCount((c) => Math.min(13, c + 1))}>+</button>
      </div>
      <CameraCapture
        drawOverlay={drawOverlay}
        captureLabel="Capture pack"
        onCapture={(blob, [vw, vh]) =>
          onDone(blob, {
            guide_positions: guideFractions(count).map((f) => f * vh),
            image_dims: [vw, vh],
            declared_count: count,
          })
        }
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
