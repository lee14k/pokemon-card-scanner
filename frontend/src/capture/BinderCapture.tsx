import CameraCapture from "./CameraCapture";

interface Props {
  onDone: (photo: Blob) => void;
}

// One-snap capture of a full binder page: the whole-photo pass finds and reads
// every card in the grid, so no guides or count are needed — just a flat, glare-
// free shot of the page. Mirrors StaircaseCapture's CameraCapture + upload flow.
export default function BinderCapture({ onDone }: Props) {
  return (
    <section>
      <h2>Snap your binder page</h2>
      <p>Lay the page flat, fill the frame, avoid glare.</p>
      <CameraCapture
        drawOverlay={() => {}}
        captureLabel="Capture page"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
