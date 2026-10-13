// Photo prep for uploads. Mirrors the web capture constants so a phone photo
// reaches the scanner at the same size whichever app took it.
import { ImageManipulator, SaveFormat } from "expo-image-manipulator";
import type { NativeFile } from "@shared/client";

/** Longest side of a camera photo we upload — the scanner never uses more
 *  (see MAX_LONG_SIDE in frontend/src/capture/CameraCapture.tsx). */
export const MAX_LONG_SIDE = 2800;
const JPEG_QUALITY = 0.92;

/** Live-scan guide box as fractions of the frame, plus the number strip
 *  (mirrors GUIDE / STRIP_FRAC / TARGET_CARD_H in LiveCapture.tsx). */
export const LIVE_GUIDE = { x: 0.14, y: 0.08, w: 0.72, h: 0.8 } as const;
export const LIVE_STRIP_FRAC = 0.16;
const LIVE_CARD_MAX_H = 1400;
const LIVE_JPEG_QUALITY = 0.8;

export interface Photo extends NativeFile {
  uri: string;
  width: number;
  height: number;
}

const asFile = (uri: string, width: number, height: number, name: string): Photo => ({
  uri,
  width,
  height,
  name,
  type: "image/jpeg",
});

/** Shrink a camera photo to MAX_LONG_SIDE (never enlarges) and re-encode. */
export async function prepCameraPhoto(uri: string, width: number, height: number): Promise<Photo> {
  const scale = Math.min(1, MAX_LONG_SIDE / Math.max(width, height));
  const ctx = ImageManipulator.manipulate(uri);
  if (scale < 1) ctx.resize(width >= height ? { width: Math.round(width * scale) } : { height: Math.round(height * scale) });
  const ref = await ctx.renderAsync();
  const out = await ref.saveAsync({ compress: JPEG_QUALITY, format: SaveFormat.JPEG });
  return asFile(out.uri, out.width, out.height, "photo.jpg");
}

/** Card crop + native-res number strip for one live-scan frame. */
export async function cropLiveFrame(uri: string, width: number, height: number): Promise<{ card: Photo; strip: Photo }> {
  const gx = Math.round(LIVE_GUIDE.x * width);
  const gy = Math.round(LIVE_GUIDE.y * height);
  const gw = Math.round(LIVE_GUIDE.w * width);
  const gh = Math.round(LIVE_GUIDE.h * height);

  const cardCtx = ImageManipulator.manipulate(uri).crop({ originX: gx, originY: gy, width: gw, height: gh });
  if (gh > LIVE_CARD_MAX_H) cardCtx.resize({ height: LIVE_CARD_MAX_H });
  const cardOut = await (await cardCtx.renderAsync()).saveAsync({ compress: LIVE_JPEG_QUALITY, format: SaveFormat.JPEG });

  const stripH = Math.round(gh * LIVE_STRIP_FRAC);
  const stripOut = await (
    await ImageManipulator.manipulate(uri)
      .crop({ originX: gx, originY: gy + gh - stripH, width: gw, height: stripH })
      .renderAsync()
  ).saveAsync({ compress: LIVE_JPEG_QUALITY, format: SaveFormat.JPEG });

  return {
    card: asFile(cardOut.uri, cardOut.width, cardOut.height, "card.jpg"),
    strip: asFile(stripOut.uri, stripOut.width, stripOut.height, "strip.jpg"),
  };
}
