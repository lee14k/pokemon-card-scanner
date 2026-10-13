import Constants from "expo-constants";
import { Platform } from "react-native";

/**
 * Where the backend lives.
 *
 * 1. EXPO_PUBLIC_API_BASE wins (set it for production builds, e.g. the Railway
 *    URL — see mobile/.env.example).
 * 2. In development, assume the API runs on port 8000 of the machine serving the
 *    Expo bundle: the phone already reaches that host, so this "just works" on a
 *    LAN with `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
 */
function devBase(): string {
  const hostUri = Constants.expoConfig?.hostUri; // e.g. "192.168.1.20:8081"
  if (hostUri) return `http://${hostUri.split(":")[0]}:8000`;
  if (Platform.OS === "web" && typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  // Android emulator's alias for the host machine; iOS simulator shares it.
  return Platform.OS === "android" ? "http://10.0.2.2:8000" : "http://localhost:8000";
}

export const API_BASE: string =
  (process.env.EXPO_PUBLIC_API_BASE ?? "").replace(/\/$/, "") || (__DEV__ ? devBase() : "");
