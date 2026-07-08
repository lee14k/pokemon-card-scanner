import { useState } from "react";
import { adminTrainers, setTrainerRole, type AdminTrainer } from "../api";

export default function RoleAdmin() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AdminTrainer[]>([]);
  const search = async () => setRows(await adminTrainers(q));
  const change = async (id: string, role: string) => { await setTrainerRole(id, role); search(); };

  return (
    <div>
      <h3>Trainer roles</h3>
      <input value={q} placeholder="email or handle" onChange={(e) => setQ(e.target.value)} />
      <button type="button" onClick={search}>Search</button>
      <ul className="card-rows">
        {rows.map((t) => (
          <li key={t.id} className="card-row">
            <div className="card-row-body">
              <strong>@{t.handle}</strong><span>{t.email} · {t.role}</span>
              <div className="card-row-flag">
                {["trainer", "analyst", "admin"].map((r) => (
                  <button key={r} type="button" disabled={r === t.role} onClick={() => change(t.id, r)}>{r}</button>
                ))}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
