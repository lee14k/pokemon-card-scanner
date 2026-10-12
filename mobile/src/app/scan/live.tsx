// Live scan on the phone: one card at a time, tap CATCH for each. The session
// engine lives in features/live/LiveSession.ts; this screen renders it. What the
// phone can't do in Expo is per-frame pixel analysis, so there's no auto-fire
// like the web version — the shutter is the trigger.

import { useEffect, useRef, useState } from "react";
import { Image, View } from "react-native";
import { router } from "expo-router";
import { captureRef } from "react-native-view-shot";
import type { NativeFile } from "@shared/client";
import { friendlyError, plural, priceRange } from "@shared/format";
import type { Encounter, PackCard, PackScanResponse } from "@shared/types";
import { api } from "@/lib/api";
import { cropLiveFrame } from "@/lib/images";
import { CameraCapture } from "@/features/capture/CameraCapture";
import { EMPTY_SNAPSHOT, LiveSession, type Chip, type LiveSnapshot } from "@/features/live/LiveSession";
import { PackReview } from "@/features/review/PackReview";
import { Celebrate, Encounters } from "@/features/scan/Celebrate";
import {
  Badge,
  Body,
  CardArt,
  ErrorBox,
  Loader,
  LoadingBlock,
  Panel,
  PixelButton,
  Row,
  ScreenTitle,
  SectionTitle,
  StatTile,
  TextBox,
  textStyles,
} from "@/ui/kit";
import { Screen } from "@/ui/Screen";

const COMPOSITE_CELL = 110;
const MAX_COMPOSITE = 100;

type Phase =
  | { name: "init" }
  | { name: "scanning" }
  | { name: "code_choice" }
  | { name: "finishing" }
  | { name: "review"; scan: PackScanResponse; sid: string; composite: NativeFile; code: NativeFile | null; thumbs: Record<number, string>; saving?: boolean }
  | { name: "summary"; verified: boolean; count: number; encounters: Encounter[]; pullId: string }
  | { name: "error"; message: string };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default function LiveScan() {
  const [phase, setPhase] = useState<Phase>({ name: "init" });
  const [snap, setSnap] = useState<LiveSnapshot>(EMPTY_SNAPSHOT);
  const [toast, setToast] = useState<{ msg: string; seq: number } | null>(null);
  const [cropping, setCropping] = useState(false);
  const [session] = useState(
    () => new LiveSession(setSnap, (msg) => setToast((prev) => ({ msg, seq: (prev?.seq ?? 0) + 1 })))
  );
  const compositeView = useRef<View>(null);
  const compositeReady = useRef<(() => void) | null>(null);

  useEffect(() => {
    session
      .start()
      .then(() => setPhase({ name: "scanning" }))
      .catch((e) => setPhase({ name: "error", message: friendlyError(e) }));
    return () => session.dispose();
  }, [session]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(t);
  }, [toast]);

  const say = (msg: string) => setToast((prev) => ({ msg, seq: (prev?.seq ?? 0) + 1 }));

  const onRawCapture = async (uri: string, width: number, height: number) => {
    setCropping(true);
    try {
      const { card, strip } = await cropLiveFrame(uri, width, height);
      session.capture(card, strip);
    } catch {
      say("Couldn't read that photo — try again");
    } finally {
      setCropping(false);
    }
  };

  const cancel = () => {
    session.cancel();
    router.back();
  };

  const usable = snap.tray.filter((c) => c.state !== "dup_prompt" && c.state !== "capturing");

  async function buildComposite(): Promise<NativeFile> {
    const fallback: NativeFile = { uri: usable[0]?.thumbUri ?? snap.code?.file.uri ?? "", name: "composite.jpg", type: "image/jpeg" };
    try {
      // Wait for the grid's thumbnails to paint (or give up after a beat).
      await Promise.race([new Promise<void>((r) => (compositeReady.current = r)), sleep(2500)]);
      const uri = await captureRef(compositeView, { format: "jpg", quality: 0.85, result: "tmpfile" });
      return { uri, name: "composite.jpg", type: "image/jpeg" };
    } catch {
      return fallback;
    }
  }

  async function finish() {
    setPhase({ name: "finishing" });
    const composite = await buildComposite();
    try {
      const out = await session.finish();
      setPhase({ name: "review", composite, ...out });
    } catch {
      say("Couldn't finish the pack — try again.");
      setPhase({ name: "scanning" });
    }
  }

  async function save(p: Extract<Phase, { name: "review" }>, cards: PackCard[]) {
    setPhase({ ...p, saving: true });
    try {
      const saved = await api.savePull(p.composite, p.code ?? p.composite, cards, {
        capture_path: "live",
        pack_confidence: p.scan.pack_confidence,
        segmentation_warning: p.scan.segmentation_warning,
        capture_meta: null,
        live_session_id: p.sid,
      });
      setPhase({ name: "summary", verified: saved.verified, count: saved.cards.length, encounters: saved.encounters ?? [], pullId: saved.id });
    } catch (e) {
      setPhase({ name: "error", message: friendlyError(e) });
    }
  }

  if (phase.name === "review") {
    return (
      <PackReview
        scan={phase.scan}
        liveSessionId={phase.sid}
        thumbs={phase.thumbs}
        saving={phase.saving}
        onConfirm={(cards) => save(phase, cards)}
        onRetake={() => router.back()}
        onBack={() => router.back()}
      />
    );
  }

  // A code card landing while the "scan the code card?" prompt is up answers it.
  const view = phase.name === "code_choice" && snap.code ? "scanning" : phase.name;
  const busy = snap.recovering || snap.tray.some((c) => c.state === "capturing");
  const count = snap.tray.filter((c) => c.state !== "dup_prompt").length;
  const cols = Math.max(1, Math.ceil(Math.sqrt(Math.min(usable.length, MAX_COMPOSITE) || 1)));

  return (
    <Screen
      back={cancel}
      title="Live scan"
      footer={
        view === "scanning" || view === "code_choice" ? (
          <>
            <PixelButton title="Cancel" onPress={cancel} style={{ flex: 1 }} />
            <PixelButton
              title="Finish"
              variant="primary"
              style={{ flex: 2 }}
              disabled={view !== "scanning" || busy || count === 0}
              onPress={() => (snap.code ? finish() : setPhase({ name: "code_choice" }))}
            />
          </>
        ) : undefined
      }
    >
      {view === "init" && <LoadingBlock label="Starting camera session…" />}

      {(view === "scanning" || view === "code_choice") && (
        <>
          <ScreenTitle kicker="Live scan" title="Show me your cards" sub="Hold one card inside the box and tap CATCH. Catch the code card too.">
            <Row gap={8}>
              <Badge label={plural(count, "CARD")} tone="gold" />
              {snap.code ? <Badge label="CODE ✓" tone="green" /> : <Badge label="NO CODE YET" tone="outline" />}
            </Row>
          </ScreenTitle>

          <CameraCapture guide="live" allowUpload={false} onRawCapture={onRawCapture} busy={cropping} shutterLabel="CATCH" />

          {toast && (
            <TextBox>
              <Body weight="black" tone="dark">
                {toast.msg}
              </Body>
            </TextBox>
          )}

          {view === "code_choice" && (
            <Panel tone="warn">
              <View style={{ gap: 10 }}>
                <Body>
                  <Body weight="black">Scan the code card too?</Body> It’s needed to battle and to count in stats.
                </Body>
                <Row gap={10}>
                  <PixelButton title="Keep scanning" variant="primary" size="sm" onPress={() => setPhase({ name: "scanning" })} />
                  <PixelButton title="Save anyway" size="sm" onPress={finish} />
                </Row>
              </View>
            </Panel>
          )}

          {snap.tray.length > 0 && <SectionTitle>Caught this session</SectionTitle>}
          <View style={{ gap: 10 }}>
            {[...snap.tray].reverse().map((chip) => (
              <ChipRow key={chip.clientId} chip={chip} onDuplicate={(add) => session.resolveDuplicate(chip, add)} />
            ))}
          </View>
        </>
      )}

      {view === "finishing" && (
        <>
          <LoadingBlock label="Building your pull…" />
          {/* The composite pull photo: this grid is what gets captured. */}
          <View style={{ alignItems: "center" }}>
            <View
              ref={compositeView}
              collapsable={false}
              style={{ width: cols * COMPOSITE_CELL, flexDirection: "row", flexWrap: "wrap", backgroundColor: "#111318" }}
            >
              <CompositeCells chips={usable.slice(0, MAX_COMPOSITE)} onReady={() => compositeReady.current?.()} />
            </View>
          </View>
        </>
      )}

      {phase.name === "summary" && (
        <>
          <Celebrate kicker="Live scan" title="Pack logged!" sub={`${plural(phase.count, "card")} saved to your pulls.`} />
          <Row gap={10}>
            <StatTile value={phase.count} label="Cards" />
            <StatTile value={phase.verified ? "YES" : "NO"} label="Verified" tone={phase.verified ? "green" : "danger"} />
          </Row>
          <Encounters encounters={phase.encounters} />
          <View style={{ gap: 12 }}>
            {phase.verified && (
              <PixelButton
                title="Battle this pack"
                icon="swords"
                variant="gold"
                onPress={() => router.dismissTo({ pathname: "/battles", params: { pull: phase.pullId } })}
              />
            )}
            <PixelButton title="My pulls" icon="pack" variant="primary" onPress={() => router.dismissTo("/pulls")} />
          </View>
        </>
      )}

      {phase.name === "error" && (
        <>
          <ScreenTitle kicker="Uh oh" title="It fled!" sub="The live session hit a snag." />
          <ErrorBox message={phase.message} />
          <PixelButton title="Back" variant="primary" onPress={cancel} />
        </>
      )}
    </Screen>
  );
}

function ChipRow({ chip, onDuplicate }: { chip: Chip; onDuplicate: (add: boolean) => void }) {
  const row = chip.row;
  const isDup = chip.state === "dup_prompt";
  const price = priceRange(row?.price_usd_low, row?.price_usd_high);
  return (
    <Panel tone={isDup ? "flagged" : chip.state === "capturing" || chip.state === "pending_vlm" ? "pending" : "default"} padding={10} depth={3}>
      <Row gap={12} style={{ alignItems: "flex-start" }}>
        <CardArt uri={chip.thumbUri} width={52} label={row?.name} />
        <View style={{ flex: 1, gap: 3 }}>
          {chip.state === "capturing" ? (
            <Row gap={8}>
              <Loader small />
              <Body weight="black" tone="cyan">
                Reading…
              </Body>
            </Row>
          ) : (
            <>
              <Body weight="black">{row?.name ?? (chip.state === "pending_vlm" ? "Identifying…" : "Unknown card")}</Body>
              <Body size={13} tone="muted">
                {row?.card_number ?? "—"} · {row?.set_name ?? "Unknown set"}
              </Body>
              <Row gap={6}>
                {price && <Body style={textStyles.price}>{price}</Body>}
                {chip.state === "pending_vlm" && <Badge label="IDENTIFYING" tone="cyan" />}
                {chip.state === "vlm_failed" && <Badge label="NEEDS REVIEW" tone="red" />}
              </Row>
            </>
          )}
          {isDup && row && (
            <View style={{ gap: 8, marginTop: 4 }}>
              <Body size={13} weight="black" tone="danger">
                Another copy of {row.name ?? "this card"}?
              </Body>
              <Row gap={8}>
                <PixelButton title="Add" size="sm" variant="primary" onPress={() => onDuplicate(true)} />
                <PixelButton title="Ignore" size="sm" variant="ghost" onPress={() => onDuplicate(false)} />
              </Row>
            </View>
          )}
        </View>
      </Row>
    </Panel>
  );
}

/** Thumbnails for the composite; calls onReady once every image has settled. */
function CompositeCells({ chips, onReady }: { chips: Chip[]; onReady: () => void }) {
  const [settled, setSettled] = useState(0);
  useEffect(() => {
    if (settled >= chips.length) onReady();
  }, [settled, chips.length, onReady]);
  return (
    <>
      {chips.map((c) => (
        <View key={c.clientId} style={{ width: COMPOSITE_CELL, height: COMPOSITE_CELL, padding: 4 }}>
          <Image source={{ uri: c.thumbUri }} style={{ flex: 1 }} resizeMode="contain" onLoadEnd={() => setSettled((n) => n + 1)} />
        </View>
      ))}
    </>
  );
}
