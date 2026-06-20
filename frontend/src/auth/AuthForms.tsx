import { useState } from "react";
import { useAuth } from "./AuthContext";

export default function AuthForms({ onDone }: { onDone?: () => void }) {
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
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="auth-form" onSubmit={submit}>
      <h2>{mode === "login" ? "Trainer login" : "Become a trainer"}</h2>
      <label>Email<input type="email" value={email} required onChange={(e) => setEmail(e.target.value)} /></label>
      {mode === "register" && (
        <label>Handle<input value={handle} required placeholder="3-20 chars a-z 0-9 _"
          onChange={(e) => setHandle(e.target.value)} /></label>
      )}
      <label>Password<input type="password" value={password} required minLength={8}
        onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p className="camera-error">{error}</p>}
      <button type="submit" className="primary" disabled={busy}>
        {busy ? "…" : mode === "login" ? "Log in" : "Sign up"}
      </button>
      <button type="button" onClick={() => setMode(mode === "login" ? "register" : "login")}>
        {mode === "login" ? "Need an account? Sign up" : "Have an account? Log in"}
      </button>
    </form>
  );
}
