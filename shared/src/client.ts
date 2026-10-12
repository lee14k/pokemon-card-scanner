// Platform-neutral client for the FastAPI backend.
//
// The web app and the mobile app both build one of these with createApi(base).
// The only platform difference that leaks in is how a photo is attached to a
// multipart form: the browser hands us a Blob/File, React Native hands us a
// `{ uri, name, type }` descriptor that its FormData knows how to stream from
// disk. `UploadFile` accepts either and appendFile() does the right thing.
//
// Auth is the backend's cookie session (fastapi-users CookieTransport). Browsers
// keep it in the cookie jar; React Native's networking layer keeps it in the
// native cookie store (NSHTTPCookieStorage / OkHttp's CookieJar), so the same
// `credentials: "include"` calls work unchanged on both.

import type {
  AdminTrainer,
  AnomalyRow,
  Battle,
  BattleList,
  BinderCard,
  BinderScan,
  CaptureMeta,
  CollectionOut,
  CollectionSaveCard,
  CollectionSaveOut,
  DexOut,
  LiveFrameOut,
  LiveState,
  PackCard,
  PackScanResponse,
  SavePullMeta,
  SavedPull,
  ScanFollowup,
  ScanProgressEvent,
  SetDetail,
  SetInfo,
  SetSummary,
  Trainer,
} from "./types";

/** A file on the device's disk, as React Native's FormData expects it. */
export interface NativeFile {
  uri: string;
  name?: string;
  type?: string;
}

export type UploadFile = Blob | NativeFile;

function isNativeFile(f: UploadFile): f is NativeFile {
  return typeof (f as NativeFile).uri === "string";
}

export function appendFile(form: FormData, field: string, file: UploadFile, filename: string): void {
  if (isNativeFile(file)) {
    // RN's FormData reads the part from `uri`; the cast is the documented
    // escape hatch, since lib.dom's FormData only knows about Blobs.
    form.append(field, {
      uri: file.uri,
      name: file.name ?? filename,
      type: file.type ?? "image/jpeg",
    } as unknown as Blob);
  } else {
    form.append(field, file, filename);
  }
}

// Carries the HTTP status alongside the message so callers that need to branch
// on it (e.g. live-scan session recovery on 404) don't have to string-match
// error text. Still a plain Error for everyone else's `e instanceof Error`.
// `detail` is FastAPI's error body (a code string like "LOGIN_BAD_CREDENTIALS",
// or pydantic's validation list) — friendlyError() turns it into a sentence.
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Rejected by scanBinder() when the page had nothing readable on it (422). */
export interface NoCardsFound {
  code: "no_cards_found";
}

export function isNoCardsFound(e: unknown): e is NoCardsFound {
  return !!e && typeof e === "object" && "code" in e && (e as { code: unknown }).code === "no_cards_found";
}

export async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? (body as { detail: unknown }).detail
        : undefined;
    const msg = detail !== undefined ? JSON.stringify(detail) : text || res.statusText;
    throw new ApiError(msg || `Request failed (${res.status})`, res.status, detail);
  }
  return body as T;
}

// ── Server-sent events ───────────────────────────────────────────────────────
// /scan/pack/stream answers with text/event-stream. EventSource can't POST a
// body, so each platform reads the raw text its own way (fetch ReadableStream in
// the browser, incremental XHR responseText on React Native) and feeds it here.

/**
 * Incremental SSE parser for the pack-scan stream. push() accepts any slice of
 * the response text (frames may straddle calls) and returns the final result
 * once the `result` event arrives. An `error` event throws.
 */
export function createScanStreamParser(onProgress: (ev: ScanProgressEvent) => void) {
  let buf = "";

  // Parses one complete "event:\ndata:\n\n" frame (blank-line separated, per
  // SSE). Lines starting with ":" are comments (our heartbeats) — ignored.
  const handleFrame = (frame: string): PackScanResponse | undefined => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (dataLines.length === 0) return undefined;
    const data = JSON.parse(dataLines.join("\n"));
    if (event === "progress") {
      onProgress(data as ScanProgressEvent);
      return undefined;
    }
    if (event === "error") {
      throw new Error(data?.message || "scan failed");
    }
    if (event === "result") {
      return data as PackScanResponse;
    }
    return undefined;
  };

  return {
    push(chunk: string): PackScanResponse | undefined {
      buf += chunk.replace(/\r\n/g, "\n");
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const result = handleFrame(frame);
        if (result) return result;
      }
      return undefined;
    },
  };
}

export function packScanForm(staircase: UploadFile, codeCard: UploadFile, meta?: CaptureMeta): FormData {
  const form = new FormData();
  appendFile(form, "staircase", staircase, "staircase.jpg");
  appendFile(form, "code_card", codeCard, "code.jpg");
  if (meta) form.append("capture_meta", JSON.stringify(meta));
  return form;
}

// ── The client ───────────────────────────────────────────────────────────────

/**
 * @param base API origin without a trailing slash. "" means same-origin (the
 *   production web build is served by the API itself).
 */
export function createApi(base: string) {
  const root = base.replace(/\/$/, "");
  const url = (path: string) => `${root}${path}`;
  const authed = (path: string, init?: RequestInit) =>
    fetch(url(path), { credentials: "include", ...init });

  async function postJson<T>(path: string, body: unknown): Promise<T> {
    return parse(
      await authed(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      })
    );
  }

  async function getScanFollowup<C extends PackCard = PackCard>(
    kind: "pack" | "binder",
    scanId: string
  ): Promise<ScanFollowup<C>> {
    return parse(await authed(`/scan/${kind}/${encodeURIComponent(scanId)}`));
  }

  return {
    base: root,

    // ── Pack scan ──
    async scanPack(staircase: UploadFile, codeCard: UploadFile, meta?: CaptureMeta): Promise<PackScanResponse> {
      return parse(await fetch(url("/scan/pack"), { method: "POST", body: packScanForm(staircase, codeCard, meta) }));
    },

    /**
     * Progressive variant of scanPack() over fetch + ReadableStream, for the
     * browser. Any parse/network/stream error rejects — callers should catch
     * and fall back to scanPack(). (React Native has no streaming fetch body;
     * the mobile app streams over XHR with the same parser instead.)
     */
    async scanPackStream(
      staircase: UploadFile,
      codeCard: UploadFile,
      meta: CaptureMeta | undefined,
      onProgress: (ev: ScanProgressEvent) => void
    ): Promise<PackScanResponse> {
      const res = await fetch(url("/scan/pack/stream"), {
        method: "POST",
        body: packScanForm(staircase, codeCard, meta),
      });
      if (!res.ok) {
        // Mirror parse()'s error shape (throws ApiError) so callers see a
        // consistent failure regardless of which scan path they took.
        return parse(res);
      }
      if (!res.body) {
        // 200 with no readable body — guard rather than risk silently
        // mis-parsing raw SSE text as a PackScanResponse.
        throw new Error("scan stream: response has no body");
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const parser = createScanStreamParser(onProgress);
      for (;;) {
        const { done, value } = await reader.read();
        if (value) {
          const result = parser.push(decoder.decode(value, { stream: true }));
          if (result) return result;
        }
        if (done) break;
      }
      throw new Error("scan stream ended without a result");
    },

    getPackFollowup: (scanId: string) => getScanFollowup<PackCard>("pack", scanId),
    getBinderFollowup: (scanId: string) => getScanFollowup<BinderCard>("binder", scanId),

    async lookupCard(setId: string, number: string): Promise<{ found: boolean; card: PackCard | null }> {
      const params = new URLSearchParams({ set_id: setId, number });
      return parse(await fetch(url(`/cards/lookup?${params}`)));
    },

    async getSets(): Promise<SetInfo[]> {
      return parse(await fetch(url("/sets")));
    },

    // ── Auth ──
    async register(email: string, password: string, handle: string): Promise<Trainer> {
      return postJson("/auth/register", { email, password, handle });
    },

    async login(email: string, password: string): Promise<void> {
      const form = new URLSearchParams({ username: email, password });
      await parse(
        await authed("/auth/cookie/login", {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded" },
          body: form.toString(),
        })
      );
    },

    async logout(): Promise<void> {
      await authed("/auth/cookie/logout", { method: "POST" });
    },

    async me(): Promise<Trainer | null> {
      const res = await authed("/users/me");
      if (res.status === 401) return null;
      return parse(res);
    },

    // ── Pulls ──
    async savePull(staircase: UploadFile, codeCard: UploadFile, cards: PackCard[], meta: SavePullMeta): Promise<SavedPull> {
      const form = new FormData();
      appendFile(form, "staircase", staircase, "staircase.jpg");
      appendFile(form, "code_card", codeCard, "code.jpg");
      form.append("cards", JSON.stringify(cards));
      form.append("capture_path", meta.capture_path);
      form.append("pack_confidence", String(meta.pack_confidence));
      if (meta.segmentation_warning) form.append("segmentation_warning", meta.segmentation_warning);
      if (meta.capture_meta) form.append("capture_meta", JSON.stringify(meta.capture_meta));
      if (meta.live_session_id) form.append("live_session_id", meta.live_session_id);
      return parse(await authed("/pulls", { method: "POST", body: form }));
    },

    async listPulls(): Promise<SavedPull[]> {
      return parse(await authed("/pulls"));
    },

    async patchPullCode(pullId: string, code: UploadFile): Promise<{ verified: boolean; code: string | null }> {
      const form = new FormData();
      appendFile(form, "code_card", code, "code.jpg");
      return parse(await authed(`/pulls/${pullId}/code`, { method: "PATCH", body: form }));
    },

    // ── Live scan ──
    async liveStart(): Promise<string> {
      const { session_id } = await parse<{ session_id: string }>(await authed("/scan/live/start", { method: "POST" }));
      return session_id;
    },

    async liveFrame(sid: string, card: UploadFile, strip?: UploadFile): Promise<LiveFrameOut> {
      const form = new FormData();
      appendFile(form, "card", card, "card.jpg");
      if (strip) appendFile(form, "strip", strip, "strip.jpg");
      return parse(await authed(`/scan/live/${sid}/frame`, { method: "POST", body: form }));
    },

    async liveState(sid: string): Promise<LiveState> {
      return parse(await authed(`/scan/live/${sid}`));
    },

    liveCardImageUrl(sid: string, row: number): string {
      return url(`/scan/live/${sid}/card/${row}/image`);
    },

    async liveDuplicate(sid: string, row: number, add: boolean): Promise<void> {
      await postJson(`/scan/live/${sid}/card/${row}/duplicate`, { add });
    },

    async liveReplace(sid: string, row: number): Promise<void> {
      await parse(await authed(`/scan/live/${sid}/card/${row}/replace`, { method: "POST" }));
    },

    async liveFinish(sid: string): Promise<PackScanResponse> {
      return parse(await authed(`/scan/live/${sid}/finish`, { method: "POST" }));
    },

    // ── Stats / admin ──
    async statsSets(): Promise<SetSummary[]> {
      return parse(await authed("/stats/sets"));
    },
    async statsSetDetail(setId: string): Promise<SetDetail> {
      return parse(await authed(`/stats/sets/${encodeURIComponent(setId)}`));
    },
    async statsAnomalies(status = "open"): Promise<AnomalyRow[]> {
      return parse(await authed(`/stats/anomalies?status=${status}`));
    },
    async updateAnomaly(id: string, status: string): Promise<AnomalyRow> {
      return parse(
        await authed(`/stats/anomalies/${id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ status }),
        })
      );
    },
    async recomputeStats(): Promise<void> {
      const res = await authed("/admin/stats/recompute", { method: "POST" });
      if (!res.ok) throw new ApiError(`recompute failed (${res.status})`, res.status);
    },
    async adminTrainers(query = ""): Promise<AdminTrainer[]> {
      return parse(await authed(`/admin/trainers?query=${encodeURIComponent(query)}`));
    },
    async setTrainerRole(id: string, role: string): Promise<AdminTrainer> {
      return parse(
        await authed(`/admin/trainers/${id}/role`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ role }),
        })
      );
    },

    // ── Pokédex ──
    async getDex(): Promise<DexOut> {
      return parse(await authed("/dex"));
    },

    // ── Binder → Collection ──
    /**
     * Scan one binder page into a grid of PackCard-shaped cells (with
     * thumbnails). A decode failure or a page with no readable cards comes back
     * as 422 — that case rejects with `{ code: "no_cards_found" }` (see
     * isNoCardsFound) so the caller can show the retake state without
     * string-matching the error body.
     */
    async scanBinder(page: UploadFile): Promise<BinderScan> {
      const form = new FormData();
      appendFile(form, "page", page, "page.jpg");
      const res = await authed("/scan/binder", { method: "POST", body: form });
      if (res.status === 422) throw { code: "no_cards_found" } satisfies NoCardsFound;
      return parse<BinderScan>(res);
    },

    async saveToCollection(cards: CollectionSaveCard[]): Promise<CollectionSaveOut> {
      return postJson("/collection", { cards });
    },

    async getCollection(): Promise<CollectionOut> {
      return parse(await authed("/collection"));
    },

    async patchCollectionQty(id: string, qty: number): Promise<void> {
      await parse(
        await authed(`/collection/${id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ qty }),
        })
      );
    },

    async deleteCollectionCard(id: string): Promise<void> {
      await parse(await authed(`/collection/${id}`, { method: "DELETE" }));
    },

    // ── Battles ──
    randomBattle: (pullId: string) => postJson<Battle>("/battles/random", { pull_id: pullId }),
    botBattle: (pullId: string) => postJson<Battle>("/battles/bot", { pull_id: pullId }),
    friendBattle: (pullId: string, handle: string) =>
      postJson<Battle>("/battles/friend", { pull_id: pullId, opponent_handle: handle }),
    acceptBattle: (id: string, pullId: string) => postJson<Battle>(`/battles/${id}/accept`, { pull_id: pullId }),
    declineBattle: (id: string) => postJson<Battle>(`/battles/${id}/decline`, {}),
    async listBattles(): Promise<BattleList> {
      return parse(await authed("/battles"));
    },
    async battleInbox(): Promise<Battle[]> {
      return parse(await authed("/battles/inbox"));
    },
  };
}

export type Api = ReturnType<typeof createApi>;
