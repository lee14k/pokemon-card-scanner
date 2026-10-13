import { useState } from "react";
import { View } from "react-native";
import { router } from "expo-router";
import { friendlyError, plural } from "@shared/format";
import type { Encounter, PackCard, PackScanResponse, ScanProgressEvent } from "@shared/types";
import { api, scanPackStream } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { CameraCapture, type CapturedPhoto } from "@/features/capture/CameraCapture";
import { PackReview } from "@/features/review/PackReview";
import { Celebrate, Encounters } from "@/features/scan/Celebrate";
import { ScanProgress } from "@/features/scan/ScanProgress";
import { Badge, Body, ErrorBox, PixelButton, Row, ScreenTitle, StatTile } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

type Step =
  | { name: "staircase" }
  | { name: "code"; staircase: CapturedPhoto }
  | { name: "submitting"; progress: Partial<ScanProgressEvent> }
  | { name: "review"; scan: PackScanResponse; staircase: CapturedPhoto; code: CapturedPhoto; saving?: boolean }
  | { name: "summary"; verified: boolean; count: number; encounters: Encounter[]; pullId: string }
  | { name: "error"; message: string };

export default function PhotoScan() {
  const { trainer } = useAuth();
  const [step, setStep] = useState<Step>({ name: "staircase" });

  const submit = async (staircase: CapturedPhoto, code: CapturedPhoto) => {
    setStep({ name: "submitting", progress: {} });
    const onProgress = (ev: ScanProgressEvent) =>
      setStep((prev) => (prev.name === "submitting" ? { ...prev, progress: { ...prev.progress, ...ev } } : prev));
    try {
      let scan: PackScanResponse;
      try {
        scan = await scanPackStream(staircase, code, undefined, onProgress);
      } catch {
        // Stream hiccup → the plain scan, same as the web app's fallback.
        setStep({ name: "submitting", progress: {} });
        scan = await api.scanPack(staircase, code);
      }
      setStep({ name: "review", scan, staircase, code });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  const save = async (s: Extract<Step, { name: "review" }>, cards: PackCard[]) => {
    if (!trainer) {
      // The review stays mounted under the login modal, so its edits survive.
      router.push({ pathname: "/login", params: { reason: "Log in to save this pack to your pulls." } });
      return;
    }
    setStep({ ...s, saving: true });
    try {
      const saved = await api.savePull(s.staircase, s.code, cards, {
        capture_path: "upload",
        pack_confidence: s.scan.pack_confidence,
        segmentation_warning: s.scan.segmentation_warning,
        capture_meta: null,
      });
      setStep({ name: "summary", verified: saved.verified, count: saved.cards.length, encounters: saved.encounters ?? [], pullId: saved.id });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  if (step.name === "review") {
    return (
      <PackReview
        scan={step.scan}
        saving={step.saving}
        onConfirm={(cards) => save(step, cards)}
        onRetake={() => setStep({ name: "staircase" })}
        onBack={() => setStep({ name: "staircase" })}
      />
    );
  }

  return (
    <Screen back={step.name === "code" ? () => setStep({ name: "staircase" }) : true} title="One photo">
      {step.name === "staircase" && (
        <>
          <ScreenTitle
            kicker="One photo · Step 1/2"
            title="Snap your pack"
            sub="Fan your cards in a staircase so each card's bottom edge — number and set symbol — shows, then take one photo."
          >
            <Row gap={8} wrap>
              <Badge label="GOOD LIGHT" tone="outline" />
              <Badge label="NO GLARE" tone="outline" />
              <Badge label="EDGES VISIBLE" tone="outline" />
            </Row>
          </ScreenTitle>
          <CameraCapture onCapture={(p) => setStep({ name: "code", staircase: p })} />
        </>
      )}

      {step.name === "code" && (
        <>
          <ScreenTitle
            kicker="One photo · Step 2/2"
            title="Code card"
            sub="Fill the gold box with the pack's code card. It verifies the pull so it can battle and count in stats."
          />
          <CameraCapture guide="code" onCapture={(p) => submit(step.staircase, p)} />
        </>
      )}

      {step.name === "submitting" && <ScanProgress progress={step.progress} />}

      {step.name === "summary" && (
        <>
          <Celebrate kicker="Pack opened" title="Pack logged!" sub={`${plural(step.count, "card")} saved to your pulls.`} />
          <Row gap={10}>
            <StatTile value={step.count} label="Cards" />
            <StatTile value={step.verified ? "YES" : "NO"} label="Verified" tone={step.verified ? "green" : "danger"} />
          </Row>
          {!step.verified && (
            <Body size={13} tone="muted">
              Unverified — the code card was a duplicate or unreadable, so this pull can’t battle or count toward pull-rate stats.
            </Body>
          )}
          <Encounters encounters={step.encounters} />
          <View style={{ gap: 12 }}>
            <PixelButton title="Scan another pack" icon="camera" variant="primary" size="lg" onPress={() => setStep({ name: "staircase" })} />
            {step.verified && (
              <PixelButton
                title="Battle this pack"
                icon="swords"
                variant="gold"
                onPress={() => router.dismissTo({ pathname: "/battles", params: { pull: step.pullId } })}
              />
            )}
            <PixelButton title="My pulls" icon="pack" onPress={() => router.dismissTo("/pulls")} />
          </View>
        </>
      )}

      {step.name === "error" && (
        <>
          <ScreenTitle kicker="Uh oh" title="It fled!" sub="Something went wrong with that scan." />
          <ErrorBox message={step.message} />
          <PixelButton title="Start over" variant="primary" onPress={() => setStep({ name: "staircase" })} />
        </>
      )}
    </Screen>
  );
}
