import { useEffect, useState } from "react";
import { Animated, Easing, View } from "react-native";
import { Badge, colors } from "@/ui/kit";

const CARDS = 5;
const ART = ["#ff9f1c", "#10b981", "#3b82f6", "#a855f7", "#d62839"];
const H = 210;
const CARD_H = 92;
const STEP = 22;

/** The landing loop: cards fan into a staircase, a scan beam sweeps, ✓. */
export function HeroDemo() {
  const [fan] = useState(() => new Animated.Value(0));
  const [beam] = useState(() => new Animated.Value(0));
  const [found] = useState(() => new Animated.Value(0));

  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(fan, { toValue: 1, duration: 900, easing: Easing.out(Easing.back(1.4)), useNativeDriver: true }),
        Animated.delay(250),
        Animated.timing(beam, { toValue: 1, duration: 1100, easing: Easing.linear, useNativeDriver: true }),
        Animated.timing(found, { toValue: 1, duration: 180, useNativeDriver: true }),
        Animated.delay(1600),
        Animated.parallel([
          Animated.timing(found, { toValue: 0, duration: 200, useNativeDriver: true }),
          Animated.timing(fan, { toValue: 0, duration: 500, useNativeDriver: true }),
          Animated.timing(beam, { toValue: 0, duration: 0, useNativeDriver: true }),
        ]),
        Animated.delay(300),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [fan, beam, found]);

  return (
    <View
      accessibilityRole="image"
      accessibilityLabel="Animated demo: cards fan into a staircase, a scan beam sweeps across, and all five are identified"
      style={{ backgroundColor: colors.ink, borderRadius: 14, paddingBottom: 4 }}
    >
      <View
        style={{
          height: H,
          borderRadius: 14,
          borderWidth: 3,
          borderColor: colors.ink,
          backgroundColor: "#1f1a4d",
          overflow: "hidden",
          alignItems: "center",
        }}
      >
        <View style={{ position: "absolute", inset: 5, borderRadius: 9, borderWidth: 2, borderColor: "#2b2470" }} />
        {Array.from({ length: CARDS }).map((_, i) => (
          <Animated.View
            key={i}
            style={{
              position: "absolute",
              top: 14,
              width: 66,
              height: CARD_H,
              zIndex: CARDS - i,
              transform: [{ translateY: fan.interpolate({ inputRange: [0, 1], outputRange: [0, i * STEP] }) }],
            }}
          >
            <View style={{ flex: 1, borderRadius: 6, borderWidth: 3, borderColor: colors.ink, backgroundColor: colors.text, padding: 4, gap: 5 }}>
              <View style={{ height: 40, borderRadius: 3, borderWidth: 2, borderColor: colors.ink, backgroundColor: ART[i] }} />
              <View style={{ height: 3, backgroundColor: "#8f88b8", width: "80%" }} />
              <View style={{ height: 3, backgroundColor: "#8f88b8", width: "60%" }} />
            </View>
          </Animated.View>
        ))}
        <Animated.View
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 0,
            height: 4,
            backgroundColor: colors.cyan,
            opacity: beam.interpolate({ inputRange: [0, 0.02, 0.98, 1], outputRange: [0, 1, 1, 0] }),
            transform: [{ translateY: beam.interpolate({ inputRange: [0, 1], outputRange: [0, H] }) }],
            zIndex: 20,
          }}
        />
        <Animated.View style={{ position: "absolute", top: 12, right: 12, zIndex: 21, opacity: found, transform: [{ scale: found.interpolate({ inputRange: [0, 1], outputRange: [0.7, 1] }) }] }}>
          <Badge label="✓ 5 CARDS" tone="green" />
        </Animated.View>
      </View>
    </View>
  );
}
