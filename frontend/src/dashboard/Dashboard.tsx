import { useState } from "react";
import { friendlyError } from "@shared/format";
import { recomputeStats } from "../api";
import { useAuth } from "../auth/AuthContext";
import { ScreenTitle } from "../ui/kit";
import SetStats from "./SetStats";
import Anomalies from "./Anomalies";
import RoleAdmin from "./RoleAdmin";
import TrainingData from "./TrainingData";

type Tab = "sets" | "anomalies" | "roles" | "training";

export default function Dashboard() {
  const { trainer } = useAuth();
  const [tab, setTab] = useState<Tab>("sets");
  const [msg, setMsg] = useState<string | null>(null);
  const isAdmin = trainer?.role === "admin";

  const recompute = async () => {
    setMsg("Recomputing…");
    try { await recomputeStats(); setMsg("Recompute started — refresh in a moment."); }
    catch (e) { setMsg(friendlyError(e)); }
  };

  const tabs: { id: Tab; label: string; admin?: boolean }[] = [
    { id: "sets", label: "Sets" },
    { id: "anomalies", label: "Anomalies" },
    { id: "roles", label: "Roles", admin: true },
    { id: "training", label: "Training data", admin: true },
  ];

  return (
    <section className="stack-lg">
      <ScreenTitle kicker={trainer?.role ?? "analyst"} title="Pull-rate lab" sub="Community pull rates, anomalies and admin tools." />
      <div className="row row-between">
        <nav className="segmented" aria-label="Dashboard sections">
          {tabs
            .filter((t) => !t.admin || isAdmin)
            .map((t) => (
              <button key={t.id} type="button" className="small" aria-pressed={tab === t.id} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
        </nav>
        {isAdmin && <button type="button" className="primary small" onClick={recompute}>Recompute now</button>}
      </div>
      {msg && <p className="textbox">{msg}</p>}
      {tab === "sets" && <SetStats />}
      {tab === "anomalies" && <Anomalies />}
      {tab === "roles" && isAdmin && <RoleAdmin />}
      {tab === "training" && isAdmin && <TrainingData />}
    </section>
  );
}
