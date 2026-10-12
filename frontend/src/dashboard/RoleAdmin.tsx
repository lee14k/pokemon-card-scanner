import { useState } from "react";
import { adminTrainers, setTrainerRole, type AdminTrainer } from "../api";
import { SectionTitle } from "../ui/kit";

export default function RoleAdmin() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AdminTrainer[]>([]);
  const search = async () => setRows(await adminTrainers(q));
  const change = async (id: string, role: string) => { await setTrainerRole(id, role); search(); };

  return (
    <div className="stack">
      <SectionTitle>Trainer roles</SectionTitle>
      <form className="row" onSubmit={(e) => { e.preventDefault(); void search(); }}>
        <input className="grow" style={{ width: "auto" }} value={q} placeholder="email or handle" onChange={(e) => setQ(e.target.value)} />
        <button type="submit">Search</button>
      </form>
      <ul className="dash-list">
        {rows.map((t) => (
          <li key={t.id} className="panel stack" style={{ gap: 6 }}>
            <strong>@{t.handle}</strong>
            <span className="small-text muted">{t.email} · {t.role}</span>
            <div className="segmented">
              {["trainer", "analyst", "admin"].map((r) => (
                <button key={r} type="button" className="small" aria-pressed={r === t.role} disabled={r === t.role} onClick={() => change(t.id, r)}>{r}</button>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
