import { useEffect, useState } from "react";

export function readData() {
  const raw = document.getElementById("roadmap-data")?.textContent || "";
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function age(secs) {
  if (secs == null || Number.isNaN(secs)) return "";
  const s = Math.max(0, Math.round(secs));
  if (s >= 86400) return `${Math.floor(s / 86400)}d`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h`;
  if (s >= 60) return `${Math.floor(s / 60)}m`;
  return `${s}s`;
}

export const since = (data, t) => (t ? age(data.renderedAt - t) : "");

export function clock(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Seconds since the page was rendered, ticking; the page reloads every 15 s, so a large value means the render loop stopped. */
export function usePageAge(renderedAt) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 5000);
    return () => clearInterval(id);
  }, []);
  return now - renderedAt;
}

export const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

/** Agent states grouped into the four tones every direction colours: moving, waiting on someone, quiet, broken. */
export const TONE = {
  working: "go",
  done: "go",
  waiting: "hold",
  asking: "hold",
  "usage-limited": "hold",
  limited: "hold",
  nudged: "hold",
  quiet: "idle",
  exited: "idle",
  "no status": "idle",
  stuck: "stop",
  paused: "idle",
  stopped: "hold",
  "handing off": "hold",
  "no-agent": "idle",
};
export const tone = (state) => TONE[state] || "idle";


export const STATE_ORDER = ["working", "waiting", "asking", "nudged", "usage-limited", "limited", "quiet", "exited", "done", "no status", "no-agent", "handing off", "stopped", "stuck"];

export function agentLabel(a) {
  return [a.agent, a.model].filter(Boolean).join(" ");
}




export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

/** "waiting on you", "waiting on review", "waiting on dev-auto-fixes-53": the same words the phase uses. */
export function stateText(a) {
  if (a.state !== "waiting") return a.detail ? `${a.state}, ${a.detail}` : a.state;
  const d = (a.detail || "").replace(/^ticket:/, "");
  return `waiting on ${d === "operator" ? "you" : d || "something"}`;
}

export function phaseTone(phase) {
  if (phase === "waiting on you") return "hold";
  if (phase === "coding") return "motion";
  if (phase === "in review" || phase.startsWith("done")) return "go";
  return "idle";
}
