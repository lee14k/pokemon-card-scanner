import { useEffect, useState } from "react";
import { Animated, Easing, View } from "react-native";
import type { Encounter } from "@shared/types";
import { Badge, Body, Panel, Pixel, Row, TextBox, celebrate, colors } from "@/ui/kit";

const CONFETTI = [colors.gold, colors.cyan, colors.primary, colors.green, "#a78bfa"];

/** Title card with falling pixel confetti and a success buzz. */
export function Celebrate({ kicker, title, sub }: { kicker: string; title: string; sub: string }) {
  const [fall] = useState(() => new Animated.Value(0));
  const [pop] = useState(() => new Animated.Value(0));
  useEffect(() => {
    celebrate();
    Animated.parallel([
      Animated.timing(fall, { toValue: 1, duration: 2200, easing: Easing.in(Easing.quad), useNativeDriver: true }),
      Animated.spring(pop, { toValue: 1, friction: 4, useNativeDriver: true }),
    ]).start();
  }, [fall, pop]);

  return (
    <Panel padding={24}>
      <View style={{ alignItems: "center", gap: 10, overflow: "hidden" }}>
        {Array.from({ length: 16 }).map((_, i) => (
          <Animated.View
            key={i}
            pointerEvents="none"
            style={{
              position: "absolute",
              top: -10,
              left: `${(i * 61) % 100}%`,
              width: 8,
              height: 8,
              backgroundColor: CONFETTI[i % CONFETTI.length],
              opacity: fall.interpolate({ inputRange: [0, 0.85, 1], outputRange: [1, 1, 0] }),
              transform: [
                { translateY: fall.interpolate({ inputRange: [0, 1], outputRange: [0, 160 + ((i * 37) % 60)] }) },
                { rotate: fall.interpolate({ inputRange: [0, 1], outputRange: ["0deg", `${(i % 2 ? 1 : -1) * 270}deg`] }) },
              ],
            }}
          />
        ))}
        <Pixel size={9} tone="gold">
          {kicker.toUpperCase()}
        </Pixel>
        <Animated.View style={{ transform: [{ scale: pop.interpolate({ inputRange: [0, 1], outputRange: [0.4, 1] }) }] }}>
          <Pixel size={18} tone="gold" shadow style={{ textAlign: "center", lineHeight: 30 }}>
            {title}
          </Pixel>
        </Animated.View>
        <Body tone="muted" style={{ textAlign: "center" }}>
          {sub}
        </Body>
      </View>
    </Panel>
  );
}

export function Encounters({ encounters }: { encounters: Encounter[] }) {
  if (encounters.length === 0) return null;
  return (
    <View style={{ gap: 10 }}>
      {encounters.map((e) => (
        <TextBox key={e.species}>
          {e.new ? (
            <View style={{ gap: 6 }}>
              <Badge label="NEW!" tone="gold" />
              <Body weight="black" tone="dark">
                {e.species} registered to your Pokédex!
              </Body>
            </View>
          ) : (
            <Row gap={8} wrap>
              <Body weight="bold" tone="dark">
                You saw a wild <Body weight="black" tone="dark">{e.species}</Body> again!
              </Body>
              <Badge label={`×${e.count}`} tone="outline" />
            </Row>
          )}
        </TextBox>
      ))}
    </View>
  );
}
