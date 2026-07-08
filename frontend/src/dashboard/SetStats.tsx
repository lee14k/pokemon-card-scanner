import { useEffect, useState } from "react";
import { statsSets, statsSetDetail, type SetDetail, type SetSummary } from "../api";

const pct = (r: number) => `${(r * 100).toFixed(1)}%`;

export default function SetStats() {
  const [sets, setSets] = useState<SetSummary[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<SetDetail | null>(null);

  useEffect(() => { statsSets().then(setSets).catch(() => setSets([])); }, []);
  useEffect(() => { if (sel) statsSetDetail(sel).then(setDetail).catch(() => setDetail(null)); }, [sel]);

  return (
    <div>
      <h3>Sets</h3>
      {sets.length === 0 && <p>No stats yet — run a recompute.</p>}
      <ul className="card-rows">
        {sets.map((s) => (
          <li key={s.set_id} className="card-row">
            <button type="button" onClick={() => setSel(s.set_id)}>
              {s.set_id} · {s.verified_pack_count} packs
            </button>
          </li>
        ))}
      </ul>
      {detail && (
        <div>
          <h3>Set {detail.set_id} — {detail.verified_pack_count} verified packs</h3>
          <h4>Rarity odds</h4>
          <table><tbody>
            {detail.rarities.map((r) => (
              <tr key={r.rarity}><td>{r.rarity}</td><td>{pct(r.blended_rate)}</td><td>(raw {pct(r.raw_rate)})</td></tr>
            ))}
          </tbody></table>
          <h4>Cards</h4>
          <table><tbody>
            {detail.cards.map((c) => (
              <tr key={c.match_id}>
                <td>{c.name ?? c.match_id}</td><td>{c.card_number}</td>
                <td>{pct(c.blended_rate)}</td><td>({c.hits}/{c.packs})</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </div>
  );
}
