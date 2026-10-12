import { Pressable, View } from "react-native";
import { Tabs } from "expo-router";
import type { BottomTabBarProps } from "expo-router/tabs";
import type { PixelIconName } from "@shared/pixelIcons";
import { Pixel, PixelIcon, colors, tap } from "@/ui/kit";

const TABS: Record<string, { icon: PixelIconName; label: string }> = {
  index: { icon: "camera", label: "SCAN" },
  pulls: { icon: "pack", label: "PULLS" },
  dex: { icon: "dex", label: "DEX" },
  collection: { icon: "chest", label: "CARDS" },
  battles: { icon: "swords", label: "BATTLE" },
};

function PixelTabBar({ state, navigation, insets }: BottomTabBarProps) {
  return (
    <View
      style={{
        flexDirection: "row",
        backgroundColor: colors.bgElevated,
        borderTopWidth: 3,
        borderTopColor: colors.ink,
        paddingTop: 6,
        paddingBottom: Math.max(8, insets.bottom),
        paddingHorizontal: 6,
      }}
    >
      {state.routes.map((route, i) => {
        const tab = TABS[route.name];
        if (!tab) return null;
        const focused = state.index === i;
        const tint = focused ? colors.gold : colors.textMuted;
        return (
          <Pressable
            key={route.key}
            accessibilityRole="tab"
            accessibilityState={{ selected: focused }}
            accessibilityLabel={tab.label}
            onPress={() => {
              const ev = navigation.emit({ type: "tabPress", target: route.key, canPreventDefault: true });
              if (!focused && !ev.defaultPrevented) {
                tap();
                navigation.navigate(route.name);
              }
            }}
            style={{
              flex: 1,
              alignItems: "center",
              gap: 6,
              paddingVertical: 6,
              borderRadius: 8,
              backgroundColor: focused ? "rgba(255,210,63,0.1)" : "transparent",
            }}
          >
            <PixelIcon name={tab.icon} size={24} color={tint} />
            <Pixel size={7} style={{ color: tint }}>
              {tab.label}
            </Pixel>
          </Pressable>
        );
      })}
    </View>
  );
}

export default function TabsLayout() {
  return (
    <Tabs
      tabBar={(props) => <PixelTabBar {...props} />}
      screenOptions={{ headerShown: false, sceneStyle: { backgroundColor: colors.bg } }}
    >
      <Tabs.Screen name="index" />
      <Tabs.Screen name="pulls" />
      <Tabs.Screen name="dex" />
      <Tabs.Screen name="collection" />
      <Tabs.Screen name="battles" />
    </Tabs>
  );
}
