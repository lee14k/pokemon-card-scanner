// The live-scan session engine, kept out of React: one in-flight frame at a
// time, duplicate prompts, the code card, pending_vlm polling, and 404 session
// recovery that replays every accepted frame. Mirrors the web LiveScanScreen's
// rules; the screen (src/app/scan/live.tsx) just renders snapshots.

import { ApiError, type NativeFile } from "@shared/client";
import type { CodeCardResult, LiveFrameOut, LiveState, PackCard, PackScanResponse } from "@shared/types";
import { api } from "@/lib/api";
import type { Photo } from "@/lib/images";

export type ChipState = "capturing" | "ok" | "pending_vlm" | "vlm_failed" | "dup_prompt";

export interface Chip {
  clientId: string;
  state: ChipState;
  thumbUri: string;
  row: PackCard | null;
}

export interface LiveSnapshot {
  sid: string | null;
  tray: Chip[];
  code: { result: CodeCardResult; file: Photo } | null;
  recovering: boolean;
}

interface Frame {
  clientId: string;
  card: Photo;
  strip?: Photo;
  capturedAt: number;
}

const POLL_MS = 2000;
const statusOf = (e: unknown) => (e instanceof ApiError ? e.status : undefined);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Survives leaving and re-entering the screen within one app run, like the
// web's sessionStorage key; cleared on cancel and on finish.
let storedSessionId: string | null = null;

export const EMPTY_SNAPSHOT: LiveSnapshot = { sid: null, tray: [], code: null, recovering: false };

export class LiveSession {
  private snap: LiveSnapshot = EMPTY_SNAPSHOT;
  private queue: Frame[] = [];
  private draining = false;
  private recoveringNow = false;
  private accepted: Frame[] = [];
  private dupDecisions = new Map<string, boolean>();
  private nextId = 0;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private disposed = false;

  constructor(
    private readonly onChange: (s: LiveSnapshot) => void,
    private readonly onToast: (msg: string) => void
  ) {}

  // ── state plumbing ──
  private set(patch: Partial<LiveSnapshot>) {
    if (this.disposed) return;
    this.snap = { ...this.snap, ...patch };
    this.onChange(this.snap);
  }
  private setTray(fn: (t: Chip[]) => Chip[]) {
    this.set({ tray: fn(this.snap.tray) });
  }
  private drop(clientId: string, toast?: string) {
    this.setTray((t) => t.filter((c) => c.clientId !== clientId));
    if (toast) this.onToast(toast);
  }
  private update(clientId: string, row: PackCard, state: ChipState) {
    this.setTray((t) => t.map((c) => (c.clientId === clientId ? { ...c, row, state } : c)));
  }
  private hydrate(sid: string, st: LiveState) {
    this.setTray((t) => [
      ...st.cards.map((c) => ({
        clientId: `resumed-${c.row_index}`,
        state: (c.state ?? "ok") as ChipState,
        thumbUri: api.liveCardImageUrl(sid, c.row_index),
        row: c,
      })),
      ...t.filter((c) => c.state === "capturing"),
    ]);
  }
  private setSid(sid: string) {
    storedSessionId = sid;
    this.set({ sid });
  }

  // ── lifecycle ──
  /** Resume the stored session if it's still alive, else open a new one. */
  async start(): Promise<void> {
    if (storedSessionId) {
      try {
        const st = await api.liveState(storedSessionId);
        this.setSid(storedSessionId);
        this.hydrate(storedSessionId, st);
        this.startPolling();
        return;
      } catch {
        storedSessionId = null; // expired — start fresh
      }
    }
    this.setSid(await api.liveStart());
    this.startPolling();
  }

  dispose() {
    this.disposed = true;
    if (this.pollTimer) clearInterval(this.pollTimer);
  }

  /** Abandon the session client-side so the next visit starts fresh. */
  cancel() {
    storedSessionId = null;
    this.dispose();
  }

  // ── capture → queue ──
  capture(card: Photo, strip: Photo) {
    const clientId = `c${this.nextId++}`;
    this.setTray((t) => [...t, { clientId, state: "capturing", thumbUri: card.uri, row: null }]);
    this.queue.push({ clientId, card, strip, capturedAt: Date.now() });
    void this.drain();
  }

  private async drain() {
    if (this.draining) return;
    this.draining = true;
    try {
      for (let f = this.queue.shift(); f; f = this.queue.shift()) await this.process(f);
    } finally {
      this.draining = false;
    }
  }

  private async send(sid: string, f: Frame): Promise<LiveFrameOut> {
    try {
      return await api.liveFrame(sid, f.card, f.strip);
    } catch (e) {
      if (statusOf(e) !== 409) throw e;
      await sleep(500); // session busy with the previous frame — one retry
      return api.liveFrame(sid, f.card, f.strip);
    }
  }

  private async process(f: Frame) {
    const sid = this.snap.sid;
    if (!sid) return this.drop(f.clientId);
    let res: LiveFrameOut;
    try {
      res = await this.send(sid, f);
    } catch (e) {
      if (statusOf(e) === 404) return this.recover(f);
      return this.drop(f.clientId, "Server was busy — that card didn't save, show it again");
    }
    switch (res.event) {
      case "card": {
        const card = res.card;
        if (!card) return this.drop(f.clientId);
        this.accepted.push(f);
        const state: ChipState = res.pending_vlm ? "pending_vlm" : "ok";
        // A same-identity re-snap inside the server's dedup window refines the
        // row already on screen instead of adding a twin.
        const existing = this.snap.tray.find(
          (c) => c.clientId !== f.clientId && c.row?.row_index === card.row_index && c.state !== "capturing"
        );
        if (existing) {
          this.update(existing.clientId, card, state);
          this.drop(f.clientId);
        } else this.update(f.clientId, card, state);
        return;
      }
      case "code_card":
        this.accepted.push(f);
        if (res.code_card) this.set({ code: { result: res.code_card, file: f.card } });
        this.onToast("Code card caught!");
        return this.drop(f.clientId);
      case "duplicate_prompt":
        if (!res.card) return this.drop(f.clientId);
        this.accepted.push(f);
        return this.update(f.clientId, res.card, "dup_prompt");
      default:
        return this.drop(f.clientId, "Didn't catch that — show it again");
    }
  }

  // ── pending_vlm poll ──
  private startPolling() {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = setInterval(async () => {
      const sid = this.snap.sid;
      if (this.disposed || !sid || !this.snap.tray.some((c) => c.state === "pending_vlm")) return;
      try {
        const st = await api.liveState(sid);
        this.setTray((t) =>
          t.map((chip) => {
            const match = chip.row && st.cards.find((c) => c.row_index === chip.row!.row_index);
            return match ? { ...chip, state: (match.state ?? chip.state) as ChipState, row: match } : chip;
          })
        );
      } catch (e) {
        if (statusOf(e) === 404) await this.recover();
      }
    }, POLL_MS);
  }

  // ── recovery ──
  // The session died server-side (TTL, restart): open a new one and replay every
  // accepted frame, keeping their original spacing (capped at 2.5s) so the
  // server's 2s dedup window treats real re-shows as copies, not refinements.
  private async recover(requeue?: Frame) {
    if (this.recoveringNow) {
      if (requeue) this.queue.unshift(requeue);
      return;
    }
    this.recoveringNow = true;
    this.set({ recovering: true });
    this.onToast("Session expired — restarting…");
    try {
      const sid = await api.liveStart();
      this.setSid(sid);
      const frames = this.accepted;
      for (let i = 0; i < frames.length; i++) {
        if (i > 0) await sleep(Math.min(Math.max(0, frames[i].capturedAt - frames[i - 1].capturedAt), 2500));
        try {
          const res = await api.liveFrame(sid, frames[i].card, frames[i].strip);
          const decision = this.dupDecisions.get(frames[i].clientId);
          if (res.event === "duplicate_prompt" && res.card && decision !== undefined) {
            await api.liveDuplicate(sid, res.card.row_index, decision);
          }
        } catch {
          // one frame failing to replay shouldn't sink the rest
        }
      }
      try {
        this.hydrate(sid, await api.liveState(sid));
      } catch {
        // reconcile is best effort
      }
      if (requeue) this.queue.unshift(requeue);
    } catch {
      this.onToast("Couldn't restart the session — check your connection.");
    } finally {
      this.recoveringNow = false;
      this.set({ recovering: false });
      void this.drain();
    }
  }

  // ── duplicates ──
  async resolveDuplicate(chip: Chip, add: boolean) {
    const sid = this.snap.sid;
    if (!sid || !chip.row) return;
    this.dupDecisions.set(chip.clientId, add);
    try {
      await api.liveDuplicate(sid, chip.row.row_index, add);
    } catch (e) {
      if (statusOf(e) === 404) await this.recover();
    }
    if (add) this.setTray((t) => t.map((c) => (c.clientId === chip.clientId ? { ...c, state: "ok" } : c)));
    else this.drop(chip.clientId);
  }

  // ── finish ──
  /** Close the session server-side; on a swept session, rebuild it and retry once. */
  async finish(): Promise<{ scan: PackScanResponse; sid: string; thumbs: Record<number, string>; code: NativeFile | null }> {
    const once = async (sid: string) => {
      const scan = await api.liveFinish(sid);
      storedSessionId = null;
      // Local frames beat server images for the review thumbnails (no network).
      const thumbs: Record<number, string> = {};
      for (const c of this.snap.tray) if (c.row && !c.thumbUri.startsWith("http")) thumbs[c.row.row_index] = c.thumbUri;
      return { scan, sid, thumbs, code: this.snap.code?.file ?? null };
    };
    const sid = this.snap.sid;
    if (!sid) throw new Error("no live session");
    try {
      return await once(sid);
    } catch (e) {
      if (statusOf(e) !== 404) throw e;
      await this.recover();
      if (!this.snap.sid) throw e;
      return once(this.snap.sid);
    }
  }
}
