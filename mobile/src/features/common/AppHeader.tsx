import { Pressable, View } from "react-native";
import { router } from "expo-router";
import { useAuth } from "@/lib/auth";
import { Body, Pixel, PixelButton, PixelIcon, colors } from "@/ui/kit";

/** Logo + trainer chip, shown at the top of every tab. */
export function AppHeader() {
  const { trainer, loading } = useAuth();
  return (
    <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
        <View style={{ paddingBottom: 3, backgroundColor: colors.ink, borderRadius: 7 }}>
          <View
            style={{
              width: 34,
              height: 34,
              borderRadius: 7,
              borderWidth: 3,
              borderColor: colors.ink,
              backgroundColor: colors.primary,
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <PixelIcon name="pack" size={18} color="#fff" />
          </View>
        </View>
        <View>
          <Pixel size={10}>Pack Scanner</Pixel>
          <Pixel size={7} tone="gold">
            Gotta scan ’em all
          </Pixel>
        </View>
      </View>
      {!loading &&
        (trainer ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Trainer @${trainer.handle}`}
            onPress={() => router.push("/trainer")}
            style={{
              flexDirection: "row",
              alignItems: "center",
              gap: 8,
              paddingVertical: 4,
              paddingLeft: 4,
              paddingRight: 10,
              borderRadius: 8,
              borderWidth: 2,
              borderColor: colors.ink,
              backgroundColor: colors.surface,
            }}
          >
            <View
              style={{
                width: 26,
                height: 26,
                borderRadius: 5,
                borderWidth: 2,
                borderColor: colors.ink,
                backgroundColor: colors.gold,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Pixel size={9} tone="dark">
                {trainer.handle.slice(0, 1).toUpperCase()}
              </Pixel>
            </View>
            <Body size={14} weight="black" numberOfLines={1} style={{ maxWidth: 110 }}>
              @{trainer.handle}
            </Body>
          </Pressable>
        ) : (
          <PixelButton title="Log in" variant="gold" size="sm" icon="trainer" onPress={() => router.push("/login")} />
        ))}
    </View>
  );
}
