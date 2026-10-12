import { useCallback, useState } from "react";
import { View, useWindowDimensions } from "react-native";
import { router, useFocusEffect } from "expo-router";
import { friendlyError, shortDate } from "@shared/format";
import type { DexOut } from "@shared/types";
import { api } from "@/lib/api";
import { AppHeader } from "@/features/common/AppHeader";
import { RequireTrainer } from "@/features/common/RequireTrainer";
import { Badge, Body, CardArt, EmptyState, ErrorBox, LoadingBlock, Panel, Pixel, PixelButton, Row, ScreenTitle } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

function DexGrid() {
  const { width } = useWindowDimensions();
  const [dex, setDex] = useState<DexOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDex(await api.getDex());
    } catch (e) {
      setError(friendlyError(e));
    }
  }, []);
  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  if (error) return <ErrorBox message={error} onRetry={load} />;
  if (!dex) return <LoadingBlock label="Booting Pokédex…" />;
  if (dex.entries.length === 0)
    return (
      <EmptyState
        icon="dex"
        title="No Pokémon seen yet"
        body="Scan a pack and every Pokémon in it registers here."
        action={<PixelButton title="Scan a pack" variant="primary" onPress={() => router.push("/scan/photo")} />}
      />
    );

  const cols = width > 600 ? 4 : 2;
  const tileW = (width - 32 - 12 * (cols - 1)) / cols;
  return (
    <View style={{ gap: 14 }}>
      <Panel padding={12}>
        <Row style={{ justifyContent: "space-between" }}>
          <Body weight="black" tone="muted" size={12}>
            SEEN
          </Body>
          <Pixel size={16} tone="gold" shadow>
            {String(dex.seen_count).padStart(3, "0")}
          </Pixel>
        </Row>
      </Panel>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 12 }}>
        {dex.entries.map((e, i) => (
          <Panel key={e.species} style={{ width: tileW }} padding={8}>
            <View style={{ gap: 6 }}>
              {/* entries arrive newest-first; number them in discovery order */}
              <Pixel size={8} tone="muted">
                No.{String(dex.entries.length - i).padStart(3, "0")}
              </Pixel>
              <CardArt uri={e.image_url} label={e.species} />
              <Body weight="black" numberOfLines={1}>
                {e.species}
              </Body>
              <Row style={{ justifyContent: "space-between" }}>
                <Badge label={`×${e.count}`} tone="outline" />
                <Body size={11} tone="muted">
                  {shortDate(e.first_seen)}
                </Body>
              </Row>
            </View>
          </Panel>
        ))}
      </View>
    </View>
  );
}

export default function DexTab() {
  return (
    <Screen>
      <AppHeader />
      <ScreenTitle kicker="Pokédex" title="Gotta see ’em all" sub="Every species you've pulled — newest discoveries first." />
      <RequireTrainer icon="dex" reason="Log in to fill your Pokédex.">
        <DexGrid />
      </RequireTrainer>
    </Screen>
  );
}
