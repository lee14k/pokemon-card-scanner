import { useEffect, useRef, useState } from "react";
import { View } from "react-native";
import { ApiError } from "@shared/client";
import { friendlyWarning } from "@shared/format";
import { FOLLOWUP_POLL_MS, isFlagged, mergeLate, settlePending } from "@shared/review";
import type { LiveCard, PackCard, PackScanResponse } from "@shared/types";
import { api } from "@/lib/api";
import { Badge, Body, Panel, Pixel, PixelButton, PixelIcon, Row, ScreenTitle, colors } from "@/ui/kit";
import { Screen } from "@/ui/Screen";
import { CardRow } from "./CardRow";
import { FixCardSheet } from "./FixCardSheet";

/**
 * Review a scanned pack before saving it — the mobile twin of the web
 * ReviewScreen, with the same rules: flagged cards must be fixed or kept,
 * pending_vlm rows don't block and are patched in as the background ID lands.
 */
export function PackReview({
  scan,
  liveSessionId,
  thumbs,
  saving,
  onConfirm,
  onRetake,
  onBack,
}: {
  scan: PackScanResponse;
  liveSessionId?: string;
  /** Local frame per row (live flow) — preferred over catalog art. */
  thumbs?: Record<number, string>;
  saving?: boolean;
  onConfirm: (cards: PackCard[]) => void;
  onRetake: () => void;
  onBack: () => void;
}) {
  const [cards, setCards] = useState<LiveCard[]>(scan.cards);
  const [resolvedRows, setResolvedRows] = useState<Set<number>>(new Set());
  const [fixing, setFixing] = useState<number | null>(null);
  const scanId = scan.scan_id ?? null;

  // Latest values for the poll tick, which must not restart on every patch.
  const cardsRef = useRef(cards);
  const resolvedRef = useRef(resolvedRows);
  useEffect(() => {
    cardsRef.current = cards;
    resolvedRef.current = resolvedRows;
  });

  const markResolved = (row: number) => setResolvedRows((prev) => new Set(prev).add(row));

  // Live scans: liveFinish's response carries no per-row `state`; the session
  // store does. Seed it once so pending rows spin and get patched.
  useEffect(() => {
    if (!liveSessionId) return;
    let cancelled = false;
    api
      .liveState(liveSessionId)
      .then((st) => {
        if (cancelled) return;
        setCards((prev) =>
          prev.map((c) => {
            if (resolvedRef.current.has(c.row_index)) return c;
            const match = st.cards.find((m) => m.row_index === c.row_index);
            return match ? { ...match } : c;
          })
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [liveSessionId]);

  // One poll per review while anything is pending_vlm (see web ReviewScreen).
  useEffect(() => {
    if (!liveSessionId && !scanId) return;
    let cancelled = false;
    const id = setInterval(async () => {
      if (cancelled || !cardsRef.current.some((c) => c.state === "pending_vlm")) return;
      let late: LiveCard[];
      let done = false;
      try {
        if (liveSessionId) late = (await api.liveState(liveSessionId)).cards;
        else {
          const st = await api.getPackFollowup(scanId as string);
          late = st.cards;
          done = st.done;
        }
      } catch (e) {
        // 404 is terminal (entry expired / process restarted): settle and stop.
        if (!cancelled && e instanceof ApiError && e.status === 404) {
          setCards(settlePending);
          cancelled = true;
          clearInterval(id);
        }
        return;
      }
      if (cancelled) return;
      setCards((prev) => mergeLate(prev, late, resolvedRef.current));
      if (done) {
        setCards(settlePending);
        cancelled = true;
        clearInterval(id);
      }
    }, FOLLOWUP_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [liveSessionId, scanId]);

  const unresolved = cards.filter((c) => c.state !== "pending_vlm" && isFlagged(c) && !resolvedRows.has(c.row_index));
  const n = unresolved.length;

  return (
    <Screen
      back={onBack}
      title="Review"
      footer={
        <PixelButton
          style={{ flex: 1 }}
          size="lg"
          variant="primary"
          loading={saving}
          disabled={n > 0}
          title={n > 0 ? `Fix ${n} flagged card${n > 1 ? "s" : ""} first` : "Looks good — save pack"}
          onPress={() => onConfirm(cards)}
        />
      }
    >
      <ScreenTitle
        kicker="Review"
        title="Your pulls"
        sub={n > 0 ? `${n} card${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} a look. Fix or keep anyway.` : "Tap any card to correct it, then save."}
      >
        <Row gap={8}>
          <Badge label={`${cards.length} CARDS`} tone="gold" />
          {n > 0 ? <Badge label={`${n} FLAGGED`} tone="red" /> : <Badge label="ALL CLEAR" tone="green" />}
        </Row>
      </ScreenTitle>

      {scan.segmentation_warning && (
        <Panel tone="warn">
          <Row gap={10} style={{ alignItems: "flex-start" }}>
            <PixelIcon name="warn" color={colors.orange} />
            <Body style={{ flex: 1 }}>{friendlyWarning(scan.segmentation_warning)}</Body>
          </Row>
          <View style={{ alignItems: "flex-end", marginTop: 10 }}>
            <PixelButton title="Retake photo" size="sm" onPress={onRetake} />
          </View>
        </Panel>
      )}

      <View style={{ gap: 10 }}>
        {cards.map((c) => (
          <CardRow
            key={c.row_index}
            card={c}
            resolved={resolvedRows.has(c.row_index)}
            thumbUri={thumbs?.[c.row_index]}
            onFix={() => setFixing(c.row_index)}
            onKeep={() => markResolved(c.row_index)}
          />
        ))}
      </View>

      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          gap: 12,
          padding: 12,
          borderWidth: 3,
          borderStyle: "dashed",
          borderColor: colors.border,
          borderRadius: 10,
        }}
      >
        <PixelIcon name="pack" color={colors.textMuted} />
        <View style={{ flex: 1, gap: 4 }}>
          <Body size={11} weight="black" tone="muted">
            CODE CARD
          </Body>
          <Pixel size={10} tone="gold" selectable>
            {scan.code_card.code ?? "not read"}
          </Pixel>
        </View>
        {scan.code_card.code && !scan.code_card.format_ok && <Badge label="ODD FORMAT" tone="outline" />}
      </View>

      {fixing !== null && (
        <FixCardSheet
          initial={cards.find((c) => c.row_index === fixing)!}
          onApply={(fixed) => {
            setCards((prev) => prev.map((c) => (c.row_index === fixing ? { ...fixed, state: "ok" } : c)));
            markResolved(fixing);
            setFixing(null);
          }}
          onCancel={() => setFixing(null)}
        />
      )}
    </Screen>
  );
}
