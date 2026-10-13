import { useState } from "react";
import { friendlyError } from "@shared/format";
import { useAuth } from "./AuthContext";

export default function AuthForms({ onDone, reason }: { onDone?: () => void; reason?: string }) {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "register") await register(email, password, handle);
      else await login(email, password);
      onDone?.();
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form auth-form" onSubmit={submit}>
      <p className="muted">
        {reason && mode === "login" ? reason : mode === "login" ? "Welcome back, trainer!" : "New here? Pick a trainer handle."}
      </p>
      <label>
        Email
        <input type="email" autoComplete="email" value={email} required onChange={(e) => setEmail(e.target.value)} />
      </label>
      {mode === "register" && (
        <label>
          Trainer handle
          <input
            value={handle}
            required
            autoComplete="username"
            placeholder="ash_ketchum"
            onChange={(e) => setHandle(e.target.value)}
          />
          <span className="form-hint">3–20 characters: a–z, 0–9 or _</span>
        </label>
      )}
      <label>
        Password
        <input
          type="password"
          value={password}
          required
          minLength={8}
          autoComplete={mode === "login" ? "current-password" : "new-password"}
          onChange={(e) => setPassword(e.target.value)}
        />
        {mode === "register" && <span className="form-hint">At least 8 characters.</span>}
      </label>
      {error && <p className="camera-error" role="alert">{error}</p>}
      <button type="submit" className="primary block" disabled={busy}>
        {busy ? "…" : mode === "login" ? "Log in" : "Sign up"}
      </button>
      <button
        type="button"
        className="ghost block"
        onClick={() => {
          setError(null);
          setMode(mode === "login" ? "register" : "login");
        }}
      >
        {mode === "login" ? "Need an account? Sign up" : "Have an account? Log in"}
      </button>
    </form>
  );
}
