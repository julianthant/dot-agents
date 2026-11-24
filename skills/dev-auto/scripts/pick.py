"""Account picker for Orca workers: applies limits.toml to every account in accounts.toml.

  pick.py pick --agent claude --model claude-sonnet-5-5 [--effort medium] [--host local]
  pick.py hold --terminal <handle> --account <label> --agent A --model M [--effort E] [--host H]
  pick.py due [--host H]

`due` lists live workers on accounts past their handoff point and exits 0 only when there is one,
so an Orca automation can use it as its --precheck and wake the coordinator only then.

`pick` prints the first account, in accounts.toml order, that may take the session, or exits 1
with each account's reason. Right after `worker-start`, `hold` records which account and model
the worker's terminal uses; Orca's terminal list decides which holds are still live.
"""

from contextlib import contextmanager
import argparse
import fcntl
import os
import tempfile
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import account_usage

HERE = Path(__file__).resolve().parent
LIMITS = HERE.parent / "limits.toml"
HOLDS = account_usage.ACCOUNTS.with_name("holds.json")


def live_handles(host):
    args = ["orca", "terminal", "list", "--limit", "10000", "--json"]
    if host != "local":
        args += ["--environment", host]
    out = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise SystemExit(f"orca terminal list failed on {host}: {out.stderr.strip()}")
    result = json.loads(out.stdout)["result"]
    if result.get("truncated"):
        raise SystemExit(f"incomplete Orca terminal list on {host}")
    return {t["handle"] for t in result["terminals"]}


@contextmanager
def edit_holds(path):
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(path.read_text()) if path.exists() else {}
        holds = old.copy()
        yield holds
        if holds != old:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
                json.dump(holds, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(f.name, path)


def live_holds(host):
    with edit_holds(HOLDS) as holds:
        alive = live_handles(host)
        for handle in list(holds):
            if holds[handle]["host"] == host and handle not in alive:
                del holds[handle]
        return [r for r in holds.values() if r["host"] == host]


def windows(cfg, row, now):
    """(five_hour %, weekly %), each window from the newest reading that has it, Orca's or the CLI
    file's; a window whose reset has passed reads 0. (None, None) when the account was never read.
    Codex homes share one sessions folder whose newest row names no account: Codex reads only Orca's."""
    found = window_readings(row, now)
    if not found:
        return None, None
    out = {w: (0.0 if r and r <= now else p) for w, (p, r) in found.items()}
    return out.get("five_hour", 0.0), out.get("weekly", 0.0)


def window_readings(row, now):
    readings = account_usage.orca_usage(now, row["host"])
    key = account_usage.orca_key(row, now)
    shared = row["agent"] == "codex" or row["host"] != "local"
    found = [r for r in (readings.get(key), None if shared else account_usage.file_usage(row)) if r]
    out = {}
    for _at, read in sorted(found, key=lambda r: float(r[0])):
        out.update({w: (float(p), float(r) if r else None) for p, r, w in read})
    return out


def usage_handoffs(host):
    return (account_usage.load_json(HOLDS.with_name("usage-handoffs.json")) or {}).get(host, {})


def record_usage_handoff(cfg, host, account, now, why):
    row = next((row for row in cfg["account"] if row["host"] == host and row["id"] == account), None)
    if not row:
        return
    plan = LIM.get(row["agent"], {}).get(row.get("plan"), {})
    readings = window_readings(row, now)
    five, _ = readings.get("five_hour", (0, None))
    week, weekly_reset = readings.get("weekly", (0, None))
    # Generic limit messages usually mean the session window. The reading can still identify
    # a weekly limit, and a plan without a session window always uses the weekly reset.
    why = why.lower()
    session_hint = any(w in why for w in ("5-hour", "5h", "session"))
    weekly_hint = "weekly" in why
    weekly = ("handoff_5h" not in plan
              or (weekly_hint and not session_hint)
              or (weekly_hint == session_hint and 100 - week < MIN_LEFT_WEEKLY
                  and 100 - five >= MIN_LEFT_5H and (not weekly_reset or weekly_reset > now)))
    window, seconds = ("weekly", 7 * 86400) if weekly else ("five_hour", 5 * 3600)
    _, reset = readings.get(window, (0, None))
    until = reset if reset and reset > now else now + seconds
    with edit_holds(HOLDS.with_name("usage-handoffs.json")) as records:
        active = {a: r for a, r in records.get(host, {}).items() if r["until"] > now}
        if account not in active or active[account]["until"] < until:
            active[account] = {"until": until, "window": "weekly" if weekly else "5-hour"}
        records[host] = active


def claude_reason(plan, model, held, five, week):
    want = "sonnet" if account_usage.family(model) == "sonnet" else "opus"  # Fable counts as Opus
    if five >= plan["handoff_5h"]:
        return f"5-hour {five:.0f}% at handoff {plan['handoff_5h']}%"
    reduced = five > plan["reduced_above_5h"]
    opus = plan["reduced_opus" if reduced else "opus"]
    if "sessions" in plan:
        total = plan["reduced_sessions" if reduced else "sessions"]
        sonnet = total
    else:
        sonnet = plan["reduced_sonnet" if reduced else "sonnet"]
        total = opus + sonnet
    if week >= LIM["weekly"]["claude_sonnet_only"] and want != "sonnet":
        return f"weekly {week:.0f}%: Sonnet only"
    if week >= LIM["weekly"]["one_ticket"]:
        total = min(total, 2)
    count = sum((account_usage.family(m) == "sonnet") == (want == "sonnet") for m in held)
    cap = sonnet if want == "sonnet" else opus
    if len(held) >= total:
        return f"{len(held)} of {total} sessions"
    if count >= cap:
        return f"{want} {count} of {cap}"
    return None


def codex_reason(plan_name, plan, route, held, five, week):
    if plan_name == "prolite":
        if week >= plan["weekly_handoff"] and (not route.startswith("gpt-6.2-sol:") or held):
            return f"weekly {week:.0f}%: one gpt-6.2-sol worker only"
        return None
    weights = plan["weights"]
    if route not in weights:
        return f"no weight for {route} in limits.toml"
    if five >= plan["handoff_5h"]:
        return f"5-hour {five:.0f}% at handoff {plan['handoff_5h']}%"
    if week >= LIM["weekly"]["codex_sol_medium_only"] and route != "gpt-6.2-sol:medium":
        return f"weekly {week:.0f}%: gpt-6.2-sol medium only"
    if week >= LIM["weekly"]["one_ticket"] and len(held) >= 2:
        return f"weekly {week:.0f}%: one ticket at a time"
    # shortcut: an unknown held route weighs 2, until every start goes through `hold`
    used = sum(weights.get(r, 2) for r in held)
    if used + weights[route] > plan["capacity"]:
        return f"capacity {used} + {weights[route]} over {plan['capacity']}"
    return None


# A worker started this close to empty hands off within minutes, so starts and restarts skip the account.
MIN_LEFT_5H = 10
MIN_LEFT_WEEKLY = 5


def low_quota(plan, five, week):
    if "handoff_5h" in plan and 100 - five < MIN_LEFT_5H:  # Pro Lite has no 5-hour limit
        return f"5-hour {five:.0f}%: under {MIN_LEFT_5H}% left"
    if 100 - week < MIN_LEFT_WEEKLY:
        return f"weekly {week:.0f}%: under {MIN_LEFT_WEEKLY}% left"
    return None


def pick(cfg, agent, model, effort, host, now, holds=None):
    if holds is None:
        holds = live_holds(host)
    route = f"{model}:{effort}"
    why = []
    handoffs = usage_handoffs(host)
    # A plan's prefer_models go there first; every other model tries that plan's accounts last.
    def rank(row):
        prefer = LIM.get(agent, {}).get(row.get("plan"), {}).get("prefer_models")
        return 0 if prefer and model in prefer else 2 if prefer else 1
    for row in sorted(cfg["account"], key=rank):
        if row["agent"] != agent or row["host"] != host or not row["enabled"]:
            continue
        name = row["label"]
        handoff = handoffs.get(row["id"])
        if handoff and now < handoff["until"]:
            why.append(f"{name}: waiting for {handoff['window']} reset after usage handoff")
            continue
        plan = LIM.get(agent, {}).get(row.get("plan"))
        if plan is None:
            why.append(f"{name}: no {agent}.{row.get('plan')} plan in limits.toml")
            continue
        if agent == "claude" and "fable" in model and row["plan"] != "max":
            why.append(f"{name}: Fable runs only on Max")
            continue
        five, week = windows(cfg, row, now)
        if five is None:
            why.append(f"{name}: usage never read; run it once by hand to read it")
            continue
        if max(five, week) >= 100:
            why.append(f"{name}: limit reached (5-hour {five:.0f}%, weekly {week:.0f}%)")
            continue
        low = low_quota(plan, five, week)
        if low:
            why.append(f"{name}: {low}")
            continue
        mine = [h for h in holds if h["account"] == row["id"]]
        if agent == "claude":
            reason = claude_reason(plan, model, [h["model"] for h in mine], five, week)
        else:
            held = [f"{h['model']}:{h['effort']}" for h in mine]
            reason = codex_reason(row["plan"], plan, route, held, five, week)
        if reason:
            why.append(f"{name}: {reason}")
            continue
        return row, why
    return None, why


def past_handoff(row, five, week):
    """Why this account must stop taking work now, or None."""
    plan = LIM.get(row["agent"], {}).get(row.get("plan")) or {}
    if max(five or 0, week or 0) >= 100:
        return f"limit reached (5-hour {five or 0:.0f}%, weekly {week or 0:.0f}%)"
    if "handoff_5h" in plan and (five or 0) >= plan["handoff_5h"]:
        return f"5-hour {five:.0f}%"
    if "weekly_handoff" in plan and (week or 0) >= plan["weekly_handoff"]:
        return f"weekly {week:.0f}%"
    return None


def coordinator(cfg, host, now, active):
    """The selected Codex account also runs the coordinator bot; past its handoff point, choose another."""
    row = next((r for r in cfg["account"] if r.get("host", "local") == host and r["id"] == active), None)
    cooldown = usage_handoffs(host).get(active)
    if row is not None and (not cooldown or now >= cooldown["until"]):
        five, week = windows(cfg, row, now)
        if five is not None and past_handoff(row, five, week) is None:
            return None
    new, _ = pick(cfg, "codex", "gpt-6.2-sol", "medium", host, now)
    return new if new is not None and new["id"] != active else None


ROADMAP = Path.home() / ".orca/bots/subaru-2/roadmap/data.json"


def ready_tickets(data):
    """Open Tasks with no open blocker that no agent works on and nobody parked for later."""
    return [i for i in data["issues"] if i["open"] and i["kids"] == 0 and i.get("type") != "Feature"
            and not i["blockers"] and not i["later"] and i.get("ms") != "Later"
            and i.get("status") != "In Progress" and not i.get("agent")]


def fill(cfg, host, now, data):
    """Ready tickets and whether any Claude Sonnet or Codex Sol seat is free to take one."""
    ready = ready_tickets(data)
    seat = any(pick(cfg, a, m, "medium", host, now)[0] for a, m in (("claude", "claude-sonnet-5-5"), ("codex", "gpt-6.2-sol")))
    return ready, seat


def due(cfg, host, now):
    """Live holds whose account is past its handoff point, as (label, terminal, why)."""
    holds = json.loads(HOLDS.read_text()) if HOLDS.exists() else {}
    alive = {h for h in live_handles(host)} if holds else set()
    rows = {r["id"]: r for r in cfg["account"] if r["host"] == host}
    found = []
    for handle, rec in holds.items():
        row = rows.get(rec["account"])
        if rec["host"] != host or handle not in alive or row is None:
            continue
        why = past_handoff(row, *windows(cfg, row, now))
        if why:
            found.append((row["label"], handle, why))
    return found


def hold(cfg, args):
    row = next((r for r in cfg["account"] if r.get("host", "local") == args.host and args.account in (r["id"], r["label"])), None)
    if row is None:
        raise SystemExit(f"no account {args.account} in accounts.toml")
    with edit_holds(HOLDS) as holds:
        holds[args.terminal] = {"account": row["id"], "label": row["label"], "agent": args.agent,
                                "model": args.model, "effort": args.effort, "host": args.host,
                                "at": int(time.time())}


LIM = tomllib.loads(LIMITS.read_text())


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["pick", "select", "hold", "due", "coordinator", "fill"])
    p.add_argument("--agent", choices=["claude", "codex"])
    p.add_argument("--model")
    p.add_argument("--effort", default="medium")
    p.add_argument("--host", default="local")
    p.add_argument("--terminal")
    p.add_argument("--account")
    args = p.parse_args(argv)
    cfg = account_usage.load_config()
    if args.command == "due":
        found = due(cfg, args.host, time.time())
        for label, handle, why in found:
            print(f"{label} {handle} {why}")
        return 0 if found else 1
    if args.command == "fill":
        ready, seat = fill(cfg, args.host, time.time(), json.loads(ROADMAP.read_text()))
        if ready and seat:
            print(f"{len(ready)} ready tickets and a free seat: " + ", ".join(f"#{i['n']}" for i in ready[:10]))
        return 0 if ready and seat else 1
    def account_command(*args_):
        if args.host != "local":
            return account_usage.remote_account(args.host, *args_)
        out = subprocess.run(["orca", "account", *args_, "--json"], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)["result"]

    if args.command == "select":
        if not (args.agent and args.account):
            raise SystemExit("select needs --agent and --account")
        account_command("select", "--agent", args.agent, "--account", args.account)
        return 0
    if args.command == "coordinator":
        active = account_command("list")["codex"]["activeAccountId"]
        new = coordinator(cfg, args.host, time.time(), active)
        if new is not None:
            account_command("select", "--agent", "codex", "--account", new["id"])
            print(f"coordinator account switched to {new['label']}")
        return 0
    if not (args.agent and args.model):
        raise SystemExit(f"{args.command} needs --agent and --model")
    if args.command == "hold":
        if not (args.terminal and args.account):
            raise SystemExit("hold needs --terminal and --account")
        hold(cfg, args)
        return 0
    row, why = pick(cfg, args.agent, args.model, args.effort, args.host, time.time())
    for line in why:
        print(line, file=sys.stderr)
    if row is None:
        return 1
    select = (["orca", "account", "select"] if args.host == "local" else
              [sys.executable, str(Path(__file__).resolve()), "select", "--host", args.host])
    print(json.dumps({"id": row["id"], "label": row["label"], "plan": row["plan"], "host": args.host,
                      "select": [*select, "--agent", args.agent, "--account", row["id"]]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
