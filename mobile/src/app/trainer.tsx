import { View } from "react-native";
import { router } from "expo-router";
import { useAuth } from "@/lib/auth";
import { API_BASE } from "@/lib/config";
import { Badge, Body, Panel, Pixel, PixelButton, colors } from "@/ui/kit";
import { Screen } from "@/ui/Screen";

export default function TrainerCard() {
  const { trainer, logout } = useAuth();
  return (
    <Screen back title="Trainer card">
      <Panel>
        <View style={{ flexDirection: "row", gap: 14, alignItems: "center" }}>
          <View
            style={{
              width: 64,
              height: 64,
              borderRadius: 10,
              borderWidth: 3,
              borderColor: colors.ink,
              backgroundColor: colors.gold,
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Pixel size={22} tone="dark">
              {(trainer?.handle ?? "?").slice(0, 1).toUpperCase()}
            </Pixel>
          </View>
          <View style={{ flex: 1, gap: 6 }}>
            <Pixel size={12} shadow numberOfLines={1}>
              @{trainer?.handle ?? "guest"}
            </Pixel>
            <Body tone="muted" numberOfLines={1}>
              {trainer?.email ?? "Not logged in"}
            </Body>
            {trainer && <Badge label={trainer.role.toUpperCase()} tone={trainer.role === "trainer" ? "outline" : "gold"} />}
          </View>
        </View>
      </Panel>

      <Panel>
        <View style={{ gap: 6 }}>
          <Body weight="black" size={13} tone="muted">
            SERVER
          </Body>
          <Body selectable>{API_BASE || "(same origin)"}</Body>
          <Body size={12} tone="muted">
            Set EXPO_PUBLIC_API_BASE to point the app at another backend.
          </Body>
        </View>
      </Panel>

      {trainer ? (
        <PixelButton
          title="Log out"
          variant="danger"
          icon="close"
          onPress={async () => {
            await logout();
            router.back();
          }}
        />
      ) : (
        <PixelButton title="Log in" variant="primary" onPress={() => router.replace("/login")} />
      )}
    </Screen>
  );
}
