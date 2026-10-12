import CameraCapture from "./CameraCapture";
import { ScreenTitle } from "../ui/kit";

interface Props {
  onDone: (photo: Blob) => void;
}

// One-snap capture of a full binder page: the whole-photo pass finds and reads
// every card in the grid, so no guides or count are needed — just a flat, glare-
// free shot of the page. Mirrors StaircaseCapture's CameraCapture + upload flow.
export default function BinderCapture({ onDone }: Props) {
  return (
    <section className="stack-lg">
      <ScreenTitle kicker="Binder page" title="Snap your page" sub="Lay the page flat, fill the frame, and avoid glare.">
        <div className="tips">
          <span className="badge outline">Flat page</span>
          <span className="badge outline">Fill the frame</span>
          <span className="badge outline">No glare</span>
        </div>
      </ScreenTitle>
      <CameraCapture
        drawOverlay={() => {}}
        captureLabel="Capture page"
        onCapture={(blob) => onDone(blob)}
        onUploadFile={(file) => onDone(file)}
      />
    </section>
  );
}
