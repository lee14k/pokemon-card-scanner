// The mobile app's handle on the shared API client (../../shared/src/client.ts),
// plus the one transport React Native needs its own way: the progressive pack
// scan. RN's fetch has no streaming body, but its XMLHttpRequest delivers
// partial responseText on progress events, which is enough to read SSE frames.
import {
  ApiError,
  createApi,
  createScanStreamParser,
  packScanForm,
  type UploadFile,
} from "@shared/client";
import type { CaptureMeta, PackScanResponse, ScanProgressEvent } from "@shared/types";
import { API_BASE } from "./config";

export const api = createApi(API_BASE);

export function scanPackStream(
  staircase: UploadFile,
  codeCard: UploadFile,
  meta: CaptureMeta | undefined,
  onProgress: (ev: ScanProgressEvent) => void
): Promise<PackScanResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const parser = createScanStreamParser(onProgress);
    let seen = 0;
    let settled = false;
    const settle = (fn: () => void) => {
      if (settled) return;
      settled = true;
      fn();
    };

    const pump = () => {
      if (xhr.status !== 200) return;
      const text = xhr.responseText ?? "";
      if (text.length <= seen) return;
      const chunk = text.slice(seen);
      seen = text.length;
      try {
        const result = parser.push(chunk);
        if (result) settle(() => resolve(result));
      } catch (e) {
        settle(() => reject(e));
      }
    };

    xhr.open("POST", `${API_BASE}/scan/pack/stream`);
    xhr.responseType = "text";
    xhr.onprogress = pump;
    xhr.onreadystatechange = () => {
      if (xhr.readyState === 3) pump();
      if (xhr.readyState !== 4) return;
      if (xhr.status !== 200) {
        let detail: unknown;
        try {
          detail = JSON.parse(xhr.responseText)?.detail;
        } catch {
          detail = undefined;
        }
        settle(() => reject(new ApiError(detail ? JSON.stringify(detail) : `Request failed (${xhr.status})`, xhr.status, detail)));
        return;
      }
      pump();
      settle(() => reject(new Error("scan stream ended without a result")));
    };
    xhr.onerror = () => settle(() => reject(new Error("Network request failed")));
    xhr.send(packScanForm(staircase, codeCard, meta));
  });
}
