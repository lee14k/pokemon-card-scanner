import { useState } from "react";
import { View } from "react-native";
import { router } from "expo-router";
import { isNoCardsFound } from "@shared/client";
import { friendlyError, plural } from "@shared/format";
import type { BinderScan, CollectionSaveCard, CollectionSaveOut } from "@shared/types";
import { api } from "@/lib/api";
import { CameraCapture, type CapturedPhoto } from "@/features/capture/CameraCapture";
import { BinderReview } from "@/features/review/BinderReview";
import { Celebrate, Encounters } from "@/features/scan/Celebrate";
import { Badge, Body, ErrorBox, LoadingBlock, Panel, PixelButton, PixelIcon, Row, ScreenTitle, StatTile, colors } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

type Step =
  | { name: "capture" }
  | { name: "scanning" }
  | { name: "review"; scan: BinderScan; photo: CapturedPhoto; saving?: boolean }
  | { name: "summary"; out: CollectionSaveOut }
  | { name: "error"; message: string };

const EMPTY: BinderScan = { cards: [], grid: { rows: 0, cols: 0 }, page_confidence: 0 };

export default function BinderScanScreen() {
  const [step, setStep] = useState<Step>({ name: "capture" });

  const scan = async (photo: CapturedPhoto) => {
    setStep({ name: "scanning" });
    try {
      setStep({ name: "review", scan: await api.scanBinder(photo), photo });
    } catch (e) {
      // An unreadable page is a retake, not an error.
      if (isNoCardsFound(e)) setStep({ name: "review", scan: EMPTY, photo });
      else setStep({ name: "error", message: friendlyError(e) });
    }
  };

  const save = async (s: Extract<Step, { name: "review" }>, cards: CollectionSaveCard[]) => {
    setStep({ ...s, saving: true });
    try {
      setStep({ name: "summary", out: await api.saveToCollection(cards) });
    } catch (e) {
      setStep({ name: "error", message: friendlyError(e) });
    }
  };

  if (step.name === "review") {
    return (
      <BinderReview
        scan={step.scan}
        photo={step.photo}
        saving={step.saving}
        onConfirm={(cards) => save(step, cards)}
        onRetake={() => setStep({ name: "capture" })}
        onBack={() => setStep({ name: "capture" })}
      />
    );
  }

  return (
    <Screen back title="Binder page">
      {step.name === "capture" && (
        <>
          <ScreenTitle kicker="Binder page" title="Snap your page" sub="Lay the page flat, fill the frame, and avoid glare.">
            <Row gap={8} wrap>
              <Badge label="FLAT PAGE" tone="outline" />
              <Badge label="FILL THE FRAME" tone="outline" />
              <Badge label="NO GLARE" tone="outline" />
            </Row>
          </ScreenTitle>
          <CameraCapture onCapture={scan} />
        </>
      )}

      {step.name === "scanning" && <LoadingBlock label="Reading every card on the page…" />}

      {step.name === "summary" && (
        <>
          <Celebrate kicker="Binder page" title="Saved!" sub="Your collection just got bigger." />
          <Row gap={10}>
            <StatTile value={step.out.added} label="New" tone="green" />
            <StatTile value={step.out.incremented} label="Bumped" tone="cyan" />
            <StatTile value={step.out.total_cards} label="Total" />
          </Row>
          {(step.out.skipped ?? 0) > 0 && (
            // Say so — otherwise the user counts the collection and finds cards missing.
            <Panel tone="warn">
              <Row gap={10} style={{ alignItems: "flex-start" }}>
                <PixelIcon name="warn" color={colors.orange} />
                <Body style={{ flex: 1 }}>
                  {plural(step.out.skipped ?? 0, "card")} skipped because {step.out.skipped === 1 ? "it" : "they"} still needed review.
                  Scan the page again and fix (or keep) {step.out.skipped === 1 ? "it" : "them"} to add {step.out.skipped === 1 ? "it" : "them"}.
                </Body>
              </Row>
            </Panel>
          )}
          <Encounters encounters={step.out.encounters} />
          <View style={{ gap: 12 }}>
            <PixelButton title="Scan another page" icon="grid" variant="primary" size="lg" onPress={() => setStep({ name: "capture" })} />
            <PixelButton title="View collection" icon="chest" onPress={() => router.dismissTo("/collection")} />
          </View>
        </>
      )}

      {step.name === "error" && (
        <>
          <ScreenTitle kicker="Uh oh" title="It fled!" sub="Something went wrong with that page." />
          <ErrorBox message={step.message} />
          <PixelButton title="Try again" variant="primary" onPress={() => setStep({ name: "capture" })} />
        </>
      )}
    </Screen>
  );
}
