// "Pixel Pop" for React Native — the same look as the web app (frontend/src/
// App.css), built from the shared tokens in shared/src/theme.ts.
//
// RN has no zero-blur box-shadow that works on Android, so the chunky hard
// drop shadow is a layer: an ink-coloured backing View with the face sitting on
// top of it, offset by `depth`. Pressing a button slides the face down into it.

import { useEffect, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Animated,
  Image,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type TextProps,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import * as Haptics from "expo-haptics";
import Svg, { Circle, Defs, Path, Pattern, Rect } from "react-native-svg";
import { colors, fonts, radius, shadowDepth } from "@shared/theme";
import { PIXEL_ICON_SIZE, pixelPath, type PixelIconName } from "@shared/pixelIcons";

export { colors, fonts };

export function tap(style: Haptics.ImpactFeedbackStyle = Haptics.ImpactFeedbackStyle.Light) {
  if (Platform.OS === "web") return;
  Haptics.impactAsync(style).catch(() => {});
}

export function celebrate() {
  if (Platform.OS === "web") return;
  Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
}

// ── Type ─────────────────────────────────────────────────────────────────────

type Tone = "default" | "muted" | "gold" | "danger" | "green" | "cyan" | "dark" | "white";
const TONE: Record<Tone, string> = {
  default: colors.text,
  muted: colors.textMuted,
  gold: colors.gold,
  danger: colors.danger,
  green: colors.green,
  cyan: colors.cyan,
  dark: colors.onBright,
  white: "#fff",
};

export function Pixel({
  size = 10,
  tone = "default",
  shadow,
  style,
  ...rest
}: TextProps & { size?: number; tone?: Tone; shadow?: boolean }) {
  return (
    <Text
      {...rest}
      style={[
        {
          fontFamily: fonts.pixel,
          fontSize: size,
          lineHeight: Math.round(size * 1.6),
          color: TONE[tone],
          letterSpacing: 0.4,
        },
        shadow && styles.textShadow,
        style,
      ]}
    />
  );
}

export function Body({
  size = 15,
  tone = "default",
  weight = "regular",
  style,
  ...rest
}: TextProps & { size?: number; tone?: Tone; weight?: "regular" | "bold" | "black" }) {
  return (
    <Text
      {...rest}
      style={[
        {
          fontFamily: weight === "black" ? fonts.bodyBlack : weight === "bold" ? fonts.bodyBold : fonts.body,
          fontSize: size,
          lineHeight: Math.round(size * 1.4),
          color: TONE[tone],
        },
        style,
      ]}
    />
  );
}

// ── Icons ────────────────────────────────────────────────────────────────────

export function PixelIcon({ name, size = 24, color = colors.text }: { name: PixelIconName; size?: number; color?: string }) {
  return (
    <Svg width={size} height={size} viewBox={`0 0 ${PIXEL_ICON_SIZE} ${PIXEL_ICON_SIZE}`}>
      <Path d={pixelPath(name)} fill={color} />
    </Svg>
  );
}

export function IconBox({
  name,
  tone = "gold",
  size = 46,
}: {
  name: PixelIconName;
  tone?: "gold" | "red" | "cyan" | "green" | "purple";
  size?: number;
}) {
  const bg = { gold: colors.gold, red: colors.primary, cyan: colors.cyan, green: colors.green, purple: "#a78bfa" }[tone];
  return (
    <View style={{ paddingBottom: 3, backgroundColor: colors.ink, borderRadius: 8 }}>
      <View
        style={{
          width: size,
          height: size,
          borderRadius: 8,
          borderWidth: 3,
          borderColor: colors.ink,
          backgroundColor: bg,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <PixelIcon name={name} size={size * 0.5} color={tone === "red" ? "#fff" : colors.onBright} />
      </View>
    </View>
  );
}

// ── Buttons ──────────────────────────────────────────────────────────────────

type Variant = "primary" | "secondary" | "gold" | "ghost" | "danger";
const FACE: Record<Variant, { bg: string; fg: Tone; border: string }> = {
  primary: { bg: colors.primary, fg: "white", border: colors.ink },
  secondary: { bg: colors.surface, fg: "default", border: colors.ink },
  gold: { bg: colors.gold, fg: "dark", border: colors.ink },
  ghost: { bg: "transparent", fg: "muted", border: "transparent" },
  danger: { bg: "transparent", fg: "danger", border: colors.danger },
};

export function PixelButton({
  title,
  onPress,
  variant = "secondary",
  size = "md",
  icon,
  disabled,
  loading,
  style,
  pixelFont,
  accessibilityLabel,
}: {
  title: string;
  onPress?: () => void;
  variant?: Variant;
  size?: "sm" | "md" | "lg";
  icon?: PixelIconName;
  disabled?: boolean;
  loading?: boolean;
  style?: StyleProp<ViewStyle>;
  /** Render the label in the pixel face (for marquee buttons like PRESS START). */
  pixelFont?: boolean;
  accessibilityLabel?: string;
}) {
  const face = FACE[variant];
  const flat = variant === "ghost";
  const depth = size === "sm" ? 3 : shadowDepth;
  const minHeight = size === "sm" ? 34 : size === "lg" ? 58 : 46;
  const fontSize = size === "sm" ? 13 : size === "lg" ? 17 : 15;
  const inactive = disabled || loading;

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityState={{ disabled: !!inactive }}
      disabled={inactive}
      onPress={() => {
        if (variant === "primary" || variant === "gold") tap();
        onPress?.();
      }}
      style={[{ opacity: inactive ? 0.5 : 1 }, style]}
    >
      {({ pressed }) => {
        const down = pressed && !flat;
        return (
          <View
            style={{
              backgroundColor: flat ? "transparent" : colors.ink,
              borderRadius: radius.md,
              paddingBottom: flat ? 0 : down ? 1 : depth,
              marginTop: down ? depth - 1 : 0,
            }}
          >
            <View
              style={{
                minHeight,
                paddingHorizontal: size === "sm" ? 12 : 18,
                paddingVertical: 6,
                borderRadius: radius.md,
                borderWidth: flat ? 0 : size === "sm" ? 2 : 3,
                borderColor: face.border,
                backgroundColor: flat && pressed ? "rgba(255,255,255,0.06)" : face.bg,
                flexDirection: "row",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
                overflow: "hidden",
              }}
            >
              {!flat && <View pointerEvents="none" style={styles.keyHighlight} />}
              {loading ? (
                <ActivityIndicator color={TONE[face.fg]} />
              ) : (
                <>
                  {icon && <PixelIcon name={icon} size={size === "sm" ? 14 : 18} color={TONE[face.fg]} />}
                  {pixelFont ? (
                    <Pixel size={size === "lg" ? 13 : 11} tone={face.fg}>
                      {title}
                    </Pixel>
                  ) : (
                    <Body size={fontSize} weight="black" tone={face.fg} style={{ textAlign: "center" }}>
                      {title}
                    </Body>
                  )}
                </>
              )}
            </View>
          </View>
        );
      }}
    </Pressable>
  );
}

// ── Panels ───────────────────────────────────────────────────────────────────

type PanelTone = "default" | "flagged" | "warn" | "kept" | "pending" | "light";
const PANEL_RULE: Record<PanelTone, string> = {
  default: colors.border,
  flagged: colors.danger,
  warn: colors.orange,
  kept: colors.gold,
  pending: colors.cyan,
  light: colors.border,
};

/** Dialog-box panel: ink outline, inner coloured rule, hard drop shadow. */
export function Panel({
  children,
  tone = "default",
  style,
  padding = 16,
  depth = shadowDepth,
}: {
  children: ReactNode;
  tone?: PanelTone;
  style?: StyleProp<ViewStyle>;
  padding?: number;
  depth?: number;
}) {
  const light = tone === "light";
  return (
    <View style={[{ backgroundColor: colors.ink, borderRadius: radius.lg, paddingBottom: depth }, style]}>
      <View
        style={{
          borderRadius: radius.lg,
          borderWidth: 3,
          borderColor: colors.ink,
          backgroundColor: light ? colors.text : tone === "flagged" ? "#2b1c3e" : colors.bgElevated,
        }}
      >
        <View
          style={{
            borderRadius: radius.lg - 3,
            borderWidth: 2,
            borderColor: PANEL_RULE[tone],
            padding,
          }}
        >
          {children}
        </View>
      </View>
    </View>
  );
}

/** RPG text box — white, double-ruled, blinking caret. */
export function TextBox({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const [blink] = useState(() => new Animated.Value(1));
  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(blink, { toValue: 0, duration: 0, delay: 450, useNativeDriver: true }),
        Animated.timing(blink, { toValue: 1, duration: 0, delay: 450, useNativeDriver: true }),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [blink]);
  return (
    <Panel tone="light" style={style} padding={12}>
      <View style={{ paddingRight: 18 }}>{children}</View>
      <Animated.View style={[styles.caret, { opacity: blink }]} />
    </Panel>
  );
}

// ── Small pieces ─────────────────────────────────────────────────────────────

type BadgeTone = "gold" | "green" | "red" | "cyan" | "outline" | "plain";
export function Badge({ label, tone = "plain" }: { label: string; tone?: BadgeTone }) {
  const bg = { gold: colors.gold, green: colors.green, red: colors.primary, cyan: colors.cyan, outline: "transparent", plain: colors.surface }[tone];
  const fg: Tone = tone === "red" ? "white" : tone === "outline" ? "muted" : tone === "plain" ? "default" : "dark";
  return (
    <View
      style={{
        backgroundColor: bg,
        borderWidth: 2,
        borderColor: tone === "outline" ? colors.border : colors.ink,
        borderRadius: 4,
        paddingHorizontal: 6,
        paddingTop: 3,
        paddingBottom: 1,
        alignSelf: "flex-start",
      }}
    >
      <Pixel size={8} tone={fg}>
        {label}
      </Pixel>
    </View>
  );
}

export function HpBar({
  value,
  max,
  label = "HP",
  color = colors.green,
  showValue = true,
  flip,
}: {
  value: number;
  max: number;
  label?: string;
  color?: string;
  showValue?: boolean;
  flip?: boolean;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <View style={{ flexDirection: flip ? "row-reverse" : "row", alignItems: "center", gap: 8 }}>
      {!!label && (
        <Pixel size={8} tone="gold">
          {label}
        </Pixel>
      )}
      <View style={styles.hpTrack}>
        <View style={{ width: `${pct * 100}%`, height: "100%", backgroundColor: color, alignSelf: flip ? "flex-end" : "flex-start" }}>
          <View style={{ height: 3, backgroundColor: "rgba(255,255,255,0.35)" }} />
        </View>
      </View>
      {showValue && (
        <Pixel size={8}>
          {value}/{max}
        </Pixel>
      )}
    </View>
  );
}

/** Three blocks hopping in turn. */
export function Loader({ small }: { small?: boolean }) {
  const [vals] = useState(() => [0, 1, 2].map(() => new Animated.Value(0)));
  useEffect(() => {
    const anims = vals.map((v, i) =>
      Animated.loop(
        Animated.sequence([
          Animated.delay(i * 150),
          Animated.timing(v, { toValue: 1, duration: 180, useNativeDriver: true }),
          Animated.timing(v, { toValue: 0, duration: 180, useNativeDriver: true }),
          Animated.delay(450 - i * 150),
        ])
      )
    );
    anims.forEach((a) => a.start());
    return () => anims.forEach((a) => a.stop());
  }, [vals]);
  const s = small ? 6 : 9;
  const tones = [colors.gold, colors.cyan, colors.primary];
  return (
    <View style={{ flexDirection: "row", gap: small ? 4 : 6, height: s * 2.2, alignItems: "flex-end" }} accessibilityRole="progressbar">
      {vals.map((v, i) => (
        <Animated.View
          key={i}
          style={{
            width: s,
            height: s,
            backgroundColor: tones[i],
            transform: [{ translateY: v.interpolate({ inputRange: [0, 1], outputRange: [0, -s] }) }],
          }}
        />
      ))}
    </View>
  );
}

export function LoadingBlock({ label }: { label: string }) {
  return (
    <View style={{ alignItems: "center", gap: 14, paddingVertical: 36 }}>
      <Loader />
      <Pixel size={9} tone="muted" style={{ textAlign: "center" }}>
        {label}
      </Pixel>
    </View>
  );
}

export function StatTile({ value, label, tone = "gold" }: { value: string | number; label: string; tone?: Tone }) {
  // Pixel glyphs are wide; step the size down for longer values rather than
  // relying on adjustsFontSizeToFit (iOS-only).
  const text = String(value);
  const size = text.length > 6 ? 10 : text.length > 4 ? 12 : 14;
  return (
    <Panel style={{ flex: 1 }} padding={12}>
      <Pixel size={size} tone={tone} shadow numberOfLines={1} style={{ minHeight: 22 }}>
        {text}
      </Pixel>
      <Body size={11} weight="black" tone="muted" style={{ marginTop: 6, letterSpacing: 0.8 }}>
        {label.toUpperCase()}
      </Body>
    </Panel>
  );
}

export function ScreenTitle({ kicker, title, sub, children }: { kicker?: string; title: string; sub?: string; children?: ReactNode }) {
  return (
    <View style={{ gap: 8 }}>
      {kicker && (
        <Pixel size={9} tone="gold" style={{ letterSpacing: 1 }}>
          {kicker.toUpperCase()}
        </Pixel>
      )}
      <Pixel size={17} shadow accessibilityRole="header">
        {title}
      </Pixel>
      {sub && (
        <Body size={15} tone="muted">
          {sub}
        </Body>
      )}
      {children}
    </View>
  );
}

export function SectionTitle({ children }: { children: string }) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 8, marginTop: 4 }}>
      <Pixel size={10} tone="muted">
        {children}
      </Pixel>
      <View style={{ flex: 1, flexDirection: "row", gap: 4, overflow: "hidden", height: 3 }}>
        {Array.from({ length: 40 }).map((_, i) => (
          <View key={i} style={{ width: 6, height: 3, backgroundColor: colors.border }} />
        ))}
      </View>
    </View>
  );
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: PixelIconName;
  title: string;
  body?: string;
  action?: ReactNode;
}) {
  return (
    <Panel padding={24}>
      <View style={{ alignItems: "center", gap: 14 }}>
        <PixelIcon name={icon} size={48} color={colors.border} />
        <Pixel size={11} style={{ textAlign: "center" }}>
          {title}
        </Pixel>
        {body && (
          <Body tone="muted" style={{ textAlign: "center" }}>
            {body}
          </Body>
        )}
        {action}
      </View>
    </Panel>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Panel tone="flagged">
      <View style={{ flexDirection: "row", gap: 10, alignItems: "center" }}>
        <PixelIcon name="warn" color={colors.danger} />
        <Body weight="black" tone="danger" style={{ flex: 1 }}>
          {message}
        </Body>
      </View>
      {onRetry && (
        <View style={{ alignItems: "flex-end", marginTop: 10 }}>
          <PixelButton title="Try again" size="sm" onPress={onRetry} />
        </View>
      )}
    </Panel>
  );
}

export function CardArt({
  uri,
  width,
  label,
  style,
}: {
  uri: string | null | undefined;
  width?: number;
  label?: string | null;
  style?: StyleProp<ViewStyle>;
}) {
  const box: ViewStyle = {
    width: width ?? "100%",
    aspectRatio: 63 / 88,
    borderRadius: 6,
    borderWidth: 2,
    borderColor: colors.ink,
    backgroundColor: colors.surface,
    overflow: "hidden",
    alignItems: "center",
    justifyContent: "center",
  };
  return (
    <View style={[box, style]}>
      {uri ? (
        <Image source={{ uri }} style={StyleSheet.absoluteFill} resizeMode="cover" accessibilityLabel={label ?? "card"} />
      ) : (
        <Pixel size={width && width < 70 ? 10 : 12} tone="muted" style={{ textAlign: "center", padding: 4 }}>
          ?
        </Pixel>
      )}
    </View>
  );
}

/** Faint LCD dot-matrix behind every screen. */
export function DotGrid() {
  return (
    <Svg style={StyleSheet.absoluteFill} pointerEvents="none">
      <Defs>
        <Pattern id="dots" width={14} height={14} patternUnits="userSpaceOnUse">
          <Circle cx={7} cy={7} r={1} fill="rgba(255,255,255,0.055)" />
        </Pattern>
      </Defs>
      <Rect width="100%" height="100%" fill="url(#dots)" />
    </Svg>
  );
}

export function Row({ children, gap = 10, style, wrap }: { children: ReactNode; gap?: number; style?: StyleProp<ViewStyle>; wrap?: boolean }) {
  return <View style={[{ flexDirection: "row", alignItems: "center", gap, flexWrap: wrap ? "wrap" : "nowrap" }, style]}>{children}</View>;
}

export const inputStyle = {
  minHeight: 48,
  paddingHorizontal: 14,
  borderWidth: 3,
  borderColor: colors.ink,
  borderRadius: 8,
  backgroundColor: "#100c24",
  color: colors.text,
  fontFamily: fonts.bodyBold,
  fontSize: 16,
  minWidth: 0,
} as const;

export const textStyles = StyleSheet.create({
  price: { fontFamily: fonts.pixel, fontSize: 9, lineHeight: 14, color: colors.gold } as TextStyle,
});

const styles = StyleSheet.create({
  textShadow: {
    textShadowColor: colors.ink,
    textShadowOffset: { width: 2, height: 2 },
    textShadowRadius: 0.1,
  },
  keyHighlight: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: 2,
    backgroundColor: "rgba(255,255,255,0.16)",
  },
  caret: {
    position: "absolute",
    right: 14,
    bottom: 14,
    width: 0,
    height: 0,
    borderLeftWidth: 6,
    borderRightWidth: 6,
    borderTopWidth: 7,
    borderLeftColor: "transparent",
    borderRightColor: "transparent",
    borderTopColor: colors.primary,
  },
  hpTrack: {
    flex: 1,
    height: 16,
    borderWidth: 3,
    borderColor: colors.ink,
    borderRadius: 4,
    backgroundColor: "#0d0a1f",
    overflow: "hidden",
  },
});
