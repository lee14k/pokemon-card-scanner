import { View } from "react-native";
import { scanStageText } from "@shared/format";
import type { ScanProgressEvent } from "@shared/types";
import { Body, HpBar, Loader, Panel, Pixel, Row, ScreenTitle, colors } from "@/ui/kit";

/** "Identifying…" with an HP bar and a placeholder row per card found. */
export function ScanProgress({ progress }: { progress: Partial<ScanProgressEvent> }) {
  const total = progress.total ?? progress.count;
  return (
    <View style={{ gap: 18 }}>
      <ScreenTitle kicker="Please wait" title="Identifying…" sub={scanStageText(progress)} />
      {typeof total === "number" && total > 0 ? (
        <HpBar value={progress.done ?? 0} max={total} label="ID" color={colors.cyan} />
      ) : (
        <View style={{ alignItems: "center", paddingVertical: 24 }}>
          <Loader />
        </View>
      )}
      {typeof progress.count === "number" &&
        Array.from({ length: progress.count }).map((_, i) => {
          const done = typeof progress.done === "number" && i < progress.done;
          return (
            <Panel key={i} tone={done ? "default" : "pending"} padding={10} depth={3} style={{ opacity: done ? 1 : 0.6 }}>
              <Row gap={12}>
                <View
                  style={{
                    width: 44,
                    height: 61,
                    borderRadius: 5,
                    borderWidth: 2,
                    borderColor: colors.ink,
                    backgroundColor: colors.surface,
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Pixel size={11} tone="muted">
                    {done ? "✓" : "?"}
                  </Pixel>
                </View>
                <View>
                  <Body weight="black">{done ? "Identified" : `Card ${i + 1}`}</Body>
                  <Body size={13} tone="muted">
                    {done ? "Ready for review" : "Reading…"}
                  </Body>
                </View>
              </Row>
            </Panel>
          );
        })}
    </View>
  );
}
