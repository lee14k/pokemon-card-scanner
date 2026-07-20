import CameraCapture from "./CameraCapture";

interface Props {
  onDone: (photo: Blob) => void;
}

// One-snap capture: whole-photo detection finds and reads each card, so no
// alignment guides or card-count needed — just a clear photo of the fan.
export default function StaircaseCapture({ onDone }: Props) {
  return (
    <section>
      <h2>Snap your pack</h2>
      <p>
        Fan your cards in a staircase so each card&apos;s bottom edge — the number
        and set symbol — is visible, then take one photo.
      </p>
      <CameraCapture
        drawOverlay={() => {}}
        captureLabel="Capture pack"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
