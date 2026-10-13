import { useState } from "react";
import { KeyboardAvoidingView, Platform, TextInput, View } from "react-native";
import { router, useLocalSearchParams } from "expo-router";
import { friendlyError } from "@shared/format";
import { useAuth } from "@/lib/auth";
import { Body, Panel, Pixel, PixelButton, colors, inputStyle } from "@/ui/kit";
import { Screen } from "@/ui/Screen";


export default function Login() {
  const { reason } = useLocalSearchParams<{ reason?: string }>();
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "register") await register(email.trim(), password, handle.trim());
      else await login(email.trim(), password);
      if (router.canGoBack()) router.back();
      else router.replace("/");
    } catch (e) {
      setError(friendlyError(e));
    } finally {
      setBusy(false);
    }
  };

  const ready = email.trim() && password.length >= 8 && (mode === "login" || handle.trim().length >= 3);

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen back title={mode === "login" ? "Trainer login" : "New trainer"}>
        <Panel>
          <View style={{ gap: 14 }}>
            <Pixel size={13} tone="gold" shadow>
              {mode === "login" ? "Trainer login" : "Become a trainer"}
            </Pixel>
            <Body tone="muted">
              {mode === "login" ? reason || "Welcome back, trainer!" : "Pick a handle — it's how other trainers challenge you."}
            </Body>
            <View style={{ gap: 6 }}>
              <Body weight="black" size={14}>
                Email
              </Body>
              <TextInput
                style={inputStyle}
                value={email}
                onChangeText={setEmail}
                autoCapitalize="none"
                autoComplete="email"
                keyboardType="email-address"
                textContentType="emailAddress"
                placeholderTextColor={colors.textMuted}
              />
            </View>
            {mode === "register" && (
              <View style={{ gap: 6 }}>
                <Body weight="black" size={14}>
                  Trainer handle
                </Body>
                <TextInput
                  style={inputStyle}
                  value={handle}
                  onChangeText={setHandle}
                  autoCapitalize="none"
                  autoCorrect={false}
                  placeholder="ash_ketchum"
                  placeholderTextColor={colors.textMuted}
                />
                <Body size={12} tone="muted">
                  3–20 characters: a–z, 0–9 or _
                </Body>
              </View>
            )}
            <View style={{ gap: 6 }}>
              <Body weight="black" size={14}>
                Password
              </Body>
              <TextInput
                style={inputStyle}
                value={password}
                onChangeText={setPassword}
                secureTextEntry
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                textContentType={mode === "login" ? "password" : "newPassword"}
                onSubmitEditing={() => ready && submit()}
              />
              {mode === "register" && (
                <Body size={12} tone="muted">
                  At least 8 characters.
                </Body>
              )}
            </View>
            {error && (
              <Body weight="black" tone="danger" accessibilityRole="alert">
                {error}
              </Body>
            )}
            <PixelButton
              title={mode === "login" ? "Log in" : "Sign up"}
              variant="primary"
              loading={busy}
              disabled={!ready}
              onPress={submit}
            />
            <PixelButton
              title={mode === "login" ? "Need an account? Sign up" : "Have an account? Log in"}
              variant="ghost"
              onPress={() => {
                setError(null);
                setMode(mode === "login" ? "register" : "login");
              }}
            />
          </View>
        </Panel>
      </Screen>
    </KeyboardAvoidingView>
  );
}
