import { useEffect, useState } from "react";
import { statsAnomalies, updateAnomaly, type AnomalyRow } from "../api";
import { SectionTitle } from "../ui/kit";

export default function Anomalies() {
  const [rows, setRows] = useState<AnomalyRow[]>([]);
  const load = () => statsAnomalies("open").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  const act = async (id: string, status: string) => { await updateAnomaly(id, status); load(); };

  return (
    <div className="stack">
      <SectionTitle>Open anomalies</SectionTitle>
      {rows.length === 0 && <p className="muted">None open. All quiet.</p>}
      <ul className="dash-list">
        {rows.map((a) => (
          <li key={a.id} className="panel flagged stack" style={{ gap: 6 }}>
            <strong>{a.detector} · {a.target_type} {a.set_id}</strong>
            <span className="small-text muted" style={{ overflowWrap: "anywhere" }}>
              severity {a.severity.toFixed(2)} · {JSON.stringify(a.detail)}
            </span>
            <div className="row">
              <button type="button" className="small" onClick={() => act(a.id, "reviewed")}>Reviewed</button>
              <button type="button" className="small ghost" onClick={() => act(a.id, "dismissed")}>Dismiss</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
