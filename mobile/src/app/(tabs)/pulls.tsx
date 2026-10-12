import { useCallback, useState } from "react";
import { Pressable, View } from "react-native";
import { router, useFocusEffect } from "expo-router";
import { compactUsd, friendlyError, plural, priceRange, shortDate, shortDateTime, usd } from "@shared/format";
import type { SavedPull } from "@shared/types";
import { api } from "@/lib/api";
import { AppHeader } from "@/features/common/AppHeader";
import { RequireTrainer } from "@/features/common/RequireTrainer";
import { Badge, Body, CardArt, EmptyState, ErrorBox, IconBox, LoadingBlock, Panel, PixelButton, Row, ScreenTitle, StatTile, textStyles } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

function PullsList() {
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setPulls(await api.listPulls());
    } catch (e) {
      setError(friendlyError(e));
    }
  }, []);

  // Refetch whenever the tab comes into view — a scan may have just saved one.
  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  if (error) return <ErrorBox message={error} onRetry={load} />;
  if (!pulls) return <LoadingBlock label="Loading your pulls…" />;
  if (pulls.length === 0)
    return (
      <EmptyState
        icon="pack"
        title="No pulls yet"
        body="Rip open a pack and scan it — it'll show up here."
        action={<PixelButton title="Scan a pack" variant="primary" onPress={() => router.push("/scan/photo")} />}
      />
    );

  const verified = pulls.filter((p) => p.verified).length;
  const value = pulls.reduce((n, p) => n + (p.estimated_value ?? 0), 0);

  return (
    <View style={{ gap: 14 }}>
      <Row gap={10}>
        <StatTile value={pulls.length} label="Packs" />
        <StatTile value={verified} label="Verified" tone="green" />
        <StatTile value={value > 0 ? compactUsd(value) : "—"} label="Value" tone="cyan" />
      </Row>
      {pulls.map((p) => {
        const isOpen = open === p.id;
        return (
          <Pressable key={p.id} onPress={() => setOpen(isOpen ? null : p.id)} accessibilityRole="button" accessibilityState={{ expanded: isOpen }}>
            <Panel padding={12}>
              <Row gap={12}>
                <IconBox name={p.verified ? "check" : "pack"} tone={p.verified ? "green" : "purple"} size={40} />
                <View style={{ flex: 1, gap: 6 }}>
                  <Body weight="black">{shortDateTime(p.created_at)}</Body>
                  <Row gap={6} wrap>
                    <Badge label={plural(p.cards.length, "CARD")} tone="outline" />
                    {p.verified ? <Badge label="VERIFIED" tone="green" /> : <Badge label="UNVERIFIED" tone="outline" />}
                    {p.estimated_value != null && <Body style={textStyles.price}>≈ {usd(p.estimated_value)}</Body>}
                  </Row>
                </View>
                <Body size={14} tone="muted">{isOpen ? "▼" : "▶"}</Body>
              </Row>
              {isOpen && (
                <View style={{ marginTop: 12, gap: 10 }}>
                  <Body size={13} tone="muted">
                    Code {p.code ?? "—"}
                    {p.priced_as_of ? ` · prices as of ${shortDate(p.priced_as_of)}` : ""}
                  </Body>
                  <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                    {p.cards.map((c) => (
                      <View key={c.row_index} style={{ width: "30.5%", gap: 3 }}>
                        <CardArt uri={c.image_url} label={c.name} />
                        <Body size={11} weight="bold" numberOfLines={1}>
                          {c.name ?? c.card_number ?? "?"}
                        </Body>
                        <Body style={[textStyles.price, { fontSize: 7 }]} numberOfLines={1}>
                          {priceRange(c.price_usd_low, c.price_usd_high) ?? "—"}
                        </Body>
                      </View>
                    ))}
                  </View>
                </View>
              )}
            </Panel>
          </Pressable>
        );
      })}
    </View>
  );
}

export default function PullsTab() {
  return (
    <Screen>
      <AppHeader />
      <ScreenTitle kicker="Trainer log" title="My pulls" sub="Every pack you've opened and logged." />
      <RequireTrainer icon="pack" reason="Log in to see the packs you've logged.">
        <PullsList />
      </RequireTrainer>
    </Screen>
  );
}
