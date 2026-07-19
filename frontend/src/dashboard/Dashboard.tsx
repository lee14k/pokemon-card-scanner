import { useState } from "react";
import { recomputeStats } from "../api";
import { useAuth } from "../auth/AuthContext";
import SetStats from "./SetStats";
import Anomalies from "./Anomalies";
import RoleAdmin from "./RoleAdmin";
import TrainingData from "./TrainingData";

export default function Dashboard() {
  const { trainer } = useAuth();
  const [tab, setTab] = useState<"sets" | "anomalies" | "roles" | "training">("sets");
  const [msg, setMsg] = useState<string | null>(null);
  const isAdmin = trainer?.role === "admin";

  const recompute = async () => {
    setMsg("Recomputing…");
    try { await recomputeStats(); setMsg("Recompute started — refresh in a moment."); }
    catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section>
      <h2>Pull-rate dashboard</h2>
      <nav className="app-header">
        <button type="button" onClick={() => setTab("sets")}>Sets</button>
        <button type="button" onClick={() => setTab("anomalies")}>Anomalies</button>
        {isAdmin && <button type="button" onClick={() => setTab("roles")}>Roles</button>}
        {isAdmin && <button type="button" onClick={() => setTab("training")}>Training Data</button>}
        {isAdmin && <button type="button" className="primary" onClick={recompute}>Recompute now</button>}
      </nav>
      {msg && <p className="status">{msg}</p>}
      {tab === "sets" && <SetStats />}
      {tab === "anomalies" && <Anomalies />}
      {tab === "roles" && isAdmin && <RoleAdmin />}
      {tab === "training" && isAdmin && <TrainingData />}
    </section>
  );
}
