import CameraCapture from "./CameraCapture";
import { ScreenTitle } from "../ui/kit";

interface Props {
  onDone: (photo: Blob) => void;
}

// One-snap capture: whole-photo detection finds and reads each card, so no
// alignment guides or card-count needed — just a clear photo of the fan.
export default function StaircaseCapture({ onDone }: Props) {
  return (
    <section className="stack-lg">
      <ScreenTitle
        kicker="One photo · Step 1/2"
        title="Snap your pack"
        sub="Fan your cards in a staircase so each card's bottom edge — the number and set symbol — is visible, then take one photo."
      >
        <div className="tips">
          <span className="badge outline">Good light</span>
          <span className="badge outline">No glare</span>
          <span className="badge outline">Edges visible</span>
        </div>
      </ScreenTitle>
      <CameraCapture
        drawOverlay={() => {}}
        captureLabel="Capture pack"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
