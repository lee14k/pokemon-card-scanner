import { useRef, useState, type ReactNode } from "react";
import { Platform, Pressable, StyleSheet, View } from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import * as ImagePicker from "expo-image-picker";
import type { NativeFile } from "@shared/client";
import { LIVE_GUIDE, LIVE_STRIP_FRAC, prepCameraPhoto, type Photo } from "@/lib/images";
import { Body, Loader, Panel, Pixel, PixelButton, PixelIcon, colors, tap } from "@/ui/kit";

export type CapturedPhoto = Photo | (NativeFile & { width?: number; height?: number });

type Guide = "none" | "code" | "live";

/**
 * Camera viewfinder with a Game Boy "A"-button shutter and an upload fallback.
 *
 * The preview is held at 3:4 — the shape of a phone sensor's photo in portrait —
 * so a guide box drawn as fractions of the preview is the same fractions of the
 * saved photo (the live crop depends on that).
 */
export function CameraCapture({
  onCapture,
  onRawCapture,
  guide = "none",
  allowUpload = true,
  busy,
  shutterLabel = "SNAP",
  extra,
}: {
  /** Gets a photo already downscaled for upload. */
  onCapture?: (photo: CapturedPhoto) => void;
  /** Gets the untouched camera frame instead (the live flow crops it itself). */
  onRawCapture?: (uri: string, width: number, height: number) => void;
  guide?: Guide;
  allowUpload?: boolean;
  busy?: boolean;
  shutterLabel?: string;
  /** Right-hand slot in the controls row. */
  extra?: ReactNode;
}) {
  const [permission, requestPermission] = useCameraPermissions();
  const cam = useRef<CameraView>(null);
  const [ready, setReady] = useState(false);
  const [snapping, setSnapping] = useState(false);
  const [facing, setFacing] = useState<"back" | "front">("back");

  const snap = async () => {
    if (!cam.current || snapping || busy) return;
    setSnapping(true);
    tap();
    try {
      const pic = await cam.current.takePictureAsync({ quality: onRawCapture ? 1 : 0.92 });
      if (!pic) return;
      if (onRawCapture) onRawCapture(pic.uri, pic.width, pic.height);
      else onCapture?.(await prepCameraPhoto(pic.uri, pic.width, pic.height));
    } finally {
      setSnapping(false);
    }
  };

  // "Upload instead" sends the file byte-for-byte, exactly like the web app's
  // upload path (re-encoding an upload measurably shifts the binder's quad
  // finder — see UPLOAD_DOWNSCALE in frontend/src/capture/CameraCapture.tsx).
  const upload = async () => {
    const res = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ["images"], quality: 1 });
    if (res.canceled || !res.assets?.[0]) return;
    const a = res.assets[0];
    onCapture?.({
      uri: a.uri,
      name: a.fileName ?? "photo.jpg",
      type: a.mimeType ?? "image/jpeg",
      width: a.width,
      height: a.height,
    });
  };

  const uploadButton = allowUpload ? (
    <PixelButton title="Upload" icon="upload" variant="ghost" size="sm" onPress={upload} />
  ) : (
    <View style={{ width: 80 }} />
  );

  if (!permission) {
    return (
      <View style={{ alignItems: "center", padding: 32 }}>
        <Loader />
      </View>
    );
  }

  if (!permission.granted) {
    return (
      <Panel padding={24}>
        <View style={{ alignItems: "center", gap: 14 }}>
          <PixelIcon name="camera" size={48} color={colors.border} />
          <Body style={{ textAlign: "center" }}>
            {permission.canAskAgain
              ? "Pack Scanner needs the camera to photograph your cards."
              : "Camera access is off — turn it on in Settings, or upload a photo instead."}
          </Body>
          {permission.canAskAgain && <PixelButton title="Allow camera" variant="primary" icon="camera" onPress={requestPermission} />}
          {allowUpload && <PixelButton title="Upload a photo instead" icon="upload" variant="ghost" onPress={upload} />}
        </View>
      </Panel>
    );
  }

  return (
    <View style={{ gap: 14 }}>
      <View style={styles.stageShadow}>
        <View style={styles.stage}>
          <CameraView
            ref={cam}
            style={StyleSheet.absoluteFill}
            facing={facing}
            ratio={Platform.OS === "android" ? "4:3" : undefined}
            onCameraReady={() => setReady(true)}
            animateShutter
          />
          <View pointerEvents="none" style={StyleSheet.absoluteFill}>
            {guide === "none" && <Viewfinder />}
            {guide === "code" && <CodeGuide />}
            {guide === "live" && <LiveGuide />}
          </View>
          {(snapping || busy) && (
            <View style={styles.busy}>
              <Loader />
            </View>
          )}
        </View>
      </View>

      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
        {uploadButton}
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Take photo"
          disabled={!ready || snapping || busy}
          onPress={snap}
          style={({ pressed }) => [styles.shutterShadow, { opacity: !ready || busy ? 0.5 : 1 }, pressed && { paddingBottom: 1, marginTop: 4 }]}
        >
          <View style={styles.shutter}>
            <PixelIcon name="camera" size={22} color="#fff" />
            <Pixel size={7} tone="white">
              {shutterLabel}
            </Pixel>
          </View>
        </Pressable>
        {extra ?? (
          <PixelButton
            title="Flip"
            icon="flip"
            variant="ghost"
            size="sm"
            onPress={() => setFacing((f) => (f === "back" ? "front" : "back"))}
          />
        )}
      </View>
    </View>
  );
}

function Viewfinder() {
  const corner = { position: "absolute" as const, width: 24, height: 24, borderColor: colors.gold };
  return (
    <View style={{ position: "absolute", inset: 14 }}>
      <View style={[corner, { top: 0, left: 0, borderTopWidth: 4, borderLeftWidth: 4 }]} />
      <View style={[corner, { top: 0, right: 0, borderTopWidth: 4, borderRightWidth: 4 }]} />
      <View style={[corner, { bottom: 0, left: 0, borderBottomWidth: 4, borderLeftWidth: 4 }]} />
      <View style={[corner, { bottom: 0, right: 0, borderBottomWidth: 4, borderRightWidth: 4 }]} />
    </View>
  );
}

function CodeGuide() {
  return (
    <View
      style={{
        position: "absolute",
        left: "8%",
        top: "25%",
        width: "84%",
        height: "50%",
        borderWidth: 4,
        borderStyle: "dashed",
        borderColor: colors.gold,
        borderRadius: 6,
      }}
    />
  );
}

function LiveGuide() {
  return (
    <View
      style={{
        position: "absolute",
        left: `${LIVE_GUIDE.x * 100}%`,
        top: `${LIVE_GUIDE.y * 100}%`,
        width: `${LIVE_GUIDE.w * 100}%`,
        height: `${LIVE_GUIDE.h * 100}%`,
        borderWidth: 4,
        borderColor: colors.cyan,
        borderRadius: 6,
        backgroundColor: "rgba(63,208,255,0.08)",
      }}
    >
      <View
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: `${LIVE_STRIP_FRAC * 100}%`,
          height: 1,
          backgroundColor: "rgba(255,255,255,0.5)",
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  stageShadow: { backgroundColor: colors.ink, borderRadius: 14, paddingBottom: 4 },
  stage: {
    width: "100%",
    aspectRatio: 3 / 4,
    borderRadius: 14,
    borderWidth: 3,
    borderColor: colors.ink,
    overflow: "hidden",
    backgroundColor: "#000",
  },
  busy: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: "rgba(10,7,23,0.45)",
    alignItems: "center",
    justifyContent: "center",
  },
  shutterShadow: { backgroundColor: colors.ink, borderRadius: 40, paddingBottom: 5 },
  shutter: {
    width: 76,
    height: 76,
    borderRadius: 38,
    borderWidth: 4,
    borderColor: colors.ink,
    backgroundColor: colors.primary,
    alignItems: "center",
    justifyContent: "center",
    gap: 4,
  },
});
