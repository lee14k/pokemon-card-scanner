import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import type { Trainer } from "@shared/types";
import { api } from "./api";

interface AuthState {
  trainer: Trainer | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, handle: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

// Same contract as the web AuthContext. The session cookie the backend sets on
// login lives in the OS cookie store, so it survives app restarts; `me()` on
// launch tells us whether it is still good.
export function AuthProvider({ children }: { children: ReactNode }) {
  const [trainer, setTrainer] = useState<Trainer | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setTrainer(await api.me());
    } catch {
      setTrainer(null); // offline / server down: act logged-out, retry later
    } finally {
      setLoading(false);
    }
  }, []);

  // Launch check: is the stored session cookie still good?
  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((t) => !cancelled && setTrainer(t))
      .catch(() => !cancelled && setTrainer(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      await api.login(email, password);
      await refresh();
    },
    [refresh]
  );

  const register = useCallback(
    async (email: string, password: string, handle: string) => {
      await api.register(email, password, handle);
      await api.login(email, password);
      await refresh();
    },
    [refresh]
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setTrainer(null);
    }
  }, []);

  return <Ctx.Provider value={{ trainer, loading, login, register, logout, refresh }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used within AuthProvider");
  return v;
}
