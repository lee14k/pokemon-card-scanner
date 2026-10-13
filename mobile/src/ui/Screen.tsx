import type { ReactNode } from "react";
import { RefreshControl, ScrollView, View, type StyleProp, type ViewStyle } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { router } from "expo-router";
import { DotGrid, Pixel, PixelButton, colors } from "./kit";

/**
 * Scrolling screen body on the dot-matrix background.
 * `back` adds a top bar with a ◀ button (stack screens); tab screens leave it
 * off and get the safe-area inset instead.
 */
export function Screen({
  children,
  back,
  title,
  scroll = true,
  footer,
  refreshing,
  onRefresh,
  contentStyle,
}: {
  children: ReactNode;
  back?: boolean | (() => void);
  title?: string;
  scroll?: boolean;
  /** Pinned below the scroll area (sticky actions). */
  footer?: ReactNode;
  refreshing?: boolean;
  onRefresh?: () => void;
  contentStyle?: StyleProp<ViewStyle>;
}) {
  const insets = useSafeAreaInsets();
  const body = (
    <View style={[{ padding: 16, gap: 20, paddingBottom: footer ? 16 : 32 }, contentStyle]}>{children}</View>
  );
  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <DotGrid />
      <View style={{ paddingTop: insets.top }}>
        {back && (
          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 6,
              paddingHorizontal: 8,
              paddingVertical: 6,
              borderBottomWidth: 3,
              borderBottomColor: colors.ink,
            }}
          >
            <PixelButton
              title="◀ Back"
              variant="ghost"
              size="sm"
              onPress={() => (typeof back === "function" ? back() : router.back())}
            />
            {title && (
              <Pixel size={10} tone="muted" numberOfLines={1} style={{ flex: 1 }}>
                {title}
              </Pixel>
            )}
          </View>
        )}
      </View>
      {scroll ? (
        <ScrollView
          style={{ flex: 1 }}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            onRefresh ? (
              <RefreshControl refreshing={!!refreshing} onRefresh={onRefresh} tintColor={colors.gold} colors={[colors.gold]} />
            ) : undefined
          }
        >
          {body}
        </ScrollView>
      ) : (
        <View style={{ flex: 1 }}>{body}</View>
      )}
      {footer && (
        <View
          style={{
            paddingHorizontal: 16,
            paddingTop: 10,
            paddingBottom: Math.max(12, back ? insets.bottom : 12),
            backgroundColor: colors.bgElevated,
            borderTopWidth: 3,
            borderTopColor: colors.ink,
            flexDirection: "row",
            gap: 10,
          }}
        >
          {footer}
        </View>
      )}
    </View>
  );
}
