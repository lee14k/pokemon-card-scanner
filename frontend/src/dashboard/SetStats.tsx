import { useEffect, useState } from "react";
import { statsSets, statsSetDetail, type SetDetail, type SetSummary } from "../api";
import { SectionTitle } from "../ui/kit";

const pct = (r: number) => `${(r * 100).toFixed(1)}%`;

export default function SetStats() {
  const [sets, setSets] = useState<SetSummary[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<SetDetail | null>(null);

  useEffect(() => { statsSets().then(setSets).catch(() => setSets([])); }, []);
  useEffect(() => { if (sel) statsSetDetail(sel).then(setDetail).catch(() => setDetail(null)); }, [sel]);

  return (
    <div className="stack-lg">
      <SectionTitle>Sets</SectionTitle>
      {sets.length === 0 && <p className="muted">No stats yet — run a recompute.</p>}
      <div className="segmented">
        {sets.map((s) => (
          <button key={s.set_id} type="button" className="small" aria-pressed={sel === s.set_id} onClick={() => setSel(s.set_id)}>
            {s.set_id} · {s.verified_pack_count} packs
          </button>
        ))}
      </div>
      {detail && (
        <div className="panel stack">
          <h3 className="pixel" style={{ fontSize: "0.7rem", lineHeight: 1.6 }}>
            {detail.set_id} — {detail.verified_pack_count} verified packs
          </h3>
          <SectionTitle>Rarity odds</SectionTitle>
          <div className="table-wrap">
            <table><tbody>
              {detail.rarities.map((r) => (
                <tr key={r.rarity}><td>{r.rarity}</td><td className="price">{pct(r.blended_rate)}</td><td className="muted">raw {pct(r.raw_rate)}</td></tr>
              ))}
            </tbody></table>
          </div>
          <SectionTitle>Cards</SectionTitle>
          <div className="table-wrap">
            <table><tbody>
              {detail.cards.map((c) => (
                <tr key={c.match_id}>
                  <td>{c.name ?? c.match_id}</td><td className="muted">{c.card_number}</td>
                  <td className="price">{pct(c.blended_rate)}</td><td className="muted">{c.hits}/{c.packs}</td>
                </tr>
              ))}
            </tbody></table>
          </div>
        </div>
      )}
    </div>
  );
}
