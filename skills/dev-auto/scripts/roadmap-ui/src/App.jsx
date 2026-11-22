import { useEffect, useState } from "react";
import Detail from "./Detail.jsx";
import { Empty, STATE_ORDER, age, agentLabel, clock, phaseTone, plural, since, stateText, tone, usePageAge } from "./shared.jsx";
import "./rail.css";

const SQUARE = ["in progress", "needs you", "blocked", "remaining"];
const sq = (s) => "k-" + s.replace(" ", "-");

function Agents({ data }) {
  const wts = data.agents.worktrees;
  const live = wts.reduce((n, w) => n + w.agents.filter((a) => a.state !== "paused").length, 0);
  return (
    <section className="rl-sheet rl-sec" aria-labelledby="h-agents">
      <header className="rl-sec-head">
        <h2 id="h-agents">Being worked on</h2>
        <span className="rl-dim">{plural(wts.length, "ticket")}, {plural(live, "agent")}</span>
        <p className="rl-states">
          {STATE_ORDER.filter((s) => data.agents.counts[s]).map((s) => <span key={s} className={`t-${tone(s)}`}><i />{data.agents.counts[s]} {s}</span>)}
        </p>
      </header>
      {wts.length > 0 ? (
        <table className="rl-table">
          <thead><tr><th>Ticket</th><th>Phase</th><th>Device</th><th>Who</th><th>Status</th><th>For</th><th>Account</th></tr></thead>
          {wts.map((w) => (
            <tbody key={w.host + w.worktree}>
              {w.agents.map((a, i) => (
                <tr key={a.key}>
                  {i === 0 && (
                    <td rowSpan={w.agents.length}>
                      <b>{w.title}</b>
                      <small>{w.worktree} on {w.host}{w.fixRounds ? `, ${plural(w.fixRounds, "FIX round")}` : ""}</small>
                    </td>
                  )}
                  {i === 0 && <td rowSpan={w.agents.length}><span className={`rl-phase t-${phaseTone(w.phase)}`}>{w.phase}</span></td>}
                  <td>{a.device}</td>
                  <td>{a.role}<small>{agentLabel(a)}</small></td>
                  <td><span className={`rl-st t-${tone(a.state)}`}><i />{stateText(a)}</span>{a.stale && <small className="rl-stale">Last observed {since(data, a.seen)} ago</small>}</td>
                  <td className="rl-num-c">{since(data, a.since)}</td>
                  <td className="rl-acct">{a.account}</td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      ) : <Empty>No agent is running</Empty>}
    </section>
  );
}

function Row({ f, n, data, open, toggle }) {
  const cells = [];
  for (let w = 1; w <= n; w++) {
    const mine = f.tickets.filter((t) => t.wave === w);
    const done = mine.filter((t) => t.state === "done").length;
    cells.push(
      <div key={w} className="rl-dots">
        {done > 0 && <span className="rl-dn" title={`${done} done in wave ${w}`}>{done}✓</span>}
        {mine.filter((t) => t.state !== "done").map((t) => (
          <span key={t.id} className="rl-sqw">
            <button type="button" className={`rl-sq ${sq(t.state)}`} title={`${t.lane}: ${t.state}`} aria-label={`${t.lane}, ${t.state}`} aria-expanded={open === t.id} onClick={() => toggle(t.id)} />
            {open === t.id && <Detail t={t} data={data} />}
          </span>
        ))}
      </div>,
    );
  }
  const prog = `${f.done}/${f.total} · ${f.pr}`;
  return (
    <>
      <span className="rl-fn">{f.name}</span>
      {cells}
      <span className="rl-pr" title={prog}>{prog}</span>
    </>
  );
}

function Grid({ feats, n, data, open, toggle }) {
  return (
    <div className="rl-mx" style={{ "--n": n }}>
      <span className="rl-mh">Feature · wave →</span>
      {Array.from({ length: n }, (_, i) => <span key={i} className="rl-mh">{i + 1}</span>)}
      <span className="rl-mh rl-mh-pr">Progress · PR</span>
      {feats.map((f) => <Row key={f.name} f={f} n={n} data={data} open={open} toggle={toggle} />)}
    </div>
  );
}

function Roadmap({ data }) {
  const { waves, features } = data.matrix;
  const live = features.filter((f) => f.state !== "done" && f.state !== "not ticketed");
  const done = features.filter((f) => f.state === "done");
  const idle = features.filter((f) => f.state === "not ticketed");
  const [open, setOpen] = useState(null);
  const toggle = (id) => setOpen((o) => (o === id ? null : id));
  useEffect(() => {
    if (!open) return undefined;
    const key = (e) => e.key === "Escape" && setOpen(null);
    const away = (e) => e.target.closest(".rl-sqw") || setOpen(null);
    document.addEventListener("keydown", key);
    document.addEventListener("mousedown", away);
    return () => {
      document.removeEventListener("keydown", key);
      document.removeEventListener("mousedown", away);
    };
  }, [open]);
  const grid = { data, open, toggle };
  return (
    <section className="rl-sheet rl-sec" aria-labelledby="h-road">
      <header className="rl-sec-head">
        <h2 id="h-road">Roadmap</h2>
        <span className="rl-dim">features by wave, one square per open ticket, click for detail</span>
        <p className="rl-states">
          {SQUARE.map((s) => <span key={s}><i className={`rl-sq ${sq(s)}`} />{s}</span>)}
          <span><b className="rl-dn">3✓</b>done in that wave</span>
        </p>
      </header>
      {live.length ? <Grid feats={live} n={waves} {...grid} /> : <Empty>No feature is in flight</Empty>}
      {done.length > 0 && <details className="rl-fold"><summary>Done features ({done.length})</summary><Grid feats={done} n={waves} {...grid} /></details>}
      {idle.length > 0 && <details className="rl-fold"><summary>Not ticketed yet ({idle.length})</summary><ul>{idle.map((f) => <li key={f.name}>{f.name}</li>)}</ul></details>}
    </section>
  );
}

function Blocked({ data }) {
  const { total, top } = data.blocked;
  return (
    <section className="rl-sheet rl-sec" aria-labelledby="h-blk">
      <header className="rl-sec-head"><h2 id="h-blk">Blocked</h2><span className="rl-dim">{plural(total, "ticket")}, top blockers</span></header>
      {top.length ? (
        <ul className="rl-list">
          {top.map((b) => (
            <li key={b.id}><i className={`rl-sq ${sq(b.state)}`} role="img" aria-label={b.state} /><b>{b.title}</b><span className="rl-dim">holds {b.holds} · {b.state}</span></li>
          ))}
        </ul>
      ) : <Empty>Nothing is blocked</Empty>}
    </section>
  );
}

function NextUp({ data }) {
  const rows = data.nextUp;
  return (
    <section className="rl-sheet rl-sec" aria-labelledby="h-next">
      <header className="rl-sec-head"><h2 id="h-next">Next up</h2><span className="rl-dim">{rows.length} ready</span></header>
      {rows.length ? (
        <ul className="rl-list">
          {rows.map((t) => (
            <li key={t.id}><i className="rl-sq k-remaining" role="img" aria-label="ready" /><b>{t.title}</b><span className="rl-dim">{t.detail}</span></li>
          ))}
        </ul>
      ) : <Empty>Nothing ready to start</Empty>}
    </section>
  );
}

export default function App({ data }) {
  const { base } = data;
  const { merging, remaining } = data.groups;
  const pageAge = usePageAge(data.renderedAt);
  const tickAge = data.tickAt == null ? null : data.renderedAt - data.tickAt;
  const late = pageAge > 60 || data.tickLate;
  return (
    <div className="rl">
      <header className="rl-top">
        <h1>{data.project} roadmap</h1>
        <p className={`rl-fresh${late ? " late" : ""}`}>
          Rendered at {clock(data.renderedAt)}. {tickAge == null
            ? "No completed supervisor tick."
            : `Last supervisor tick ${age(tickAge)} before that${data.tickLate ? " (overdue)" : ""}.`}
          {pageAge > 60 && ` This page is ${age(pageAge)} old; the render loop may have stopped.`}
        </p>
      </header>
      <p className="rl-counts">
        <span><b>{data.groups[base]}</b> in {base}</span>
        <span><b>{merging}</b> merging</span>
        <span><b>{remaining}</b> remaining</span>
        <span className="rl-dim">features</span>
        <span className={`rl-tk${data.tickets["needs you"] ? " hot" : ""}`}><b>{data.tickets["needs you"]}</b> waiting on you</span>
        <span><b>{data.tickets.blocked}</b> blocked</span>
        <span className="rl-dim">tickets</span>
        {data.prError && <span className="rl-warn">Pull requests unavailable: {data.prError}</span>}
      </p>
      <Agents data={data} />
      <div className="rl-pair"><Blocked data={data} /><NextUp data={data} /></div>
      <Roadmap data={data} />
    </div>
  );
}
