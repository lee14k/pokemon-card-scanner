import { Pressable, View } from "react-native";
import { priceRange, reasonText } from "@shared/format";
import { isFlagged } from "@shared/review";
import type { LiveCard } from "@shared/types";
import { Badge, Body, CardArt, Loader, Panel, PixelButton, Row, textStyles } from "@/ui/kit";

export function CardRow({
  card,
  resolved,
  thumbUri,
  onFix,
  onKeep,
}: {
  card: LiveCard;
  resolved: boolean;
  /** Overrides card.image_url — the live flow shows the frame it captured. */
  thumbUri?: string | null;
  onFix: () => void;
  onKeep: () => void;
}) {
  // Pending VLM identification pre-empts the flag: it isn't user-actionable yet.
  const pending = card.state === "pending_vlm" && !resolved;
  const flagged = isFlagged(card) && !resolved && !pending;
  const kept = isFlagged(card) && resolved;
  const price = priceRange(card.price_usd_low, card.price_usd_high);
  const tone = flagged ? "flagged" : pending ? "pending" : kept ? "kept" : "default";

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${card.name ?? "Unknown card"}${card.card_number ? ` ${card.card_number}` : ""} — edit`}
      onPress={onFix}
    >
      <Panel tone={tone} padding={10} depth={3}>
        <Row gap={12} style={{ alignItems: "flex-start" }}>
          <CardArt uri={thumbUri ?? card.image_url} width={60} label={card.name} />
          <View style={{ flex: 1, gap: 3 }}>
            <Body weight="black" size={16}>
              {card.name ?? "Unknown card"}
            </Body>
            {pending ? (
              <Row gap={8}>
                <Loader small />
                <Body size={13} weight="bold" tone="cyan">
                  Still identifying — wait or fix it
                </Body>
              </Row>
            ) : (
              <Body size={13} tone="muted">
                {card.card_number ?? "—"} · {card.set_name ?? "Unknown set"}
                {card.rarity ? ` · ${card.rarity}` : ""}
              </Body>
            )}
            {!pending && (price || kept) && (
              <Row gap={6}>
                {price && <Body style={textStyles.price}>{price}</Body>}
                {kept && <Badge label="KEPT" tone="gold" />}
              </Row>
            )}
            {flagged && (
              <View style={{ gap: 8, marginTop: 4 }}>
                <Body size={13} weight="black" tone="danger">
                  ! {reasonText(card.low_confidence_reason)}
                </Body>
                <Row gap={8}>
                  <PixelButton title="Fix" variant="primary" size="sm" onPress={onFix} />
                  <PixelButton title="Keep anyway" size="sm" onPress={onKeep} />
                </Row>
              </View>
            )}
          </View>
          {!flagged && !pending && (
            <Body size={13} weight="black" tone="muted" style={{ alignSelf: "center" }}>
              Edit
            </Body>
          )}
        </Row>
      </Panel>
    </Pressable>
  );
}
