import { useEffect } from "react";
import { View } from "react-native";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import * as SplashScreen from "expo-splash-screen";
import { useFonts } from "expo-font";
import { PressStart2P_400Regular } from "@expo-google-fonts/press-start-2p";
import { Nunito_600SemiBold, Nunito_800ExtraBold, Nunito_900Black } from "@expo-google-fonts/nunito";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { colors, fonts } from "@shared/theme";
import { AuthProvider } from "@/lib/auth";

SplashScreen.preventAutoHideAsync().catch(() => {});

export default function RootLayout() {
  const [loaded, error] = useFonts({
    [fonts.pixel]: PressStart2P_400Regular,
    [fonts.body]: Nunito_600SemiBold,
    [fonts.bodyBold]: Nunito_800ExtraBold,
    [fonts.bodyBlack]: Nunito_900Black,
  });

  useEffect(() => {
    if (loaded || error) SplashScreen.hideAsync().catch(() => {});
  }, [loaded, error]);

  if (!loaded && !error) return <View style={{ flex: 1, backgroundColor: colors.bg }} />;

  return (
    <SafeAreaProvider>
      <AuthProvider>
        <StatusBar style="light" />
        <Stack
          screenOptions={{
            headerShown: false,
            contentStyle: { backgroundColor: colors.bg },
            animation: "slide_from_right",
          }}
        >
          <Stack.Screen name="(tabs)" />
          <Stack.Screen name="login" options={{ presentation: "modal", animation: "slide_from_bottom" }} />
          <Stack.Screen name="trainer" options={{ presentation: "modal", animation: "slide_from_bottom" }} />
          <Stack.Screen name="scan/photo" options={{ gestureEnabled: false }} />
          <Stack.Screen name="scan/live" options={{ gestureEnabled: false }} />
          <Stack.Screen name="scan/binder" options={{ gestureEnabled: false }} />
        </Stack>
      </AuthProvider>
    </SafeAreaProvider>
  );
}
