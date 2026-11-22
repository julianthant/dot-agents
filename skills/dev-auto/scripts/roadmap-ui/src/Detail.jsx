import { agentLabel, age, plural, tone } from "./shared.jsx";

/** The panel a square opens: one ticket's fields, from the same render (no fetch). */
export default function Detail({ t, data }) {
  const d = t.detail;
  const ago = (epoch) => (epoch ? age(data.renderedAt - epoch) : "");
  const waits = [...d.blockers.map(([id, st]) => `${id} (${st})`), ...(d.wait ? [d.wait] : [])];
  return (
    <div className="rl-pop" role="dialog" aria-label={`${t.id} detail`}>
      <h3>{d.title}</h3>
      <p className="rl-dim">{t.id}</p>
      <dl>
        <dt>Status</dt><dd>{t.state}</dd>
        <dt>Tier</dt><dd>{d.tier || "none"}</dd>
        <dt>Coder</dt><dd>{d.coder || "none"}</dd>
        <dt>Reviewer</dt><dd>{d.reviewer || "none"}</dd>
        <dt>Working</dt>
        <dd>
          {d.who.length ? d.who.map((w) => (
            <span key={w.role} className={`t-${tone(w.state)}`}>{w.role}: {agentLabel(w) || "?"}, {w.state}{w.since ? `, ${ago(w.since)}` : ""}<br /></span>
          )) : "nobody"}
        </dd>
        <dt>Waits on</dt><dd>{waits.length ? waits.map((w) => <span key={w}>{w}<br /></span>) : "nothing"}</dd>
        <dt>FIX rounds</dt><dd>{d.fix}</dd>
        <dt>Latest</dt><dd>{d.latest ? `${d.latest.line} (${ago(d.latest.at)} ago)` : "no update"}</dd>
      </dl>
    </div>
  );
}
