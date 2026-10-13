import { Pressable, View } from "react-native";
import { router, type Href } from "expo-router";
import type { PixelIconName } from "@shared/pixelIcons";
import { useAuth } from "@/lib/auth";
import { AppHeader } from "@/features/common/AppHeader";
import { HeroDemo } from "@/features/scan/HeroDemo";
import { Badge, Body, IconBox, Panel, Pixel, Row, ScreenTitle, colors, tap } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

function ModeOption({
  icon,
  tone,
  title,
  body,
  badge,
  onPress,
}: {
  icon: PixelIconName;
  tone: "red" | "cyan" | "green";
  title: string;
  body: string;
  badge?: { label: string; tone: "gold" | "outline" };
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${title}. ${body}`}
      onPress={() => {
        tap();
        onPress();
      }}
    >
      {({ pressed }) => (
        <Panel style={{ transform: [{ translateY: pressed ? 2 : 0 }] }} padding={14}>
          <Row gap={14}>
            <IconBox name={icon} tone={tone} />
            <View style={{ flex: 1, gap: 6 }}>
              <Row gap={8} wrap>
                <Pixel size={11}>{title}</Pixel>
                {badge && <Badge label={badge.label} tone={badge.tone} />}
              </Row>
              <Body tone="muted" size={14}>
                {body}
              </Body>
            </View>
            <Body size={16} tone={pressed ? "gold" : "muted"}>▶</Body>
          </Row>
        </Panel>
      )}
    </Pressable>
  );
}

export default function ScanHome() {
  const { trainer } = useAuth();

  // Live + binder scans run against the trainer's session server-side.
  const gated = (path: Href, reason: string) => () =>
    trainer ? router.push(path) : router.push({ pathname: "/login", params: { reason } });

  return (
    <Screen>
      <AppHeader />
      <View style={{ gap: 14 }}>
        <Row gap={8}>
          <Badge label="NEW GAME" tone="red" />
          <Badge label="1 PLAYER" tone="outline" />
        </Row>
        <Pixel size={18} shadow style={{ lineHeight: 30 }}>
          Scan a whole pack in <Pixel size={18} tone="gold" shadow style={{ lineHeight: 30 }}>one shot</Pixel>
        </Pixel>
        <Body tone="muted">Snap your pull, and we identify, price and log every card — then fill your Pokédex and battle it.</Body>
      </View>

      <HeroDemo />

      <ScreenTitle kicker="Player 1 ready" title="Select mode" />
      <View style={{ gap: 14 }}>
        <ModeOption
          icon="camera"
          tone="red"
          title="One photo"
          badge={{ label: "FASTEST", tone: "gold" }}
          body="Fan a whole pack in a staircase and snap it once."
          onPress={() => router.push("/scan/photo")}
        />
        <ModeOption
          icon="bolt"
          tone="cyan"
          title="Live"
          badge={trainer ? undefined : { label: "LOGIN", tone: "outline" }}
          body="Show cards one at a time and tap to catch each one."
          onPress={gated("/scan/live", "Log in to use live scan.")}
        />
        <ModeOption
          icon="grid"
          tone="green"
          title="Binder page"
          badge={trainer ? undefined : { label: "LOGIN", tone: "outline" }}
          body="Snap a full binder page straight into your collection."
          onPress={gated("/scan/binder", "Log in to scan binder pages into your collection.")}
        />
      </View>
      <View style={{ height: 4, backgroundColor: colors.bg }} />
    </Screen>
  );
}
