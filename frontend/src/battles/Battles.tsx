import { useEffect, useState } from "react";
import { friendlyError, outcomeLabel, shortDate, shortDateTime, usd } from "@shared/format";
import {
  acceptBattle, battleInbox, botBattle, declineBattle, friendBattle, listPulls,
  listBattles, randomBattle, type Battle, type BattleList, type SavedPull,
} from "../api";
import PixelIcon from "../ui/PixelIcon";
import { EmptyState, HpBar, ScreenTitle, SectionTitle, StatTile } from "../ui/kit";

const OUTCOME_BADGE: Record<string, string> = { win: "green", loss: "red", tie: "gold" };

export default function Battles({
  preselectPullId,
  onScan,
}: {
  preselectPullId?: string | null;
  onScan: () => void;
}) {
  const [data, setData] = useState<BattleList | null>(null);
  const [inbox, setInbox] = useState<Battle[]>([]);
  const [pulls, setPulls] = useState<SavedPull[] | null>(null);
  const [sel, setSel] = useState<string>("");
  const [handle, setHandle] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    setBusy(true);
    try {
      const res = await fn();
      refresh();
      // A resolved battle comes straight back — pop it open so the result shows.
      if (res && typeof res === "object" && "id" in res) setOpen((res as Battle).id);
    } catch (e) {
      setMsg(friendlyError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="stack-lg">
      <ScreenTitle kicker="Arena" title="Pack battles" sub="Highest total pack value wins. Only verified packs can enter." />

      <div className="record">
        <StatTile value={data?.wins ?? "–"} label="Wins" tone="green" />
        <StatTile value={data?.losses ?? "–"} label="Losses" tone="red" />
        <StatTile value={data?.ties ?? "–"} label="Ties" />
      </div>

      {inbox.length > 0 && (
        <>
          <SectionTitle>Challengers</SectionTitle>
          <ul className="card-rows">
            {inbox.map((b) => (
              <li key={b.id} className="panel flagged stack">
                <div className="row">
                  <span className="icon-box red"><PixelIcon name="swords" size={20} /></span>
                  <p className="grow">
                    <strong>{b.opponent.label}</strong> wants to battle!
                  </p>
                </div>
                <div className="row">
                  <button type="button" className="primary small" disabled={!sel || busy} onClick={() => act(() => acceptBattle(b.id, sel))}>
                    Accept with selected pack
                  </button>
                  <button type="button" className="small ghost" disabled={busy} onClick={() => act(() => declineBattle(b.id))}>
                    Run away
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <SectionTitle>New battle</SectionTitle>
      {pulls && pulls.length === 0 ? (
        <EmptyState icon="swords" title="No verified packs yet">
          <p className="muted">Scan a pack with its code card to enter the arena.</p>
          <button type="button" className="primary" onClick={onScan}>
            Scan a pack
          </button>
        </EmptyState>
      ) : (
        <div className="panel form">
          <label>
            Your pack
            <select value={sel} onChange={(e) => setSel(e.target.value)} disabled={!pulls}>
              {(pulls ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {shortDate(p.created_at)} · {p.cards.length} cards
                  {p.estimated_value != null ? ` · ≈${usd(p.estimated_value)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="row">
            <button type="button" className="primary grow" disabled={!sel || busy} onClick={() => act(() => randomBattle(sel))}>
              <PixelIcon name="star" size={16} /> Random trainer
            </button>
            <button type="button" className="grow" disabled={!sel || busy} onClick={() => act(() => botBattle(sel))}>
              <PixelIcon name="heart" size={16} /> Bot
            </button>
          </div>
          <div className="row">
            <input
              className="grow"
              style={{ width: "auto" }}
              placeholder="friend's handle"
              value={handle}
              onChange={(e) => setHandle(e.target.value)}
              aria-label="Friend's handle"
            />
            <button type="button" className="gold" disabled={!sel || !handle.trim() || busy} onClick={() => act(() => friendBattle(sel, handle.trim()))}>
              Challenge
            </button>
          </div>
          {msg && <p className="camera-error" role="alert">{msg}</p>}
        </div>
      )}

      <SectionTitle>History</SectionTitle>
      {(!data || data.battles.length === 0) && <p className="muted">No battles yet — go pick a fight!</p>}
      <ul className="card-rows">
        {(data?.battles ?? []).map((b) => (
          <BattleCard key={b.id} b={b} open={open === b.id} onToggle={() => setOpen(open === b.id ? null : b.id)} />
        ))}
      </ul>
    </section>
  );
}

function BattleCard({ b, open, onToggle }: { b: Battle; open: boolean; onToggle: () => void }) {
  const mine = b.me.score ?? 0;
  const theirs = b.opponent.score ?? 0;
  const max = Math.max(mine, theirs, 0.01);
  const pending = b.me.score == null || b.opponent.score == null;
  return (
    <li className="panel battle">
      <button type="button" className="pull-head" aria-expanded={open} onClick={onToggle}>
        <span className="battle-head">
          <span className={`badge ${OUTCOME_BADGE[b.outcome] ?? "outline"}`}>{outcomeLabel(b.outcome)}</span>
          <span className="small-text muted">
            {b.mode} · {shortDateTime(b.created_at)}
          </span>
        </span>
        <span className="chev">▶</span>
      </button>
      <div className="vs">
        <div className="vs-side">
          <span className="vs-name">You</span>
          <HpBar value={Math.round((mine / max) * 100)} max={100} label="" showValue={false} tone="green" />
          <span className="vs-score">{b.me.score != null ? usd(b.me.score) : "?"}</span>
        </div>
        <span className="vs-mid">VS</span>
        <div className="vs-side right">
          <span className="vs-name">{b.opponent.label}</span>
          <HpBar value={Math.round((theirs / max) * 100)} max={100} label="" showValue={false} tone="cyan" />
          <span className="vs-score">{pending ? "?" : usd(theirs)}</span>
        </div>
      </div>
      {open && (
        <div className="battle-cards">
          {[b.me, b.opponent].map((side, i) => (
            <div key={i} className="stack" style={{ gap: 6 }}>
              <span className="stat-label">{i === 0 ? "Your pack" : side.label}</span>
              <ul>
                {side.cards.map((c, j) => (
                  <li key={j}>
                    <span>{c.name ?? "?"}</span>
                    <span className="price">{c.price != null ? usd(c.price) : "—"}</span>
                  </li>
                ))}
                {side.cards.length === 0 && <li className="muted">No cards yet</li>}
              </ul>
            </div>
          ))}
        </div>
      )}
    </li>
  );
}
