import { useEffect, useRef, useState } from "react";
import { Image, Pressable, View, useWindowDimensions } from "react-native";
import { ApiError } from "@shared/client";
import { priceRange, reasonText } from "@shared/format";
import { FOLLOWUP_POLL_MS, hasIdentity, isFlagged, keepBinderGeometry, mergeLate, settlePending } from "@shared/review";
import type { BinderCard, BinderScan, CollectionSaveCard } from "@shared/types";
import { api } from "@/lib/api";
import { Badge, Body, CardArt, EmptyState, Loader, Panel, Pixel, PixelButton, Row, ScreenTitle, colors, textStyles } from "@/ui/kit";
import { Screen } from "@/ui/Screen";
import { FixCardSheet } from "./FixCardSheet";

/**
 * Review a scanned binder page — mobile twin of the web BinderReview. A flagged
 * cell is saved only once the user fixes it or keeps it; the server makes the
 * final call and reports what it skipped.
 */
export function BinderReview({
  scan,
  photo,
  saving,
  onConfirm,
  onRetake,
  onBack,
}: {
  scan: BinderScan;
  photo: { uri: string; width?: number; height?: number };
  saving?: boolean;
  onConfirm: (cards: CollectionSaveCard[]) => void;
  onRetake: () => void;
  onBack: () => void;
}) {
  const { width: screenW } = useWindowDimensions();
  const [cards, setCards] = useState<BinderCard[]>(scan.cards);
  const [fixing, setFixing] = useState<number | null>(null);
  const [settledRows, setSettledRows] = useState<Set<number>>(new Set());
  const [showFinder, setShowFinder] = useState(true);
  const scanId = scan.scan_id ?? null;

  // Latest values for the poll tick, which must not restart on every patch.
  const cardsRef = useRef(cards);
  const settledRef = useRef(settledRows);
  useEffect(() => {
    cardsRef.current = cards;
    settledRef.current = settledRows;
  });

  useEffect(() => {
    if (!scanId) return;
    let cancelled = false;
    const id = setInterval(async () => {
      if (cancelled || !cardsRef.current.some((c) => c.state === "pending_vlm")) return;
      try {
        const st = await api.getBinderFollowup(scanId);
        if (cancelled) return;
        setCards((prev) => mergeLate(prev, st.cards, settledRef.current, keepBinderGeometry));
        if (st.done) {
          setCards(settlePending);
          cancelled = true;
          clearInterval(id);
        }
      } catch (e) {
        if (!cancelled && e instanceof ApiError && e.status === 404) {
          setCards(settlePending);
          cancelled = true;
          clearInterval(id);
        }
      }
    }, FOLLOWUP_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [scanId]);

  if (cards.length === 0) {
    return (
      <Screen back={onBack} title="Binder page">
        <ScreenTitle kicker="Binder page" title="No cards found" />
        <EmptyState
          icon="grid"
          title="Nothing readable"
          body="We couldn't read any cards on that page. Lay it flat, fill the frame, avoid glare, and try again."
          action={<PixelButton title="Retake photo" variant="primary" onPress={onRetake} />}
        />
      </Screen>
    );
  }

  const cols = Math.max(1, scan.grid.cols || 3);
  const gap = 8;
  const cellW = Math.floor((screenW - 32 - gap * (cols - 1)) / cols);
  const needReview = cards.filter(isFlagged).length;
  const unsettled = cards.filter((c) => isFlagged(c) && !settledRows.has(c.row_index));
  const saveCount = cards.length - unsettled.length;

  // Page photo with each detected cell outlined — a bad segmentation is visible
  // instead of surfacing as mystery cells.
  const natW = photo.width ?? 0;
  const natH = photo.height ?? 0;
  const finderW = screenW - 32 - 30;
  const scale = natW > 0 ? finderW / natW : 0;
  const canDrawFinder = natW > 0 && natH > 0 && cards.some((c) => Array.isArray(c.cell) && c.cell.length === 4);

  return (
    <Screen
      back={onBack}
      title="Binder page"
      footer={
        <>
          <PixelButton title="Retake" onPress={onRetake} style={{ flex: 1 }} />
          <PixelButton
            title={unsettled.length > 0 ? `Save ${saveCount}` : "Save to collection"}
            variant="primary"
            style={{ flex: 2 }}
            loading={saving}
            disabled={saveCount === 0}
            onPress={() =>
              onConfirm(cards.map((c) => (isFlagged(c) && settledRows.has(c.row_index) ? { ...c, confirmed: true } : c)))
            }
          />
        </>
      }
    >
      <ScreenTitle kicker="Binder page" title="Review page" sub="Tap a card to fix it. A flagged card is saved only after you fix it or keep it.">
        <Row gap={8} wrap>
          <Badge label={`${scan.grid.rows}×${scan.grid.cols} PAGE`} tone="gold" />
          <Badge label={`${cards.length} FOUND`} tone="outline" />
          {needReview > 0 ? <Badge label={`${needReview} TO REVIEW`} tone="red" /> : <Badge label="ALL CLEAR" tone="green" />}
        </Row>
      </ScreenTitle>

      {canDrawFinder && (
        <Panel padding={12}>
          <Pressable onPress={() => setShowFinder((s) => !s)} accessibilityRole="button">
            <Row gap={8}>
              <Body size={13} tone="gold">{showFinder ? "▼" : "▶"}</Body>
              <Pixel size={9}>What the scanner found</Pixel>
            </Row>
          </Pressable>
          {showFinder && (
            <View style={{ marginTop: 10, width: finderW, height: natH * scale, borderWidth: 3, borderColor: colors.ink, borderRadius: 8, overflow: "hidden" }}>
              <Image source={{ uri: photo.uri }} style={{ width: finderW, height: natH * scale }} />
              {cards.map((c, i) => {
                if (!Array.isArray(c.cell) || c.cell.length !== 4) return null;
                const [x, y, w, h] = c.cell;
                const tone = isFlagged(c) ? colors.danger : colors.green;
                return (
                  <Pressable
                    key={c.row_index}
                    onPress={() => setFixing(c.row_index)}
                    accessibilityLabel={`Card ${i + 1}`}
                    style={{ position: "absolute", left: x * scale, top: y * scale, width: w * scale, height: h * scale, borderWidth: 2, borderColor: tone }}
                  >
                    <View style={{ backgroundColor: tone, alignSelf: "flex-start", paddingHorizontal: 3 }}>
                      <Body size={10} weight="black" tone="dark">
                        {i + 1}
                      </Body>
                    </View>
                  </Pressable>
                );
              })}
            </View>
          )}
        </Panel>
      )}

      <View style={{ flexDirection: "row", flexWrap: "wrap", gap }}>
        {cards.map((c) => {
          const flagged = isFlagged(c);
          const pending = c.state === "pending_vlm";
          const kept = flagged && settledRows.has(c.row_index);
          const price = priceRange(c.price_usd_low, c.price_usd_high);
          return (
            <Pressable
              key={c.row_index}
              style={{ width: cellW }}
              onPress={() => setFixing(c.row_index)}
              accessibilityRole="button"
              accessibilityLabel={`${c.name ?? "Unknown card"}${c.card_number ? ` ${c.card_number}` : ""} — edit`}
            >
              <Panel tone={kept ? "kept" : flagged ? "flagged" : "default"} padding={5} depth={3}>
                <View style={{ gap: 3 }}>
                  <CardArt uri={c.thumb_b64 ? `data:image/jpeg;base64,${c.thumb_b64}` : null} label={c.name} />
                  <Body size={12} weight="black" numberOfLines={2}>
                    {c.name ?? "Unknown card"}
                  </Body>
                  <Body size={10} tone="muted" numberOfLines={2}>
                    {c.card_number ?? "—"} · {c.set_name ?? "?"}
                  </Body>
                  {price && (
                    <Body style={[textStyles.price, { fontSize: 7 }]} numberOfLines={1}>
                      {price}
                    </Body>
                  )}
                  {pending ? (
                    <Loader small />
                  ) : (
                    flagged && (
                      <>
                        <Body size={10} weight="black" tone="danger" numberOfLines={2}>
                          ! {reasonText(c.low_confidence_reason)}
                        </Body>
                        {kept ? (
                          <Badge label="KEPT" tone="gold" />
                        ) : hasIdentity(c) ? (
                          <PixelButton
                            title="Keep"
                            size="sm"
                            onPress={() => setSettledRows((prev) => new Set(prev).add(c.row_index))}
                          />
                        ) : (
                          <Body size={10} tone="muted">
                            Fix it to add it
                          </Body>
                        )}
                      </>
                    )
                  )}
                </View>
              </Panel>
            </Pressable>
          );
        })}
      </View>

      {unsettled.length > 0 && (
        <Body size={13} weight="bold" tone="danger">
          {unsettled.length} card{unsettled.length === 1 ? "" : "s"} still need{unsettled.length === 1 ? "s" : ""} review and won’t be saved — fix
          {unsettled.length === 1 ? " it" : " them"}, or tap “Keep”.
        </Body>
      )}

      {fixing !== null && (
        <FixCardSheet
          initial={cards.find((c) => c.row_index === fixing)!}
          onApply={(fixed) => {
            // Keep the binder-only fields (geometry + thumbnail) and row_index;
            // the user's answer is final, whatever the background ID says.
            setCards((prev) =>
              prev.map((c) =>
                c.row_index === fixing ? { ...fixed, row_index: c.row_index, cell: c.cell, thumb_b64: c.thumb_b64, state: "ok" } : c
              )
            );
            setSettledRows((prev) => new Set(prev).add(fixing));
            setFixing(null);
          }}
          onCancel={() => setFixing(null)}
        />
      )}
    </Screen>
  );
}
