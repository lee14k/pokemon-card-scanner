import { useEffect, useState } from "react";
import { statsAnomalies, updateAnomaly, type AnomalyRow } from "../api";

export default function Anomalies() {
  const [rows, setRows] = useState<AnomalyRow[]>([]);
  const load = () => statsAnomalies("open").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  const act = async (id: string, status: string) => { await updateAnomaly(id, status); load(); };

  return (
    <div>
      <h3>Open anomalies</h3>
      {rows.length === 0 && <p>None open.</p>}
      <ul className="card-rows">
        {rows.map((a) => (
          <li key={a.id} className="card-row flagged">
            <div className="card-row-body">
              <strong>{a.detector} · {a.target_type} {a.set_id}</strong>
              <span>severity {a.severity.toFixed(2)} · {JSON.stringify(a.detail)}</span>
              <div className="card-row-flag">
                <button type="button" onClick={() => act(a.id, "reviewed")}>Reviewed</button>
                <button type="button" onClick={() => act(a.id, "dismissed")}>Dismiss</button>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
