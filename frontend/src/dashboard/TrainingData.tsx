import { useEffect, useState } from "react";

const base = import.meta.env.VITE_API_BASE?.replace(/\/$/, "") || (import.meta.env.DEV ? "/api" : "");

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${base}${path}`, { credentials: "include", ...init });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? res.statusText));
  return body as T;
}

interface PhotoRow { strip_id: string; row_index: number; card_key: string | null }
interface Photo { photo_id: string; tier: string; split: string; labeled: boolean; set_hint: string | null; created_at: string; rows: PhotoRow[] }
interface Template { set_id: string; set_code: string | null; set_name: string; tcgdex_set_id: string | null; cards: { number: string; name: string | null; card_key: string }[] }

export default function TrainingData() {
  const [view, setView] = useState<"pools" | "refs" | "synth" | "eval">("pools");
  return (
    <section>
      <h3>Training Data</h3>
      <nav className="app-header">
        <button type="button" onClick={() => setView("pools")}>Intake &amp; Pools</button>
        <button type="button" onClick={() => setView("refs")}>References</button>
        <button type="button" onClick={() => setView("synth")}>Synthetic</button>
        <button type="button" onClick={() => setView("eval")}>Eval</button>
      </nav>
      {view === "pools" && <Pools />}
      {view === "refs" && <References />}
      {view === "synth" && <Synthetic />}
      {view === "eval" && <Eval />}
    </section>
  );
}

function Pools() {
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [file, setFile] = useState<File | null>(null);
  const [tier, setTier] = useState("standard");
  const [split, setSplit] = useState("train");
  const [setHint, setSetHint] = useState("");
  const [labelJson, setLabelJson] = useState("");
  const [labelTarget, setLabelTarget] = useState<Photo | null>(null);
  const [preds, setPreds] = useState<Record<string, any>>({});
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = () => {
    j<Photo[]>("/admin/training/photos").then(setPhotos).catch((e) => setMsg(String(e)));
    j("/admin/training/pools/summary").then(setSummary).catch(() => setSummary(null));
  };
  useEffect(refresh, []);

  const upload = async () => {
    if (!file) return;
    setMsg("Uploading…");
    const form = new FormData();
    form.append("photo", file);
    form.append("tier", tier);
    form.append("split", split);
    if (setHint.trim()) form.append("set_hint", setHint.trim());
    try {
      const r = await j<{ photo_id: string; rows: unknown[] }>("/admin/training/photos", { method: "POST", body: form });
      setMsg(`Uploaded: ${r.rows.length} rows detected.`);
      refresh();
    } catch (e) { setMsg(String(e)); }
  };

  const template = async (p: Photo) => {
    const q = p.set_hint || prompt("Set (code, id, or name)?") || "";
    if (!q) return;
    try {
      const t = await j<Template>(`/admin/training/label-template/${encodeURIComponent(q)}`);
      setLabelTarget(p);
      setLabelJson(JSON.stringify({ set: t.set_code ?? t.set_id, rows: p.rows.map(() => null) }, null, 1));
      setMsg(`Template for ${t.set_name} (${t.cards.length} cards) — fill numbers, null = skip row.`);
    } catch (e) { setMsg(String(e)); }
  };

  const applyLabels = async () => {
    if (!labelTarget) return;
    try {
      const body = JSON.parse(labelJson);
      const r = await j<{ labeled_rows: number; skipped_rows: number }>(
        `/admin/training/photos/${labelTarget.photo_id}/labels`,
        { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      setMsg(`Labeled ${r.labeled_rows} rows (${r.skipped_rows} skipped).`);
      setLabelTarget(null); refresh();
    } catch (e) { setMsg(String(e)); }
  };

  const predict = async (p: Photo) => {
    try { setPreds({ ...preds, [p.photo_id]: await j(`/admin/training/photos/${p.photo_id}/predictions`) }); }
    catch (e) { setMsg(String(e)); }
  };

  return (
    <div>
      <div className="auth-form">
        <label>Photo (any card count)
          <input type="file" accept="image/*,.heic" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label>Tier
          <select value={tier} onChange={(e) => setTier(e.target.value)}>
            <option value="standard">standard (deploy gate)</option>
            <option value="stress">stress</option>
          </select>
        </label>
        <label>Split
          <select value={split} onChange={(e) => setSplit(e.target.value)}>
            <option value="train">train</option>
            <option value="test">test</option>
          </select>
        </label>
        <input placeholder="set hint (e.g. TWM)" value={setHint} onChange={(e) => setSetHint(e.target.value)} />
        <button type="button" className="primary" disabled={!file} onClick={upload}>Upload</button>
      </div>
      {msg && <p className="status">{msg}</p>}
      {summary && (
        <p>
          {summary.photos.map((r: any, i: number) => (
            <span key={i}>{r.tier}/{r.split}/{r.labeled ? "labeled" : "unlabeled"}: {r.count} &nbsp;</span>
          ))}
          {summary.labeled_strips_by_set.map((r: any) => (
            <span key={r.set_id}>| set {r.set_id}: {r.count} strips </span>
          ))}
        </p>
      )}
      {labelTarget && (
        <div className="auth-form">
          <strong>Labels for {labelTarget.photo_id.slice(0, 8)}…</strong>
          <textarea rows={6} value={labelJson} onChange={(e) => setLabelJson(e.target.value)} />
          <div className="card-row-flag">
            <button type="button" className="primary" onClick={applyLabels}>Apply labels</button>
            <button type="button" onClick={() => setLabelTarget(null)}>Cancel</button>
          </div>
        </div>
      )}
      <ul className="card-rows">
        {photos.map((p) => (
          <li key={p.photo_id} className="card-row">
            <div className="card-row-body">
              <strong>{p.tier}/{p.split} · {p.rows.length} rows · {p.labeled ? "labeled ✓" : "unlabeled"}
                {p.set_hint ? ` · hint ${p.set_hint}` : ""} · {new Date(p.created_at).toLocaleString()}</strong>
              <div className="card-row-flag">
                <button type="button" onClick={() => template(p)}>{p.labeled ? "Relabel" : "Label (JSON)"}</button>
                {!p.labeled && <button type="button" onClick={() => predict(p)}>Predictions</button>}
              </div>
              <div style={{ overflowX: "auto", whiteSpace: "nowrap" }}>
                {p.rows.map((r) => (
                  <img key={r.strip_id} src={`${base}/admin/training/strips/${r.strip_id}/image`}
                       alt={`row ${r.row_index}`} title={r.card_key ?? `row ${r.row_index}`}
                       style={{ height: 34, marginRight: 4, border: r.card_key ? "2px solid #4a4" : "1px solid #555" }} />
                ))}
              </div>
              {preds[p.photo_id] && (
                <div>{preds[p.photo_id].rows.map((r: any) => (
                  <div key={r.row_index}>row {r.row_index}: {r.top.map((t: any) => `${t.id}@${t.score}`).join("  ")}</div>
                ))}</div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function References() {
  const [q, setQ] = useState("");
  const [data, setData] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = () => j(`/admin/training/references/${encodeURIComponent(q)}`).then(setData).catch((e) => setMsg(String(e)));
  return (
    <div>
      <div className="card-row-flag">
        <input placeholder="set code / name (e.g. TWM)" value={q} onChange={(e) => setQ(e.target.value)} />
        <button type="button" className="primary" disabled={!q.trim()} onClick={load}>Browse</button>
      </div>
      {msg && <p className="status">{msg}</p>}
      {data && (
        <>
          <p>{data.set_name} — {data.cards.length} reference cards</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {data.cards.map((c: any) => (
              <figure key={c.card_key} style={{ width: 110, margin: 0 }}>
                {c.image_url && <img src={c.image_url} alt={c.name ?? c.number} style={{ width: "100%" }} loading="lazy" />}
                <figcaption style={{ fontSize: 11 }}>{c.number} {c.name ?? ""}</figcaption>
              </figure>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function Synthetic() {
  const [data, setData] = useState<any>(null);
  const [version, setVersion] = useState<string>("");
  useEffect(() => { j("/admin/training/synthetic").then(setData).catch(() => setData({ available: false })); }, []);
  const loadVersion = (v: string) => {
    setVersion(v);
    j(`/admin/training/synthetic?version=${encodeURIComponent(v)}`).then(setData).catch(() => null);
  };
  if (!data) return <p className="status">Loading…</p>;
  if (!data.available) return <p>No synthetic data on this deployment (generated on the training machine).</p>;
  return (
    <div>
      <div className="card-row-flag">
        {data.datasets.map((d: string) => (
          <button key={d} type="button" className={d === version ? "primary" : ""} onClick={() => loadVersion(d)}>{d}</button>
        ))}
      </div>
      {data.counts && <p>{data.counts.strips} strips, {data.counts.labeled} labeled</p>}
      {data.samples && (
        <div>
          {data.samples.map((s: any, i: number) => (
            <figure key={i} style={{ margin: "4px 0" }}>
              <img src={`${base}/admin/training/synthetic/image?version=${version}&path=${encodeURIComponent(s.path)}`}
                   alt={s.card_key} style={{ height: 40 }} />
              <figcaption style={{ fontSize: 11 }}>{s.card_key} ({s.set}/{s.split})</figcaption>
            </figure>
          ))}
        </div>
      )}
    </div>
  );
}

function Eval() {
  const [runs, setRuns] = useState<any[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const refresh = () => { j<any[]>("/admin/training/eval-runs").then(setRuns).catch((e) => setMsg(String(e))); };
  useEffect(refresh, []);
  const run = async () => {
    setMsg("Running evaluation…");
    try { await j("/admin/training/eval-runs", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }); setMsg(null); refresh(); }
    catch (e) { setMsg(String(e)); }
  };
  return (
    <div>
      <button type="button" className="primary" onClick={run}>Run evaluation (labeled test split)</button>
      {msg && <p className="status">{msg}</p>}
      <ul className="card-rows">
        {runs.map((r) => (
          <li key={r.id} className="card-row"><div className="card-row-body">
            <strong>{r.model_version} · {r.tier}</strong>
            <span>top-1 {r.top1}/{r.total} · top-3 {r.top3}/{r.total} · {new Date(r.created_at).toLocaleString()}</span>
          </div></li>
        ))}
      </ul>
    </div>
  );
}
