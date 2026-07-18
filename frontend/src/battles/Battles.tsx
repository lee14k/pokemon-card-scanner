import { useEffect, useState } from "react";
import {
  acceptBattle, battleInbox, botBattle, declineBattle, friendBattle, listPulls,
  listBattles, randomBattle, type Battle, type BattleList, type SavedPull,
} from "../api";

const badge = (o: string) =>
  o === "win" ? "🏆 win" : o === "loss" ? "💀 loss" : o === "tie" ? "🤝 tie" : o;

export default function Battles({ preselectPullId }: { preselectPullId?: string | null }) {
  const [data, setData] = useState<BattleList | null>(null);
  const [inbox, setInbox] = useState<Battle[]>([]);
  const [pulls, setPulls] = useState<SavedPull[]>([]);
  const [sel, setSel] = useState<string>("");
  const [handle, setHandle] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = () => {
    listBattles().then(setData).catch(() => setData(null));
    battleInbox().then(setInbox).catch(() => setInbox([]));
  };
  useEffect(() => {
    refresh();
    listPulls().then((ps) => {
      const verified = ps.filter((p) => p.verified);
      setPulls(verified);
      setSel(preselectPullId && verified.some((p) => p.id === preselectPullId)
        ? preselectPullId : verified[0]?.id ?? "");
    }).catch(() => setPulls([]));
  }, [preselectPullId]);

  const act = async (fn: () => Promise<unknown>) => {
    setMsg(null);
    try { await fn(); refresh(); } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <section>
      <h2>Pack Battles{data && <> — {data.wins}W / {data.losses}L / {data.ties}T</>}</h2>

      <h3>New battle</h3>
      {pulls.length === 0 ? <p>You need a verified pull to battle — scan a pack!</p> : (
        <div className="auth-form">
          <label>Your pack
            <select value={sel} onChange={(e) => setSel(e.target.value)}>
              {pulls.map((p) => (
                <option key={p.id} value={p.id}>
                  {new Date(p.created_at).toLocaleDateString()} · {p.cards.length} cards
                  {p.estimated_value != null ? ` · ≈$${p.estimated_value.toFixed(2)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="card-row-flag">
            <button type="button" className="primary" disabled={!sel} onClick={() => act(() => randomBattle(sel))}>Random</button>
            <button type="button" disabled={!sel} onClick={() => act(() => botBattle(sel))}>Bot</button>
            <input placeholder="friend's handle" value={handle} onChange={(e) => setHandle(e.target.value)} />
            <button type="button" disabled={!sel || !handle.trim()} onClick={() => act(() => friendBattle(sel, handle.trim()))}>Challenge</button>
          </div>
          {msg && <p className="camera-error">{msg}</p>}
        </div>
      )}

      {inbox.length > 0 && (
        <>
          <h3>Challenges for you</h3>
          <ul className="card-rows">
            {inbox.map((b) => (
              <li key={b.id} className="card-row flagged">
                <div className="card-row-body">
                  <strong>{b.opponent.label} challenged you!</strong>
                  <div className="card-row-flag">
                    <button type="button" disabled={!sel} onClick={() => act(() => acceptBattle(b.id, sel))}>
                      Accept with selected pack
                    </button>
                    <button type="button" onClick={() => act(() => declineBattle(b.id))}>Decline</button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>History</h3>
      {(!data || data.battles.length === 0) && <p>No battles yet.</p>}
      <ul className="card-rows">
        {(data?.battles ?? []).map((b) => (
          <li key={b.id} className="card-row">
            <div className="card-row-body">
              <button type="button" className="pull-row-toggle" onClick={() => setOpen(open === b.id ? null : b.id)}>
                <strong>{badge(b.outcome)} · vs {b.opponent.label} · {b.mode}</strong>
              </button>
              <span>
                you ${b.me.score?.toFixed(2) ?? "?"} vs {b.opponent.label} ${b.opponent.score?.toFixed(2) ?? "?"}
                {" · "}{new Date(b.created_at).toLocaleString()}
              </span>
              {open === b.id && (
                <div>
                  {[b.me, b.opponent].map((side, i) => (
                    <ul key={i} className="card-rows">
                      <li className="card-row"><div className="card-row-body"><strong>{side.label}</strong></div></li>
                      {side.cards.map((c, j) => (
                        <li key={j} className="card-row"><div className="card-row-body">
                          <span>{c.name ?? "?"} · {c.price != null ? `$${c.price.toFixed(2)}` : "—"}</span>
                        </div></li>
                      ))}
                    </ul>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
