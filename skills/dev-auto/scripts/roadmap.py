#!/usr/bin/env python3
"""Roadmap of a project from stored facts: Concord ledger, .work/specs and gh pull requests."""

import argparse
import fcntl
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

DONE = ("complete", "closed")
UI = Path(__file__).parent / "roadmap-ui" / "dist" / "index.html"
HEARTBEAT_TIMEOUT_SECONDS = 180
PR_CACHE_SECONDS = 60
REFRESH_SECONDS = 15
ACTIVE = ("active", "review_ready")
TICKET_ORDER = ["needs you", "in progress", "blocked", "remaining", "done"]
FEATURE_ORDER = ["needs you", "in progress", "remaining", "done", "not ticketed"]
SEGMENTS = ("done", "in progress", "needs you", "blocked", "remaining")
SEG_CHAR = {
    "done": "#",
    "in progress": ">",
    "needs you": "!",
    "blocked": "x",
    "remaining": ".",
}
BAR_WIDTH = 20
ICON = {
    "needs you": "▸",
    "in progress": "▶",
    "blocked": "⏳",
    "remaining": "○",
    "done": "✅",
}
FAILED = (
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "ERROR",
    "STARTUP_FAILURE",
)


def stamp(text):
    return datetime.fromisoformat(text)


def ago(then, now):
    secs = max(0, int((now - then).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return f"{secs}s ago"


def read_ledger():
    root = os.environ.get("CONCORD_REPO_ROOT", "")
    db = Path(root).expanduser() / ".concord" / "concord.db"
    if not root or not db.exists():
        return []
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM tasks LIMIT 1")
        tickets = {
            (r["project"], r["task_id"]): {**dict(r), "updates": []}
            for r in con.execute(
                "SELECT project, task_id, title, status, agent, agent_id, assigned_agent_id,"
                " notes, updated_at FROM tasks"
            )
        }
        for r in con.execute(
            "SELECT project, task_id, kind, content, agent, created_at, superseded_by"
            " FROM task_updates ORDER BY id"
        ):
            if (r["project"], r["task_id"]) in tickets:
                tickets[r["project"], r["task_id"]]["updates"].append(dict(r))
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return list(tickets.values())


def latest_wait(t):
    """The owner's newest live update when it is a Waiting on blocker, else ""."""
    mine = [
        u
        for u in t["updates"]
        if u["agent"] == t["agent_id"] and u["superseded_by"] is None
    ]
    last = mine[-1] if mine else None
    if last and last["kind"] == "blocker" and last["content"].startswith("Waiting on:"):
        return last["content"]
    return ""


def classify(tickets, now, workers=None):
    done_ids = {t["task_id"] for t in tickets if t["status"] in DONE}
    known = {t["task_id"] for t in tickets}
    for t in tickets:
        title = t["title"]
        t["feature"], _, t["lane"] = (
            title.partition(" / ") if " / " in title else ("", "", title)
        )
        notes = t["notes"] or ""
        found = re.search(r"(?i)Blocked by:\s*([^\n]*)", notes)
        part = re.split(r"(?i)Waiting on:", found.group(1))[0].strip() if found else ""
        none = part.lower().startswith("none")
        t["all_blockers"] = (
            [] if none else [w for w in re.findall(r"[\w-]+", part) if w in known]
        )
        t["blockers"] = [b for b in t["all_blockers"] if b not in done_ids]
        t["blocked_note"] = (
            "" if t["all_blockers"] or part.lower().rstrip(".") == "none" else part
        )
        op = re.search(r"(?i)Waiting on:\s*operator\s*(?:\(([^)]*)\))?", notes)
        brief = re.sub(r"(?i)Blocked by:[^\n]*", "", notes)
        brief = re.sub(r"(?i)^\s*brief:\s*", "", brief.strip())
        t["brief"] = re.split(r"(?<=[.!?])\s", " ".join(brief.split()), maxsplit=1)[0]
        t["wait"] = wait = latest_wait(t)
        live = [u for u in t["updates"] if u["superseded_by"] is None]
        t["detail_wait"] = next(
            (u for u in reversed(live) if u["content"].startswith("Waiting on:")),
            {"content": op.group(0)} if op else None,
        )
        t["verdict"] = next(
            (u for u in reversed(live) if u["content"].startswith("REVIEW VERDICT:")),
            None,
        )
        t["recent"] = t["updates"][-5:]
        t["fields"] = {
            k: next(iter(re.findall(rf"(?i)\b{k}:\s*([^\n·]+)", notes)), "").strip()
            for k in ("Skills", "Coder", "Reviewer", "Tier", "Host")
        }
        t["paths"] = owned_paths(notes)
        t["area"] = area(t["paths"])
        t["notes_text"] = re.sub(r"(?i)^\s*brief:\s*", "", notes.strip())
        seen = max([t["updated_at"]] + [u["created_at"] for u in t["updates"]])
        t["age"] = ago(stamp(seen), now)
        if t["status"] in DONE:
            state, detail = "done", "done"
        elif wait.startswith("Waiting on: operator"):
            state, detail = "needs you", wait.removeprefix("Waiting on: ").rstrip(".")
        elif op and not wait and not t["agent_id"]:
            state, detail = "needs you", f"operator {op.group(1) or ''}".strip()
        elif t["agent_id"] and t["status"] in ACTIVE:
            state = "in progress"
            detail = f"{t['agent_id']}, {t['age']}"
            if wait:
                detail += ", waiting on " + wait.removeprefix("Waiting on: ").rstrip(
                    "."
                )
        elif t["blockers"]:
            state, detail = "blocked", "blocked by " + ", ".join(t["blockers"])
        else:
            state = "remaining"
            who = t["assigned_agent_id"]
            detail = f"assigned to {who}, not accepted" if who else "unowned"
        t["state"], t["detail"] = state, detail
    owners = path_owners(tickets, workers)
    for t in tickets:
        tid = t["task_id"]
        t["path_blockers"] = []
        if (
            t["state"] == "remaining"
            and t["status"] == "proposed"
            and not (t["agent_id"] or t["assigned_agent_id"])
            and not tid.endswith("-review")
        ):
            t["path_blockers"] = [
                owner["task_id"]
                for owner in owners.values()
                if owner["task_id"] != tid and overlaps(t["paths"], owner["paths"])
            ]
            if t["path_blockers"]:
                t["state"] = "blocked"
                t["detail"] = "blocked by " + ", ".join(t["path_blockers"])
    states = {t["task_id"]: t["state"] for t in tickets}
    for t in tickets:
        t["owner"] = t["agent_id"] or t["assigned_agent_id"] or ""
        t["blocked_states"] = [
            (b, states.get(b, "not in ledger"))
            for b in t["all_blockers"] + t["path_blockers"]
        ]
    return tickets


def plural(n):
    return f"{n} ticket{'' if n == 1 else 's'}"


def owned_paths(notes):
    found = re.search(r"(?i)Owned paths:\s*([^\n]*)", notes)
    if not found:
        return []
    unannotated = []
    depth = 0
    for part in re.split(r"([()])", found.group(1)):
        if part == "(":
            if depth == 0:
                unannotated.append(" ")
            depth += 1
        elif part == ")" and depth:
            depth -= 1
        elif depth == 0:
            unannotated.append(part)
    parts = (p.lstrip(" `").rstrip(" .`") for p in "".join(unannotated).split(","))
    # Free text ("none in the repo (...)") is not a path.
    return [
        p.rstrip("/") for p in parts if p.strip("/") and not re.search(r"[\s()]", p)
    ]


def area(paths):
    """Shortest set of directory prefixes covering the paths; '.' stands for files at the root."""
    dirs = set()
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        dirs.add(p if "." not in base else p.rpartition("/")[0] or ".")
    keep = sorted(
        d
        for d in dirs
        if d == "."
        or not any(o != d and o != "." and d.startswith(o + "/") for o in dirs)
    )
    return ", ".join(keep) or "-"


def overlaps(a, b):
    return any(
        x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in a for y in b
    )


def path_owners(tickets, workers):
    """Use fill's worker records and coder/reviewer grouping to find held paths."""
    if not isinstance(workers, dict):
        return {}
    by_key = {(t["project"], t["task_id"]): t for t in tickets}
    owners = {}
    for tid, rec in workers.items():
        if not isinstance(rec, dict) or rec.get("done") or rec.get("lost"):
            continue
        project = rec.get("project", "")
        held = by_key.get((project, tid))
        if held and held["status"] not in DONE:
            base = tid.removesuffix("-review")
            owners.setdefault(base, by_key.get((project, base)) or held)
    return owners


def read_state():
    """supervisor/state.json; empty when the file is absent, unreadable or not an object."""
    root = os.environ.get("CONCORD_REPO_ROOT", "")
    try:
        data = json.loads(
            (Path(root).expanduser() / "supervisor" / "state.json").read_text()
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_status():
    """supervisor/status.json, the rows each tick writes; None when absent or unreadable."""
    root = os.environ.get("CONCORD_REPO_ROOT", "")
    try:
        data = json.loads(
            (Path(root).expanduser() / "supervisor" / "status.json").read_text()
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("rows"), list) else None


def read_workers():
    """The tick's status rows keyed by ticket id, from status.json; a ledger whose tick has
    written none yet falls back to state.json's records. Empty when neither can be read."""
    status = read_status()
    if status is not None:
        return {
            r["ticket"]: r | {"snapshot_at": number(status.get("at"))}
            for r in status["rows"]
            if isinstance(r, dict) and r.get("ticket")
        }
    state = read_state()
    workers = state.get("workers")
    if not isinstance(workers, dict):
        return {}
    scan_at = scan_start(state.get("tick"))
    return {
        key: row | {"snapshot_at": scan_at}
        for key, row in workers.items()
        if isinstance(row, dict)
    }


def counts(tickets):
    return {s: sum(t["state"] == s for t in tickets) for s in SEGMENTS}


def apportion(cnt, units, floor_one=False):
    """Whole numbers per state that sum to `units` (largest remainder); all zero for no tickets."""
    total = sum(cnt.values())
    if not total:
        return dict.fromkeys(cnt, 0)
    raw = {k: v * units / total for k, v in cnt.items()}
    out = {k: int(r) for k, r in raw.items()}
    extra = sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)
    for k in extra[: units - sum(out.values())]:
        out[k] += 1
    if floor_one:
        for k in [k for k, v in cnt.items() if v and not out[k]]:
            out[max(out, key=out.get)] -= 1
            out[k] = 1
    return out


def headline(feats):
    tickets = [t for f in feats for t in f["tickets"]]
    left = sum(t["state"] != "done" for t in tickets)
    done = sum(f["state"] == "done" for f in feats)
    return (
        f"{left} of {len(tickets)} tickets left · {done} of {len(feats)} features done"
    )


def count_text(cnt):
    return ", ".join(f"{cnt[s]} {s}" for s in SEGMENTS if cnt[s]) or "no tickets"


def text_bar(cnt):
    share = apportion(cnt, BAR_WIDTH, floor_one=True)
    return "".join(SEG_CHAR[s] * share[s] for s in SEGMENTS).ljust(BAR_WIDTH, "-")


def key_line():
    return "key: " + "  ".join(f"{SEG_CHAR[s]} {s}" for s in SEGMENTS) + "  - empty"


def machine(t):
    return (t.get("worker") or {}).get("host") or t["fields"]["Host"] or "not assigned"


def minutes(rec, now):
    since = rec.get("launched_at")
    return f"{max(0, int((now.timestamp() - since) // 60))} min" if since else "?"


def running(tickets, now):
    """One row per recorded worker whose ticket is open."""
    rows = []
    for t in tickets:
        w = t.get("worker")
        if not w or t["state"] == "done":
            continue
        shown = w.get("status")
        state = shown.get("state") if isinstance(shown, dict) else None
        rows.append({
            "feature": t["feature"] or "No feature",
            "lane": t["lane"],
            "area": t["area"],
            "agent": " ".join(
                x for x in (w.get("agent"), w.get("model"), w.get("effort")) if x
            ),
            "account": w.get("account") or "?",
            "machine": machine(t),
            "host": short_host(machine(t)),
            "role": "reviewer" if t["task_id"].endswith("-review") else "coder",
            "lost": bool(w.get("lost") or w.get("stopped")),
            "stopped": bool(w.get("stopped")),
            "live_slot": w.get("live_slot", not any(w.get(key) for key in (
                "done", "stopped", "lost", "held", "exited_at",
            )) and state != "exited"),
            "state": str(state or "inactive"),
            "running": minutes(w, now),
        })
    return rows


def host_counts(rows):
    """Per host, [coders, reviewers] holding live slots, busiest host first."""
    hosts = {}
    for r in rows:
        if r["live_slot"]:
            hosts.setdefault(r["host"], [0, 0])[r["role"] == "reviewer"] += 1
    return sorted(hosts.items(), key=lambda kv: (-sum(kv[1]), kv[0]))


def count_words(coders, reviewers):
    return f"{plural_n(coders, 'coder')}, {plural_n(reviewers, 'reviewer')}"


def plural_n(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def plan_row(t):
    f = t["fields"]
    return {
        "feature": t["feature"] or "No feature",
        "lane": t["lane"],
        "area": t["area"],
        "tier": f["Tier"] or "-",
        "coder": f["Coder"] or "-",
        "reviewer": f["Reviewer"] or "-",
        "machine": machine(t),
        "state": t["state"],
        "paths": t["paths"],
    }


def plan(tickets):
    """Operator tickets first, then waves from the Blocked by edges; each wave splits into groups
    that run at once, and a group of several tickets runs one at a time (owned paths overlap)."""
    open_ = {t["task_id"]: t for t in tickets if t["state"] != "done"}
    depth = {}

    def level(key, path):
        if key in depth:
            return depth[key]
        t = open_[key]
        if t["state"] == "needs you":
            lv = 1
        elif key in path:
            return 0  # a cycle: drop the edge that closes it
        else:
            lv = 1 + max(
                (level(b, path | {key}) for b in t["blockers"] if b in open_), default=0
            )
        depth[key] = lv
        return lv

    rows = {k: plan_row(t) for k, t in open_.items()}
    order = lambda k: (rows[k]["feature"], rows[k]["lane"], k)
    waves = {}
    for k in open_:
        if open_[k]["state"] != "needs you":
            waves.setdefault(level(k, frozenset()), []).append(k)
    out = []
    for n in sorted(waves):
        keys, groups = sorted(waves[n], key=order), []
        for k in keys:
            hit = [
                g
                for g in groups
                if any(overlaps(rows[k]["paths"], rows[j]["paths"]) for j in g)
            ]
            merged = [k] + [j for g in hit for j in g]
            groups = [g for g in groups if g not in hit] + [sorted(merged, key=order)]
        groups.sort(key=lambda g: (len(g) > 1, order(g[0])))
        out.append({"n": n, "groups": [[rows[k] for k in g] for g in groups]})
    return {
        "needs": [
            rows[k]
            for k in sorted(open_, key=order)
            if open_[k]["state"] == "needs you"
        ],
        "waves": out,
    }


def plan_text(p):
    out = ["PLAN"]
    if not (p["needs"] or p["waves"]):
        return out + ["  (nothing left)"]
    row = lambda r: clip(
        f"{r['feature']} / {r['lane']} · {r['state']} · {r['tier']} · "
        f"{r['coder']} → {r['reviewer']} · {r['machine']} · {r['area']}",
        96,
    )
    if p["needs"]:
        out.append(f"  Needs you ({len(p['needs'])})")
        out += [f"    {row(r)}" for r in p["needs"]]
    for w in p["waves"]:
        total = sum(len(g) for g in w["groups"])
        out.append(f"  Wave {w['n']} · {plural(total)}, {len(w['groups'])} at once")
        for g in w["groups"]:
            if len(g) > 1:
                out.append("    one at a time:")
            out += [f"    {'  ' if len(g) > 1 else ''}{row(r)}" for r in g]
    return out


def running_text(rows):
    live = [r for r in rows if r["live_slot"]]
    inactive = [r for r in rows if not r["live_slot"]]
    hosts = host_counts(rows)
    parts = [
        f"{h} {c + v} ({count_words(c, v) if i == 0 else f'{c}, {v}'})"
        for i, (h, (c, v)) in enumerate(hosts)
    ]
    head = "RUNNING NOW"
    out = (
        [f"{head} \u00b7 {plural_n(len(live), 'worker')}: " + ", ".join(parts)]
        if parts
        else [head]
    )
    out += [
        clip(
            f"  {r['host']} \u00b7 {r['agent']} \u00b7 {r['feature']} / {r['lane']} \u00b7 "
            f"{r['area']} \u00b7 {r['account']} \u00b7 {r['running']}"
        )
        for r in live
    ]
    for r in inactive:
        label = "PAUSED" if r["stopped"] else "LOST" if r["lost"] else r["state"].upper()
        out.append(
            clip(
                f"  {label}, not counted \u00b7 {r['host']} \u00b7 launched {r['running']} ago \u00b7 {r['lane']}"
            )
        )
    return out if rows else [head, "  No workers running"]


def read_specs(repo):
    specs = {}
    for f in sorted((repo / ".work" / "specs").glob("*.md")):
        text = f.read_text()
        title = re.search(r"(?m)^# (.+)$", text)
        body = re.sub(r"(?m)^#.*$", "", text).strip().split("\n\n", 1)[0]
        specs[f.stem] = {
            "title": title.group(1).strip() if title else f.stem,
            "summary": " ".join(body.split()),
            "path": f".work/specs/{f.name}",
            "text": text,
        }
    return specs


def read_prs(repo):
    """(prs, error), reusing the list fetched in the last minute from <ledger>/roadmap-prs.json."""
    root = os.environ.get("CONCORD_REPO_ROOT", "")
    cache = Path(root).expanduser() / "roadmap-prs.json"
    if root:
        try:
            if time.time() - cache.stat().st_mtime < PR_CACHE_SECONDS:
                prs = json.loads(cache.read_text())
                if isinstance(prs, list) and all(isinstance(p, dict) and "baseRefName" in p for p in prs):
                    return prs, ""
        except (OSError, ValueError):
            pass
    prs, error = fetch_prs(repo)
    if root and prs is not None:
        try:
            write_atomic(cache, json.dumps(prs))
        except OSError:
            pass
    return prs, error


def read_accounts():
    """{account id: email} from `orca account list --json`, reusing the map read in the last minute from <ledger>/roadmap-accounts.json; {} when Orca cannot say (not cached)."""
    root = os.environ.get("CONCORD_REPO_ROOT", "")
    cache = Path(root).expanduser() / "roadmap-accounts.json"
    if root:
        try:
            if time.time() - cache.stat().st_mtime < PR_CACHE_SECONDS:
                return json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
    try:
        done = subprocess.run(
            ["orca", "account", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if done.returncode:
            return {}
        result = json.loads(done.stdout)["result"]
        emails = {
            a["id"]: a["email"]
            for group in result.values()
            for a in group.get("accounts", [])
            if a.get("id") and a.get("email")
        }
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError):
        return {}
    if root:
        try:
            write_atomic(cache, json.dumps(emails))
        except OSError:
            pass
    return emails


def account_labels():
    """{account id: label} from accounts.toml; {} when unreadable."""
    try:
        rows = tomllib.loads(
            (Path(__file__).parent.parent / "accounts.toml").read_text()
        ).get("account", [])
        return {r["id"]: r["label"] for r in rows if r.get("id") and r.get("label")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def local_name():
    """This Mac's name, for workers on host "local"."""
    try:
        done = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname()


def fetch_prs(repo):
    """(prs, error): open PRs with checks, all others without them (one big query returns 502)."""
    fields = "number,title,headRefName,baseRefName,state,isDraft,mergedAt,url"
    prs = {}
    for state, extra in (("all", ""), ("open", ",statusCheckRollup")):
        cmd = [
            "gh",
            "pr",
            "list",
            "--state",
            state,
            "--limit",
            "100",
            "--json",
            fields + extra,
        ]
        try:
            done = subprocess.run(
                cmd, cwd=repo, capture_output=True, text=True, check=False, timeout=60
            )
            if done.returncode:
                return None, (done.stderr.strip().splitlines() or ["gh failed"])[0]
            prs.update({p["number"]: p for p in json.loads(done.stdout)})
        except (OSError, subprocess.SubprocessError, ValueError) as err:
            return None, str(err).splitlines()[0] if str(err) else type(err).__name__
    return list(prs.values()), ""


def checks(pr):
    ok = bad = wait = skipped = 0
    for c in pr.get("statusCheckRollup") or []:
        result = c.get("conclusion") or c.get("state") or ""
        if result in FAILED:
            bad += 1
        elif c.get("status", "COMPLETED") != "COMPLETED" or result in (
            "",
            "PENDING",
            "EXPECTED",
        ):
            wait += 1
        elif result == "SKIPPED":
            skipped += 1
        else:
            ok += 1
    return ok, bad, wait, skipped


def pr_text(pr):
    ok, bad, wait, skipped = checks(pr)
    state = "draft" if pr.get("isDraft") else pr["state"].lower()
    return (
        f"PR #{pr['number']} {state}, checks {ok} passing, {bad} failing, "
        f"{wait} pending, {skipped} skipped"
    )


def delivery_branches(tickets, feature):
    branches = set()
    for ticket in tickets:
        notes = ticket.get("notes") or ""
        found = re.search(r"(?m)^(?:[^:\n·]+:[^\n·]*·\s*)*Feature branch:\s*(?:`([^`\s]+)`|(none)\b)", notes)
        if not found:
            continue
        if found[1] and found[1] != "none":
            branches.add(found[1])
        else:
            standalone = re.search(r"(?m)^(?:[^:\n·]+:[^\n·]*·\s*)*Branch:\s*`([^`\s]+)`", notes)
            if standalone:
                branches.add(standalone[1])
    if branches or feature == "No feature":
        return branches
    return {f"feature/{feature}", f"feat/{feature}"}


def build(repo, now):
    tickets = classify(read_ledger(), now, read_state().get("workers"))
    workers = read_workers()
    for t in tickets:
        t["worker"] = workers.get(t["task_id"])
    specs = read_specs(repo)
    prs, gh_error = read_prs(repo)
    names = {t["feature"] or "No feature" for t in tickets} | set(specs)
    feats = []
    for name in names:
        mine = sorted(
            (t for t in tickets if (t["feature"] or "No feature") == name),
            key=lambda t: (TICKET_ORDER.index(t["state"]), t["task_id"]),
        )
        states = {t["state"] for t in mine}
        done = sum(t["state"] == "done" for t in mine)
        if not mine:
            state = "not ticketed"
        elif done == len(mine):
            state = "done"
        else:
            state = next(
                (s for s in ("needs you", "in progress") if s in states), "remaining"
            )
        branches = delivery_branches(mine, name)
        feats.append(
            {
                "name": name,
                "state": state,
                "tickets": mine,
                "done": done,
                "spec": specs.get(name),
                "prs": [p for p in prs or [] if p["headRefName"] in branches],
            }
        )
    feats.sort(
        key=lambda f: (
            FEATURE_ORDER.index(f["state"]),
            f["name"] == "No feature",
            f["name"],
        )
    )
    return tickets, feats, gh_error


def clip(line, width=100):
    return line if len(line) <= width else line[: width - 1] + "…"


def bar_line(f):
    cnt = counts(f["tickets"])
    return clip(f"  [{text_bar(cnt)}] {count_text(cnt)}")


def render_text(tickets, feats, gh_error, now, base="staging"):
    cnt = counts(tickets)
    grouped = groups(feats, base)
    out = [
        headline(feats),
        clip(f"{grouped[base]} in {base} · {grouped['merging']} merging · {grouped['remaining']} remaining"),
        f"ROADMAP · {now:%Y-%m-%d %H:%M} UTC",
        key_line(),
        clip(f"ALL  [{text_bar(cnt)}] {count_text(cnt)}"),
        "",
        *running_text(running(tickets, now)),
        "",
        *plan_text(plan(tickets)),
    ]
    if not tickets:
        out.append("no tickets yet")
    need = [
        (f["name"], t) for f in feats for t in f["tickets"] if t["state"] == "needs you"
    ]
    out += ["", "NEEDS YOU"] + [
        clip(f"  ▸ {n} · {t['lane']} — {t['detail']}") for n, t in need
    ]
    if not need:
        out.append("  (nothing)")
    for f in feats:
        label = "not ticketed yet" if f["state"] == "not ticketed" else f["state"]
        out += [
            "",
            clip(
                f"{f['name'].upper()}  {f['done']}/{len(f['tickets'])} done · {label}"
            ),
        ]
        out.append(bar_line(f))
        if f["spec"]:
            out.append(clip(f"  {f['spec']['title']} ({f['spec']['path']})"))
        out += [clip(f"  {pr_text(p)}") for p in f["prs"]]
        for i, t in enumerate(f["tickets"]):
            branch = "└─" if i == len(f["tickets"]) - 1 else "├─"
            line = f"  {branch} {ICON[t['state']]} {t['lane']} · {t['detail']}"
            out.append(clip(line + (f" — {t['brief']}" if t["brief"] else "")))
    if gh_error:
        out += ["", f"PR data unavailable: {gh_error}"]
    return "\n".join(out)


COLUMNS = ("needs you", "in progress", "blocked", "remaining", "done")


def board_text(feats):
    """The per-feature board in the shape of Backlog.md's `board -m` (idea only, no code copied)."""
    out = [headline(feats), key_line(), ""]
    for f in feats:
        out.append(f"## {f['name']} ({f['done']}/{len(f['tickets'])} done)")
        out.append(bar_line(f).strip())
        for col in COLUMNS:
            cards = [t for t in f["tickets"] if t["state"] == col]
            out.append(f"### {col.title()} ({len(cards)})")
            for t in cards:
                who = f" [@{t['owner']}]" if t["owner"] else ""
                out.append(clip(f"- **{t['task_id']}** - {t['lane']}{who}"))
        out.append("")
    return "\n".join(out).rstrip()


def pr_state(f):
    return pr_text(f["prs"][0]) if f["prs"] else "no PR"


HOSTS = re.compile(r"Juzzark-WSL|Mac2|main Mac")


def short_host(text):
    """Short machine name from the host text before its first "." or "(": every known host named there joined with " or "; a bare one-word value (a supervisor host id) stays."""
    head = re.split(r"[.(]", text or "", maxsplit=1)[0]
    if re.match(r"\s*any\b", head):
        return "any backend"
    if found := list(dict.fromkeys(HOSTS.findall(head))):
        return " or ".join(found)
    return text if re.fullmatch(r"[a-z][\w-]{0,15}", text or "") else "not assigned"


def depths(tickets):
    """Wave of every ticket, done ones included: 1 + the deepest blocker; a cycle drops its closing edge."""
    by = {t["task_id"]: t for t in tickets}
    depth = {}

    def level(key, path):
        if key in depth:
            return depth[key]
        if key in path:
            return 0
        lv = 1 + max(
            (level(b, path | {key}) for b in by[key]["all_blockers"] if b in by),
            default=0,
        )
        depth[key] = lv
        return lv

    return {k: level(k, frozenset()) for k in by}


def title(t):
    return f"{t['feature'] or 'No feature'} / {t['lane']}"


def fix_rounds(t):
    """REVIEW VERDICT: FIX notes on the ticket, the rule dev_auto.checkin_text uses."""
    return sum(u["content"].startswith("REVIEW VERDICT: FIX") for u in t["updates"])


def phase(t, by):
    """Where a ticket is, from its latest wait note, its review requests and verdicts, and its
    reviewer's live state and wakes; state.json has no phase. A FIX verdict newer than the
    latest REVIEW READY is fixing; an open request is in review once the reviewer works or
    dev-auto woke it for that request's SHA since the latest verdict."""
    if t["state"] == "done":
        return "done, closing"
    wait = t["wait"].removeprefix("Waiting on: ")
    low = wait.lower()
    if low.startswith("ci"):
        return "waiting on CI"
    if low.startswith("operator"):
        return "waiting on you"
    if low.startswith("ticket:") and wait[7:].split():
        return "waiting on " + wait[7:].split()[0]
    owner = t["agent_id"]

    def request(line):
        """The SHA a review request line names, "" for none, None when it is no request:
        dev_auto.review_request's rules."""
        if line.startswith("REVIEW READY"):
            sha = (line.split()[2:3] or [""])[0]
            return sha if re.fullmatch(r"[0-9a-f]{7,40}", sha) else ""
        if "REVIEW READY" not in line or not line.startswith("Waiting on:"):
            return None
        target = (line.removeprefix("Waiting on:").split() or [""])[0].rstrip(".,;")
        if target.lower() != "review":
            return None
        found = sorted(
            {s for s in re.findall(r"\b[0-9a-f]{7,40}\b", line) if re.search(r"[a-f]", s)},
            key=len,
        )
        return found[-1] if found and all(found[-1].startswith(s) for s in found) else None

    # (update, SHA) per request line since the newest verdict, as review_requests reads them.
    verdict, asks = None, []
    for u in t["updates"]:
        if u.get("agent") != owner and u["content"].startswith("REVIEW VERDICT:"):
            verdict, asks = u, []
        elif u.get("agent") == owner:
            asks += [
                (u, sha)
                for line in u["content"].splitlines()
                if (sha := request(line)) is not None
            ]
    if not asks and verdict and verdict["content"].startswith("REVIEW VERDICT: FIX"):
        return "fixing"

    def same(a, b):
        return a.startswith(b) or b.startswith(a) if a and b else a == b

    # A request repeated for one SHA is the same round, as dev-auto wakes for it once.
    last = asks[-1][1] if asks else ""
    ask = next((u for u, s in asks if same(s, last)), None)
    if not (ask or low.startswith("review")):
        return "coding"
    review = by.get(t["task_id"] + "-review") or {}
    rev = review.get("worker") or {}
    st = rev.get("status") if isinstance(rev.get("status"), dict) else {}
    wake = f"dev-auto: REVIEW READY was posted on ticket {t['task_id']}."
    woken = ask is not None and any(
        u["content"].startswith(wake) and stamp(u["created_at"]) >= stamp(ask["created_at"])
        for u in review.get("updates", [])
    )
    return "in review" if st.get("state") == "working" or woken else "waiting on review"


def account(name, emails, label=None):
    """The account's email, else the label the tick recorded or its accounts.toml label, else the name as written with a uuid-like id shortened to its first 8 characters."""
    name = name or "?"
    if known := emails.get(name) or label or account_labels().get(name):
        return known
    return name[:8] if re.fullmatch(r"[0-9a-f-]{16,}", name) else name


def number(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def scan_start(tick):
    tick = tick if isinstance(tick, dict) else {}
    tick_at = number(tick.get("at"))
    duration = number(tick.get("seconds"))
    if tick_at is not None and duration is not None and duration >= 0:
        return tick_at - duration
    return None


def agents(tickets, now, emails=None, *, scan_at=None):
    """The agents table: running()'s rows grouped per worktree; a lost or stopped record is a "paused" row that keeps its device."""
    emails = emails or {}
    here = local_name()
    by = {t["task_id"]: t for t in tickets}
    open_ = [t for t in tickets if t.get("worker") and t["state"] != "done"]
    groups, states = {}, {}
    for r, t in zip(running(tickets, now), open_):
        w = t["worker"]
        wt = str(w.get("worktree") or "").rstrip("/").rsplit("/", 1)[-1]
        base = by.get(t["task_id"].removesuffix("-review"), t)
        g = groups.setdefault(
            (r["host"], wt),
            {
                "host": r["host"],
                "worktree": wt,
                "title": title(base),
                # The tick's phase when its status row holds one.
                "phase": (base.get("worker") or w).get("phase") or phase(base, by),
                "fixRounds": fix_rounds(base),
                "agents": [],
            },
        )
        device = here if r["host"] == "local" else r["host"]
        if r["lost"]:
            g["agents"].append(
                {
                    "key": t["task_id"],
                    "role": "",
                    "device": device,
                    "agent": "",
                    "model": "",
                    "account": "",
                    "state": "paused",
                    "detail": "",
                    "since": None,
                    "seen": None,
                    "stale": False,
                }
            )
            continue
        st = w.get("status") if isinstance(w.get("status"), dict) else {}
        state = str(st.get("state") or "no status")
        states[state] = states.get(state, 0) + 1
        seen = number(st.get("seen"))
        snapshot_at = w.get("snapshot_at")
        # State may have advanced since the worker snapshot was read.
        observed_scan = (
            min(scan_at, snapshot_at)
            if scan_at is not None and snapshot_at is not None
            else scan_at
        )
        # The supervisor rounds its timestamps.
        stale = (
            observed_scan is not None and seen is not None and observed_scan - seen > 1
        )
        g["agents"].append(
            {
                "key": t["task_id"],
                "role": r["role"],
                "device": device,
                "agent": w.get("agent"),
                "model": w.get("model"),
                "account": account(w.get("account"), emails, w.get("account_label")),
                "effort": w.get("effort"),
                "context": number(w.get("context")),
                "state": state,
                "detail": str(st.get("detail") or ""),
                "since": number(st.get("since")),
                "seen": seen,
                "stale": stale,
            }
        )
    for g in groups.values():
        g["agents"].sort(key=lambda a: (a["role"] == "", a["role"]))
    rows = sorted(groups.values(), key=lambda g: (g["host"], g["title"]))
    return {"worktrees": rows, "counts": states}


def repo_base(repo):
    """The base named for this origin in accounts.toml, defaulting to staging."""
    path = Path(
        os.environ.get("DEV_AUTO_ACCOUNTS") or Path(__file__).parent.parent / "accounts.toml"
    )
    try:
        repos = tomllib.loads(path.read_text()).get("repos", {})
        if not repos:
            return "staging"
        done = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if done.returncode:
            return "staging"
        url = done.stdout.strip()
        found = re.search(r"([\w.-]+)[/:]([\w.-]+?)(?:\.git)?/?$", url)
        slug = f"{found[1]}/{found[2]}" if found else url
        return repos.get(slug, {}).get("base", "staging")
    except (OSError, ValueError, subprocess.SubprocessError):
        return "staging"


def groups(feats, base="staging"):
    """Features in the repo's base (all done, a PR merged), merging and remaining."""
    out = {base: 0, "merging": 0, "remaining": 0}
    for f in feats:
        if not f["tickets"]:
            continue
        prs = [p for p in f["prs"] if p.get("baseRefName") == base]
        if any(p["state"] == "OPEN" for p in prs):
            out["merging"] += 1
        elif f["state"] != "done":
            out["remaining"] += 1
        elif any(p.get("mergedAt") for p in prs):
            out[base] += 1
    return out


def blocked(tickets):
    """Blocked tickets and the six blockers that hold the most of them."""
    by = {t["task_id"]: t for t in tickets}
    holds = {}
    for t in tickets:
        if t["state"] == "blocked":
            for b in t["blockers"] + t["path_blockers"]:
                holds[b] = holds.get(b, 0) + 1
    top = sorted(holds.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    return {
        "total": sum(t["state"] == "blocked" for t in tickets),
        "top": [
            {"id": b, "title": title(by[b]), "state": by[b]["state"], "holds": n}
            for b, n in top
        ],
    }


def detail(t, by, now):
    """What a square's panel shows: fields as the ledger gives them, ages left to the page (epoch seconds)."""
    f = t["fields"]
    who = []
    for key, role in ((t["task_id"], "coder"), (t["task_id"] + "-review", "reviewer")):
        w = (by.get(key) or {}).get("worker")
        if w and not w.get("lost") and by[key]["state"] != "done":
            st = w.get("status") if isinstance(w.get("status"), dict) else {}
            who.append(
                {
                    "role": role,
                    "agent": w.get("agent"),
                    "model": w.get("model"),
                    "state": str(st.get("state") or "no status"),
                    "since": number(st.get("since")),
                }
            )
    live = [u for u in t["updates"] if u["superseded_by"] is None]
    last = live[-1] if live else None
    return {
        "title": t["title"],
        "tier": f["Tier"][:60],
        "coder": f["Coder"][:60],
        "reviewer": f["Reviewer"][:60],
        "who": who,
        "blockers": [list(b) for b in t["blocked_states"]],
        "wait": t["wait"] or t["blocked_note"],
        "fix": fix_rounds(t),
        "latest": {
            "line": (last["content"].splitlines() or [""])[0][:160],
            "at": stamp(last["created_at"]).timestamp(),
        }
        if last
        else None,
    }


def matrix(tickets, feats, now):
    """The features-by-wave grid: per feature its PR line and each ticket with its wave and panel detail."""
    wave = depths(tickets)
    by = {t["task_id"]: t for t in tickets}
    return {
        "waves": max(wave.values(), default=1),
        "features": [
            {
                "name": f["name"],
                "state": f["state"],
                "done": f["done"],
                "total": len(f["tickets"]),
                "pr": pr_state(f),
                "tickets": [
                    {
                        "id": t["task_id"],
                        "lane": t["lane"],
                        "state": t["state"],
                        "wave": wave[t["task_id"]],
                        "detail": detail(t, by, now),
                    }
                    for t in f["tickets"]
                ],
            }
            for f in feats
        ],
    }


def dashboard_data(tickets, feats, gh_error, now, base="staging"):
    tick = read_state().get("tick")
    tick = tick if isinstance(tick, dict) else {}
    tick_at = number(tick.get("at"))
    tick_late = tick_at is None or now.timestamp() - tick_at > HEARTBEAT_TIMEOUT_SECONDS
    scan_at = scan_start(tick)
    return {
        "project": Path(os.environ.get("CONCORD_REPO_ROOT", "")).name or "Project",
        "renderedAt": now.timestamp(),
        "tickAt": tick_at,
        "tickLate": tick_late,
        "prError": gh_error,
        "base": base,
        "groups": groups(feats, base),
        "tickets": counts(tickets),
        "agents": agents(tickets, now, read_accounts(), scan_at=scan_at),
        "blocked": blocked(tickets),
        "nextUp": [
            {"id": t["task_id"], "title": title(t), "detail": t["detail"]}
            for t in tickets
            if t["state"] == "remaining"
        ],
        "matrix": matrix(tickets, feats, now),
    }


def render_html(tickets, feats, gh_error, now, base="staging"):
    data = json.dumps(
        dashboard_data(tickets, feats, gh_error, now, base), separators=(",", ":")
    )
    # "<" escaped so no ledger text can end the data script early
    return UI.read_text().replace("__ROADMAP_DATA__", data.replace("<", "\\u003c"), 1)


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def refresh_document(repo):
    now = datetime.now(UTC)
    return render_html(*build(repo, now), now, repo_base(repo))


def current_refresh(pidfile, identity):
    try:
        return os.path.samestat(pidfile.stat(), identity)
    except FileNotFoundError:
        return False


def refresh_loop(repo, ledger, fd):
    pidfile = ledger / "roadmap.pid"
    with os.fdopen(fd) as lease:
        identity = os.fstat(lease.fileno())
        while True:
            time.sleep(REFRESH_SECONDS)
            if not current_refresh(pidfile, identity):
                return
            try:
                document = refresh_document(repo)
            except Exception:  # noqa: BLE001 - a failed background render is retried
                continue
            try:
                with (ledger / "roadmap.lock").open("a") as control:
                    fcntl.flock(control, fcntl.LOCK_EX)
                    if not current_refresh(pidfile, identity):
                        return
                    write_atomic(ledger / "roadmap.html", document)
            except (FileNotFoundError, PermissionError):
                if not current_refresh(pidfile, identity):
                    return


def manage_refresh(repo, ledger, start):
    pidfile = ledger / "roadmap.pid"
    if start:
        write_atomic(ledger / "roadmap.html", refresh_document(repo))
    with (ledger / "roadmap.lock").open("a") as control:
        fcntl.flock(control, fcntl.LOCK_EX)
        if not start:
            pidfile.unlink(missing_ok=True)
            return
        with pidfile.open("a+") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            fd = lease.fileno()
            # The child keeps this inode locked. Unlinking it revokes just its refresh.
            child = subprocess.Popen(
                [sys.executable, __file__, "--repo", str(repo), "--refresh-fd", str(fd)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(fd,), start_new_session=True,
            )
            lease.truncate(0)
            lease.write(f"{child.pid}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--board", action="store_true")
    ap.add_argument("--html", metavar="PATH")
    refresh = ap.add_mutually_exclusive_group()
    refresh.add_argument("--start-refresh", action="store_true", help=argparse.SUPPRESS)
    refresh.add_argument("--stop-refresh", action="store_true", help=argparse.SUPPRESS)
    refresh.add_argument("--refresh-fd", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    repo = Path(args.repo)
    if args.refresh_fd is not None or args.start_refresh or args.stop_refresh:
        ledger = Path(os.environ["CONCORD_REPO_ROOT"])
        if args.refresh_fd is not None:
            refresh_loop(repo, ledger, args.refresh_fd)
        else:
            manage_refresh(repo, ledger, args.start_refresh)
        return
    now = datetime.now(UTC)
    data = build(repo, now)
    base = repo_base(repo)
    if args.text or not (args.html or args.board):
        print(render_text(*data, now, base))
    if args.board:
        print(board_text(data[1]))
    if args.html:
        write_atomic(Path(args.html), render_html(*data, now, base))


if __name__ == "__main__":
    main()
