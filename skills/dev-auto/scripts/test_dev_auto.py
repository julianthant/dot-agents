#!/usr/bin/env python3
"""dev_auto.py scenarios against fake orca, concord, git, gh, ps and agent CLIs on PATH.

Run every scenario, or name some: python3 test_dev_auto.py [scenario ...]
"""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import select
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
MOCK = r"""
import json, os, subprocess, sys, time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
fixture_path = Path(os.environ["DA_FIXTURE"])


def fixture():
    return json.loads(fixture_path.read_text())


def save(data):
    fixture_path.write_text(json.dumps(data))


def log(**entry):
    with open(os.environ["DA_LOG"], "a") as stream:
        stream.write(json.dumps({"cmd": name, "args": args, "cwd": os.getcwd(), **entry}) + "\n")


def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None


def reply(result):
    print(json.dumps({"ok": True, "result": result}))
    sys.exit(0)


def refuse(code="refused"):
    print(json.dumps({"ok": False, "error": {"code": code}}))
    sys.exit(1)


# inspect_work, and a release or a finish by the owner at its version, on the fake store;
# any other tool call is recorded as a note. As in Concord, the env identity acts: an
# agent_id argument is ignored. inspect_work answers as concord-mcp 0.22 does: a text
# summary in content, the task only in structuredContent.
def ledger_reply(params):
    import sqlite3

    tool, given = params["name"], params["arguments"]
    if tool not in ("inspect_work", "transfer_work", "finish_work", "start_work"):
        return {"content": [{"type": "text", "text": "Recorded finding"}]}
    store = Path(os.environ["CONCORD_REPO_ROOT"]) / ".concord" / "concord.db"
    con = sqlite3.connect(store)
    try:
        # A new task, claimed by the env identity, answered as concord-mcp 0.22 does: a text
        # summary in content, the version only in structuredContent.
        if tool == "start_work":
            version = fixture().get("start_version", 1)
            con.execute(
                "INSERT INTO tasks (task_id, title, notes, status, agent_id, version,"
                " created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, 'now', 'now')",
                (given["task_id"], given["title"], given.get("notes"), os.environ.get("CONCORD_AGENT_ID"), version),
            )
            con.commit()
            text = (
                f"Workspace: ws_fake ({os.environ['CONCORD_REPO_ROOT']})\n"
                f"Started {given['task_id']}; status active, version {version}, 0 overlap(s)."
            )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"task_id": given["task_id"], "status": "active", "version": version},
            }
        found = con.execute(
            "SELECT agent_id, version, status FROM tasks WHERE task_id = ?", (given["task_id"],)
        ).fetchone()
        if found is None:
            return {"isError": True, "content": [{"type": "text", "text": "no such task"}]}
        owner, version, status = found
        if tool == "inspect_work":
            task = {"taskId": given["task_id"], "agentId": owner, "version": version, "status": status}
            summary = (
                f"Workspace: ws_fake ({os.environ['CONCORD_REPO_ROOT']})\n"
                f"{given['task_id']}; status {status}, version {version}, 0 update(s)."
            )
            return {
                "content": [{"type": "text", "text": summary}],
                "structuredContent": {"workspace_id": "ws_fake", "scope": "task", "task": task},
            }
        refused = fixture().get("release_refused" if tool == "transfer_work" else "finish_refused")
        if refused:
            return {"isError": True, "content": [{"type": "text", "text": refused}]}
        actor = os.environ.get("CONCORD_AGENT_ID")
        if tool == "finish_work":
            if actor != owner or given.get("expected_version") != version:
                text = f"refused: {actor} may not finish {owner}'s task at version {version}"
                return {"isError": True, "content": [{"type": "text", "text": text}]}
            con.execute(
                "UPDATE tasks SET status = ?, version = version + 1 WHERE task_id = ?",
                (given.get("outcome", "complete"), given["task_id"]),
            )
            con.commit()
            return {"content": [{"type": "text", "text": "finished"}]}
        if given.get("action") != "release" or actor != owner or given.get("expected_version") != version:
            text = f"refused: {actor} may not release {owner} at version {version}"
            return {"isError": True, "content": [{"type": "text", "text": text}]}
        con.execute(
            "UPDATE tasks SET agent_id = NULL, status = 'proposed', version = version + 1"
            " WHERE task_id = ?",
            (given["task_id"],),
        )
        con.commit()
        return {"content": [{"type": "text", "text": "released"}]}
    finally:
        con.close()


env = {k: v for k, v in os.environ.items() if k.startswith(("CONCORD_", "CLAUDE_CONFIG_DIR", "CODEX_HOME"))}
if name == "orca":
    log()
    data = fixture()
    host = opt("--environment") or "local"
    # Like Orca, --environment may come before the subcommand.
    verb = args[2:4] if args[:1] == ["--environment"] else args[:2]
    if verb == ["terminal", "list"]:
        listed = data.get("terminals", {}).get(host, [])
        if listed == "down":
            sys.exit(1)
        reply({"terminals": listed, "truncated": data.get("truncated", False)})
    if verb == ["terminal", "create"]:
        if data.get("hang_create"):
            time.sleep(30)
            sys.exit(1)
        if data.get("create_lost") and opt("--title") != "dev-auto probe":
            # The terminal starts, but its answer never comes back.
            data["created"] = data.get("created", 0) + 1
            save(data)
            sys.exit(1)
        if data.get("create_no_handle") and opt("--title") != "dev-auto probe":
            # The terminal starts, but its answer carries no handle.
            data["created"] = data.get("created", 0) + 1
            save(data)
            reply({})
        if opt("--title") == "dev-auto probe" and data.get("probe_fails"):
            refuse()
        data["created"] = data.get("created", 0) + 1
        handle = f"term-{host}-{data['created']}"
        if opt("--title") == "dev-auto probe":
            cwd = opt("--worktree").removeprefix("path:")
            extra = data.get("probe_env") or {}
            done = subprocess.run(
                ["sh", "-c", opt("--command")],
                cwd=cwd if os.path.isdir(cwd) else None,
                capture_output=True,
                text=True,
                env={**os.environ, **extra} if extra else None,
            )
            # The probed commands may have written the fixture; keep what they wrote.
            data = {**fixture(), "created": data["created"]}
            tail = ["$ " + opt("--command"), *done.stdout.splitlines()]
            data.setdefault("screens", {})[handle] = {"status": "running", "tail": tail}
        save(data)
        reply({"terminal": {"handle": handle}})
    if verb == ["terminal", "read"]:
        screen = data.get("screens", {}).get(opt("--terminal"))
        if screen is None or screen.get("unread"):
            refuse()
        if data.get("probe_read_delay"):
            time.sleep(data["probe_read_delay"])
        if data.get("probe_cursors"):
            output = screen["tail"]
            limit = int(opt("--limit"))
            cursor = opt("--cursor")
            start = int(cursor) if cursor is not None else max(0, len(output) - limit)
            end = min(len(output), start + limit)
            reply({"terminal": {
                **screen, "tail": output[start:end], "oldestCursor": 0,
                "nextCursor": end, "latestCursor": len(output),
                "limited": start > 0 if cursor is None else end < len(output),
            }})
        # reads_left lets one tick see a frame, then fail the next read or show "then".
        left = screen.get("reads_left")
        if isinstance(left, int):
            if left <= 0:
                if "then" in screen:
                    reply({"terminal": {"handle": opt("--terminal"), **screen["then"]}})
                refuse()
            screen["reads_left"] = left - 1
            save(data)
        reply({"terminal": {"handle": opt("--terminal"), **screen}})
    if verb == ["terminal", "close"] and opt("--terminal") in data.get("close_fails", []):
        refuse()
    if verb == ["terminal", "close"]:
        handle, whole = opt("--terminal"), "--tab" in args
        rows = data.get("terminals", {}).get(host)
        if isinstance(rows, list):
            tab = next((r.get("tabId") for r in rows if r["handle"] == handle), None)
            data["terminals"][host] = [
                r for r in rows
                if r["handle"] != handle and not (whole and tab and r.get("tabId") == tab)
            ]
        # Like Orca: a pane close keeps the tab's resume record, which a restart restores.
        if not whole:
            data.setdefault("resume", {}).setdefault(host, []).append(handle)
        save(data)
    if verb == ["worktree", "create"]:
        if data.get("create_fails"):
            refuse()
        reply({"worktree": {"path": f"/wt/{opt('--name')}"}})
    if verb == ["worktree", "list"]:
        reply(data.get("worktree_list", {}).get(host, {"worktrees": [{"path": "/repo"}]}))
    if verb == ["worktree", "show"]:
        if data.get("worktree_show") == "missing":
            refuse("selector_not_found")
        if data.get("worktree_show") == "fails":
            refuse("timeout")
        reply(data.get("worktree_show", {}))
    if verb == ["account", "list"]:
        reply(data.get("accounts", {}))
    # A host's memory as `orca diagnostics memory` gives it; by default a Linux host
    # with room. A string is that host's error message; "hang" never answers. A list is
    # one reading per call, the last one repeated.
    if verb == ["diagnostics", "memory"]:
        roomy = 20 * 2**30
        found = data.get("memory", {}).get(
            host, {"availableMemory": roomy, "freeMemory": roomy, "availableMemorySource": "proc-meminfo"}
        )
        if isinstance(found, list):
            if len(found) > 1:
                data["memory"][host] = found[1:]
                save(data)
            found = found[0]
        if found == "hang":
            time.sleep(10)
            sys.exit(1)
        if isinstance(found, str):
            print(json.dumps({"ok": False, "error": {"code": "remote_runtime_unavailable", "message": found}}))
            sys.exit(1)
        reply({"host": found})
    if args[:1] == ["status"]:
        version = data.get("orca_version", {}).get(host, "1.4.257")
        mode = data.get("orca_mode", {}).get(host, "interactive")
        reply({"runtime": {"appVersion": version, "remoteUpdateSupport": {"installMode": mode}}})
    if verb == ["terminal", "send"] and opt("--terminal") in data.get("send_fails", []):
        refuse()
    if verb == ["terminal", "send"] and opt("--terminal") in data.get("send_refused", []):
        reply({"send": {"accepted": False, "handle": opt("--terminal")}})
    if verb == ["worktree", "rm"] and data.get("rm_fails"):
        refuse()
    # Like git worktree remove: untracked files refuse a removal without --force.
    if verb == ["worktree", "rm"] and data.get("untracked") and "--force" not in args:
        refuse()
    if verb == ["terminal", "send"]:
        handle = opt("--terminal")
        screen = data.setdefault("screens", {}).setdefault(handle, {"status": "running", "tail": []})
        text = opt("--text") or ""
        # A held screen accepts the keys and keeps the previous frame, so a wake
        # cannot treat its own echo as proof that the send landed.
        if screen.get("hold"):
            reply({"send": {"accepted": True, "handle": handle}})
        buf = screen.get("input") or ""
        if text == "\x15":
            screen["input"] = ""
        elif text == "\x04":
            # A runner at its prompt exits on end of input to the frame it leaves; one
            # without on_eof keeps running.
            screen.update(screen.pop("on_eof", {}))
        elif "--enter" in args:
            submitted = buf + text
            screen["input"] = ""
            if submitted.startswith("/") and not submitted.startswith("/goal"):
                line = "Unknown command: " + submitted
            else:
                line = submitted
            screen["tail"] = [*(screen.get("tail") or []), line]
            # Like a real terminal, the submitted line's echo is output at echo_at.
            if "echo_at" in data:
                for row in data.get("terminals", {}).get(host) or []:
                    if row["handle"] == handle:
                        row["lastOutputAt"] = data["echo_at"]
        else:
            screen["input"] = buf + text
        save(data)
        reply({} if screen.get("unknown_receipt") else {"send": {"accepted": True, "handle": handle}})
    reply({})
if name in ("git", "gh", "npm", "sudo", "systemd-run", "lsof", "uname"):
    log()
    data = fixture()
    table = data.get(name, {})
    key = " ".join(args)
    if key in table:
        code, out = table[key]
    elif name == "git" and data.get("git_bin"):
        os.execv(data["git_bin"], [data["git_bin"], *args])
    else:
        code, out = [1, ""]
    if name == "systemd-run" and code == 0 and "restart_to" in data:
        data.setdefault("orca_version", {}).update(data.pop("restart_to"))
        save(data)
    print(out, end="")
    sys.exit(code)
if name == "curl":
    log()
    body = fixture().get("curl", {}).get(args[-1])
    if body is None and args[-1].endswith("/latest-linux.yml"):
        body = "version: 1.4.257\n"
    if body is None:
        print("curl: (22) The requested URL returned error: 404", file=sys.stderr)
        sys.exit(22)
    if "-o" in args:
        Path(opt("-o")).write_text(body)
    else:
        print(body, end="")
    sys.exit(0)
if name in ("claude", "codex", "grok", "cursor-agent") and args in (["--version"], ["update"]):
    log()
    data = fixture()
    versions = data.setdefault("versions", {})
    if args == ["update"]:
        if name in data.get("update_fail", {}):
            print(data["update_fail"][name], file=sys.stderr)
            sys.exit(1)
        versions[name] = data.get("latest", {}).get(name, versions.get(name, ""))
        save(data)
    else:
        print(versions.get(name, ""))
    sys.exit(0)
if name == "ps":
    if args[:1] == ["-p"]:
        log()
        alive = args[1] in fixture().get("pids", [])
        print(f"{args[1]}\n" if alive else "", end="")
        sys.exit(0 if alive else 1)
    print(fixture().get("ps", ""), end="")
    sys.exit(0)
if name == "ssh":
    log()
    command = args[-1] if args else ""
    closed = "-n" in args and command.startswith("concord-mcp script:") and command.endswith("-selfcheck")
    failed = fixture().get("ssh_fail") or any("MISSING" in part for part in args)
    if not closed or failed:
        print("ssh: connect failed", file=sys.stderr)
        sys.exit(255)
    sys.exit(0)
if name == "concord-mcp":
    if args:
        failed = (
            fixture().get("selfcheck_fail")
            and len(args) == 1
            and args[0].startswith("script:")
            and args[0].endswith("-selfcheck")
        )
        sys.exit(1 if failed else 0)
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        result = {}
        if message["method"] == "tools/call":
            data = fixture()
            data["calls"] = data.get("calls", 0) + 1
            if data.get("locked", 0) > 0:
                data["locked"] -= 1
                result = {"isError": True, "content": [{"type": "text", "text": "database is locked"}]}
            elif data.get("tool_error"):
                result = {"isError": True, "content": [{"type": "text", "text": data["tool_error"]}]}
            else:
                log(call=message["params"], env=env)
                result = ledger_reply(message["params"])
            save(data)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    sys.exit(0)
if name == "cursor-agent":
    plugin = Path(args[args.index("--plugin-dir") + 1]) / ".cursor-plugin" / "plugin.json"
    log(env=env, plugin=json.loads(plugin.read_text()))
    sys.exit(0)
log(env=env)
"""

tmp = Path(tempfile.mkdtemp()).resolve()
commands = tmp / "bin"
commands.mkdir()
mock = commands / "mock"
mock.write_text(f"#!{sys.executable}\n{MOCK}")
mock.chmod(0o755)
for command in (
    "orca",
    "git",
    "gh",
    "ps",
    "ssh",
    "npm",
    "sudo",
    "curl",
    "systemd-run",
    "lsof",
    "uname",
    "concord",
    "concord-mcp",
    "claude",
    "codex",
    "cursor-agent",
    "grok",
    "uv",
):
    (commands / command).symlink_to(mock)
fixture_file, log_file = tmp / "fixture.json", tmp / "calls.log"
root = tmp / "ledger"
(root / ".concord").mkdir(parents=True)
(root / "supervisor").mkdir()
state_file = root / "supervisor" / "state.json"
homes = {name: tmp / "homes" / name for name in ("c1", "c2", "c3", "x1", "x2")}
for home in homes.values():
    home.mkdir(parents=True)
accounts = tmp / "accounts.toml"
accounts.write_text(
    "usage_full_percent = 90\nrelay_percent = 95\nsilent_minutes = 30\ncheckin_minutes = 0\n"
    '[hosts.Juzzark-WSL]\nprefix = "wsl"\naddress = "100.64.0.9"\n'
    'ledger_ssh = ["ssh", "-i", "~/.ssh/k", "me@mac"]\n'
    '[hosts.Mac2]\nprefix = "mac2"\nledger_ssh = ["ssh", "me@mac"]\n'
    + "".join(
        "[[account]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in row.items())
        for row in [
            {
                "agent": "claude",
                "label": "pro1",
                "id": "c1",
                "home": str(homes["c1"]),
                "plan": "pro",
            },
            {
                "agent": "claude",
                "label": "pro2",
                "id": "c2",
                "home": str(homes["c2"]),
                "plan": "team",
            },
            {
                "agent": "claude",
                "label": "max",
                "id": "c3",
                "home": str(homes["c3"]),
                "plan": "max",
            },
            {
                "agent": "claude",
                "label": "off",
                "id": "c4",
                "plan": "pro",
                "enabled": False,
            },
            {
                "agent": "claude",
                "label": "max@wsl",
                "id": "c3",
                "host": "Juzzark-WSL",
                "plan": "max",
            },
            {
                "agent": "codex",
                "label": "x1",
                "id": "x1",
                "home": str(homes["x1"]),
                "slots": 2,
            },
            {"agent": "codex", "label": "x2", "id": "x2", "home": str(homes["x2"])},
            {"agent": "codex", "label": "xw", "id": "xw", "host": "Juzzark-WSL"},
            {"agent": "cursor", "label": "cur", "id": "cur"},
            {"agent": "grok", "label": "gk", "id": "gk", "slots": 2},
            {"agent": "codex", "label": "xoff", "id": "xoff", "enabled": False},
        ]
    )
)
os.environ.update(
    PATH=f"{commands}:{os.environ['PATH']}",
    DA_FIXTURE=str(fixture_file),
    DA_LOG=str(log_file),
    ORCA_CLI_COMMAND="orca",
    CONCORD_REPO_ROOT=str(root),
    DEV_AUTO_ACCOUNTS=str(accounts),
    # A refused host's second reading comes at once, not 60 seconds later.
    DEV_AUTO_MEMORY_RECHECK="0",
)
# A run inside Claude Code would register its own session as the orchestrator's.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
spec =importlib.util.spec_from_file_location("dev_auto", HERE / "dev_auto.py")
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)
cfg = da.load_config()
_append_event = da.append_event


def logged_event(st, text, line=None, key=None):
    """The real append, also logged in order with the typed lines (see sends)."""
    done = _append_event(st, text, line, key)
    # With no orchestrator set (after a RELAY) the line waits in the file for the next.
    entry = {"cmd": "event", "args": [], "text": text, "to": (st["orchestrator"] or {}).get("terminal")}
    with open(log_file, "a") as stream:
        stream.write(json.dumps(entry) + "\n")
    return done


da.append_event = logged_event
# An unshown wake is rechecked at once; recheck_wait() puts the 10 seconds on a fake clock.
da.WAKE_RECHECK = 0
NOW = 2_000_000_000.0
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
CHECKS = "pr checks feat/a --json bucket --jq .[].bucket"
SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


def set_fixture(**data):
    fixture_file.write_text(json.dumps(data))
    log_file.write_text("")
    # A host's memory marks (refusals, floors held, Windows trend) belong to one scenario.
    (root / "supervisor" / "memory.json").unlink(missing_ok=True)


def fixture():
    return json.loads(fixture_file.read_text())


def calls(cmd="orca", verb=None):
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    return [
        r for r in rows if r["cmd"] == cmd and (verb is None or r["args"][:2] == verb)
    ]


def sends(include_clear=False, events=True):
    """Typed lines, in order. An event appended to events.md for the orchestrator reads
    as ("term-orch", text, True), and the one EVENTS wake typed after them is omitted;
    with events=False the file's lines are left out and the wake is kept. The Ctrl-U
    that clears the input is omitted unless asked for."""
    found = []
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    for r in rows:
        if r["cmd"] == "event":
            if events and r["to"]:
                found.append((r["to"], r["text"], True))
            continue
        if r["cmd"] != "orca" or r["args"][:2] != ["terminal", "send"]:
            continue
        a = r["args"]
        text = a[a.index("--text") + 1]
        if not include_clear and text == "\x15":
            continue
        if events and text.startswith("EVENTS: "):
            continue
        found.append((a[a.index("--terminal") + 1], text, "--enter" in a))
    return found


def created():
    return [
        r
        for r in calls("orca", ["terminal", "create"])
        if "dev-auto probe" not in r["args"]
    ]


def opt(row, flag):
    return row["args"][row["args"].index(flag) + 1]


def notes():
    return [
        r["call"]["arguments"]["content"]
        for r in calls("concord-mcp")
        if r["call"]["name"] == "update_work"
    ]


def ledger_calls(tool):
    return [r["call"]["arguments"] for r in calls("concord-mcp") if r["call"]["name"] == tool]


def owner_of(tid):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT agent_id FROM tasks WHERE task_id = ?", (tid,)).fetchone()[0]
    finally:
        con.close()


def claude_usage(name, five, reset, week=5, saved=NOW, week_reset=NOW + 86400):
    data = {
        "saved_at": int(saved),
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": reset},
            "seven_day": {"used_percentage": week, "resets_at": week_reset},
        },
    }
    (homes[name] / "rate-limits.json").write_text(json.dumps(data))


def orca_reading(five, week, updated=NOW, reset=NOW + 3600):
    """One account's `orca account list` usage: epochs in milliseconds, as Orca gives them."""
    return {
        "session": {
            "usedPercent": five,
            "windowMinutes": 300,
            "resetsAt": reset * 1000,
        },
        "weekly": {
            "usedPercent": week,
            "windowMinutes": 10080,
            "resetsAt": (NOW + 86400) * 1000,
        },
        "updatedAt": updated * 1000,
        "status": "ok",
    }


def orca_accounts(claude=(), codex=(), grok=None, logins=()):
    """An `orca account list` result. Each agent's first (id, reading) is the selected
    account. Orca's reply is read once per `now`, so a changed one needs its own `now`.
    `logins` are (agent, id, home) of Orca's own account entries."""
    limits = {"grok": grok} if grok else {}
    result = {"rateLimits": limits}
    for agent, pairs in (("claude", claude), ("codex", codex)):
        result[agent] = {
            "activeAccountId": pairs[0][0] if pairs else None,
            "accounts": [{"id": i, "home": h} for a, i, h in logins if a == agent],
        }
        if pairs:
            limits[agent] = pairs[0][1]
        limits[f"inactive{agent.title()}Accounts"] = [
            {"accountId": i, "rateLimits": r} for i, r in pairs[1:]
        ]
    return result


def codex_line(limits, saved=NOW):
    """A rollout line as Codex writes it, stamped in UTC."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(saved))
    return json.dumps({"timestamp": stamp, "payload": {"rate_limits": limits}}) + "\n"


def codex_usage(name, five, week, reset, saved=NOW):
    folder = homes[name] / "sessions" / "2033" / "05" / "18"
    folder.mkdir(parents=True, exist_ok=True)
    limits = {
        "primary": {"used_percent": five, "window_minutes": 300, "resets_at": reset},
        "secondary": {
            "used_percent": week,
            "window_minutes": 10080,
            "resets_at": reset,
        },
    }
    (folder / "rollout.jsonl").write_text(codex_line(limits, saved))


def clear_usage():
    for home in homes.values():
        (home / "rate-limits.json").unlink(missing_ok=True)
        for path in home.glob("sessions/*/*/*/rollout.jsonl"):
            path.unlink()


def rec(ticket, account, agent="claude", model=OPUS, **extra):
    base = {
        # Launched by this version: every text it was typed is known.
        "typed_texts": [],
        "ticket": ticket,
        "agent": agent,
        "model": model,
        "effort": "high",
        "worktree": str(tmp / "wt" / ticket),
        "host": "local",
        "agent_id": f"{agent}:{ticket}",
        "title": f"{ticket} {agent}",
        "terminal": f"term-{ticket}",
        "account": account,
        "launched_at": NOW - 7200,
        "session": f"sess-{ticket}",
        "pty": f"pty-term-{ticket}",
        "owned_branch": "feat/a",
        "owned_common": f"{tmp / 'repo'}/.git",
    }
    base.update(extra)
    if base["agent"] == "claude" and "claude_input" not in extra:
        base["claude_input"] = [base["session"], da.time.time()]
    Path(base["worktree"]).mkdir(parents=True, exist_ok=True)
    return base


def woke(tid, text, agent="claude"):
    """A wake as typed: it sets the worker's goal again first."""
    return da.armed({"agent": agent}, da.goal_line(tid), text)


def pick(workers, agent="claude", model=OPUS, **kw):
    row = da.slot(cfg, workers, agent, model, now=NOW, **kw)
    return row["label"] if row else None


def cli(*args, **env):
    command = [sys.executable, str(HERE / "dev_auto.py"), *args]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
    )


def run_command(command):
    log_file.write_text("")
    subprocess.run(["sh", "-c", command], check=True, cwd=tmp)
    (entry,) = [
        r
        for r in (json.loads(x) for x in log_file.read_text().splitlines())
        if r["cmd"] in ("claude", "codex", "cursor-agent", "grok", "uv")
    ]
    return entry


# Ledger store for the tick scenarios.
trial_sql = (HERE / "testdata" / "trial-store.sql").read_text()
db = root / ".concord" / "concord.db"


def reset_db():
    db.unlink(missing_ok=True)
    (root / "supervisor" / "checkout").unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript(trial_sql + "DELETE FROM tasks; DELETE FROM task_updates;")
    # Concord's agents table, as far as dev-auto reads it: where each id registered.
    con.execute("CREATE TABLE agents (agent_id TEXT PRIMARY KEY, worktree TEXT, cwd TEXT)")
    con.commit()
    con.close()


def ticket_row(tid, status="active", agent=None, title=None, project="", notes=None):
    con = sqlite3.connect(db)
    con.execute("DELETE FROM tasks WHERE task_id = ? AND project = ?", (tid, project))
    con.execute(
        "INSERT INTO tasks (task_id, title, status, agent_id, created_at, updated_at,"
        " project) VALUES (?, ?, ?, ?, 'now', 'now', ?)",
        (tid, title or f"feat / {tid}", status, agent or f"claude:{tid}", project),
    )
    if notes is not None:
        con.execute(
            "UPDATE tasks SET notes = ? WHERE task_id = ? AND project = ?",
            (notes, tid, project),
        )
    con.commit()
    con.close()


def update(tid, kind, content, agent=None, project="", at=None):
    """One ticket update; `at`, an epoch, stamps it as Concord does, else 'now'."""
    stamp = datetime.fromtimestamp(at, UTC).isoformat() if at else "now"
    con = sqlite3.connect(db)
    cur = con.execute(
        "INSERT INTO task_updates (task_id, kind, content, agent, created_at, project)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (tid, kind, content, agent or f"claude:{tid}", stamp, project),
    )
    con.commit()
    con.close()
    return cur.lastrowid


def term(
    handle,
    quiet,
    tail=("❯",),
    status="running",
    branch="refs/heads/feat/a",
    ident="claude",
    worktree=None,
    pty=None,
    tab=None,
):
    """A listed terminal and its screen; a worker's default worktree and pty follow rec().
    Like every terminal dev-auto creates, it sits alone in its own tab unless given one."""
    row = {
        "handle": handle,
        "tabId": tab or f"tab-{handle}",
        "branch": branch,
        "lastOutputAt": int((NOW - quiet) * 1000),
        "agentIdentity": ident,
        "worktreePath": worktree or str(tmp / "wt" / handle.removeprefix("term-")),
        "ptyId": pty or f"pty-{handle}",
    }
    return row, {"status": status, "tail": list(tail)}


def world(terms, orch="term-orch", orch_ident="claude", orch_tail=("❯",), orch_worktree="/repo", **extra):
    """Fixture with these terminals plus the orchestrator's, by default at its prompt."""
    rows, screens = {}, {}
    terms = list(terms)
    if orch:
        row, screen = term(orch, 5, orch_tail, ident=orch_ident, worktree=orch_worktree)
        terms.append(("local", orch, row, screen))
    for host, handle, row, screen in terms:
        rows.setdefault(host, []).append(row)
        screens[handle] = screen
    set_fixture(terminals=rows, screens=screens, **extra)


def local(*items):
    return [("local", row["handle"], row, screen) for row, screen in items]


def just_created(ago=5):
    """`orca worktree show` for a worktree Orca created that many seconds ago."""
    return {"worktree": {"createdAt": int((time.time() - ago) * 1000)}}


def fresh_state(**workers):
    data = {
        "workers": workers,
        "sent": {},
        "orchestrator": {"terminal": "term-orch", "host": "local", "account": None},
        "relay": {"used": False},
        "stopped": False,
        "limits": {},
        # Begun with its ledger: every verdict in it is new.
        "cleared_floor": 0,
        "updates": {
            f"{host}|{tool}": NOW
            for host in ("local", "Juzzark-WSL", "Mac2")
            for tool in ("claude", "codex", "cursor", "grok", "orca")
        },
        # A remote host's transcripts were just read: no context probe opens.
        "context_read": {host: NOW for host in ("Juzzark-WSL", "Mac2")},
    }
    return da.State(data, tmp / "scratch-state.json")


@contextmanager
def settings(**values):
    """accounts.toml settings the orchestrator turns on, for one scenario."""
    old = {key: cfg[key] for key in values if key in cfg}
    cfg.update(values)
    try:
        yield
    finally:
        for key in values:
            cfg.pop(key)
        cfg.update(old)


def tick(st, now=NOW):
    da.tick(cfg, root, st, da.read_tickets(root), now)


def tick_text(st, now=NOW):
    buf = io.StringIO()
    with redirect_stdout(buf):
        tick(st, now)
    return buf.getvalue()


STATUS = "status --porcelain --untracked-files=all"
git = {
    "rev-parse --abbrev-ref HEAD": [0, "feat/a\n"],
    "rev-parse HEAD": [0, "c0ffee1\n"],
    "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"],
    STATUS: [0, ""],
    "fetch -q origin": [0, ""],
    "remote get-url origin": [0, "git@github.com:o/r.git\n"],
    "merge-base --is-ancestor HEAD abc": [0, ""],
    "ls-remote --heads origin refs/heads/feat/a": [0, ""],
    "update-ref -d refs/heads/feat/a c0ffee1": [0, ""],
}
HR_RELAY_ORIGIN = "https://github.com/julianthant/hr-relay.git"
HR_RELAY_GIT = {**git, "remote get-url origin": [0, f"{HR_RELAY_ORIGIN}\n"]}
UNREAD_ORIGIN_GIT = {**git, "remote get-url origin": [127, ""]}


def assert_ci_only_prompt(command):
    assert "Run all tests, lint, and typecheck only in GitHub Actions" in command, command
    assert "including named integration and smoke tests" in command, command
    assert "Do not run any of these checks on this machine" in command, command
    assert "host-check-lock" not in command and "Run only the named integration" not in command, command


MERGED_PRS = (
    "pr list --head feat/a --state merged --json headRefOid --jq .[].headRefOid"
)
OPEN_PRS = "pr list --head feat/a --state open --json number --jq .[].number"
merged = {MERGED_PRS: [0, "abc\n"]}
opened = {MERGED_PRS: [0, ""], OPEN_PRS: [0, "41\n"]}
no_pr = {MERGED_PRS: [0, ""], OPEN_PRS: [0, ""]}
(tmp / "repo").mkdir()


def event_lines():
    path = root / "supervisor" / "events.md"
    return [l for l in path.read_text().splitlines() if l.startswith("- ")] if path.exists() else []


@scenario
def events_go_to_the_file_with_one_wake():
    """Two orchestrator events in one tick are two lines in events.md and one EVENTS wake;
    a line the orchestrator deleted is not added again, and a tick with nothing new types
    nothing. A line open over 30 minutes is counted once in the next wake."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state(ea=rec("ea", "c1"), eb=rec("eb", "c1"))
    screens = [term("term-ea", 5), term("term-eb", 5)]
    world(local(*screens))
    tick(st)
    lines = event_lines()
    assert [l.split(" ", 2)[2] for l in lines] == [
        "STUCK ea: the ticket is not in the ledger",
        "STUCK eb: the ticket is not in the ledger",
    ], lines
    assert re.fullmatch(r"- \d{4}-\d\d-\d\dT\d\d:\d\dZ STUCK ea: .*", lines[0]), lines
    wake = f"EVENTS: 2 new in {path}, 2 open, batch 1"
    assert sends(events=False) == [("term-orch", wake, True)], sends(events=False)
    path.write_text(lines[1] + "\n")
    world(local(*screens))
    tick(st)
    assert event_lines() == [lines[1]] and sends(events=False) == [], sends(events=False)
    st["events_at"][lines[1]] -= 31 * 60
    st["workers"]["ec"] = rec("ec", "c1")
    world(local(*screens, term("term-ec", 5)))
    tick(st)
    late = f"EVENTS: 1 new in {path}, 2 open, 1 older than 30 min, batch 2"
    assert sends(events=False) == [("term-orch", late, True)], sends(events=False)
    st["workers"]["ed"] = rec("ed", "c1")
    world(local(*screens, term("term-ec", 5), term("term-ed", 5)))
    tick(st)
    assert sends(events=False) == [("term-orch", f"EVENTS: 1 new in {path}, 3 open, batch 3", True)]


@scenario
def unshown_events_wake_keeps_its_batch():
    """da-logs-8: an EVENTS wake Orca accepts but the orchestrator's screen does not show
    keeps its count: the next tick types it once more, a third types nothing, and once the
    screen shows it the batch is told and nothing is typed again."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state(ua=rec("ua", "c1"), ub=rec("ub", "c1"))
    screens = [term("term-ua", 5), term("term-ub", 5)]
    wake = f"EVENTS: 2 new in {path}, 2 open, batch 1"
    typed = []
    for later in (0, 60, 120):
        world(local(*screens))
        hold("term-orch")
        tick(st, NOW + later)
        typed.append(sends(events=False))
        assert st["events_new"] == 2, (later, st["events_new"])
    assert typed == [[("term-orch", wake, True)], [("term-orch", wake, True)], []], typed
    world(local(*screens), orch_tail=["❯", wake])
    tick(st, NOW + 180)
    assert sends(events=False) == [] and st["events_new"] == 0, (sends(events=False), st["events_new"])



@scenario
def events_batches_with_equal_counts_are_told_apart():
    """da-logs-8: batch A told and handled, then batch B with the same counts: B's wake
    names its own batch, so a frame repainting A's old wake does not confirm it and B's
    count is kept."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state()
    world([])
    da.emit(st, "x|a", "NEEDS YOU xa: batch A")
    da.wake_events(st)
    ((_, first, _),) = sends(events=False)
    assert st["events_new"] == 0, st["events_new"]
    path.write_text("")
    da.emit(st, "x|b", "NEEDS YOU xb: batch B")
    # Read by at_prompt and before the send; the frame after it repaints A's wake.
    frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 2}
    world([])
    data = fixture()
    data["screens"]["term-orch"] = {**frame, "then": {"status": "running", "tail": [first, "❯"]}}
    fixture_file.write_text(json.dumps(data))
    da.wake_events(st)
    assert st["events_new"] == 1, st["events_new"]
    assert sends(events=False) == [("term-orch", f"EVENTS: 1 new in {path}, 1 open, batch 2", True)], sends(events=False)


@scenario
def replaced_events_snapshot_gets_its_own_batch():
    """da-logs-8: batch A's wake typed and not shown, A handled in the file, then B: the
    replacing wake counts two and names a new batch, so a frame repainting A's wake does
    not confirm it and both counts are kept."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state()
    world([])
    hold("term-orch")
    da.emit(st, "x|a", "NEEDS YOU xa: batch A")
    da.wake_events(st)
    ((_, first, _),) = sends(events=False)
    assert st["events_new"] == 1, st["events_new"]
    path.write_text("")
    da.emit(st, "x|b", "NEEDS YOU xb: batch B")
    # Read for A's pending wake, by at_prompt and before the send; then A's wake repaints.
    frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 3}
    world([])
    data = fixture()
    data["screens"]["term-orch"] = {**frame, "then": {"status": "running", "tail": [first, "❯"]}}
    fixture_file.write_text(json.dumps(data))
    da.wake_events(st)
    assert st["events_new"] == 2, st["events_new"]
    ((_, second, _),) = sends(events=False)
    assert second.startswith("EVENTS: 2 new") and second.endswith(", batch 2"), second


@scenario
def events_batch_is_saved_before_its_keys():
    """da-logs-8: a new EVENTS snapshot's batch number is in the saved state before any
    key is typed, so a stop mid-send never gives that number to a changed snapshot."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state()
    world([])
    hold("term-orch")
    seen = []
    real = da.type_message

    def recorded(host, terminal, text, *args, **kwargs):
        if text.startswith("EVENTS: "):
            seen.append((text.rsplit(" ", 1)[1], json.loads(st.path.read_text()).get("events_batch")))
        return real(host, terminal, text, *args, **kwargs)

    da.type_message = recorded
    try:
        da.emit(st, "x|a", "NEEDS YOU xa: batch A")
        da.wake_events(st)
        path.write_text("")
        da.emit(st, "x|b", "NEEDS YOU xb: batch B")
        da.wake_events(st)
    finally:
        da.type_message = real
    assert seen == [("1", 1), ("2", 2)], seen

@scenario
def delivered_log_refused_leaves_the_tick_whole():
    """With delivered.log refused once the events line has landed, the tick still writes
    status and types its EVENTS wake, and once the log is back no line is appended again."""
    reset_db()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    st = fresh_state(dl=rec("dl", "c1"))
    log = st.path.parent / "delivered.log"
    log.unlink(missing_ok=True)
    log.mkdir()
    status_file = root / "supervisor" / "status.json"
    status_file.unlink(missing_ok=True)
    try:
        world(local(term("term-dl", 5)))
        tick(st)
        lines = event_lines()
        assert [l.split(" ", 2)[2] for l in lines] == ["STUCK dl: the ticket is not in the ledger"], lines
        assert status_file.exists(), "status written"
        assert sends(events=False) == [("term-orch", f"EVENTS: 1 new in {path}, 1 open, batch 1", True)], sends(events=False)
    finally:
        log.rmdir()
    world(local(term("term-dl", 5)))
    tick(st, NOW + 60)
    assert event_lines() == lines and not st.get("outbox"), (event_lines(), st.get("outbox"))
    path.unlink()


def pushed(tid, count):
    """Standing PUSH update ids on a ticket made for them: a CLEARED REVIEW replayed from
    the outbox is written only while the PUSH its key names stands."""
    ticket_row(tid)
    return [update(tid, "finding", "REVIEW VERDICT: PUSH 1a2b3c4", f"codex:{tid}-review") for _ in range(count)]


@scenario
def event_cut_short_is_appended_once_on_a_later_tick():
    """An orchestrator event whose append a kill or a full disk cut short after its key was
    saved is appended by the next tick, whole after the earlier lines even when the disk
    filled mid-write; one whose line landed before the kill is not appended again. Each
    gets its one EVENTS wake, also when the kill came between the line and its count."""
    reset_db()
    path = root / "supervisor" / "events.md"
    real, real_write = da.append_event, Path.write_text
    earlier = "- 00:59 STUCK zz: earlier"
    for name in ("killed", "full", "short", "written", "landed"):
        path.write_text(earlier + "\n")
        st = fresh_state()
        st["cleared_floor"] = 10**9  # its PUSH rows back replays only; the tick raises none
        (push,) = pushed(name, 1)
        text = f"CLEARED REVIEW {name}: REVIEW VERDICT: PUSH 1a2b3c4 by codex:{name}-review"

        def append(at, words, line, key=None, how=name):
            if how == "full":
                raise OSError(28, "No space left on device")
            if how == "short":
                return real(at, words, line, key)
            if how == "written":
                with open(path, "a") as stream:
                    stream.write(line + "\n")
            if how == "landed":
                real(at, words, line, key)
            raise KeyboardInterrupt

        def cut(self, data, *args, **kwargs):
            if self.name != "events.tmp":
                return real_write(self, data, *args, **kwargs)
            real_write(self, data[: len(data) - 9], *args, **kwargs)
            raise OSError(28, "No space left on device")

        da.append_event = append
        Path.write_text = cut
        try:
            da.emit(st, f"{name}|cleared|{push}", text)
        except KeyboardInterrupt:
            pass
        finally:
            da.append_event, Path.write_text = real, real_write
        assert path.read_text() == earlier + "\n" or name in ("written", "landed"), name
        saved = da.State(json.loads(st.path.read_text()), st.path)
        world(local())
        tick(saved)
        tick(saved, NOW + 60)
        lines = event_lines()
        assert lines[0] == earlier and [l.split(" ", 2)[2] for l in lines[1:]] == [text], (name, lines)
        assert path.read_text().endswith("\n") and saved["sent"].get(f"{name}|cleared|{push}"), name
        wake = ("term-orch", f"EVENTS: 1 new in {path}, 2 open, batch 1", True)
        assert sends(events=False) == [wake], (name, sends(events=False))
        assert not saved.get("outbox"), (name, saved["outbox"])
    path.unlink()


@scenario
def withdrawn_clearance_is_not_written_from_the_outbox():
    """A CLEARED REVIEW or UNPUSHED whose events.md append failed waits in the outbox; if
    its PUSH is withdrawn before the next tick, that tick drops it unwritten, and if the
    PUSH still stands, it writes it. Every case is checked; the failing ones are listed."""
    real = da.append_event
    bad = []
    for kind in ("cleared", "unpushed"):
        for withdraw in (True, False):
            reset_db()
            (root / "supervisor" / "events.md").unlink(missing_ok=True)
            ticket_row("ow")
            st = fresh_state()
            world(local())
            tick(st)
            push = update("ow", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:ow-r")
            if kind == "unpushed":
                st["cleared_floor"] = push
                text = "UNPUSHED ow: fix/x on local at 1a2b3c4, 1 commit ahead of origin"
                st["cleared_pending"] = {"ow|unpushed|1a2b3c4|": ["ow", text, 0, [push], "|ow"]}
            else:
                text = "CLEARED REVIEW ow: PUSH 1a2b3c4"

            def full(*_args, **_kwargs):
                raise OSError(28, "No space left on device")

            da.append_event = full
            try:
                world(local())
                tick(st, NOW + 60)
            finally:
                da.append_event = real
            if not st.get("outbox"):
                bad.append(f"{kind}: nothing waited in the outbox")
                continue
            if withdraw:
                supersede(push, update("ow", "decision", "withdrawn", agent="codex:ow-r"))
            world(local())
            tick(st, NOW + 120)
            written = any(text in line for line in event_lines())
            if written == withdraw:
                bad.append(f"{kind} {'withdrawn' if withdraw else 'standing'}: written={written}")
    assert not bad, bad


@scenario
def unwritten_clearance_with_no_ticket_is_withheld():
    """An unwritten CLEARED REVIEW whose ticket the ledger no longer holds is withheld at
    replay: no ticket, no standing PUSH. One whose ticket is closed but whose PUSH still
    stands is written. Every case is checked; the failing ones are listed."""
    real = da.append_event
    bad = []
    text = "CLEARED REVIEW nt: PUSH 1a2b3c4"
    for case in ("missing", "closed"):
        reset_db()
        (root / "supervisor" / "events.md").unlink(missing_ok=True)
        ticket_row("nt")
        push = update("nt", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:nt-r")
        st = fresh_state()
        st["cleared_floor"] = push
        world(local())
        tick(st)

        def full(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        da.append_event = full
        try:
            da.emit(st, f"nt|cleared|{push}", text)
        finally:
            da.append_event = real
        if case == "missing":
            con = sqlite3.connect(db)
            con.execute("DELETE FROM tasks WHERE task_id = 'nt'")
            con.execute("DELETE FROM task_updates WHERE task_id = 'nt'")
            con.commit()
            con.close()
        else:
            ticket_row("nt", status="complete")
        world(local())
        tick(st, NOW + 60)
        written = any(text in line for line in event_lines())
        if written != (case == "closed"):
            bad.append(f"{case}: written={written}")
        if st.get("outbox"):
            bad.append(f"{case}: left in the outbox")
    assert not bad, bad


@scenario
def unpushed_reservation_cut_short_keeps_its_own_proof():
    """A kill right after emit saved an UNPUSHED reservation, before any line is written:
    the state read back holds the reservation with its owner and PUSH ids. If that PUSH (A)
    is withdrawn while another PUSH (B) on the ticket stands, the next tick never writes
    the line; if A still stands, it writes it. A reservation saved without that proof is
    withheld. Every case is checked; the failing ones are listed."""
    real = da.append_event
    bad = []
    text = "UNPUSHED ow: fix/x on local at 1a2b3c4, 1 commit ahead of origin"
    for case in ("withdrawn", "standing", "no-proof"):
        reset_db()
        (root / "supervisor" / "events.md").unlink(missing_ok=True)
        ticket_row("ow")
        st = fresh_state()
        world(local())
        tick(st)
        a = update("ow", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:ow-r")
        update("ow", "finding", "REVIEW VERDICT: PUSH 5e6f7a8", agent="codex:ow-r")
        st["cleared_floor"] = a + 1
        st["cleared_pending"] = {"ow|unpushed|1a2b3c4|": ["ow", text, 0, [a], "|ow"]}

        def killed(*_args, **_kwargs):
            raise KeyboardInterrupt

        da.append_event = killed
        try:
            world(local())
            tick(st, NOW + 60)
        except KeyboardInterrupt:
            pass
        finally:
            da.append_event = real
        saved = da.State(json.loads(st.path.read_text()), st.path)
        entry = (saved.get("outbox") or {}).get("ow|unpushed|1a2b3c4|")
        if not entry or entry.get("backs") != [a] or entry.get("owner") != "|ow":
            bad.append(f"{case}: reservation saved without its proof: {entry}")
            continue
        if case == "no-proof":
            del entry["backs"], entry["owner"]
        if case == "withdrawn":
            supersede(a, update("ow", "decision", "withdrawn", agent="codex:ow-r"))
        world(local())
        tick(saved, NOW + 120)
        written = any(text in line for line in event_lines())
        if written != (case == "standing"):
            bad.append(f"{case}: written={written}")
    assert not bad, bad


@scenario
def event_with_the_line_of_an_open_one_is_its_own():
    """Two events whose text and minute match are two lines: the later one, cut short by a
    kill, is appended by the next tick though the earlier one's line is open, and
    acknowledging the earlier one leaves the later one to be appended."""
    reset_db()
    five, six = pushed("dup", 2)
    path = root / "supervisor" / "events.md"
    text = "CLEARED REVIEW dup: PUSH 1a2b3c4"
    real = da.append_event

    def killed(*args, **kwargs):
        raise KeyboardInterrupt

    for ack in (False, True):
        path.unlink(missing_ok=True)
        st = da.State(dict(fresh_state()), state_file)
        st["cleared_floor"] = 10**9  # its PUSH rows back replays only; the tick raises none
        assert da.emit(st, f"dup|cleared|{five}", text)
        (first,) = event_lines()
        da.append_event = killed
        try:
            da.emit(st, f"dup|cleared|{six}", text)
        except KeyboardInterrupt:
            pass
        finally:
            da.append_event = real
        second = st["outbox"][f"dup|cleared|{six}"]["line"]
        assert second != first and text in second, (first, second)
        if ack:
            da.cmd_event_done(da.argparse.Namespace(lines=[first]))
        saved = da.State(json.loads(state_file.read_text()), state_file)
        world(local())
        tick(saved)
        assert event_lines() == ([second] if ack else [first, second]), (ack, event_lines())
        wake = ("term-orch", f"EVENTS: 2 new in {path}, {2 - ack} open, batch 1", True)
        assert sends(events=False) == [wake], (ack, sends(events=False))
        assert not saved.get("outbox"), saved["outbox"]
    path.unlink()
    state_file.unlink()


@contextmanager
def frozen_minute():
    """dev_auto's clock stopped at NOW, so every event line carries one minute; the
    yielded clock's now moves it."""

    class Clock:
        now = NOW

        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return self.now

        def gmtime(self, at=None):
            return time.gmtime(self.now if at is None else at)

    old, da.time = da.time, Clock()
    try:
        yield da.time
    finally:
        da.time = old


@scenario
def event_line_stays_its_own_through_an_unread_file_and_a_late_ack():
    """In one minute: an event whose file read and append both fail still gets a line of
    its own, appended beside the open one by the next tick; a line event-done removed is
    not handed out again, so a late repeat of that event-done removes nothing."""
    reset_db()
    five, six, seven = pushed("fz", 3)
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    text = "CLEARED REVIEW fz: PUSH 1a2b3c4"
    real_read, real_append = Path.read_text, da.append_event

    def unread(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self, *args, **kwargs)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    st = da.State(dict(fresh_state()), state_file)
    st["cleared_floor"] = 10**9  # its PUSH rows back replays only; the tick raises none
    try:
        with frozen_minute():
            assert da.emit(st, f"fz|cleared|{five}", text)
            (first,) = event_lines()
            Path.read_text, da.append_event = unread, full
            try:
                assert not da.emit(st, f"fz|cleared|{six}", text)
            finally:
                Path.read_text, da.append_event = real_read, real_append
            second = st["outbox"][f"fz|cleared|{six}"]["line"]
            assert second != first, second
            world(local())
            tick(st)
            assert event_lines() == [first, second], event_lines()
            da.cmd_event_done(da.argparse.Namespace(lines=[first]))
            st = da.State(json.loads(state_file.read_text()), state_file)
            assert da.emit(st, f"fz|cleared|{seven}", text)
            third = event_lines()[-1]
            assert third not in (first, second), third
            late = None
            try:
                da.cmd_event_done(da.argparse.Namespace(lines=[first]))
            except SystemExit as stop:
                late = str(stop.code)
            assert late and "nothing removed" in late, late
            assert event_lines() == [second, third], event_lines()
    finally:
        path.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)


@scenario
def event_line_a_day_later_is_never_an_earlier_ones():
    """At the same UTC minute a day and more later, a new event's line is not the line of
    an earlier one, whether that one still waits in the outbox or was acknowledged: both
    queued lines are appended, and a late repeat of the acknowledgement removes nothing."""
    reset_db()
    five, six = pushed("dy", 2)
    path = root / "supervisor" / "events.md"
    text = "CLEARED REVIEW dy: PUSH 1a2b3c4"
    real = da.append_event

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    later = NOW + da.EVENT_KEEP + 24 * 3600
    try:
        for acked in (False, True):
            path.unlink(missing_ok=True)
            st = da.State(dict(fresh_state()), state_file)
            st["cleared_floor"] = 10**9  # its PUSH rows back replays only; the tick raises none
            with frozen_minute() as clock:
                if acked:
                    assert da.emit(st, f"dy|cleared|{five}", text)
                    (first,) = event_lines()
                    da.cmd_event_done(da.argparse.Namespace(lines=[first]))
                    st = da.State(json.loads(state_file.read_text()), state_file)
                else:
                    da.append_event = full
                    try:
                        assert not da.emit(st, f"dy|cleared|{five}", text)
                    finally:
                        da.append_event = real
                    first = st["outbox"][f"dy|cleared|{five}"]["line"]
                clock.now = later
                assert da.emit(st, f"dy|cleared|{six}", text)
                second = event_lines()[-1]
                assert second != first and text in second, (first, second)
                world(local())
                tick(st, later)
                assert event_lines() == ([second] if acked else [second, first]), (acked, event_lines())
                assert not st.get("outbox"), st["outbox"]
                if acked:
                    late = None
                    try:
                        da.cmd_event_done(da.argparse.Namespace(lines=[first]))
                    except SystemExit as stop:
                        late = str(stop.code)
                    assert late and "nothing removed" in late and event_lines() == [second], (late, event_lines())
    finally:
        da.append_event = real
        path.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)


@scenario
def event_done_removes_only_the_acknowledged_line():
    """`event-done` rereads events.md under the supervisor lock and removes only the lines
    it names, with or without their "- ": a line a tick appended after the orchestrator
    read the file stays. A line not in the file changes nothing."""
    path = root / "supervisor" / "events.md"
    path.write_text("- 01:00 STUCK a: one\n- 01:01 STUCK b: two\n")
    with open(path, "a") as stream:
        stream.write("- 01:02 STUCK c: three\n")
    try:
        done = cli("event-done", "- 01:00 STUCK a: one", "01:02 STUCK c: three")
        assert done.returncode == 0, done
        assert path.read_text() == "- 01:01 STUCK b: two\n", path.read_text()
        done = cli("event-done", "- 09:59 STUCK z: none")
        assert done.returncode == 1 and "09:59 STUCK z: none" in done.stderr, done
        assert path.read_text() == "- 01:01 STUCK b: two\n", path.read_text()
    finally:
        path.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)


@scenario
def event_done_cut_short_never_brings_the_line_back():
    """A kill between event-done's two writes, the state and the file, leaves the line in
    the file to remove again; the next tick never appends or announces that acknowledged
    event once more."""
    reset_db()
    path = root / "supervisor" / "events.md"
    line = "- 01:00 CLEARED REVIEW ak: REVIEW VERDICT: PUSH 1a2b3c4 by codex:ak-review"
    path.write_text(line + "\n")
    data = dict(fresh_state())
    data["outbox"] = {"ak|cleared|7": {"line": line, "text": line[8:], "note_on": None, "detail": ""}}
    state_file.write_text(json.dumps(data))
    real, moves = Path.replace, []

    def replace(self, target):
        moves.append(target)
        if len(moves) == 2:
            raise KeyboardInterrupt
        return real(self, target)

    Path.replace = replace
    try:
        da.cmd_event_done(da.argparse.Namespace(lines=[line]))
    except KeyboardInterrupt:
        pass
    finally:
        Path.replace = real
    try:
        saved = da.State(json.loads(state_file.read_text()), state_file)
        world(local())
        tick(saved)
        assert event_lines().count(line) <= 1 and not saved.get("outbox"), (event_lines(), saved.get("outbox"))
        assert sends(events=False) == [], sends(events=False)
        da.cmd_event_done(da.argparse.Namespace(lines=[line]))
        assert event_lines() == [], event_lines()
    finally:
        path.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)


@scenario
def slots():
    """Caps, no mixing, disabled, at-limit, exclusion, shared ids, no capacity."""
    set_fixture()
    claude_usage("c1", 1, NOW + 600)
    assert pick({}) == "pro1" and pick({}, model=SONNET) == "pro1"
    one_opus = {"a": rec("a", "c1")}
    assert pick(one_opus) == "pro2", "pro holds one Opus"
    assert pick(one_opus, model=SONNET) == "pro2", "no Sonnet beside an Opus"
    one_sonnet = {"a": rec("a", "c1", model=SONNET)}
    assert pick(one_sonnet, model=SONNET) == "pro1", "pro holds two Sonnet"
    assert pick(one_sonnet) == "pro2", "no Opus beside a Sonnet"
    two_sonnet = {"a": rec("a", "c1", model=SONNET), "b": rec("b", "c1", model=SONNET)}
    assert pick(two_sonnet, model=SONNET) == "pro2"
    assert pick({"a": rec("a", "c1", model="claude-fable-5-2")}) == "pro2", (
        "Fable takes an Opus slot"
    )
    full = {"a": rec("a", "c1"), "b": rec("b", "c2"), "c": rec("c", "c3")}
    assert pick(full) == "max", "max holds two Opus"
    full["d"] = rec("d", "c3")
    assert pick(full) is None, "disabled row never picked"
    assert pick(full, ignore="d") == "max"
    assert pick({}, exclude={"c1"}) == "pro2"
    assert pick({}, host="Juzzark-WSL") == "max@wsl"
    assert (
        pick({"c": rec("c", "c3"), "d": rec("d", "c3")}, host="Juzzark-WSL") is None
    ), "same id shares slots"
    claude_usage("c1", 91, NOW + 600)
    assert pick({}) == "pro2", "5-hour use at the limit"
    claude_usage("c1", 99, NOW - 1)
    assert pick({}) == "pro1", "a window whose reset passed is free"
    claude_usage("c1", 1, NOW + 60, week=90, week_reset=NOW + 9000)
    assert pick({}, model=SONNET) == "pro1", "weekly use at the limit blocks nothing"
    clear_usage()
    assert pick({}, agent="codex", model="gpt-6.1-sol") == "x1"
    assert (
        pick({"a": rec("a", "x1", agent="codex")}, agent="codex", model="m") == "x1"
    ), "row slots"
    two = {"a": rec("a", "x1", agent="codex"), "b": rec("b", "x1", agent="codex")}
    assert pick(two, agent="codex", model="m") == "x2"
    codex_usage("x1", 2, 95, NOW + 600)
    assert pick({}, agent="codex", model="m") == "x1", "Codex weekly use blocks nothing"
    clear_usage()
    assert pick({}, agent="cursor", model="grok-4.7-high") == "cur"
    two["c"] = rec("c", "x2", agent="codex")
    state_file.write_text(json.dumps({"workers": two}))
    done = cli("slot", "--agent", "codex", "--model", "m")
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    state_file.unlink()


@scenario
def unreadable_state_file_stops_the_command_and_stays():
    """A state.json that exists but is no JSON object, or cannot be read, stops a slot
    lookup and a tick with an error naming it and stays byte for byte; a missing one still
    starts empty."""
    set_fixture()
    reset_db()
    clear_usage()
    try:
        for body in (b'{"workers": {"a": ', b"[]\n"):
            state_file.write_bytes(body)
            for args in (("slot", "--agent", "codex", "--model", "m"), ("tick",)):
                done = cli(*args)
                assert done.returncode == 1 and str(state_file) in done.stderr, (body, args, done)
                assert state_file.read_bytes() == body, (body, args)
        state_file.write_text('{"workers": {}}\n')
        state_file.chmod(0)
        done = cli("tick")
        state_file.chmod(0o600)
        assert done.returncode == 1 and str(state_file) in done.stderr, done
        assert state_file.read_text() == '{"workers": {}}\n'
        state_file.unlink()
        done = cli("slot", "--agent", "codex", "--model", "m")
        assert done.returncode == 0 and json.loads(state_file.read_text())["workers"] == {}, done
    finally:
        if state_file.exists():
            state_file.chmod(0o600)
        state_file.unlink(missing_ok=True)


@scenario
def gone_terminals_hold_no_slot():
    """A record whose terminal is gone (lost, held after exits, or exited with no slot to
    relaunch in) holds no slot on its id on any host; a live one still does."""
    set_fixture()
    claude_usage("c1", 1, NOW + 600)
    lost = {
        "a": rec("a", "c1", model=SONNET, lost="STUCK a: terminal gone"),
        "b": rec("b", "c1", model=SONNET, held="STUCK b: exited"),
    }
    assert pick(lost, model=SONNET) == "pro1", "lost and held records hold nothing"
    wsl = {
        "c": rec("c", "c3", host="Juzzark-WSL", lost="STUCK c: lost"),
        "d": rec("d", "c3", host="Juzzark-WSL", exited_at=NOW - 60),
    }
    others = {"c1", "c2"}
    assert pick(wsl, exclude=others) == "max", "gone workers of the id on another host"
    wsl["e"] = rec("e", "c3", host="Juzzark-WSL")
    wsl["f"] = rec("f", "c3", host="Juzzark-WSL")
    assert pick(wsl, exclude=others) is None, "two live Opus on the shared id fill it"
    held = da.models_held(cfg, lost, None)
    assert held.get("c1", []) == [], held
    clear_usage()


def limit_accounts(rows, head=""):
    """An accounts file with these rows, after accounts.toml's own settings and `head`."""
    path = tmp / "limits.toml"
    path.write_text(head + accounts.read_text().split("[[account]]")[0] + rows)
    return path


@scenario
def account_total_limit_moves_on():
    """A row's max_workers caps its running workers on any model, and slot moves to the
    next row; a row without it keeps its plan's slots."""
    set_fixture()
    path = limit_accounts(
        """
[[account]]
agent = "claude"
label = "one"
id = "t1"
plan = "team"
max_workers = 1

[[account]]
agent = "claude"
label = "next"
id = "t2"
plan = "team"

[[account]]
agent = "codex"
label = "cx"
id = "t3"
slots = 4
max_workers = 1

[[account]]
agent = "codex"
label = "cx2"
id = "t4"
"""
    )
    limited = da.load_config(path)

    def take(workers, agent="claude", model=SONNET):
        row = da.slot(limited, workers, agent, model, now=NOW)
        return row["label"] if row else None

    assert take({}) == "one"
    sonnet = {"a": rec("a", "t1", model=SONNET)}
    assert take(sonnet) == "next", "a second worker past max_workers 1"
    assert take({"a": rec("a", "t2", model=SONNET)}) == "one", "no key: two Sonnet"
    assert take({"a": rec("a", "t2", model=SONNET)}, model=OPUS) == "one"
    codex = {"a": rec("a", "t3", agent="codex", model="m")}
    assert take(codex, "codex", "m") == "cx2", "max_workers below slots"


@scenario
def account_model_limit_allows_other_model():
    """With max_workers, a Claude row's opus or sonnet cap holds that model alone: one
    Opus blocks a second Opus but leaves a Sonnet room up to the total. A finished
    ticket's worker holds nothing."""
    set_fixture()
    path = limit_accounts(
        """
[[account]]
agent = "claude"
label = "mixed"
id = "m1"
plan = "max"
max_workers = 2
opus = 1

[[account]]
agent = "claude"
label = "spare"
id = "m2"
plan = "max"
"""
    )
    limited = da.load_config(path)
    assert limited["account"][0]["max_workers"] == 2
    assert "max_workers" not in limited["account"][1]

    def take(workers, model):
        row = da.slot(limited, workers, "claude", model, now=NOW)
        return row["label"] if row else None

    opus = {"a": rec("a", "m1")}
    assert take(opus, OPUS) == "spare", "a second Opus past opus 1"
    assert take(opus, SONNET) == "mixed", "a Sonnet beside the Opus"
    both = {"a": rec("a", "m1"), "b": rec("b", "m1", model=SONNET)}
    assert take(both, SONNET) == "spare", "the total holds"
    finished = {"a": rec("a", "m1", done=True), "b": rec("b", "m1", model=SONNET)}
    assert take(finished, OPUS) == "mixed", "a finished ticket's worker holds nothing"
    both["c"] = rec("c", "m2", model=SONNET)
    assert take(both, OPUS) is None, "no key: never Opus beside a Sonnet"


@scenario
def no_capacity_names_the_limits():
    """When no row has room, slot prints `no capacity` and names each row's limit on
    stderr; a row over its usage rule names that rule, which is checked first."""
    set_fixture()
    path = limit_accounts(
        f"""
[[account]]
agent = "claude"
label = "total"
id = "n1"
plan = "team"
max_workers = 1

[[account]]
agent = "claude"
label = "opus"
id = "n2"
plan = "max"
max_workers = 3
opus = 1

[[account]]
agent = "claude"
label = "plan"
id = "n3"
plan = "team"

[[account]]
agent = "claude"
label = "used"
id = "n4"
home = "{homes["c1"]}"
plan = "team"
max_workers = 1
""",
        head="usage_reset_minutes = 40\n",
    )
    workers = {
        "a": rec("a", "n1", model=SONNET),
        "b": rec("b", "n2"),
        "c": rec("c", "n3"),
        "d": rec("d", "n4"),
    }
    state_file.write_text(json.dumps({"workers": workers}))
    try:
        claude_usage("c1", 95, NOW + 3600)
        done = cli(
            "slot", "--agent", "claude", "--model", OPUS, DEV_AUTO_ACCOUNTS=str(path)
        )
    finally:
        clear_usage()
        state_file.unlink()
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    assert done.stderr.splitlines() == [
        "total: max_workers 1 of 1",
        "opus: opus 1 of 1",
        "plan: opus 1 of 1",
        f"used: 5-hour use 95% at usage_full_percent 90, resets {da.stamp(NOW + 3600)}",
    ], done.stderr


def memory_accounts(wsl="", head="", tail=""):
    """The test accounts with `wsl` added to [hosts.Juzzark-WSL], `head` at the top and
    `tail` at the end."""
    path = tmp / "memory.toml"
    text = accounts.read_text().replace("[hosts.Juzzark-WSL]\n", "[hosts.Juzzark-WSL]\n" + wsl, 1)
    path.write_text(head + text + tail)
    return path


def host_memory(gb, source="proc-meminfo"):
    size = int(gb * 2**30)
    return {"availableMemory": size, "freeMemory": size, "availableMemorySource": source}


def memory_reads():
    return [r.get("args") for r in calls("orca", ["diagnostics", "memory"])]


WSL_SLOT = ("slot", "--agent", "codex", "--model", "m", "--host", "Juzzark-WSL")


@scenario
def memory_floor_slot_skips_a_linux_host_under_its_floor():
    """slot reads a Linux host's memory once: with more available than its floor
    (memory_floor_gb, default 8) its rows are offered as before; under it slot prints
    `no capacity` and names the host, the reading and the floor. A host's own
    memory_floor_gb overrides the default, and 0 turns the check off with no reading."""
    set_fixture(memory={"Juzzark-WSL": host_memory(12)})
    done = cli(*WSL_SLOT)
    assert (done.returncode, done.stdout.split("\t")[0]) == (0, "xw"), done
    assert memory_reads() == [["diagnostics", "memory", "--json", "--environment", "Juzzark-WSL"]]
    set_fixture(memory={"Juzzark-WSL": host_memory(6.5)})
    done = cli(*WSL_SLOT)
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    assert done.stderr.splitlines() == [
        "Juzzark-WSL: 6.5 GB memory available, under its floor of 8 GB; no worker starts there"
    ], done.stderr
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=str(memory_accounts(wsl="memory_floor_gb = 6\n")))
    assert done.returncode == 0, done
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=str(memory_accounts(head="memory_floor_gb = 10\n")))
    assert "under its floor of 10 GB" in done.stderr, done
    set_fixture(memory={"Juzzark-WSL": host_memory(0.5)})
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=str(memory_accounts(wsl="memory_floor_gb = 0\n")))
    assert done.returncode == 0 and memory_reads() == [], done


@scenario
def memory_floor_leaves_non_linux_hosts_unchecked():
    """A macOS host such as Mac2 is held to no floor: its memory-pressure reading says so,
    and so does its `uname -s` when Orca falls back to free-memory, as it does on macOS
    and Linux alike. A Linux host's fallback is held to the floor, and a fallback with no
    uname answer is unread. The local machine is read only when it is Linux."""
    set_fixture(memory={"Juzzark-WSL": host_memory(0.5, source="memory-pressure")})
    done = cli(*WSL_SLOT)
    assert done.returncode == 0, done
    mac2 = str(memory_accounts(tail='[[account]]\nagent = "codex"\nlabel = "xm"\nid = "xm"\nhost = "Mac2"\n'))
    mac2_slot = ("slot", "--agent", "codex", "--model", "m", "--host", "Mac2")
    fallback = host_memory(0.5, source="free-memory")
    set_fixture(memory={"Mac2": fallback}, uname={"-s": [0, "Darwin\n"]})
    done = cli(*mac2_slot, DEV_AUTO_ACCOUNTS=mac2)
    assert (done.returncode, done.stdout.split("\t")[0]) == (0, "xm"), done
    reset_db()
    ticket_row("mm")
    state_file.unlink(missing_ok=True)
    set_fixture(memory={"Mac2": fallback}, uname={"-s": [0, "Darwin\n"]})
    done = cli(
        *("launch", "mm", "--agent", "codex", "--model", "m", "--worktree", str(tmp)),
        *("--host", "Mac2"),
        DEV_AUTO_ACCOUNTS=mac2,
    )
    assert done.returncode == 0 and created(), done
    state_file.unlink(missing_ok=True)
    set_fixture(memory={"Juzzark-WSL": fallback}, uname={"-s": [0, "Linux\n"]})
    done = cli(*WSL_SLOT)
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    assert done.stderr.startswith("Juzzark-WSL: 0.5 GB memory available, under its floor"), done
    set_fixture(memory={"Juzzark-WSL": fallback})
    done = cli(*WSL_SLOT)
    assert done.stderr.splitlines()[0] == (
        "Juzzark-WSL: memory unread (a free-memory reading, and no uname -s from the host);"
        " no worker starts there"
    ), done

    def local_slot(platform):
        set_fixture(memory={"local": host_memory(0.5)})
        was, sys.platform = sys.platform, platform
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                da.main(["slot", "--agent", "codex", "--model", "m"])
        except SystemExit as stop:
            assert stop.code == 1, stop.code
        finally:
            sys.platform = was
        return out.getvalue(), err.getvalue()

    out, _err = local_slot("darwin")
    assert out.startswith("x1\t") and memory_reads() == [], out
    out, err = local_slot("linux")
    assert out == "no capacity\n" and memory_reads() == [["diagnostics", "memory", "--json"]] * 2, out
    assert err.splitlines()[0].startswith("local: 0.5 GB memory available"), err


@scenario
def memory_floor_holds_a_wsl_host_to_its_windows_side():
    """A WSL host whose entry names its Windows side (`windows`, an Orca environment) is
    also refused when that side's free memory is under windows_floor_gb (default 4).
    When the Windows side cannot be read the Linux reading alone decides, and slot says
    so. Each host is read once, and once more when that reading refuses."""
    path = str(memory_accounts(wsl='windows = "Juzzark"\n'))
    windows = host_memory(1.5, source="free-memory")
    set_fixture(memory={"Juzzark-WSL": host_memory(12), "Juzzark": windows})
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=path)
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    side = "Juzzark-WSL: its Windows side Juzzark has 1.5 GB memory free, under its floor of 4 GB"
    assert done.stderr.splitlines() == [f"{side}; no worker starts there"], done.stderr
    assert [a[-1] for a in memory_reads()] == ["Juzzark-WSL", "Juzzark"] * 2, memory_reads()
    low = str(memory_accounts(wsl='windows = "Juzzark"\nwindows_floor_gb = 1\n'))
    assert cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=low).returncode == 0
    lost = "Could not connect to the remote Orca runtime."
    set_fixture(memory={"Juzzark-WSL": host_memory(12), "Juzzark": lost})
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=path)
    assert (done.returncode, done.stdout.split("\t")[0]) == (0, "xw"), done
    assert done.stderr.splitlines() == [
        f"Juzzark-WSL: its Windows side Juzzark is unread ({lost}); the Linux reading alone decides"
    ], done.stderr
    set_fixture(memory={"Juzzark-WSL": host_memory(6), "Juzzark": windows})
    done = cli(*WSL_SLOT, DEV_AUTO_ACCOUNTS=path)
    assert "6.0 GB memory available" in done.stderr and len(memory_reads()) == 2, done


@scenario
def memory_refusal_is_read_again_and_held_above_its_floor():
    """A refused host is read once more before it is refused, so a dip passes; a host
    refused under its floor takes workers again only 1 GB above it, and for the recheck
    time after a refusal the host is refused without another read."""
    set_fixture(memory={"Juzzark-WSL": [host_memory(6.5), host_memory(12)]})
    done = cli(*WSL_SLOT)
    assert (done.returncode, done.stdout.split("\t")[0]) == (0, "xw"), done
    assert len(memory_reads()) == 2, memory_reads()
    set_fixture(memory={"Juzzark-WSL": host_memory(7.5)})
    assert cli(*WSL_SLOT).returncode == 1
    log_file.write_text("")
    data = json.loads(fixture_file.read_text())
    data["memory"] = {"Juzzark-WSL": host_memory(8.5)}
    fixture_file.write_text(json.dumps(data))
    done = cli(*WSL_SLOT)
    assert done.stderr.splitlines() == [
        "Juzzark-WSL: 8.5 GB memory available, under its floor of 8 GB (9 GB to take "
        "workers again); no worker starts there"
    ], done.stderr
    data["memory"] = {"Juzzark-WSL": host_memory(9.2)}
    fixture_file.write_text(json.dumps(data))
    assert cli(*WSL_SLOT).returncode == 0
    data["memory"] = {"Juzzark-WSL": host_memory(5)}
    fixture_file.write_text(json.dumps(data))
    log_file.write_text("")
    wait, da.MEMORY_RECHECK = da.MEMORY_RECHECK, 300
    sleep, da.time.sleep = da.time.sleep, lambda _s: None
    try:
        first = da.memory_refusal(cfg, "Juzzark-WSL")[0]
        again = da.memory_refusal(cfg, "Juzzark-WSL")[0]
    finally:
        da.MEMORY_RECHECK, da.time.sleep = wait, sleep
    assert first and first == again and len(memory_reads()) == 2, memory_reads()


@scenario
def memory_refusal_keeps_what_a_tick_saved_during_its_recheck():
    """A launch's memory recheck never writes its older copy of the memory file over a
    tick's: the Windows trend and low mark the tick saves while the launch waits to read
    again stay, beside the launch's own refusal."""
    on = da.load_config(memory_accounts(wsl='windows = "Juzzark"\n'))
    da.memory_file().unlink(missing_ok=True)
    st = fresh_state(ww=rec("ww", "c3", host="Juzzark-WSL"))
    windows = host_memory(2, source="free-memory")
    world([], memory={"Juzzark-WSL": host_memory(0.5), "Juzzark": windows})
    sleep, da.time.sleep = da.time.sleep, lambda _s: da.windows_watch(on, st, NOW)
    try:
        refusal, _notes = da.memory_refusal(on, "Juzzark-WSL")
    finally:
        da.time.sleep = sleep
    seen = da.memory_store()["Juzzark-WSL"]
    assert refusal and seen["refused"]["text"] == refusal, seen
    assert seen.get("windows_low") == int(NOW) and seen.get("windows_trend") == [[int(NOW), 2.0]], seen
    da.memory_file().unlink(missing_ok=True)



@scenario
def memory_refusal_keeps_the_later_of_two_readings():
    """Two admission checks of one host: one reads room and pauses before its save while
    the other reads the host under its floor twice and saves the refusal. The earlier
    reading's save leaves the later one's floor and refusal in place."""
    on = da.load_config(memory_accounts())
    da.memory_file().unlink(missing_ok=True)
    world([], memory={"Juzzark-WSL": [host_memory(12), host_memory(5), host_memory(5)]})
    real, inner = da.save_memory, []

    def first_waits(host, seen, keys):
        if not inner:
            inner.append(None)
            inner[0] = da.memory_refusal(on, "Juzzark-WSL")[0]
        return real(host, seen, keys)

    da.save_memory = first_waits
    try:
        outer = da.memory_refusal(on, "Juzzark-WSL")[0]
    finally:
        da.save_memory = real
    assert outer is None and inner[0], (outer, inner)
    seen = da.memory_store()["Juzzark-WSL"]
    assert (seen.get("refused") or {}).get("text") == inner[0], seen
    assert seen.get("under") == {"linux": 8}, seen
    da.memory_file().unlink(missing_ok=True)


@scenario
def windows_side_low_is_one_stuck_with_its_trend():
    """Each tick reads the Windows side of a WSL host with live workers; under its floor
    it is one STUCK with the trend, and a host whose list then fails twice names it."""
    path = memory_accounts(wsl='windows = "Juzzark"\n')
    on = da.load_config(path)
    reset_db()
    ticket_row("ww")
    st = fresh_state(ww=rec("ww", "c3", host="Juzzark-WSL"))
    row, screen = term("term-ww", 5)
    seq = [host_memory(gb, source="free-memory") for gb in (5, 3.2, 2.1)]
    world([("Juzzark-WSL", "term-ww", row, screen)], memory={"Juzzark": seq})
    for later in (0, 30, 60):
        da.tick(on, root, st, da.read_tickets(root), NOW + later)
    stuck = [t for _w, t, _e in sends() if "Windows side" in t]
    assert stuck == [
        "STUCK Juzzark-WSL: its Windows side Juzzark has 3.2 GB memory free, under its floor "
        "of 4 GB, trend 5 then 3.2 GB. Its 1 workers keep running and no worker starts there"
    ], stuck
    data = json.loads(fixture_file.read_text())
    data["terminals"]["Juzzark-WSL"] = "down"
    fixture_file.write_text(json.dumps(data))
    for later in (90, 120):
        da.tick(on, root, st, da.read_tickets(root), NOW + later)
    down = [t for _w, t, _e in sends() if "failed twice" in t]
    assert down and down[0].endswith("Its Windows side last read 5 then 3.2 then 2.1 then 2.1 GB"), down


@scenario
def host_max_workers_caps_every_kind_on_the_host():
    """A [hosts.<name>] max_workers caps the live records of every agent kind there;
    gone records do not count and other hosts are unaffected."""
    on = da.load_config(memory_accounts(wsl="max_workers = 2\n"))
    workers = {
        "a": rec("a", "c3", host="Juzzark-WSL"),
        "b": rec("b", "xw", agent="codex", host="Juzzark-WSL"),
    }
    set_fixture()
    why = []
    assert da.free_row(on, workers, "codex", "m", "Juzzark-WSL", now=NOW, why=why) is None
    assert why == ["Juzzark-WSL: max_workers 2 of 2 on the host, every agent kind"], why
    workers["b"]["lost"] = "STUCK b: gone"
    row = da.free_row(on, workers, "codex", "m", "Juzzark-WSL", now=NOW)
    assert row and row["label"] == "xw", row
    assert da.free_row(on, workers, "codex", "m", "local", now=NOW)["label"] == "x1"


@scenario
def host_check_lock_runs_one_check_at_a_time():
    """host-check-lock runs its command under the host's lock and returns its code; a
    second check waits until the first ends."""
    lock = tmp / "check.lock"
    env = dict(os.environ, DEV_AUTO_CHECK_LOCK=str(lock))
    tool = str(HERE / "host-check-lock")
    done = subprocess.run([tool, "sh", "-c", "exit 3"], env=env, capture_output=True, text=True)
    assert done.returncode == 3, done
    first = subprocess.Popen([tool, "sh", "-c", "sleep 1; echo first"], env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    began = time.monotonic()
    second = subprocess.run([tool, "echo", "second"], env=env, capture_output=True, text=True)
    waited = time.monotonic() - began
    assert first.wait() == 0 and second.returncode == 0, second
    assert waited >= 0.5 and "waiting" in second.stderr, (waited, second.stderr)


@scenario
def host_check_lock_survives_wrapper_death():
    lock = tmp / "killed-wrapper.lock"
    env = dict(os.environ, DEV_AUTO_CHECK_LOCK=str(lock))
    tool = str(HERE / "host-check-lock")
    first = subprocess.Popen(
        [tool, sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.readline()"],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert first.stdout.readline().strip() == "ready"
        first.kill()
        first.wait(timeout=5)
        second = subprocess.Popen(
            [tool, "true"], env=env, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert "waiting" in second.stderr.readline(), "second check entered while first was alive"
            first.stdin.close()
            assert second.wait(timeout=5) == 0
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=5)
            second.stderr.close()
    finally:
        first.stdin.close()
        first.stdout.close()
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)


@scenario
def usage_grant_takes_workers_up_to_its_cap():
    """usage_grant on a row with max_workers takes workers past usage_full_percent and
    the Pro Opus cap, never past max_workers; without max_workers the row is refused
    as today. slot names the grant it used on stderr."""
    set_fixture()
    path = limit_accounts(
        f"""
[[account]]
agent = "claude"
label = "granted"
id = "u1"
home = "{homes["c1"]}"
plan = "team"
max_workers = 1
usage_grant = true

[[account]]
agent = "claude"
label = "uncapped"
id = "u2"
home = "{homes["c2"]}"
plan = "team"
usage_grant = true

[[account]]
agent = "claude"
label = "pro"
id = "u3"
home = "{homes["c3"]}"
plan = "pro"
max_workers = 1
usage_grant = true
""",
        head="usage_reset_minutes = 40\n",
    )
    limited = da.load_config(path)

    def take(workers, label, model=SONNET):
        row = da.slot(limited, workers, "claude", model, now=NOW, account=label)
        return row["label"] if row else None

    try:
        claude_usage("c1", 95, NOW + 3600)
        claude_usage("c2", 95, NOW + 3600)
        claude_usage("c3", 75, NOW + 3600)
        assert take({}, "granted") == "granted", "past usage_full_percent"
        assert take({"a": rec("a", "u1", model=SONNET)}, "granted") is None, (
            "never past max_workers"
        )
        assert take({}, "uncapped") is None, "no max_workers: refused as today"
        assert take({}, "pro", OPUS) == "pro", "Opus past the Pro cap"
        assert take({"a": rec("a", "u3")}, "pro", OPUS) is None
        state_file.unlink(missing_ok=True)
        done = cli(
            "slot", "--agent", "claude", "--model", OPUS, DEV_AUTO_ACCOUNTS=str(path)
        )
    finally:
        clear_usage()
    assert done.returncode == 0 and done.stdout.startswith("u1\tgranted\t"), done
    granted = "granted: usage_grant past 5-hour use 95% at usage_full_percent 90"
    assert done.stderr.splitlines() == [
        f"{granted}, resets {da.stamp(NOW + 3600)}"
    ], done.stderr


SPEND = root / "supervisor" / "sdk-spend.json"


def dead_pid():
    done = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True)
    return int(done.stdout)
KEY = tmp / "anthropic.key"
KEY.write_text("sk-ant-api03-SECRET-test-key\n")


def sdk_accounts():
    """One Team Claude row, a disabled one, then an sdk row billed to the API key."""
    return da.load_config(
        limit_accounts(
            f"""
[[account]]
agent = "claude"
label = "team1"
id = "c1"
home = "{homes["c1"]}"
plan = "team"

[[account]]
agent = "claude"
label = "off"
id = "c4"
plan = "max"
enabled = false

[[account]]
agent = "sdk"
label = "api"
id = "sdk1"
key_file = "{KEY}"
monthly_usd = 100
ticket_usd = 20

[[account]]
agent = "sdk"
label = "api2"
id = "sdk2"
key_file = "{KEY}"
monthly_usd = 100
ticket_usd = 20
"""
        )
    )


@scenario
def sdk_row_is_overflow_within_its_monthly_cap():
    """slot gives the sdk row only when no enabled subscription Claude row can take the
    model, and never once the month's recorded spend (UTC) reaches monthly_usd; an
    explicit --account still selects it, within that cap."""
    set_fixture()
    sdk = sdk_accounts()
    busy = {"a": rec("a", "c1")}

    def take(workers, model=OPUS, **kw):
        why = []
        row = da.slot(sdk, workers, "sdk", model, now=NOW, why=why, **kw)
        return (row["label"] if row else None), why

    unkeyed = tmp / "unkeyed.toml"
    unkeyed.write_text('[[account]]\nagent = "sdk"\nlabel = "bare"\nid = "s0"\nmonthly_usd = 5\n')
    try:
        da.load_config(unkeyed)
        raise AssertionError("an sdk row without key_file and ticket_usd loads")
    except SystemExit as error:
        assert str(error) == "account bare: agent sdk needs key_file, ticket_usd", error
    try:
        SPEND.unlink(missing_ok=True)
        label, why = take({})
        assert label is None, "team1 can take Opus"
        assert why == [
            "api: overflow only; team1 can take claude-opus-5-5",
            "api2: overflow only; team1 can take claude-opus-5-5",
        ], why
        assert take(busy) == ("api", []), "team1 is full; the disabled row never counts"
        one_sonnet = {"a": rec("a", "c1", model=SONNET)}
        assert take(one_sonnet, SONNET)[0] is None, "team1 still takes a second Sonnet"
        assert take({}, account="api") == ("api", []), "--account selects it"
        # 2_000_000_000 is 2033-05-18 in UTC.
        SPEND.write_text(json.dumps({"2033-04": {"usd": 250.0}, "2033-05": {"usd": 99.5}}))
        assert take(busy) == ("api", []), "below the cap; last month does not count"
        SPEND.write_text(json.dumps({"2033-05": {"usd": 100.0}}))
        label, why = take(busy)
        full = ["api: monthly_usd 100 spent or reserved (100.00)", "api2: monthly_usd 100 spent or reserved (100.00)"]
        assert label is None and why == full, why
        assert take({}, account="api")[0] is None, "--account never passes the cap"
        # A live runner's unspent reservation holds the month's headroom; a dead one's not.
        held = {"other": {"pid": os.getpid(), "usd": 12.0, "spent": 2.0}}
        SPEND.write_text(json.dumps({"2033-05": {"usd": 90.0}, "reserved": held}))
        assert take(busy) == (None, full)
        held["other"]["pid"] = dead_pid()
        SPEND.write_text(json.dumps({"2033-05": {"usd": 90.0}, "reserved": held}))
        assert take(busy) == ("api", []), "a dead runner holds nothing"
        this_month = time.strftime("%Y-%m", time.gmtime())
        SPEND.write_text(json.dumps({this_month: {"usd": 100.0}}))
        done = cli("slot", "--agent", "sdk", "--model", OPUS, "--account", "api",
                   DEV_AUTO_ACCOUNTS=str(tmp / "limits.toml"))
        assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    finally:
        SPEND.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)


@scenario
def sdk_row_max_workers_caps_its_runners():
    """An sdk row's max_workers caps its runners like any row's, below its slots and with
    the month's spend still in room."""
    set_fixture()
    sdk = sdk_accounts()
    api = next(row for row in sdk["account"] if row["label"] == "api")
    api.update(slots=2, max_workers=1)
    SPEND.unlink(missing_ok=True)
    why = []
    runner = {"r": rec("r", "sdk1", agent="sdk")}
    row = da.slot(sdk, runner, "sdk", OPUS, now=NOW, why=why, account="api")
    assert row is None and why == ["api: max_workers 1 of 1"], (row, why)
    assert da.slot(sdk, {}, "sdk", OPUS, now=NOW, account="api")["label"] == "api"


@scenario
def sdk_slot_and_runner_use_the_selected_rows_monthly_budget():
    set_fixture()
    config = sdk_accounts()
    second_key = tmp / "sdk-second.key"
    second_key.write_text("different-test-key\n")
    second_row = da.account(config, "sdk2", "local")
    second_row["key_file"] = str(second_key)
    month = time.time()
    period = runner.month_key(month)
    SPEND.write_text(json.dumps({
        period: {"usd": 96, "tickets": {"other": 96}, "accounts": {"sdk1": 96}},
        "reserved": {"held": {"account": "sdk1", "pid": None, "open": True, "usd": 4, "spent": 0}},
    }))
    sdk, log, plays = fake_sdk()
    plays.append([[sdk_init(sdk), sdk_result(sdk, 0.3)]])
    args = runner_args(ticket_usd=200)
    args.account = "sdk2"
    args.key_file = str(second_key)
    out = io.StringIO()
    os.environ["CLAUDE_CONFIG_DIR"] = str(SDK_HOME)
    try:
        first = da.slot(config, {}, "sdk", OPUS, now=month, account="sdk1")
        second = da.slot(config, {}, "sdk", OPUS, now=month, account="sdk2")
        code = asyncio.run(runner.run(args, sdk, io.StringIO(), out))
        budgets = [client["max_budget_usd"] for client in clients(log)]
        assert (first, second and second["id"], code, budgets) == (None, "sdk2", 0, [100]), (
            first, second, code, budgets, out.getvalue(),
        )
        data = json.loads(SPEND.read_text())
        assert data[period] == {
            "usd": 96.3, "tickets": {"other": 96, "t-1": 0.3},
            "accounts": {"sdk1": 96, "sdk2": 0.3},
        }, data
        held = {
            "account": "sdk2", "pid": None, "open": True, "usd": 10,
            "spent": 0.3, "exact": 0, "months": {period: 0.3},
        }
        data["reserved"]["t-1"] = held
        SPEND.write_text(json.dumps(data))
        assert runner.settle(["--ticket", "t-1", "--spend", str(SPEND), "--usd", "0.2"]) == 0
        assert json.loads(SPEND.read_text())[period]["accounts"] == {"sdk1": 96, "sdk2": 0.2}
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_ticket_keeps_its_key():
    """A ticket whose worker ran on an sdk row stays on that row, so its prompt cache
    survives: a relaunch or a replacing launch never moves it to another key, takes no
    overflow check on its own row, and --account cannot name another one."""
    set_fixture()
    sdk = sdk_accounts()
    mine = {"t-1": rec("t-1", "sdk2", agent="sdk")}

    def take(workers, **kw):
        why = []
        row = da.slot(sdk, workers, "sdk", OPUS, now=NOW, why=why, ignore="t-1", **kw)
        return (row["label"] if row else None), why

    SPEND.unlink(missing_ok=True)
    assert take(mine) == ("api2", ["api: ticket t-1 keeps its key on api2"]), take(mine)
    label, why = take(mine, account="api")
    assert label is None and why == ["api: ticket t-1 keeps its key on api2"], why
    claude_before = {"t-1": rec("t-1", "c1")}
    assert take(claude_before)[0] is None, "a Claude ticket still takes sdk only as overflow"


@scenario
def pro_opus_above_70():
    """Pro above 70 on the 5-hour window takes no Opus worker; weekly use never counts.
    Sonnet on that Pro row, Max and Team at 75, and Pro at exactly 70 still do. The
    account file omits hard_pro_full_percent, so the skip is the loader default of 70."""
    assert "hard_pro_full_percent" not in tomllib.loads(accounts.read_text())
    set_fixture()

    def windows(name, five, week):
        data = {
            "rate_limits": {
                "five_hour": {"used_percentage": five, "resets_at": NOW + 3600},
                "seven_day": {"used_percentage": week, "resets_at": NOW + 86400},
            }
        }
        (homes[name] / "rate-limits.json").write_text(json.dumps(data))

    def hold(pairs):
        workers = {}
        n = 0
        for account, count in pairs:
            for _ in range(count):
                n += 1
                workers[f"h{n}"] = rec(f"h{n}", account)
        state_file.write_text(json.dumps({"workers": workers}))

    def slot_for(model):
        done = cli("slot", "--agent", "claude", "--model", model)
        return done.returncode, done.stdout

    def chose(model, account_id, label):
        code, out = slot_for(model)
        assert code == 0 and out.startswith(f"{account_id}\t{label}\t"), out

    # Team holds one Opus and Max holds two, so only the Pro row can take this worker.
    blocked = (("c2", 1), ("c3", 2))
    try:
        hold(blocked)
        windows("c1", 1, 75)
        chose(OPUS, "c1", "pro1")
        chose(SONNET, "c1", "pro1")
        windows("c1", 75, 1)
        code, out = slot_for(OPUS)
        assert (code, out) == (1, "no capacity\n"), out
        hold((("c1", 1), ("c2", 1)))
        windows("c3", 1, 75)
        chose(OPUS, "c3", "max")
        hold((("c1", 1),))
        windows("c2", 1, 75)
        chose(OPUS, "c2", "pro2")
        hold(blocked)
        windows("c1", 70, 1)
        chose(OPUS, "c1", "pro1")
    finally:
        clear_usage()
        state_file.unlink(missing_ok=True)


@scenario
def usage_old_or_reset_reading_is_unread():
    """A reading older than the age limit, or one whose windows have all reset, is
    unread, never 0%; a single reset window only drops out."""
    set_fixture()
    pro = da.account(cfg, "c1", "local")
    try:
        claude_usage(
            "c1", 100, NOW - 60, week=100, week_reset=NOW - 30, saved=NOW - 3 * 86400
        )
        assert da.peak(cfg, pro, NOW) == (None, None), da.peak(cfg, pro, NOW)
        assert not da.at_limit(cfg, pro, NOW)
        assert da.pro_opus_full(cfg, pro, OPUS, NOW), "unread Pro takes no Opus"
        assert pick({}) == "pro2"
        claude_usage("c1", 28, NOW + 8000, week=52, saved=NOW - 96 * 60)
        assert da.peak(cfg, pro, NOW) == (None, None), "96 minutes old is unread"
        claude_usage("c1", 99, NOW - 1, week=99, week_reset=NOW - 1)
        assert da.peak(cfg, pro, NOW) == (None, None), "every window reset is unread"
        claude_usage("c1", 99, NOW - 1, week=52)
        assert da.peak(cfg, pro, NOW) == (52.0, NOW + 86400), "the live window stays"
    finally:
        clear_usage()


@scenario
def usage_codex_age_is_its_usage_line():
    """A Codex file's age is the usage line's own timestamp: a newer rollout without
    usage, or later events after the line, do not make an old reading current; a line
    without a timestamp is unread."""
    x1 = da.account(cfg, "x1", "local")
    folder = homes["x1"] / "sessions" / "2033" / "05" / "18"
    try:
        set_fixture()
        codex_usage("x1", 28, 52, NOW + 8000, saved=NOW - 96 * 60)
        os.utime(folder / "rollout.jsonl", (NOW - 96 * 60, NOW - 96 * 60))
        fresh = folder / "rollout-new.jsonl"
        fresh.write_text(json.dumps({"payload": {"type": "session_started"}}) + "\n")
        os.utime(fresh, (NOW, NOW))
        assert da.peak(cfg, x1, NOW) == (None, None), "a newer file without usage"
        fresh.unlink()
        with open(folder / "rollout.jsonl", "a") as stream:
            stream.write(json.dumps({"payload": {"type": "agent_message"}}) + "\n")
        os.utime(folder / "rollout.jsonl", (NOW, NOW))
        assert da.peak(cfg, x1, NOW) == (None, None), "later events in the same file"
        limits = {"primary": {"used_percent": 28, "resets_at": NOW + 8000}}
        (folder / "rollout.jsonl").write_text(
            json.dumps({"payload": {"rate_limits": limits}}) + "\n"
        )
        assert da.peak(cfg, x1, NOW) == (None, None), "no timestamp is unread"
        codex_usage("x1", 28, 52, NOW + 8000)
        assert da.peak(cfg, x1, NOW) == (52.0, NOW + 8000)
    finally:
        clear_usage()


@scenario
def usage_codex_session_from_yesterday_is_still_read():
    """A Codex session started before midnight keeps writing to its own day's folder;
    another session's folder for today, without usage, does not hide its fresh reading."""
    x1 = da.account(cfg, "x1", "local")
    today = homes["x1"] / "sessions" / "2033" / "05" / "19"
    try:
        set_fixture()
        codex_usage("x1", 28, 100, NOW + 8000)
        today.mkdir(parents=True)
        started = today / "rollout-today.jsonl"
        started.write_text(json.dumps({"payload": {"type": "session_started"}}) + "\n")
        os.utime(started, (NOW - 60, NOW - 60))
        assert da.peak(cfg, x1, NOW) == (100.0, NOW + 8000), da.peak(cfg, x1, NOW)
    finally:
        shutil.rmtree(today, ignore_errors=True)
        clear_usage()



@scenario
def usage_codex_newest_line_wins_across_rollouts():
    """Across rollouts the newest usage line is the reading: a file written last with only
    other events after its old line never hides another file's fresh one, and a session
    resumed in a folder older than a week of later ones is still read."""
    x1 = da.account(cfg, "x1", "local")
    sessions = homes["x1"] / "sessions" / "2033"
    full = {
        "primary": {"used_percent": 28, "window_minutes": 300, "resets_at": NOW + 8000},
        "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_at": NOW + 8000},
    }
    made = []

    def rollout(day, name, text, at):
        folder = sessions / day
        folder.mkdir(parents=True, exist_ok=True)
        made.append(folder)
        path = folder / name
        path.write_text(text)
        os.utime(path, (at, at))

    try:
        set_fixture()
        codex_usage("x1", 28, 52, NOW + 8000, saved=NOW - 96 * 60)
        old = homes["x1"] / "sessions" / "2033" / "05" / "18" / "rollout.jsonl"
        with open(old, "a") as stream:
            stream.write(json.dumps({"payload": {"type": "agent_message"}}) + "\n")
        os.utime(old, (NOW, NOW))
        rollout("05/19", "rollout-today.jsonl", codex_line(full, NOW - 30), NOW - 30)
        assert da.peak(cfg, x1, NOW) == (100.0, NOW + 8000), da.peak(cfg, x1, NOW)
        clear_usage()
        for folder in made:
            shutil.rmtree(folder, ignore_errors=True)
        rollout("04/01", "rollout-resumed.jsonl", codex_line(full, NOW - 30), NOW - 30)
        for day in range(10, 18):
            note = json.dumps({"payload": {"type": "session_started"}}) + "\n"
            rollout(f"05/{day}", "rollout-other.jsonl", note, NOW - 3600)
        assert da.peak(cfg, x1, NOW) == (100.0, NOW + 8000), da.peak(cfg, x1, NOW)
    finally:
        for folder in made:
            shutil.rmtree(folder, ignore_errors=True)
        clear_usage()


@scenario
def usage_remote_row_never_reads_a_local_home():
    """A remote row of an account id never reads the main Mac's home of that id. Orca's
    reading of that id is the login's own usage, so it does count."""
    set_fixture()
    wsl = da.account(cfg, "c3", "Juzzark-WSL")
    try:
        claude_usage("c3", 99, NOW + 600)
        assert da.peak(cfg, da.account(cfg, "c3", "local"), NOW)[0] == 99
        assert da.peak(cfg, wsl, NOW) == (None, None), da.peak(cfg, wsl, NOW)
        assert pick({}, host="Juzzark-WSL") == "max@wsl"
        now = NOW + 0.1
        set_fixture(accounts=orca_accounts(claude=[("c3", orca_reading(95, 10))]))
        on, _ = rules_config(usage_reset_minutes=40)
        assert da.slot(on, {}, "claude", OPUS, "Juzzark-WSL", now=now) is None
    finally:
        clear_usage()


@scenario
def usage_unread_pro_takes_no_opus():
    """A Claude Pro account with no reading takes no Opus worker; Sonnet still goes."""
    set_fixture()
    clear_usage()
    pro = da.account(cfg, "c1", "local")
    assert da.peak(cfg, pro, NOW) == (None, None)
    assert pick({}) == "pro2", "unread Pro takes no Opus"
    assert pick({}, model=SONNET) == "pro1"
    claude_usage("c1", 10, NOW + 600)
    assert pick({}) == "pro1"
    clear_usage()


@scenario
def usage_orca_wins_over_older_file():
    """Orca's reading of an account, selected or not, comes before its CLI file; an Orca
    reading past the age limit gives way to a fresh file."""
    try:
        claude_usage("c1", 1, NOW + 600, saved=NOW - 120)
        codex_usage("x1", 1, 1, NOW + 600)
        now = NOW + 0.25
        readings = orca_accounts(
            claude=[("c2", orca_reading(10, 10)), ("c1", orca_reading(95, 10))],
            codex=[("x1", orca_reading(96, 2))],
            grok=orca_reading(91, 0),
        )
        set_fixture(accounts=readings)
        on, _ = rules_config(usage_reset_minutes=40)
        row = da.slot(on, {}, "claude", SONNET, now=now)
        assert row and row["id"] == "c2", "Orca's 95% beats the file's 1%"
        row = da.slot(on, {}, "codex", "m", now=now)
        assert row and row["id"] == "x2", "the selected Codex account at 96% 5-hour"
        assert da.slot(on, {}, "grok", "grok-4.7", now=now) is None
        now = NOW + 0.5
        old = orca_reading(95, 10, updated=NOW - 3600)
        set_fixture(accounts=orca_accounts(claude=[("c1", old)]))
        row = da.slot(on, {}, "claude", SONNET, now=now)
        assert row and row["id"] == "c1", "an old Orca reading gives way to the file"
    finally:
        clear_usage()


@scenario
def usage_row_under_an_old_id_reads_its_login_through_its_home():
    """A row whose id Orca no longer reads finds its login through its home: the Orca
    login with that home, or whose id names a folder of it, is the row's reading."""
    set_fixture()
    clear_usage()
    try:
        now = NOW + 0.75
        set_fixture(
            accounts=orca_accounts(
                claude=[("o2", orca_reading(10, 10)), ("o1", orca_reading(100, 40))],
                logins=[("claude", "o1", str(homes["c1"])), ("claude", "o2", "/elsewhere")],
            )
        )
        pro1 = da.account(cfg, "c1", "local")
        assert da.peak(cfg, pro1, now)[0] == 100.0, da.peak(cfg, pro1, now)
        on, _ = rules_config(usage_reset_minutes=40)
        row = da.slot(on, {}, "claude", SONNET, now=now)
        assert row and row["label"] == "pro2", "pro1's login is at 100%"
        path = limit_accounts(
            '[[account]]\nagent = "codex"\nlabel = "moved"\nid = "x-old"\n'
            f'home = "{tmp / "codex-accounts" / "x9" / "home"}"\n'
        )
        moved = da.load_config(path)["account"][0]
        now = NOW + 0.8
        set_fixture(
            accounts=orca_accounts(
                codex=[("x9", orca_reading(97, 50))], logins=[("codex", "x9", "")]
            )
        )
        assert da.peak(cfg, moved, now)[0] == 97.0, "the id in its home's path"
        remote = da.account(cfg, "c3", "Juzzark-WSL")
        assert da.orca_key(remote, now) == ("claude", "c3"), "a remote row keeps its id"
    finally:
        clear_usage()


@scenario
def usage_file_parsed_once_until_it_changes():
    """A CLI usage file is parsed again only when it changes, so every slot question of a
    tick reads it once."""
    set_fixture()
    clear_usage()
    claude_usage("c1", 20, NOW + 600)
    parsed = []
    real = da.usage_windows
    da.usage_windows = lambda row: parsed.append(row["id"]) or real(row)
    try:
        pro1 = da.account(cfg, "c1", "local")
        for _ in range(5):
            assert da.peak(cfg, pro1, NOW)[0] == 20.0
        assert parsed.count("c1") == 1, parsed
        claude_usage("c1", 95, NOW + 600, saved=NOW + 1)
        assert da.peak(cfg, pro1, NOW)[0] == 95.0, "a changed file is read again"
        assert parsed.count("c1") == 2, parsed
    finally:
        da.usage_windows = real
        clear_usage()


@scenario
def usage_file_removed_while_read_is_unread():
    """A Codex rollout removed between its listing and its stat, a day folder removed
    before it is listed, or a Claude file without saved_at removed after its read leaves
    that account unread; the slot question goes on."""
    set_fixture()
    clear_usage()
    da.FILE_USAGE.clear()
    x1, c1 = da.account(cfg, "x1", "local"), da.account(cfg, "c1", "local")
    real_stat, real_iterdir = Path.stat, Path.iterdir
    gone = set()

    def stat(self, *args, **kwargs):
        if self.name in gone:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, *args, **kwargs)

    def iterdir(self):
        if self.name in gone:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_iterdir(self)

    codex_usage("x1", 28, 52, NOW + 8000)
    limits = {"five_hour": {"used_percentage": 20, "resets_at": NOW + 600}}
    (homes["c1"] / "rate-limits.json").write_text(json.dumps({"rate_limits": limits}))
    Path.stat, Path.iterdir = stat, iterdir
    try:
        for name, row in (("rollout.jsonl", x1), ("05", x1), ("rate-limits.json", c1)):
            gone.clear()
            gone.add(name)
            da.FILE_USAGE.clear()
            assert da.peak(cfg, row, NOW) == (None, None), name
        assert pick({}, agent="codex", model="m") == "x1"
    finally:
        Path.stat, Path.iterdir = real_stat, real_iterdir
        da.FILE_USAGE.clear()
        clear_usage()


def rules_config(**keys):
    """The test accounts with these launch-rule keys set at the top level."""
    path = tmp / "rules.toml"
    head = "".join(f"{k} = {json.dumps(v)}\n" for k, v in keys.items())
    path.write_text(head + accounts.read_text())
    return da.load_config(path), path


RULES = {"usage_reset_minutes": 40, "max_hard_tickets": 2, "max_medium_tickets": 3}


def take(config, label, model=SONNET, agent="claude", why=None):
    row = da.slot(config, {}, agent, model, now=NOW, account=label, why=why)
    return row["label"] if row else None


@scenario
def launch_rules_weekly_use_blocks_no_launch():
    """The operator resets a Claude row's weekly use, so a full weekly window blocks no
    Claude account, with the rule keys set or unset; the Pro Opus rule reads the 5-hour
    window only."""
    set_fixture()
    off = da.load_config(accounts)
    on, _ = rules_config(**RULES)
    try:
        for config in (off, on):
            claude_usage("c1", 10, NOW + 3600, week=100, week_reset=NOW + 9000)
            claude_usage("c2", 10, NOW + 3600, week=100, week_reset=NOW + 9000)
            assert take(config, "pro2") == "pro2", "team at weekly 100"
            assert take(config, "pro1", OPUS) == "pro1", "Pro Opus at weekly 100"
            claude_usage("c1", 75, NOW + 3600, week=1)
            assert take(config, "pro1", OPUS) is None, "Pro Opus over 70 on the 5-hour"
            assert take(config, "pro1") == "pro1", "Sonnet on that Pro row"
    finally:
        clear_usage()


@scenario
def launch_rules_full_five_hour_blocks_unless_it_resets_soon():
    """With usage_reset_minutes set, a 5-hour window at usage_full_percent blocks unless
    it resets within that many minutes; a refusal names the percent and the reset, and a
    full window with no reset time is refused."""
    set_fixture()
    on, _ = rules_config(**RULES)
    try:
        claude_usage("c2", 95, NOW + 30 * 60)
        assert take(on, "pro2") == "pro2", "resets in 30 minutes"
        claude_usage("c2", 95, NOW + 60 * 60)
        why = []
        assert take(on, "pro2", why=why) is None, "resets in 60 minutes"
        assert len(why) == 1 and "95%" in why[0], why
        assert da.stamp(NOW + 3600) in why[0], why
        claude_usage("c2", 95, None)
        why = []
        assert take(on, "pro2", why=why) is None, "no reset time"
        assert len(why) == 1 and "95%" in why[0] and "no reset time" in why[0], why
        codex_usage("x1", 95, 1, NOW + 60 * 60)
        assert take(on, "x1", "m", "codex") is None, "Codex 5-hour at 95"
        codex_usage("x1", 95, 1, NOW + 30 * 60)
        assert take(on, "x1", "m", "codex") == "x1"
        claude_usage("c2", 89, NOW + 60 * 60)
        assert take(on, "pro2") == "pro2", "below usage_full_percent"
    finally:
        clear_usage()


@scenario
def launch_rules_unset_keys_stop_nothing():
    """With usage_reset_minutes unset, as accounts.toml ships, a full 5-hour window takes
    the worker: the operator resets usage when it is full."""
    set_fixture()
    off = da.load_config(accounts)
    assert off.get("usage_reset_minutes") is None, off.get("usage_reset_minutes")
    try:
        claude_usage("c2", 95, NOW + 60 * 60)
        assert take(off, "pro2") == "pro2"
        claude_usage("c2", 100, None)
        assert take(off, "pro2") == "pro2"
        codex_usage("x1", 100, 99, NOW + 3 * 3600)
        assert take(off, "x1", "m", "codex") == "x1"
    finally:
        clear_usage()


@scenario
def launch_rules_unread_usage_is_never_free():
    """A too-old reading, or a fresh one without the 5-hour window, stays unread: Pro
    takes no Opus, as with no reading at all. Unread usage blocks no other worker, and an
    agent kind whose usage is not read takes its worker as today."""
    set_fixture()
    on, _ = rules_config(**RULES)
    path = homes["c1"] / "rate-limits.json"
    try:
        for config in (cfg, on):
            claude_usage("c1", 10, NOW + 3600, week=10, saved=NOW - 3600)
            assert take(config, "pro1", OPUS) is None, "a reading an hour old"
            assert take(config, "pro1") == "pro1", "unread blocks no Sonnet"
            weekly = {"used_percentage": 10, "resets_at": NOW + 9000}
            path.write_text(
                json.dumps({"saved_at": int(NOW), "rate_limits": {"seven_day": weekly}})
            )
            assert take(config, "pro1", OPUS) is None, "no 5-hour window is unread"
            claude_usage("c1", 99, NOW - 1, week=10, week_reset=NOW + 9000)
            assert take(config, "pro1", OPUS) == "pro1", "a 5-hour window that reset"
            assert take(config, "cur", "grok-4.7-high", "cursor") == "cur"
            assert take(config, "gk", "grok-4.7", "grok") == "gk"
    finally:
        clear_usage()


@scenario
def five_hour_stop_reads_unread_as_no_stop():
    """dev-auto-fixes-50 (f), disproved: an hour-old reading at 5-hour 100 percent is
    unread, so five_hour_stop gives no reason and the row takes a Sonnet worker, as
    "Unread usage alone blocks nothing" says; "never free" is the Pro Opus rule, which
    still refuses Opus on that unread Pro row."""
    set_fixture()
    on, _ = rules_config(**RULES)
    try:
        claude_usage("c1", 100, NOW + 3600, week=10, saved=NOW - 3600)
        row = da.account_row(on, "c1")
        assert da.five_hour_stop(on, row, NOW) is None
        assert take(on, "pro1") == "pro1", "unread blocks no Sonnet"
        assert take(on, "pro1", OPUS) is None, "unread Pro takes no Opus"
    finally:
        clear_usage()


@scenario
def weekly_full_codex_row_is_skipped():
    """A Codex row whose weekly window is at 100 percent takes no coder or reviewer, with
    the rule keys set or unset, and the next free row is offered; 99 percent, a fresher
    reading under 100, or a weekly window that reset offers it again. A Claude row at
    weekly 100 is offered as before."""
    set_fixture()
    on, _ = rules_config(**RULES)
    coder = {"t": rec("t", "x1", agent="codex", model="m")}
    try:
        for config in (cfg, on):
            codex_usage("x1", 10, 100, NOW + 3600)
            row = da.slot(config, {}, "codex", "m", now=NOW)
            assert row and row["label"] == "x2", "weekly 100 goes to the next row"
            row = da.slot(config, coder, "codex", "m", ignore="t-review", now=NOW)
            assert row and row["label"] == "x2", "a reviewer follows the same rule"
            codex_usage("x1", 10, 99, NOW + 3600)
            assert take(config, "x1", "m", "codex") == "x1", "weekly 99 is offered"
            codex_usage("x1", 10, 40, NOW + 3600)
            assert take(config, "x1", "m", "codex") == "x1", "a fresher reading at 40"
            folder = homes["x1"] / "sessions" / "2033" / "05" / "18"
            limits = {
                "primary": {"used_percent": 10, "resets_at": NOW + 3600},
                "secondary": {"used_percent": 100, "resets_at": NOW - 1},
            }
            (folder / "rollout.jsonl").write_text(codex_line(limits))
            assert take(config, "x1", "m", "codex") == "x1", "a weekly window that reset"
            claude_usage("c2", 10, NOW + 3600, week=100, week_reset=NOW + 9000)
            assert take(config, "pro2") == "pro2", "Claude at weekly 100"
    finally:
        clear_usage()


@scenario
def weekly_reset_by_operator_overrides_the_default():
    """`weekly_reset_by_operator` defaults to true on Claude rows and false on the others;
    set on a row, it wins: a Codex row the operator resets is offered at weekly 100, and
    a Claude row they do not reset is skipped."""
    set_fixture()
    path = limit_accounts(
        f"""
[[account]]
agent = "claude"
label = "kept"
id = "w1"
home = "{homes["c1"]}"
plan = "team"
weekly_reset_by_operator = false

[[account]]
agent = "codex"
label = "reset"
id = "w2"
home = "{homes["x1"]}"
weekly_reset_by_operator = true
"""
    )
    rows = da.load_config(path)
    assert [r["weekly_reset_by_operator"] for r in cfg["account"][:2]] == [True, True]
    assert da.account(cfg, "x1", "local")["weekly_reset_by_operator"] is False
    assert da.account(cfg, "cur", "local")["weekly_reset_by_operator"] is False
    try:
        claude_usage("c1", 10, NOW + 3600, week=100, week_reset=NOW + 9000)
        codex_usage("x1", 10, 100, NOW + 3600)
        why = []
        assert da.slot(rows, {}, "claude", SONNET, now=NOW, why=why) is None
        assert why == [f"kept: weekly use 100% at 100, resets {da.stamp(NOW + 9000)}"], why
        row = da.slot(rows, {}, "codex", "m", now=NOW)
        assert row and row["label"] == "reset", "the operator resets this Codex row"
    finally:
        clear_usage()


@scenario
def weekly_limit_screen_blocks_until_its_reset():
    """On a row the operator does not reset, a stored weekly limit screen whose reset is
    ahead skips the row whatever the reading says, until that reset passes, even once its
    ticket finished; a Claude row's screen blocks nothing. Unread usage stays unread: no
    reading blocks nothing, and an old reading at 100 is no reading."""
    set_fixture()
    reset = NOW + 3 * 86400
    limited = {"t": rec("t", "x1", agent="codex", model="m", limit_reset=reset)}
    try:
        codex_usage("x1", 10, 10, NOW + 3600)
        why = []
        row = da.slot(cfg, limited, "codex", "m", now=NOW, why=why)
        assert row and row["label"] == "x2", "a limit screen ahead skips x1"
        assert why == [f"x1: weekly use 10%, limit screen until {da.stamp(reset)}"], why
        row = da.slot(cfg, limited, "codex", "m", now=reset + 1)
        assert row and row["label"] == "x1", "offered once the reset passed"
        done = {"t": rec("t", "x1", agent="codex", model="m", limit_reset=reset, done=True)}
        assert pick(done, agent="codex", model="m") == "x2", "a finished ticket's screen"
        clear_usage()
        why = []
        assert da.slot(cfg, limited, "codex", "m", now=NOW, why=why)["label"] == "x2"
        assert why == [f"x1: weekly use unread, limit screen until {da.stamp(reset)}"], why
        six = {"t": rec("t", "x1", agent="codex", model="m", limit_reset=NOW + 6 * 3600)}
        for later in (NOW, NOW + 3600, NOW + 3 * 3600):
            row = da.slot(cfg, six, "codex", "m", now=later)
            assert row and row["label"] == "x2", f"unread, {later - NOW:.0f}s on"
        assert pick({}, agent="codex", model="m") == "x1", "unread blocks nothing"
        codex_usage("x1", 10, 100, NOW + 3600, saved=NOW - 3600)
        assert pick({}, agent="codex", model="m") == "x1", "an old 100 is unread"
        claude = {"t": rec("t", "c2", model=SONNET, limit_reset=reset)}
        assert pick(claude, model=SONNET, exclude={"c1"}) == "pro2", "Claude's screen"
    finally:
        clear_usage()


def codex_windows(name, five, five_reset, week, week_reset):
    """A Codex reading whose 5-hour and weekly windows reset at different times."""
    folder = homes[name] / "sessions" / "2033" / "05" / "18"
    folder.mkdir(parents=True, exist_ok=True)
    limits = {
        "primary": {"used_percent": five, "resets_at": five_reset},
        "secondary": {"used_percent": week, "resets_at": week_reset},
    }
    (folder / "rollout.jsonl").write_text(codex_line(limits))


@scenario
def weekly_rule_blocks_every_stored_screen():
    """On a row the operator does not reset, a stored 5-hour screen blocks until its
    reset like a weekly one, with usage_reset_minutes 40 or unset, a fresh or an old
    reading. Without a screen the 5-hour rule on readings is unchanged: a full 5-hour
    window resetting in 30 minutes still takes the worker."""
    set_fixture()
    on, _ = rules_config(**RULES)
    week_reset = NOW + 3 * 86400
    try:
        codex_windows("x1", 100, NOW + 1800, 10, week_reset)
        assert da.slot(on, {}, "codex", "m", now=NOW)["label"] == "x1", "no screen"
        soon = {"t": rec("t", "x1", agent="codex", model="m", limit_reset=NOW + 1800)}
        why = []
        assert da.slot(on, soon, "codex", "m", now=NOW, why=why)["label"] == "x2"
        assert why == [f"x1: weekly use 10%, limit screen until {da.stamp(NOW + 1800)}"], why
        assert pick(soon, agent="codex", model="m") == "x2", "rules unset"
        codex_windows("x1", 100, NOW + 1800, 10, NOW + 1800)
        assert da.slot(on, soon, "codex", "m", now=NOW)["label"] == "x2", "same resets"
        assert da.slot(on, soon, "codex", "m", now=NOW + 600)["label"] == "x2", "old"
        assert da.slot(on, soon, "codex", "m", now=NOW + 1801)["label"] == "x1"
    finally:
        clear_usage()


@scenario
def weekly_rule_lets_a_recovered_screen_go():
    """A stored screen whose wait a full reading backed stops blocking once a fresh
    reading is below the limit, the rule that frees its worker, so the parked worker
    can take its own account again; a screen no full reading backed still blocks."""
    set_fixture()
    on, _ = rules_config(**RULES)
    try:
        codex_windows("x1", 10, NOW + 1800, 10, NOW + 3 * 86400)
        parked = rec("t", "x1", agent="codex", model="m", limit_reset=NOW + 86400, limit_full=True)
        assert da.freed(on, da.account(on, "x1", "local"), parked, NOW)
        why = []
        row = da.slot(on, {"t": parked}, "codex", "m", now=NOW, ignore="t", why=why)
        assert row["label"] == "x1", (row, why)
        parked.pop("limit_full")
        row = da.slot(on, {"t": parked}, "codex", "m", now=NOW, ignore="t")
        assert row["label"] == "x2", "an unbacked screen still blocks"
    finally:
        clear_usage()


@scenario
def weekly_stop_takes_no_grant():
    """usage_grant lifts the 5-hour rule, never the weekly one: a granted Codex row at
    weekly 100, or with a weekly screen ahead, takes no worker."""
    set_fixture()
    path = limit_accounts(
        f"""
[[account]]
agent = "codex"
label = "granted"
id = "g1"
home = "{homes["x1"]}"
max_workers = 2
usage_grant = true
""",
        head="usage_reset_minutes = 40\n",
    )
    granted = da.load_config(path)
    try:
        codex_usage("x1", 10, 100, NOW + 3600)
        why = []
        assert da.slot(granted, {}, "codex", "m", now=NOW, why=why) is None
        assert why == [f"granted: weekly use 100% at 100, resets {da.stamp(NOW + 3600)}"], why
        codex_usage("x1", 10, 10, NOW + 3600)
        screen = {"t": rec("t", "g1", agent="codex", model="m", limit_reset=NOW + 86400)}
        assert da.slot(granted, screen, "codex", "m", now=NOW) is None, "a weekly screen"
        codex_usage("x1", 95, 10, NOW + 3600)
        why = []
        assert da.slot(granted, {}, "codex", "m", now=NOW, why=why)["label"] == "granted"
        assert len(why) == 1 and "usage_grant past 5-hour use 95%" in why[0], why
    finally:
        clear_usage()


@scenario
def weekly_skipped_rows_are_named():
    """With every free row skipped, `slot` prints `no capacity` and names each skipped
    row with its weekly percent and reset time."""
    set_fixture()
    reset = NOW + 2 * 86400
    try:
        codex_usage("x1", 10, 100, NOW + 3600)
        codex_usage("x2", 10, 40, NOW + 3600)
        workers = {"t": rec("t", "x2", agent="codex", model="m", limit_reset=reset)}
        state_file.write_text(json.dumps({"workers": workers}))
        done = cli("slot", "--agent", "codex", "--model", "m")
        assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
        assert done.stderr.splitlines() == [
            f"x1: weekly use 100% at 100, resets {da.stamp(NOW + 3600)}",
            f"x2: weekly use 40%, limit screen until {da.stamp(reset)}",
        ], done.stderr
    finally:
        state_file.unlink(missing_ok=True)
        clear_usage()


@scenario
def launch_commands():
    """Launch command construction, run through sh against the fake agent CLIs."""
    set_fixture()
    ticket = {"task_id": "t-1", "title": "served pages / Settings wiring"}
    assert da.lane_slug(ticket) == "settings-wiring"
    assert (
        da.agent_id(cfg, "codex", "Juzzark-WSL", da.id_slug(ticket))
        == "codex:wsl-served-pages-settings-wiring"
    )
    assert (
        da.agent_id(cfg, "codex", "local", "settings-wiring") == "codex:settings-wiring"
    )
    assert (
        da.agent_id(cfg, "codex", "Juzzark-WSL", "settings-wiring")
        == "codex:wsl-settings-wiring"
    )
    concord_env = {
        "CONCORD_REPO_ROOT": str(root),
        "CONCORD_AGENT_ID": "claude:settings-wiring",
        "CONCORD_AGENT_KIND": "claude",
        "CONCORD_TELEMETRY_DISABLED": "1",
        "CONCORD_NO_UPDATE_CHECK": "1",
    }
    worker = rec("t-1", "c1", agent_id="claude:settings-wiring")
    row = da.account(cfg, "c1", "local")
    entry = run_command(
        da.worker_command(cfg, root, worker, row, da.brief(worker, ticket))
    )
    a = entry["args"]
    assert entry["env"]["CLAUDE_CONFIG_DIR"] == str(homes["c1"])
    assert entry["env"]["CONCORD_AGENT_ID"] == "claude:settings-wiring", entry["env"]
    server = json.loads(a[a.index("--mcp-config") + 1])["mcpServers"]["concord"]
    assert server == {
        "command": str(commands / "concord-mcp"),
        "args": [],
        "env": concord_env,
    }, server
    assert "--strict-mcp-config" not in a
    assert a[a.index("--model") + 1] == OPUS and a[a.index("--effort") + 1] == "high"
    assert (
        a[a.index("--session-id") + 1] == "sess-t-1"
        and "--dangerously-skip-permissions" in a
    )
    assert a[-1].startswith(
        "/goal Finish Concord ticket t-1 (served pages / Settings wiring)"
    )
    assert (
        "start_work" in a[-1]
        and "Skills:" in a[-1]
        and "Waiting on: ticket:<id>" in a[-1]
    )
    entry = run_command(
        da.worker_command(cfg, root, worker, row, "go on", resume="sess-t-1")
    )
    a = entry["args"]
    assert entry["env"]["CONCORD_AGENT_ID"] == "claude:settings-wiring", "resume too"
    assert (
        a[a.index("--resume") + 1] == "sess-t-1"
        and "--session-id" not in a
        and a[-1] == "go on"
    )

    worker = rec(
        "t-1",
        "x1",
        agent="codex",
        model="gpt-6.1-sol",
        effort="xhigh",
        agent_id="codex:settings-wiring",
    )
    entry = run_command(
        da.worker_command(
            cfg, root, worker, da.account(cfg, "x1", "local"), da.brief(worker, ticket)
        )
    )
    a = entry["args"]
    assert entry["env"]["CODEX_HOME"] == str(homes["x1"])
    assert entry["env"]["CONCORD_AGENT_ID"] == "codex:settings-wiring"
    assert a[:3] == ["-m", "gpt-6.1-sol", "-c"] and "model_reasoning_effort=xhigh" in a
    assert (
        "--dangerously-bypass-approvals-and-sandbox" in a
        and "--dangerously-bypass-hook-trust" in a
    )
    overrides = dict(a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-c")
    parsed = {
        k: tomllib.loads(f"v = {v}")["v"]
        for k, v in overrides.items()
        if k != "model_reasoning_effort"
    }
    assert parsed["service_tier"] == "default"
    assert parsed["mcp_servers.concord.command"] == str(commands / "concord-mcp")
    assert parsed["mcp_servers.concord.args"] == []
    assert parsed["mcp_servers.concord.env"] == dict(
        concord_env,
        CONCORD_AGENT_ID="codex:settings-wiring",
        CONCORD_AGENT_KIND="codex",
    )
    # Only this launch trusts the worktree, so no trust prompt and no config write.
    assert parsed["projects"] == {worker["worktree"]: {"trust_level": "trusted"}}
    assert (
        a[-1].startswith("/goal Finish Concord ticket t-1")
        and "fast" not in " ".join(a[:-1]).lower()
    )

    worker = rec(
        "t-1",
        "cur",
        agent="cursor",
        model="grok-4.7-high",
        agent_id="cursor:settings-wiring",
    )
    entry = run_command(
        da.worker_command(
            cfg, root, worker, da.account(cfg, "cur", "local"), da.brief(worker, ticket)
        )
    )
    a = entry["args"]
    assert entry["env"]["CONCORD_AGENT_ID"] == "cursor:settings-wiring"
    assert entry["plugin"]["mcpServers"]["concord"]["env"] == dict(
        concord_env,
        CONCORD_AGENT_ID="cursor:settings-wiring",
        CONCORD_AGENT_KIND="cursor",
    )
    assert a[:2] == ["--model", "grok-4.7-high"] and {
        "--force",
        "--trust",
        "--approve-mcps",
    } <= set(a)
    assert a[-1].startswith("Finish Concord ticket t-1") and not a[-1].startswith(
        "/goal"
    )
    assert (
        "Create this as your goal with your goal tool, update it as you work, and stop only when it is met."
        in a[-1]
    )

    worker = rec(
        "t-1",
        "xw",
        agent="codex",
        model="gpt-6.1-sol",
        host="Juzzark-WSL",
        agent_id="codex:wsl-settings-wiring",
    )
    command = da.worker_command(
        cfg, root, worker, da.account(cfg, "xw", "Juzzark-WSL"), "go"
    )
    a = run_command(command)["args"]
    assert not command.startswith("CODEX_HOME="), (
        "remote rows use that host's default home"
    )
    overrides = dict(a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-c")
    assert (
        tomllib.loads(f"v = {overrides['mcp_servers.concord.command']}")["v"] == "ssh"
    )
    assert tomllib.loads(f"v = {overrides['mcp_servers.concord.args']}")["v"] == [
        "-i",
        "~/.ssh/k",
        "me@mac",
        "concord-mcp codex:wsl-settings-wiring",
    ]
    assert "mcp_servers.concord.env" not in overrides, (
        "the gate sets the env on the ledger side"
    )
    worker = rec("t-1", "c3", host="Juzzark-WSL", agent_id="claude:wsl-settings-wiring")
    a = run_command(
        da.worker_command(cfg, root, worker, da.account(cfg, "c3", "Juzzark-WSL"), "go")
    )["args"]
    server = json.loads(a[a.index("--mcp-config") + 1])["mcpServers"]["concord"]
    assert (
        server["command"] == "ssh"
        and server["args"][-1] == "concord-mcp claude:wsl-settings-wiring"
    )


IDENTITY = ("CONCORD_REPO_ROOT", "CONCORD_AGENT_ID", "CONCORD_AGENT_KIND")


def gate_identity(command, prefix):
    """What concord-ssh-gate.sh, installed in the ledger, starts concord-mcp with for
    this ssh command; None when the gate rejects it."""
    gate = root / "concord-ssh-gate.sh"
    shutil.copyfile(HERE / "concord-ssh-gate.sh", gate)
    home = tmp / "launch-gate-home"
    server = home / ".local" / "bin" / "concord-mcp"
    server.parent.mkdir(parents=True, exist_ok=True)
    server.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        f"print(json.dumps({{k: os.environ.get(k) for k in {IDENTITY!r}}}))\n"
    )
    server.chmod(0o755)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command}
    done = subprocess.run(
        ["sh", str(gate), prefix], check=False, capture_output=True, text=True, env=env
    )
    return json.loads(done.stdout) if done.returncode == 0 else None


def concord_identity(entry, host):
    """The ledger root, agent id and agent kind the concord-mcp of this logged agent
    launch starts with."""
    a = entry["args"]
    if entry["cmd"] == "claude":
        server = json.loads(a[a.index("--mcp-config") + 1])["mcpServers"]["concord"]
    elif entry["cmd"] == "cursor-agent":
        server = entry["plugin"]["mcpServers"]["concord"]
    elif entry["cmd"] == "codex":
        overrides = dict(a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-c")
        server = {
            k: tomllib.loads(f"v = {overrides[f'mcp_servers.concord.{k}']}")["v"]
            for k in ("command", "args", "env")
            if f"mcp_servers.concord.{k}" in overrides
        }
    else:
        # Grok's user-scope server has no env of its own. In leader mode the shared
        # leader process spawns it with the leader's env, never this launch line's.
        env = entry["env"] if "--no-leader" in a else {}
        server = {"command": "concord-mcp", "args": [], "env": env}
    if Path(server["command"]).name == "ssh":
        return gate_identity(server["args"][-1], cfg["hosts"][host]["prefix"])
    return {k: server.get("env", {}).get(k) for k in IDENTITY}


@scenario
def every_agent_gives_concord_its_ledger():
    """Each agent kind's launch, here and on a host, starts concord-mcp with the ledger
    root, the agent id and the agent kind."""
    set_fixture()
    launches = [
        ("claude", "local", "c1", "claude:settings-wiring"),
        ("codex", "local", "x1", "codex:settings-wiring"),
        ("cursor", "local", "cur", "cursor:settings-wiring"),
        ("grok", "local", "gk", "grok:settings-wiring"),
        ("claude", "Juzzark-WSL", "c3", "claude:wsl-settings-wiring"),
        ("codex", "Juzzark-WSL", "xw", "codex:wsl-settings-wiring"),
        ("cursor", "Juzzark-WSL", None, "cursor:wsl-settings-wiring"),
    ]
    for agent, host, account, agent_id in launches:
        worker = rec("t-1", account, agent=agent, host=host, agent_id=agent_id)
        row = da.account(cfg, account, host) if account else {"home": None}
        entry = run_command(da.worker_command(cfg, root, worker, row, "go"))
        assert concord_identity(entry, host) == {
            "CONCORD_REPO_ROOT": str(root),
            "CONCORD_AGENT_ID": agent_id,
            "CONCORD_AGENT_KIND": agent,
        }, (agent, host, entry)


@scenario
def sdk_launch_runs_the_runner_without_the_key():
    """An sdk worker starts sdk_worker.py through uv with the claude-agent-sdk package,
    its Concord server and identity, its caps and the spend file. Only the key file's
    path is on the command line, never the key; it runs on the ledger's host only."""
    set_fixture()
    sdk = sdk_accounts()
    row = da.account(sdk, "sdk1", "local")
    worker = rec("t-1", "sdk1", agent="sdk", agent_id="sdk:settings-wiring")
    command = da.worker_command(sdk, root, worker, row, "go on")
    assert "SECRET" not in command and "ANTHROPIC_API_KEY" not in command, command
    entry = run_command(command)
    assert entry["cmd"] == "uv" and entry["env"]["CONCORD_AGENT_ID"] == "sdk:settings-wiring"
    a = entry["args"]
    runner = ["run", "--quiet", "--no-project", "--with", "claude-agent-sdk"]
    assert a[: len(runner) + 1] == [*runner, str(HERE / "sdk_worker.py")], a
    flags = dict(zip(a[len(runner) + 1 : -1 : 2], a[len(runner) + 2 : -1 : 2]))
    server = json.loads(flags.pop("--mcp"))
    assert server == {
        "command": str(commands / "concord-mcp"),
        "args": [],
        "env": {
            "CONCORD_REPO_ROOT": str(root),
            "CONCORD_AGENT_ID": "sdk:settings-wiring",
            "CONCORD_AGENT_KIND": "sdk",
            "CONCORD_TELEMETRY_DISABLED": "1",
            "CONCORD_NO_UPDATE_CHECK": "1",
        },
    }, server
    assert flags == {
        "--ticket": "t-1",
        "--account": "sdk1",
        "--model": OPUS,
        "--effort": "high",
        "--key-file": str(KEY),
        "--ticket-usd": "20",
        "--monthly-usd": "100",
        "--spend": str(SPEND),
    }, flags
    assert a[-1] == "go on"
    done = cli(
        *("launch", "t-1", "--agent", "sdk", "--model", OPUS, "--account", "api"),
        *("--worktree", str(tmp), "--host", "Juzzark-WSL"),
        DEV_AUTO_ACCOUNTS=str(tmp / "limits.toml"),
    )
    assert done.returncode and "the ledger's host" in done.stderr, done


@scenario
def sdk_launch_opens_its_terminal():
    """A launch of agent sdk on its account opens one terminal running the runner, with
    no Claude seed and no CLI update: the SDK carries its own CLI."""
    sdk_accounts()
    reset_db()
    ticket_row("sk", status="proposed", title="trial / sk", notes=(
        "Coder: claude (claude-sonnet-5-5, effort high) · Tier: hard"
    ))
    state_file.unlink(missing_ok=True)
    wt = tmp / "wt" / "sk"
    wt.mkdir(parents=True, exist_ok=True)
    world([], worktree_show=just_created(), git={"rev-parse --abbrev-ref HEAD": [0, "task/trial/sk\n"]})
    done = cli(
        *("launch", "sk", "--agent", "sdk", "--model", SONNET, "--effort", "high"),
        *("--account", "api", "--worktree", str(wt)),
        DEV_AUTO_ACCOUNTS=str(tmp / "limits.toml"),
    )
    try:
        assert done.returncode == 0, done
        assert done.stdout.strip().endswith("sk: sdk:ticket-sk on api in term-local-1"), done.stdout
        assert "update" not in done.stdout, done.stdout
        (made,) = created()
        command = opt(made, "--command")
        assert "sdk_worker.py" in command and "SECRET" not in command, command
        worker = json.loads(state_file.read_text())["workers"]["sk"]
        assert (worker["agent"], worker["account"], worker.get("session")) == ("sdk", "sdk1", None)
    finally:
        state_file.unlink(missing_ok=True)


runner_spec = importlib.util.spec_from_file_location("sdk_worker", HERE / "sdk_worker.py")
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)
SERVER = {"command": "concord-mcp", "args": [], "env": {"CONCORD_AGENT_ID": "sdk:t-1"}}


def fake_sdk(transcripts=True, interrupt_fails=False):
    """A claude_agent_sdk stand-in, the log of what the runner did with it, and the
    connections to play: each client plays the next list of turns, one list of messages
    per query. With transcripts, each result's model_usage is also written as the
    session's transcript, one request per model, as the CLI would keep it."""
    log, connections = [], []

    class Message:
        def __init__(self, **fields):
            self.__dict__.update(fields)

    class SystemMessage(Message):
        pass

    class AssistantMessage(Message):
        pass

    class TextBlock(Message):
        pass

    class ToolUseBlock(Message):
        pass

    class ResultMessage(Message):
        pass

    class ClaudeAgentOptions:
        def __init__(self, **given):
            self.given = given

    class ClaudeSDKClient:
        def __init__(self, options):
            assert connections, f"no connection left for a client with {options.given}"
            # A connection given as an exception fails to connect.
            self.turns = connections.pop(0)
            if not isinstance(self.turns, BaseException):
                self.turns = iter(self.turns)
            log.append(("client", options.given))

        async def connect(self):
            log.append(("connect",))
            await asyncio.sleep(0)
            if isinstance(self.turns, BaseException):
                raise self.turns

        async def query(self, text):
            log.append(("query", text))
            self.playing = next(self.turns, None)
            assert self.playing is not None, f"no turn left for {text!r}: {log}"

        async def receive_response(self):
            for message in self.playing:
                await asyncio.sleep(0)
                if callable(message):
                    # A step that changes the world mid-turn, such as a transcript write.
                    message()
                    continue
                if isinstance(message, float):
                    await asyncio.sleep(message)
                    continue
                if isinstance(message, BaseException):
                    raise message
                if transcripts and isinstance(message, ResultMessage) and message.model_usage:
                    transcript(message.session_id, [
                        (f"auto-{model}", model, turn_usage(
                            used["inputTokens"], used["cacheCreationInputTokens"],
                            used["cacheReadInputTokens"], used["outputTokens"]))
                        for model, used in message.model_usage.items()
                    ])
                yield message

        async def interrupt(self):
            log.append(("interrupt",))
            if interrupt_fails:
                # The SDK's interrupt is a control request that can fail or time out.
                raise RuntimeError("control request timed out")

        async def disconnect(self):
            log.append(("disconnect",))

    sdk = Message(**{c.__name__: c for c in (
        SystemMessage, AssistantMessage, TextBlock, ToolUseBlock, ResultMessage,
        ClaudeAgentOptions, ClaudeSDKClient,
    )}, project_key_for_directory=runner_key)
    return sdk, log, connections


def sdk_init(sdk, source="ANTHROPIC_API_KEY", session="s-1", mode="default"):
    data = {
        "session_id": session,
        "apiKeySource": source,
        "permissionMode": mode,
        "mcp_servers": [{"name": "concord", "status": "connected"}],
        "skills": ["duo", "tdd", "done-check"],
    }
    return sdk.SystemMessage(subtype="init", data=data)


def sdk_said(sdk, *blocks, model=OPUS, message_id=None, usage=None, error=None):
    content = [
        sdk.ToolUseBlock(name=b[1:]) if b.startswith("@") else sdk.TextBlock(text=b)
        for b in blocks
    ]
    return sdk.AssistantMessage(
        content=content, model=model, message_id=message_id, usage=usage, error=error
    )


def sdk_result(sdk, total=None, subtype="success", session="s-1", models=None, usage=None,
               estimate=None, **fields):
    """A result whose session tokens cost `total` USD at Opus 5.5's output price (20 USD
    per million), or carry `models` as given. total_cost_usd is the CLI's estimate, which
    the caps never use while model_usage is there."""
    if models is None and total is not None:
        models = {OPUS: {"inputTokens": 0, "outputTokens": round(total * 50_000),
                         "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.0}}
    given = {"is_error": subtype != "success", "api_error_status": None, "terminal_reason": "completed",
             "num_turns": 1, "duration_api_ms": 900}
    return sdk.ResultMessage(
        subtype=subtype, total_cost_usd=estimate, session_id=session, model_usage=models,
        usage=usage or {}, **(given | fields),
    )


def tokens(fresh=0, wrote=0, read=0, out=0, estimate=0.0):
    return {"inputTokens": fresh, "cacheCreationInputTokens": wrote,
            "cacheReadInputTokens": read, "outputTokens": out, "costUSD": estimate}


def turn_usage(fresh=0, wrote=0, read=0, out=0):
    return {"input_tokens": fresh, "cache_creation_input_tokens": wrote,
            "cache_read_input_tokens": read, "output_tokens": out}


def runner_args(ticket="t-1", ticket_usd=20, monthly_usd=100, text="go"):
    argv = ["--ticket", ticket, "--model", OPUS, "--effort", "high", "--key-file", str(KEY)]
    argv += ["--ticket-usd", str(ticket_usd), "--monthly-usd", str(monthly_usd)]
    argv += ["--spend", str(SPEND), "--mcp", json.dumps(SERVER), text]
    return runner.parse(argv)


SDK_HOME = tmp / "sdk-home"


def runner_key(directory):
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(directory))


def run_runner(sdk, lines="", keep_home=False, **given):
    out = io.StringIO()
    if not keep_home:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(SDK_HOME)
    try:
        code = asyncio.run(runner.run(runner_args(**given), sdk, io.StringIO(lines), out))
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]
    return code, out.getvalue().splitlines()


def queries(log):
    return [entry[1] for entry in log if entry[0] == "query"]


def clients(log):
    return [entry[1] for entry in log if entry[0] == "client"]


THIS_MONTH = time.strftime("%Y-%m", time.gmtime())


@scenario
def sdk_runner_runs_one_session_and_takes_wakes():
    """The runner's one SDK session loads user and project settings, Claude Code's prompt,
    the Concord server alone and the key from its file, with the worker's tools allowed by
    name and the launched model and effort. Its first message is the launch text; each typed line after the
    idle line is the next message, a line kill drops what came before it and an empty
    line sends nothing. It streams text and tool names, and adds each result's new spend
    to the month's spend file once."""
    SPEND.write_text(json.dumps({THIS_MONTH: {"usd": 2.0, "tickets": {"other": 2.0}}}))
    sdk, log, plays = fake_sdk()
    claim = sdk_said(sdk, "Claiming t-1", "@mcp__concord__start_work")
    # Turn 1 writes the cache; turn 2 reads it and runs a Haiku subagent. The CLI's
    # estimates (99 USD, costUSD 0) are wrong on purpose: the caps use the tokens.
    first = {OPUS: tokens(fresh=2000, wrote=50_000, out=1000)}
    second = {
        OPUS: tokens(fresh=2500, wrote=50_800, read=52_000, out=1500),
        "claude-haiku-5-5": tokens(fresh=10_000, out=1000),
    }
    plays.append([
        [sdk_init(sdk), claim, sdk_result(sdk, models=first, estimate=99.0,
                                          usage=turn_usage(2000, 50_000, 0, 1000))],
        [sdk_init(sdk), sdk_said(sdk, "Woken\nreading the verdict"),
         sdk_result(sdk, models=second, estimate=99.0, usage=turn_usage(500, 800, 52_000, 500))],
    ])
    try:
        code, out = run_runner(sdk, "stale\x15/goal finish t-1 dev-auto: wake\n\x15\n")
        assert code == 0, out
        assert queries(log) == ["go", "/goal finish t-1 dev-auto: wake"], log
        (given,) = clients(log)
        assert given == {
            "cwd": os.getcwd(),
            "model": OPUS,
            "effort": "high",
            # The scrub holds the CLI in default mode: the tools a worker uses run by name.
            "allowed_tools": [
                "Agent", "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Skill",
                "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "ToolSearch", "WebFetch",
                "WebSearch", "Write", "mcp__concord__*",
            ],
            "setting_sources": ["user", "project"],
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "mcp_servers": {"concord": {"type": "stdio", **SERVER}},
            "strict_mcp_config": True,
            "env": {
                "ANTHROPIC_API_KEY": "sk-ant-api03-SECRET-test-key",
                "ANTHROPIC_AUTH_TOKEN": "",
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "CLAUDE_CODE_USE_BEDROCK": "",
                "CLAUDE_CODE_USE_VERTEX": "",
                "CLAUDE_CODE_USE_FOUNDRY": "",
                # A one-hour cache for the main loop and subagents, never turned off.
                "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h",
                "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h",
                "ENABLE_PROMPT_CACHING_1H": "1",
                "FORCE_PROMPT_CACHING_5M": "",
                "DISABLE_PROMPT_CACHING": "",
                "DISABLE_PROMPT_CACHING_HAIKU": "",
                "DISABLE_PROMPT_CACHING_SONNET": "",
                "DISABLE_PROMPT_CACHING_OPUS": "",
                "DISABLE_PROMPT_CACHING_FABLE": "",
                # Fast mode bills twice the rates; the runner keeps it off.
                "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
                # No background work or MCP task after a result, no key in tool
                # subprocesses (O3, O4).
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                "CLAUDE_AUTO_BACKGROUND_TASKS": "",
                "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND": "1",
                "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
                "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
            },
            "disallowed_tools": ["Monitor"],
            "max_budget_usd": 16.0,
        }, given
        loaded = "· sdk worker: billed to ANTHROPIC_API_KEY, concord connected, 3 skills"
        assert out[:5] == [runner.BUSY, loaded, "⏺ Claiming t-1", "⏺ mcp__concord__start_work", runner.BUSY], out
        assert out.count(loaded) == 1, "the first turn's init only"
        assert out.count(runner.IDLE) == 2 and "  reading the verdict" in out, out
        assert not any("SECRET" in line for line in out), out
        # Opus 5.5 per million: 4 input, 8 for a 1-hour cache write, 0.20 a cache read, 20
        # output; Haiku 5.5 up to 100k tokens a prompt, 0.10 input and 0.50 output. Turn 1:
        # 2000*4 + 50000*8 + 1000*20 = 0.428 USD. Turn 2 adds 500*4 + 800*8 + 52000*0.20
        # + 500*20 for Opus and 10000*0.10 + 1000*0.50 for Haiku = 0.0303 USD.
        assert "· sdk worker: cache 0% hit, 0 read, 50000 written, 2000 uncached, 1000 out" in out, out
        assert "· sdk worker: cache 98% hit, 52000 read, 800 written, 500 uncached, 500 out" in out, out
        assert "· sdk worker: success, 0.43 USD this turn, 0.43 of 20 USD on t-1" in out, out
        assert "· sdk worker: success, 0.03 USD this turn, 0.46 of 20 USD on t-1" in out, out
        spend = json.loads(SPEND.read_text())
        assert spend == {THIS_MONTH: {"usd": 2.4583, "tickets": {"other": 2.0, "t-1": 0.4583}}}, spend
        # Review of bbf8cee: searches cost 10 USD per 1000 on top of their tokens, from the
        # usage count, whatever the CLI estimates: 250000*4 + 100 searches = 2.00 USD.
        searched = turn_usage(250_000) | {"server_tool_use": {"web_search_requests": 100}}
        cost, why = runner.request_cost(OPUS, searched)
        assert (round(cost, 6), why) == (2.0, None), (cost, why)
    finally:
        SPEND.unlink(missing_ok=True)


def transcript(session, entries, agent=None):
    """Write Claude Code's transcript for a session, or one of its subagents', as the CLI
    keeps it under CLAUDE_CONFIG_DIR: one line per streamed assistant message."""
    folder = SDK_HOME / "projects" / runner_key(os.getcwd())
    path = folder / f"{session}.jsonl"
    if agent:
        path = folder / session / "subagents" / f"agent-{agent}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "assistant", "sessionId": session,
                    "message": {"id": mid, "model": model, "usage": usage}})
        for mid, model, usage in entries
    ]
    path.write_text("".join(f"{line}\n" for line in lines))


@scenario
def sdk_runner_prices_each_request_from_its_transcript():
    """Reviews of bbf8cee and 506966d: Haiku 5.5 must be priced request by request on its
    own prompt (input, cache writes and reads), and a foreground subagent's text never
    reaches the stream. The runner reads the session's own and its subagents'
    transcripts, where each request's final usage sits under its message id, and prices
    each one on its own. Review of 58b65d2: tokens model_usage counts that no transcript
    holds have no known prompt or region, so they are priced at their upper bound,
    Haiku's long-prompt rates times the 1.1 US multiplier, and the cost row says so."""
    shutil.rmtree(SDK_HOME, ignore_errors=True)
    SPEND.unlink(missing_ok=True)
    sdk, _log, plays = fake_sdk(transcripts=False)
    haiku = "claude-haiku-5-5"
    # The parent's message is streamed twice; its first line carries the placeholder
    # output count, its last the final one.
    transcript("s-1", [
        ("m1", OPUS, turn_usage(1000, 0, 0, 1)),
        ("m1", OPUS, turn_usage(1000, 0, 0, 100)),
    ])
    transcript("s-1", [
        ("h1", haiku, turn_usage(1000, 0, 0, 1000)),
        ("h2", haiku, turn_usage(20_000, 0, 90_000, 1000)),
    ], agent="a1")
    searched = turn_usage(110_000, 0, 0, 1000) | {"server_tool_use": {"web_search_requests": 2}}
    transcript("s-1", [("h3", haiku, searched)], agent="a2")
    totals = {
        OPUS: tokens(fresh=1000, out=100),
        # 500 more input than the transcripts hold: a helper request outside them. Its
        # two searches are the ones h3 made, counted once.
        haiku: tokens(fresh=131_500, read=90_000, out=3000) | {"webSearchRequests": 2},
    }
    plays.append([[sdk_init(sdk), sdk_said(sdk, "@Agent"), sdk_result(sdk, models=totals)]])
    try:
        code, out = run_runner(sdk, keep_home=True)
        assert code == 0, out
        assert any(row.endswith(", 0.0003 USD at upper bounds") for row in out), out
        # Opus 1000*4 + 100*20 = 0.006. h1 is short: 1000*0.10 + 1000*0.50 = 0.0006. h2's
        # prompt is 110000: 20000*0.50 + 90000*0.05 + 1000*2.50 = 0.017. h3 is a long
        # text-only subagent reply: 110000*0.50 + 1000*2.50 + 2 searches = 0.0775. The
        # helper's 500 input at Haiku's long rate times 1.1: 0.000275. Total 0.101375 USD.
        spend = json.loads(SPEND.read_text())[THIS_MONTH]
        assert spend == {"usd": 0.101375, "tickets": {"t-1": 0.101375}}, spend
    finally:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_prices_regions_and_stops_on_unpriced_usage():
    """Review of 58b65d2: a missing transcript, an unlisted model or an unlisted request
    mode left usage that no tariff could price, and the runner billed it at a guess.
    Requests with inference_geo "us" pay 1.1 times, every listed Claude 5 model has its
    own row, and anything else prints one row of what could not be priced, then the
    fixed stop row. The ticket's reservation stays held, so no later runner or slot
    reuses that headroom, until settle records what the Console billed."""
    shutil.rmtree(SDK_HOME, ignore_errors=True)
    SPEND.unlink(missing_ok=True)
    us = turn_usage(100_000, 0, 0, 1000) | {"inference_geo": "us", "speed": "standard",
                                            "service_tier": "standard"}
    fable = "claude-fable-5-1"
    transcript("s-1", [("o1", OPUS, us), ("f1", fable, turn_usage(100_000, 0, 0, 1000))])
    sdk, _log, plays = fake_sdk(transcripts=False)
    totals = {OPUS: tokens(fresh=100_000, out=1000), fable: tokens(fresh=100_000, out=1000)}
    plays.append([[sdk_init(sdk), sdk_result(sdk, models=totals)]])
    try:
        code, out = run_runner(sdk, keep_home=True)
        assert code == 0, out
        # Opus in the US: (100000*4 + 1000*20) * 1.1 = 0.462. Fable 5.1: 100000*10 +
        # 1000*50 = 1.05.
        assert json.loads(SPEND.read_text())[THIS_MONTH]["usd"] == 1.512, SPEND.read_text()
        haiku = "claude-haiku-5-5"
        cases = [
            ("no transcript", None, {haiku: tokens(fresh=110_000, out=1000)}),
            ("model claude-opus-4-7", ("x1", "claude-opus-4-7", turn_usage(1000, 0, 0, 10)),
             {"claude-opus-4-7": tokens(fresh=1000, out=10)}),
            ("speed fast", ("x2", OPUS, turn_usage(1000, 0, 0, 10) | {"speed": "fast"}),
             {OPUS: tokens(fresh=1000, out=10)}),
            ("inference_geo eu", ("x3", OPUS, turn_usage(1000, 0, 0, 10) | {"inference_geo": "eu"}),
             {OPUS: tokens(fresh=1000, out=10)}),
            ("service_tier priority", ("x4", OPUS, turn_usage(1000, 0, 0, 10) | {"service_tier": "priority"}),
             {OPUS: tokens(fresh=1000, out=10)}),
        ]
        for reason, entry, models in cases:
            shutil.rmtree(SDK_HOME, ignore_errors=True)
            SPEND.unlink(missing_ok=True)
            if entry:
                transcript("s-2", [entry])
            sdk, log, plays = fake_sdk(transcripts=False)
            plays.append([[sdk_init(sdk, session="s-2"), sdk_result(sdk, session="s-2", models=models)]])
            code, out = run_runner(sdk, "a wake\n", keep_home=True)
            assert code == 1 and out[-2:] == [f"· sdk worker: unpriced usage: {reason}", runner.UNPRICED], out
            assert queries(log) == ["go"], log
            held = json.loads(SPEND.read_text())["reserved"]["t-1"]
            assert (held["open"], held["pid"], held["usd"]) == (True, None, 20.0), held
        # A held ticket starts no turn, and its hold counts against the month.
        sdk, log, _plays = fake_sdk()
        code, out = run_runner(sdk)
        assert (code, out) == (1, ["· sdk worker: unpriced usage: spend held from an earlier run", runner.UNPRICED]), out
        assert log == []
        assert da.month_spent(time.time()) >= 19.0, "the held allowance counts"
        # settle records what the Console billed and drops the hold.
        done = subprocess.run(
            [sys.executable, str(HERE / "sdk_worker.py"), "settle", "--ticket", "t-1",
             "--spend", str(SPEND), "--usd", "0.25"],
            capture_output=True, text=True, check=False,
        )
        assert (done.returncode, done.stdout) == (0, "· sdk worker: settled t-1 at 0.25 USD\n"), done
        spend = json.loads(SPEND.read_text())
        assert "reserved" not in spend and spend[THIS_MONTH]["tickets"]["t-1"] == 0.25, spend
        assert all(len(row) <= 40 for row in (runner.UNPRICED,))
    finally:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_never_trusts_zeroed_usage_or_a_shrinking_bound():
    """Review of 526edad. A crashed session's last result may carry zeroed usage and
    model_usage, and a result may come without model_usage: the runner then records what
    the transcripts price, holds the rest of the allowance and stops, and never reads
    the CLI's estimate. And an upper bound that shrinks once a transcript catches up is
    no new session: spend only grows past the highest total recorded."""
    shutil.rmtree(SDK_HOME, ignore_errors=True)
    SPEND.unlink(missing_ok=True)
    try:
        # A first-turn crash after a 100000-token Opus request, every cost field zeroed.
        transcript("s-1", [("o1", OPUS, turn_usage(100_000, 0, 0, 0))])
        sdk, _log, plays = fake_sdk(transcripts=False)
        crash = sdk_result(sdk, subtype="error_during_execution", models={OPUS: tokens()},
                           estimate=0.0)
        plays.append([[sdk_init(sdk), sdk_said(sdk, "@Bash"), crash]])
        code, out = run_runner(sdk, "a wake\n", keep_home=True)
        assert code == 1 and out[-2:] == ["· sdk worker: unpriced usage: model_usage missing or zeroed", runner.UNPRICED], out
        spend = json.loads(SPEND.read_text())
        held = spend["reserved"]["t-1"]
        assert spend[THIS_MONTH]["usd"] == 0.4 and (held["open"], held["pid"]) == (True, None), spend
        # Usage on the result but no model_usage and no transcript: two unknowns, held.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, _log, plays = fake_sdk(transcripts=False)
        bare = sdk_result(sdk, models=None, estimate=0.0, usage=turn_usage(100_000, 0, 0, 1000))
        plays.append([[sdk_init(sdk), bare]])
        code, out = run_runner(sdk, "a wake\n", keep_home=True)
        assert out[-2:] == ["· sdk worker: unpriced usage: no transcript, model_usage missing or zeroed", runner.UNPRICED], out
        assert code == 1 and json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True
        # Result 1 bounds 250000 Opus input it cannot find: 250000*4*1.1 = 1.10. By result
        # 2 the transcript holds that request and one more output token: 1.00002 in all.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        transcript("s-1", [("z0", OPUS, turn_usage())])
        caught_up = lambda: transcript("s-1", [("o1", OPUS, turn_usage(250_000, 0, 0, 1))])
        sdk, _log, plays = fake_sdk(transcripts=False)
        plays.append([
            [sdk_init(sdk), sdk_result(sdk, models={OPUS: tokens(fresh=250_000)})],
            [sdk_init(sdk), caught_up, sdk_result(sdk, models={OPUS: tokens(fresh=250_000, out=1)})],
        ])
        code, out = run_runner(sdk, "next\n", keep_home=True)
        assert code == 0, out
        assert json.loads(SPEND.read_text())[THIS_MONTH]["usd"] == 1.1, SPEND.read_text()
    finally:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_settle_credits_the_months_the_hold_spent_in():
    """Review of 526edad and SPEND MODEL C8, H2 to H5: settle credited the clock's month,
    so a November hold settled in December left November overstated and December
    negative. Each reading lands in its own UTC month and the reservation keeps every
    month it touched; settle replaces that month's share, needs one figure per month
    when there are several, and refuses an ambiguous one, a figure below the exact
    share, and anything but a hold."""

    def settle(*given):
        done = subprocess.run(
            [sys.executable, str(HERE / "sdk_worker.py"), "settle", "--ticket", "t-1",
             "--spend", str(SPEND), *given],
            capture_output=True, text=True, check=False,
        )
        return done.returncode, done.stdout

    def held(months):
        hold = {"pid": None, "open": True, "usd": 20.0, "spent": round(sum(months.values()), 6),
                "exact": 0.0, "months": months, "why": ["no result"]}
        data = {m: {"usd": usd + 5.0, "tickets": {"t-1": usd, "other": 5.0}} for m, usd in months.items()}
        SPEND.write_text(json.dumps({**data, "reserved": {"t-1": hold}}))

    try:
        # Readings either side of midnight UTC on the 1st land in their own months (C8),
        # and the reservation keeps both, so its hold needs both figures (H4).
        SPEND.unlink(missing_ok=True)
        november, december = 975_628_799.0, 975_628_801.0  # 2000-11-30 23:59:59 and 2000-12-01 00:00:01
        given = runner_args(ticket_usd=20)
        runner.reserve(given, november)
        runner.open_turn(given, november)
        runner.record(given, 0.4, 0.4, november, close=False)
        runner.record(given, 0.1, 0.5, december, close=False)
        runner.finish(given, ["no result"], False)
        spend = json.loads(SPEND.read_text())
        assert (spend["2000-11"]["usd"], spend["2000-12"]["usd"]) == (0.4, 0.1), spend
        assert spend["reserved"]["t-1"]["months"] == {"2000-11": 0.4, "2000-12": 0.1}, spend
        assert settle("--usd", "0.5")[0] == 1
        assert settle("--month", "2000-11=0.4", "--month", "2000-12=0.15")[0] == 0
        assert json.loads(SPEND.read_text())["2000-12"]["usd"] == 0.15
        held({"2000-11": 1.10})
        assert settle("--usd", "1") == (0, "· sdk worker: settled t-1 at 1 USD\n")
        spend = json.loads(SPEND.read_text())
        assert spend == {"2000-11": {"usd": 6.0, "tickets": {"t-1": 1.0, "other": 5.0}}}, spend
        held({"2000-11": 0.6, "2000-12": 0.5})
        code, said = settle("--usd", "1")
        assert (code, said) == (1, "· sdk worker: held spend for t-1 spans 2000-11, 2000-12; give --month YYYY-MM=USD for each\n"), said
        assert settle("--month", "2000-11=0.5", "--month", "2000-12=0.4")[0] == 0
        spend = json.loads(SPEND.read_text())
        assert (spend["2000-11"]["usd"], spend["2000-12"]["usd"], "reserved" in spend) == (5.5, 5.4, False), spend
        # A month the open turn reached before it recorded anything there still counts (H4).
        held({"2000-11": 1.10, "2000-12": 0.0})
        assert settle("--usd", "1")[0] == 1
        # Below the exact share is refused and the hold stays (H5).
        held({"2000-11": 1.10})
        data = json.loads(SPEND.read_text())
        data["reserved"]["t-1"]["exact"] = 0.9
        SPEND.write_text(json.dumps(data))
        code, said = settle("--usd", "0.5")
        assert code == 1 and "below its exact share 0.9000 USD" in said, said
        assert json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True
        # A live reservation, or none, is not a hold (H2).
        data["reserved"]["t-1"].update(pid=os.getpid())
        SPEND.write_text(json.dumps(data))
        assert settle("--usd", "1") == (1, "· sdk worker: no held spend for t-1\n")
        SPEND.write_text(json.dumps({"2000-11": {"usd": 1.0}}))
        assert settle("--usd", "1") == (1, "· sdk worker: no held spend for t-1\n")
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_settle_refuses_nonfinite_or_negative_monthly_amounts():
    one = {"2000-11": 0.2}
    two = one | {"2000-12": 0.3}
    cases = [
        (one, ["--usd", "nan"]),
        (one, ["--usd", "inf"]),
        (two, ["--month", "2000-11=-0.2", "--month", "2000-12=1.2"]),
    ]
    for months, given in cases:
        held = {
            "pid": None, "open": True, "usd": 20, "spent": sum(months.values()),
            "exact": 0.2, "months": months,
        }
        before = json.dumps({"reserved": {"t-1": held}})
        SPEND.write_text(before)
        try:
            done = subprocess.run(
                [sys.executable, str(HERE / "sdk_worker.py"), "settle", "--ticket", "t-1",
                 "--spend", str(SPEND), *given],
                capture_output=True, text=True, timeout=5,
            )
            assert done.returncode != 0 and SPEND.read_text() == before, (given, done)
        finally:
            SPEND.unlink(missing_ok=True)


def opus_line(session, mid, fresh=0, **extra):
    """A step writing one Opus request into the session's transcript mid-turn."""
    return lambda: transcript(session, [(mid, OPUS, turn_usage(fresh) | extra)])


@scenario
def sdk_runner_holds_every_ending_without_proof():
    """SPEND MODEL shape 2 and rows R5, C7, X3: a turn that ends with no result (the
    stream stops, the SDK raises) or a spend file that fails mid-run deletes nothing.
    What the readings found is recorded, the reservation stays open with its pid cleared,
    and the ticket's next start is refused until settle."""
    try:
        for ending in (None, ConnectionError("dropped")):
            shutil.rmtree(SDK_HOME, ignore_errors=True)
            SPEND.unlink(missing_ok=True)
            sdk, _log, plays = fake_sdk(transcripts=False)
            steps = [sdk_init(sdk), opus_line("s-1", "o1", 100_000)]
            plays.append([steps + ([ending] if ending else [])])
            code, out = run_runner(sdk, "a wake\n", keep_home=True)
            assert code == 1 and out[-2:] == ["· sdk worker: unpriced usage: no result", runner.UNPRICED], out
            spend = json.loads(SPEND.read_text())
            held = spend["reserved"]["t-1"]
            assert spend[THIS_MONTH]["usd"] == 0.4, spend
            assert (held["open"], held["pid"], held["months"]) == (True, None, {THIS_MONTH: 0.4}), held
            assert "no result" in held["why"], held
            sdk, log, _plays = fake_sdk()
            assert run_runner(sdk)[1][-1] == runner.UNPRICED and log == [], "the next start is refused"
        # The spend file turns into a folder mid-turn: the runner stops at once.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, _log, plays = fake_sdk()

        def break_file():
            SPEND.unlink()
            SPEND.mkdir()

        plays.append([[sdk_init(sdk), break_file, sdk_result(sdk, 0.1)]])
        code, out = run_runner(sdk)
        assert (code, out[-1]) == (1, runner.NO_SPEND), out
        SPEND.rmdir()
        # A line that is JSON but not an object, and a half-written last line, are skipped.
        folder = SDK_HOME / "projects" / runner_key(os.getcwd())
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "assistant", "message": {"id": "a", "model": OPUS, "usage": turn_usage(5)}})
        (folder / "s-9.jsonl").write_text(f"[1, 2]\n{line}\n{line[:30]}")
        found, problems = runner.requests(SDK_HOME / "projects", folder.name, ["s-9"])
        assert (list(found), problems) == (["a"], []), (found, problems)
    finally:
        if SPEND.is_dir():
            SPEND.rmdir()
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_holds_spend_with_an_unreadable_subagent_transcript():
    def write_subagent():
        folder = SDK_HOME / "projects" / runner_key(os.getcwd()) / "s-1" / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "agent-partial.jsonl").write_bytes(b'{"type":"assistant","text":"\xc3')

    sdk, _log, plays = fake_sdk()
    plays.append([[sdk_init(sdk), sdk_said(sdk, "done"), write_subagent, sdk_result(sdk, 0.3)]])
    try:
        code, out = run_runner(sdk)
        held = json.loads(SPEND.read_text()).get("reserved", {}).get("t-1", {})
        assert code == 1 and out[-1] == runner.UNPRICED and held.get("open"), (out, held)
        assert "transcript unreadable" in held["why"], held
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_releases_a_session_that_never_sent_a_request():
    """Row 44: a session that fails before its first request (the CLI cannot spawn, a
    sandbox tool is missing, a bad option) spent nothing, so its reservation is released
    at once and the month's spend is unchanged; the runner exits on FAILED, with no hold
    to settle, and the ticket can launch again. Once a request is sent, its UTC month is
    on the reservation before any reading, so the hold it leaves names that month and
    settle accepts it, though never below its exact share."""

    def settle(*given):
        done = subprocess.run(
            [sys.executable, str(HERE / "sdk_worker.py"), "settle", "--ticket", "t-1",
             "--spend", str(SPEND), *given],
            capture_output=True, text=True, check=False,
        )
        return done.returncode, done.stdout

    try:
        for failing in ("connect", "options"):
            SPEND.write_text(json.dumps({THIS_MONTH: {"usd": 3.0, "tickets": {"other": 3.0}}}))
            sdk, log, plays = fake_sdk()
            if failing == "connect":
                plays.append(FileNotFoundError("claude: command not found"))
            else:
                def bad_option(**given):
                    raise TypeError("unexpected keyword argument")

                sdk.ClaudeAgentOptions = bad_option
            code, out = run_runner(sdk)
            assert code == 1 and out[-1] == runner.FAILED, (failing, out)
            assert queries(log) == [], log
            spend = json.loads(SPEND.read_text())
            assert spend == {THIS_MONTH: {"usd": 3.0, "tickets": {"other": 3.0}}}, (failing, spend)
            assert runner.reserve(runner_args(), time.time())[2] is None, "the next start is not held"
        # A request sent, then a crash before any reading: the hold carries its month.
        SPEND.unlink(missing_ok=True)
        sdk, _log, plays = fake_sdk(transcripts=False)
        plays.append([[ConnectionError("dropped")]])
        code, out = run_runner(sdk)
        assert code == 1 and out[-1] == runner.UNPRICED, out
        held = json.loads(SPEND.read_text())["reserved"]["t-1"]
        assert (held["open"], held["pid"], sorted(held["months"])) == (True, None, [THIS_MONTH]), held
        assert settle("--usd", "0.2") == (0, "· sdk worker: settled t-1 at 0.2 USD\n")
        # Spend read before the crash is the exact share settle may not go below.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, _log, plays = fake_sdk(transcripts=False)
        plays.append([[sdk_init(sdk), opus_line("s-1", "o1", 100_000), ConnectionError("dropped")]])
        run_runner(sdk, keep_home=True)
        held = json.loads(SPEND.read_text())["reserved"]["t-1"]
        assert (sorted(held["months"]), held["exact"]) == ([THIS_MONTH], 0.4), held
        code, said = settle("--usd", "0.1")
        assert code == 1 and "below its exact share 0.4000 USD" in said, said
        assert settle("--usd", "0.4")[0] == 0
    finally:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_reservations_count_until_proven_spent():
    """SPEND MODEL C2, C3, C4, C5: a dead runner's open reservation is a hold that counts
    in full; a dead runner's closed one is dropped; a ticket whose runner lives, or that
    has a hold, is refused and never overwritten; dev-auto's month_spent and reserve
    take the same sum."""
    gone = dead_pid()
    month = {"usd": 80.0}
    reserved = {
        "dead-open": {"pid": gone, "usd": 10.0, "spent": 0.0, "exact": 0.0, "months": {}, "open": True, "why": []},
        "dead-closed": {"pid": gone, "usd": 10.0, "spent": 0.0, "exact": 0.0, "months": {}, "open": False, "why": []},
    }
    try:
        SPEND.write_text(json.dumps({THIS_MONTH: month, "reserved": reserved}))
        before, allowance, refusal = runner.reserve(runner_args(), time.time())
        assert (before, allowance, refusal) == (0.0, 10.0, None), (before, allowance, refusal)
        data = json.loads(SPEND.read_text())
        assert sorted(data["reserved"]) == ["dead-open", "t-1"], data
        assert da.month_spent(time.time()) == 80.0 + runner.unrecorded(data) == 100.0
        was = SPEND.read_text()
        assert runner.reserve(runner_args(), time.time())[2] == "running" and SPEND.read_text() == was
        data["reserved"]["t-1"].update(pid=None, open=True)
        SPEND.write_text(json.dumps(data))
        assert runner.reserve(runner_args(), time.time())[2] == "held"
        assert runner.reserve(runner_args("dead-open"), time.time())[2] == "held"
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_stops_inside_a_turn():
    """SPEND MODEL C6, R10, T3: a reading every READ_EVERY seconds inside a turn records
    what the transcripts show and interrupts the turn once recorded spend reaches the
    cap, though the CLI's estimate stays 0, or once a request has no price. All of it is
    recorded, never clipped; the budget stop ends on BUDGET and releases."""
    every = runner.READ_EVERY
    runner.READ_EVERY = 0.01
    try:
        # 17 of 20 already spent: the cap leaves 3 USD; one request reads 4 USD.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.write_text(json.dumps({"2000-01": {"usd": 17.0, "tickets": {"t-1": 17.0}}}))
        sdk, log, plays = fake_sdk(transcripts=False)
        result = sdk_result(sdk, models={OPUS: tokens(fresh=1_000_000)}, estimate=0.0)
        plays.append([[sdk_init(sdk), opus_line("s-1", "o1", 1_000_000), 0.2, result]])
        code, out = run_runner(sdk, "a wake\n", keep_home=True)
        assert ("interrupt",) in log and (code, out[-1]) == (0, runner.BUDGET), (log, out)
        spend = json.loads(SPEND.read_text())
        assert spend[THIS_MONTH]["usd"] == 4.0 and "reserved" not in spend, spend
        # A watcher reading inside a normal turn is part of that turn's cost row.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, log, plays = fake_sdk(transcripts=False)
        plays.append([[sdk_init(sdk), opus_line("s-1", "o1", 250_000), 0.2,
                       sdk_result(sdk, models={OPUS: tokens(fresh=250_000)})]])
        code, out = run_runner(sdk, keep_home=True)
        assert code == 0 and "· sdk worker: success, 1.00 USD this turn, 1.00 of 20 USD on t-1" in out, out
        # A code execution request has no price: the reading stops the turn and holds.
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, log, plays = fake_sdk(transcripts=False)
        tool = {"server_tool_use": {"code_execution_requests": 7}}
        plays.append([[sdk_init(sdk), opus_line("s-1", "c1", 1000, **tool), 0.2,
                       sdk_result(sdk, models={OPUS: tokens(fresh=1000)})]])
        code, out = run_runner(sdk, keep_home=True)
        assert ("interrupt",) in log and code == 1, (log, out)
        assert out[-2:] == ["· sdk worker: unpriced usage: server_tool_use code_execution_requests", runner.UNPRICED], out
        assert json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True
    finally:
        runner.READ_EVERY = every
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


class LateStdin:
    """Standard input that runs a step, then reports end of input: work that lands after
    the last result, seen only by the exit reading."""

    def __init__(self, step):
        self.step = step

    def readline(self):
        self.step()
        return ""


@scenario
def sdk_runner_exits_on_term_with_stdin_still_open():
    script = """
import json, os, shutil, sys
import test_dev_auto as fixture

try:
    sdk, _, plays = fixture.fake_sdk()
    plays.append([[fixture.sdk_init(sdk), fixture.sdk_result(sdk, 0.1)]])
    sys.modules["claude_agent_sdk"] = sdk
    os.environ["CLAUDE_CONFIG_DIR"] = str(fixture.SDK_HOME)
    raise SystemExit(fixture.runner.main([
        "--ticket", "t-1", "--model", fixture.OPUS, "--key-file", str(fixture.KEY),
        "--ticket-usd", "20", "--monthly-usd", "100", "--spend", str(fixture.SPEND),
        "--mcp", json.dumps(fixture.SERVER), "go",
    ]))
finally:
    shutil.rmtree(fixture.tmp, ignore_errors=True)
"""
    env = {**os.environ, "PYTHONPATH": str(HERE)}
    with subprocess.Popen(
        [sys.executable, "-c", script], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, bufsize=0,
    ) as proc:
        try:
            output = b""
            deadline = time.monotonic() + 5
            prompt = runner.PROMPT.encode()
            while prompt not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and select.select([proc.stdout], [], [], remaining)[0], output
                chunk = os.read(proc.stdout.fileno(), 4096)
                assert chunk, output
                output += chunk
            proc.stdin.write(b"partial")
            time.sleep(0.1)
            proc.terminate()
            try:
                assert proc.wait(timeout=1.5) == 1
            except subprocess.TimeoutExpired as error:
                raise AssertionError("runner waited for stdin after SIGTERM") from error
            assert not proc.stdin.closed
        finally:
            if proc.poll() is None:
                proc.stdin.close()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


@scenario
def sdk_runner_releases_only_on_final_proof():
    """Review of 2099934, shape 2: a reservation goes only when the last reading at a
    result was complete and nothing found since adds doubt. A late subagent request with
    no price at exit stops visibly and holds; a connection that fails before any query
    spent nothing and holds nothing (row 44); a watcher reading whose write fails stops the turn through C7 and leaves
    the file as last written; a costUSD with no token counts, or a transcript with more
    searches than model_usage, is no proof."""
    every = runner.READ_EVERY
    try:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)
        sdk, log, plays = fake_sdk()
        plays.append([[sdk_init(sdk), sdk_result(sdk, 0.1)]])
        tool = turn_usage(1000) | {"server_tool_use": {"code_execution_requests": 7}}
        late = LateStdin(lambda: transcript("s-1", [("c1", OPUS, tool)], agent="a1"))
        out = io.StringIO()
        os.environ["CLAUDE_CONFIG_DIR"] = str(SDK_HOME)
        code = asyncio.run(runner.run(runner_args(), sdk, late, out))
        rows = out.getvalue().splitlines()
        # The late request is also usage past the last whole model_usage (R2 at exit).
        reasons = "model_usage missing or zeroed, server_tool_use code_execution_requests"
        assert code == 1 and rows[-2:] == [f"› · sdk worker: unpriced usage: {reasons}", runner.UNPRICED], rows
        held = json.loads(SPEND.read_text())["reserved"]["t-1"]
        assert (held["open"], held["pid"]) == (True, None), held
        # A connection that fails before any query.
        SPEND.unlink()
        sdk, log, plays = fake_sdk()
        plays.append(ConnectionError("refused"))
        code, out = run_runner(sdk)
        assert code == 1 and out[-1] == runner.FAILED, out
        assert "reserved" not in json.loads(SPEND.read_text())
        # A watcher reading whose write fails, with the file back before the result.
        SPEND.unlink()
        runner.READ_EVERY = 0.01
        saved = {}

        def break_file():
            saved["text"] = SPEND.read_text()
            SPEND.unlink()
            SPEND.mkdir()

        def mend_file():
            SPEND.rmdir()
            SPEND.write_text(saved["text"])

        sdk, log, plays = fake_sdk(transcripts=False)
        plays.append([[sdk_init(sdk), opus_line("s-1", "o1", 1000), break_file, 0.2, mend_file,
                       sdk_result(sdk, models={OPUS: tokens(fresh=1000)})]])
        code, out = run_runner(sdk, keep_home=True)
        # The watcher cancels the turn and the session disconnects, which ends the CLI.
        assert log[-1] == ("disconnect",) and (code, out[-1]) == (1, runner.NO_SPEND), (log, out)
        if SPEND.is_dir():
            mend_file()
        assert json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True, "left as last written"
        runner.READ_EVERY = every
        # costUSD with no token counts, and fewer searches than the transcript holds.
        for models, entry, reasons in (
            ({OPUS: tokens(estimate=0.42)}, None, "no transcript, model_usage missing or zeroed"),
            ({OPUS: tokens(fresh=1000) | {"webSearchRequests": 0}},
             ("w1", OPUS, turn_usage(1000) | {"server_tool_use": {"web_search_requests": 5}}),
             "model_usage missing or zeroed"),
        ):
            shutil.rmtree(SDK_HOME, ignore_errors=True)
            SPEND.unlink(missing_ok=True)
            if entry:
                transcript("s-1", [entry])
            sdk, log, plays = fake_sdk(transcripts=False)
            plays.append([[sdk_init(sdk), sdk_result(sdk, models=models, estimate=0.0)]])
            code, out = run_runner(sdk, keep_home=True)
            assert code == 1 and out[-2:] == [f"· sdk worker: unpriced usage: {reasons}", runner.UNPRICED], out
            assert json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True
    finally:
        runner.READ_EVERY = every
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        if SPEND.is_dir():
            SPEND.rmdir()
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_ends_a_turn_it_cannot_stop_and_rechecks_at_exit():
    """Review of 5e4dc44. C6: when the interrupt at a stop point itself fails, the
    watcher cancels the turn, so no request starts after the failed stop, and the session
    ends through R5 and holds. R2 on the way out: an exit reading whose transcripts have
    grown past the last whole model_usage holds; late lines still within it are X4's
    catch-up and release."""
    every = runner.READ_EVERY
    runner.READ_EVERY = 0.01
    try:
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.write_text(json.dumps({"2000-01": {"usd": 17.0, "tickets": {"t-1": 17.0}}}))
        sdk, log, plays = fake_sdk(transcripts=False, interrupt_fails=True)
        plays.append([[sdk_init(sdk), opus_line("s-1", "o1", 1_000_000), 0.2,
                       opus_line("s-1", "o2", 1_000_000),
                       sdk_result(sdk, models={OPUS: tokens(fresh=2_000_000)})]])
        code, out = run_runner(sdk, keep_home=True)
        assert ("interrupt",) in log and code == 1, (log, out)
        assert out[-2:] == ["· sdk worker: unpriced usage: no result", runner.UNPRICED], out
        spend = json.loads(SPEND.read_text())
        assert spend[THIS_MONTH]["usd"] == 4.0 and spend["reserved"]["t-1"]["open"] is True, spend
        runner.READ_EVERY = every
        os.environ["CLAUDE_CONFIG_DIR"] = str(SDK_HOME)
        for whole, late, ending in (
            (1000, 1000, "· sdk worker: unpriced usage: model_usage missing or zeroed"),
            (2000, 1000, None),
        ):
            shutil.rmtree(SDK_HOME, ignore_errors=True)
            SPEND.unlink(missing_ok=True)
            transcript("s-1", [("o1", OPUS, turn_usage(1000))])
            sdk, log, plays = fake_sdk(transcripts=False)
            plays.append([[sdk_init(sdk), sdk_result(sdk, models={OPUS: tokens(fresh=whole)})]])
            arrives = LateStdin(lambda late=late: transcript("s-1", [
                ("o1", OPUS, turn_usage(1000)), ("o2", OPUS, turn_usage(late))]))
            out = io.StringIO()
            code = asyncio.run(runner.run(runner_args(), sdk, arrives, out))
            rows = out.getvalue().splitlines()
            spend = json.loads(SPEND.read_text())
            if ending:
                assert code == 1 and rows[-2:] == [f"› {ending}", runner.UNPRICED], rows
                assert spend["reserved"]["t-1"]["open"] is True, spend
            else:
                assert code == 0 and "reserved" not in spend, (rows, spend)
    finally:
        runner.READ_EVERY = every
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_refuses_an_unlisted_model_and_ignores_resets():
    """SPEND MODEL T5, R8, O3, O4: a --model with no price stops before it connects or
    reserves; a typed /clear, /reset or /new restarts the CLI's totals, so it is never
    forwarded; the session env keeps background work off and the key out of tool
    subprocesses."""
    SPEND.unlink(missing_ok=True)
    sdk, log, _plays = fake_sdk()
    out = io.StringIO()
    args = runner.parse(["--ticket", "t-1", "--model", "claude-opus-4-7", "--key-file", str(KEY),
                         "--ticket-usd", "20", "--monthly-usd", "100", "--spend", str(SPEND),
                         "--mcp", json.dumps(SERVER), "go"])
    code = asyncio.run(runner.run(args, sdk, io.StringIO(), out))
    rows = out.getvalue().splitlines()
    assert (code, rows) == (1, ["· sdk worker: unpriced usage: model claude-opus-4-7", runner.UNPRICED]), rows
    assert log == [] and not SPEND.exists()
    sdk, log, plays = fake_sdk()
    plays.append([[sdk_init(sdk), sdk_result(sdk, 0.1)], [sdk_init(sdk), sdk_result(sdk, 0.2)]])
    try:
        code, out = run_runner(sdk, "/clear\n/new now\nnext\n")
        assert code == 0 and queries(log) == ["go", "next"], log
        # The typed line's echo ends the prompt's row on a terminal; here nothing echoes.
        assert [r for r in out if r.endswith(("ignored /clear", "ignored /new"))] == [
            "› · sdk worker: ignored /clear", "› · sdk worker: ignored /new"], out
        env = clients(log)[0]["env"]
        assert (env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"]) == ("1", "1")
        # An inherited opt-in to MCP tasks is emptied, so none can start an idle model (O3).
        os.environ["CLAUDE_AUTO_BACKGROUND_TASKS"] = "1"
        try:
            sdk, log, plays = fake_sdk()
            plays.append([[sdk_init(sdk), sdk_result(sdk, 0.1)]])
            assert run_runner(sdk)[0] == 0
        finally:
            del os.environ["CLAUDE_AUTO_BACKGROUND_TASKS"]
        given = clients(log)[0]
        assert given["env"]["CLAUDE_AUTO_BACKGROUND_TASKS"] == "", given["env"]
        assert (given["env"]["CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND"], given["env"]["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"]) == ("1", "0")
        assert given["strict_mcp_config"] is True and list(given["mcp_servers"]) == ["concord"], given
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_stops_on_a_turn_with_no_api_usage():
    """Live smoke at 7be8e9c: all three turns reported "success" with no tokens and 0 USD,
    so a failed or blocked request read as a finished turn. A result that is an error,
    or that records no usage at all, prints one row of its codes and counts, never its
    text, then the fixed stop row, and the runner exits before reading another message."""
    sdk, log, plays = fake_sdk()
    failed = sdk_said(sdk, "API Error: Credit balance is too low", model="<synthetic>",
                      error="billing_error")
    plays.append([[sdk_init(sdk), failed, sdk_result(sdk, estimate=0.0, is_error=True,
                   api_error_status=400, terminal_reason="api_error", duration_api_ms=210)]])
    code, out = run_runner(sdk, "a wake\n")
    assert code == 1 and out[-1] == runner.FAILED and runner.IDLE not in out, out
    assert out[-2] == (
        "· sdk worker: no API usage: success, is_error True, api_error_status 400, "
        "error billing_error, terminal_reason api_error, 1 messages, 1 turns, 210 ms API"
    ), out
    assert not any("Credit balance" in row and row.startswith("· ") for row in out), out
    assert queries(log) == ["go"], log
    assert "reserved" not in json.loads(SPEND.read_text()), "a result with no usage anywhere releases (R3)"
    # A zeroed crash result with nothing found is no proof of 0: it holds (R2, R3).
    sdk, log, plays = fake_sdk(transcripts=False)
    plays.append([[sdk_init(sdk), sdk_result(sdk, subtype="error_during_execution", estimate=0.0)]])
    code, out = run_runner(sdk)
    assert code == 1 and out[-2:] == ["· sdk worker: unpriced usage: model_usage missing or zeroed", runner.UNPRICED], out
    assert json.loads(SPEND.read_text())["reserved"]["t-1"]["open"] is True
    SPEND.unlink()
    # A success that carries no usage at all, as a prompt a hook blocked, stops the same.
    sdk, log, plays = fake_sdk()
    plays.append([[sdk_init(sdk), sdk_result(sdk, estimate=0.0, terminal_reason=None,
                   num_turns=0, duration_api_ms=0)]])
    code, out = run_runner(sdk, "a wake\n")
    assert code == 1 and out[-1] == runner.FAILED, out
    assert out[-2] == (
        "· sdk worker: no API usage: success, is_error False, api_error_status None, "
        "error None, terminal_reason None, 0 messages, 0 turns, 0 ms API"
    ), out
    SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_hands_off_at_80_percent_then_stops_at_its_cap():
    """At 80 percent of ticket_usd, counting the ticket's spend from earlier months, the
    session's budget stops it; the runner resumes the session with the rest of the cap
    and sends one handoff message. At the cap it prints the budget line once and exits
    without reading another message."""
    SPEND.write_text(json.dumps({"2000-01": {"usd": 2.0, "tickets": {"t-1": 2.0}}}))
    sdk, log, plays = fake_sdk()
    capped = "error_max_budget_usd"
    plays.append([[sdk_init(sdk, session="s-9"), sdk_said(sdk, "@Bash"), sdk_result(sdk, 6.05, capped, "s-9")]])
    # A resumed session's total carries the spend it restored.
    wrote = sdk_said(sdk, "handoff written")
    plays.append([[sdk_init(sdk, session="s-9"), wrote, sdk_result(sdk, 8.1, capped, "s-9")]])
    try:
        code, out = run_runner(sdk, "a wake that is never read\n", ticket_usd=10)
        assert code == 0 and out[-1] == runner.BUDGET and out.count(runner.BUDGET) == 1, out
        assert runner.IDLE not in out, out
        assert queries(log) == ["go", runner.HANDOFF], log
        one, two = clients(log)
        assert (one["max_budget_usd"], "resume" in one) == (6.0, False), one
        assert (two["max_budget_usd"], two["resume"]) == (1.95, "s-9"), two
        same = {k: v for k, v in one.items() if k != "max_budget_usd"}
        assert {k: v for k, v in two.items() if k not in ("max_budget_usd", "resume")} == same, (
            "the resumed session keeps the cached prefix: same prompt, tools, effort and env"
        )
        assert "append" not in one["system_prompt"], "nothing volatile in the system prompt"
        spend = json.loads(SPEND.read_text())
        assert spend[THIS_MONTH] == {"usd": 8.1, "tickets": {"t-1": 8.1}}, spend
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_skips_handoff_when_monthly_budget_runs_out_first():
    SPEND.write_text(json.dumps({THIS_MONTH: {"usd": 90, "tickets": {"other": 90}}}))
    sdk, log, plays = fake_sdk()
    plays.append([[sdk_init(sdk), sdk_said(sdk, "done"),
                   sdk_result(sdk, 10.01, "error_max_budget_usd")]])
    try:
        code, out = run_runner(sdk, ticket_usd=100)
        assert code == 0 and out[-1] == runner.BUDGET, out
        assert queries(log) == ["go"], log
        assert clients(log)[0]["max_budget_usd"] == 10, log
    finally:
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runners_share_the_months_headroom():
    """Review of e23fd91: two runners each took all of the month's remaining headroom and
    together spent past monthly_usd. Each runner reserves its allowance in the spend file
    under its lock, so a second one gets only what the first left; the reservation goes
    when the runner exits, and a dead runner's is dropped."""
    month = {"usd": 90.0}
    gone = {"pid": dead_pid(), "usd": 10.0, "spent": 0.0}
    SPEND.write_text(json.dumps({THIS_MONTH: month, "reserved": {"t-9": gone}}))
    first, first_log, plays = fake_sdk()
    capped = "error_max_budget_usd"
    plays.append([[sdk_init(first, session="a"), sdk_result(first, 8.0, capped, "a")]])
    plays.append([[sdk_init(first, session="a"), sdk_result(first, 10.0, capped, "a")]])
    second, second_log, _plays = fake_sdk()
    outs = io.StringIO(), io.StringIO()

    async def both():
        return await asyncio.gather(
            runner.run(runner_args("t-1", 10), first, io.StringIO(), outs[0]),
            runner.run(runner_args("t-2", 10), second, io.StringIO(), outs[1]),
        )

    os.environ["CLAUDE_CONFIG_DIR"] = str(SDK_HOME)
    try:
        assert asyncio.run(both()) == [0, 0]
        budgets = [c["max_budget_usd"] for c in clients(first_log)]
        assert budgets == [8.0, 2.0], budgets
        assert outs[1].getvalue().splitlines() == [runner.BUDGET] and second_log == []
        spend = json.loads(SPEND.read_text())
        assert spend == {THIS_MONTH: {"usd": 100.0, "tickets": {"t-1": 10.0}}}, spend
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]
        SPEND.unlink(missing_ok=True)


@scenario
def sdk_runner_refuses_to_start_past_its_caps_or_off_the_key():
    """The runner starts no turn when the ticket or the month is at its cap, or the key
    file is empty. A ticket already past 80 percent gets the handoff with its first
    message. A session not billed to ANTHROPIC_API_KEY is interrupted at its init
    message and the runner exits with one fixed line."""
    try:
        for spent in ({"2000-01": {"usd": 20, "tickets": {"t-1": 20}}}, {THIS_MONTH: {"usd": 100}}):
            SPEND.write_text(json.dumps(spent))
            sdk, log, _plays = fake_sdk()
            assert run_runner(sdk) == (0, [runner.BUDGET]) and log == [], spent
        SPEND.write_text("{not json")
        sdk, log, _plays = fake_sdk()
        assert run_runner(sdk) == (1, [runner.NO_SPEND]) and log == []
        SPEND.write_text(json.dumps({THIS_MONTH: {"usd": 17, "tickets": {"t-1": 17}}}))
        sdk, log, plays = fake_sdk()
        plays.append([[sdk_init(sdk), sdk_result(sdk, 0.1)]])
        run_runner(sdk)
        assert json.loads(SPEND.read_text())[THIS_MONTH]["usd"] == 17.1, SPEND.read_text()
        assert queries(log) == [f"go {runner.HANDOFF}"], log
        assert clients(log)[0]["max_budget_usd"] == 3.0, clients(log)
        SPEND.unlink()
        sdk, log, plays = fake_sdk()
        plays.append([[sdk_init(sdk, "none"), sdk_said(sdk, "never shown"), sdk_result(sdk, 0.3)]])
        code, out = run_runner(sdk, "a wake\n")
        assert (code, out) == (1, [runner.BUSY, runner.NOT_API]), out
        assert queries(log) == ["go"] and ("interrupt",) in log, log
        assert json.loads(SPEND.read_text()) == {}, "nothing is counted or held"
        # Live smoke at 4c4b5ec: the scrub forces default mode. A session in any other mode
        # runs without the scrub, so it stops, and as the query was sent, its spend holds.
        SPEND.unlink(missing_ok=True)
        sdk, log, plays = fake_sdk()
        plays.append([[sdk_init(sdk, mode="bypassPermissions"), sdk_result(sdk, 0.3)]])
        code, out = run_runner(sdk, "a wake\n")
        assert (code, out) == (1, [runner.BUSY, runner.NO_SCRUB]), out
        assert queries(log) == ["go"] and ("interrupt",) in log, log
        hold = json.loads(SPEND.read_text())["reserved"]["t-1"]
        assert (hold["open"], hold["pid"], hold["spent"], hold["why"]) == (True, None, 0.0, [runner.NO_SCRUB]), hold
        # Usage that lands while the interrupt is in flight, with no result after it, is
        # recorded at its exact price and held.
        SPEND.unlink()
        sdk, log, plays = fake_sdk()
        late = lambda: transcript("s-late", [("msg-late", OPUS, turn_usage(fresh=1000))])
        plays.append([[late, sdk_init(sdk, session="s-late", mode="acceptEdits")]])
        code, out = run_runner(sdk, "a wake\n")
        assert (code, out) == (1, [
            runner.BUSY, runner.NO_SCRUB,
            "· sdk worker: unpriced usage: model_usage missing or zeroed", runner.UNPRICED,
        ]), out
        spend = json.loads(SPEND.read_text())
        assert spend[THIS_MONTH]["tickets"]["t-1"] == 0.004, spend
        hold = spend["reserved"]["t-1"]
        assert (hold["open"], hold["exact"]) == (True, 0.004), hold
        assert hold["why"] == [runner.NO_SCRUB, "model_usage missing or zeroed"], hold
        shutil.rmtree(SDK_HOME, ignore_errors=True)
        KEY.write_text("\n")
        sdk, log, _plays = fake_sdk()
        assert run_runner(sdk) == (1, [runner.NO_KEY]) and log == []
    finally:
        KEY.write_text("sk-ant-api03-SECRET-test-key\n")
        SPEND.unlink(missing_ok=True)


@scenario
def launch_without_ledger_root_refused():
    """A launch whose concord-mcp would start without the ledger root is refused before
    its terminal is created: a root that is not an absolute path, and a kind on a host
    the ledger's ssh gate cannot name."""
    set_fixture()
    refused = [
        (rec("t-1", "c1", agent_id="claude:settings-wiring"), Path("ledger")),
        (
            rec(
                "t-1",
                "gk",
                agent="grok",
                model="grok-4.7",
                host="Juzzark-WSL",
                agent_id="grok:wsl-settings-wiring",
            ),
            root,
        ),
    ]
    for worker, ledger_root in refused:
        row = da.account(cfg, worker["account"], "local")
        try:
            da.worker_command(cfg, ledger_root, worker, row, "go")
        except SystemExit as stop:
            assert "ledger" in str(stop.code), stop.code
        else:
            raise AssertionError(f"{worker['agent_id']} launched without its ledger")
        st = fresh_state()
        log_file.write_text("")
        try:
            da.start(cfg, ledger_root, st, worker, row, "go", NOW)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"{worker['agent_id']} started")
        assert calls("orca", ["terminal", "create"]) == [], calls("orca")


@scenario
def wait_review():
    """Nothing until the verdict, then one wake; a PUSH also clears the review."""
    reset_db()
    ticket_row("r1")
    ticket_row("r1-review", agent="codex:r1-review")
    update("r1", "blocker", "Waiting on: review. REVIEW READY abc1234")
    st = fresh_state(r1=rec("r1", "c1"))
    world(local(term("term-r1", 7200)))
    tick(st)
    assert sends() == [], "waiting on running review: nothing, and silence is no stall"
    update("r1", "finding", "REVIEW VERDICT: FIX\nfindings", agent="codex:r1-review")
    world(local(term("term-r1", 7200)))
    tick(st)
    wake = "dev-auto: a review verdict was posted on ticket r1. Read it there and continue."
    assert sends() == [("term-r1", woke("r1", wake), True)], sends()
    assert notes() == [wake], "one dev-auto prefix"
    world(local(term("term-r1", 7200)))
    tick(st)
    assert sends() == [], "each event is delivered once"
    update("r1", "blocker", "Waiting on: review of def456")
    push = update("r1", "finding", "REVIEW VERDICT: PUSH", agent="codex:r1-review")
    world(local(term("term-r1", 7200)))
    tick(st)
    # A repeat of a typed text carries its event; a PUSH naming no SHA clears nothing.
    assert sends() == [("term-r1", woke("r1", f"{wake} Event verdict {push}."), True)], sends()
    sha = update("r1", "finding", "REVIEW VERDICT: PUSH 1a2b3c4d", agent="codex:r")
    world(local(term("term-r1", 7200)))
    tick(st)
    assert ("term-orch", "CLEARED REVIEW r1: PUSH 1a2b3c4d", True) in sends(), sha


# workspace-conventions-07a update 6970. One line, no REVIEW READY, and its SHA-256 is
# not a commit SHA. A review wait in this form has nothing a verdict can clear.
NOTE_6970 = (
    "Waiting on: review — review_ready; independent Codex review "
    "workspace-conventions-07a-review of the ignored filesystem candidate. "
    ".work/operations.md SHA-256 "
    "76dc321907eace4400fbdd848fb855c13b2dae32bfd14b7a8ba8acf04f90f00b; "
    "12 moves/743 objects unchanged; exact seven-entry layout and +4 KiB note "
    "verified. Before/after listings, sizes and backup inventory are in "
    "#6960/#6968; finish_work handoff carries acceptance evidence. No commits "
    "or push required."
)


def review_pair(tid):
    """A coder and its review ticket, both live, so a later request can wake the reviewer."""
    ticket_row(tid)
    ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
    reviewer = rec(f"{tid}-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{tid: rec(tid, "c1"), f"{tid}-review": reviewer})
    screen = local(
        term(f"term-{tid}", 100),
        term(f"term-{tid}-review", 5, ident="codex"),
    )
    return st, screen


def review_stuck(tid, note, why):
    return ("term-orch", f"STUCK {tid}: its review wait {why}, update {note} on the ticket", True)


@scenario
def wait_review_untracked_note():
    """Live (update 6970): a review wait with no REVIEW READY SHA is one STUCK, once."""
    reset_db()
    st, screen = review_pair("u7")
    note = update("u7", "blocker", NOTE_6970)
    world(screen)
    tick(st)
    assert sends() == [review_stuck("u7", note, "has no REVIEW READY with a SHA")], sends()
    world(screen)
    tick(st)
    assert sends() == [], "the same note raises nothing more"


@scenario
def wait_review_after_unanswered_ready():
    """The 6970 note after a bare REVIEW READY line no verdict has answered raises no STUCK."""
    reset_db()
    st, screen = review_pair("ua")
    update("ua", "progress", "REVIEW READY 1a2b3c4")
    update("ua", "blocker", NOTE_6970)
    world(screen)
    tick(st)
    wake = "dev-auto: REVIEW READY was posted on ticket ua. Review it as your ticket says."
    assert sends() == [("term-ua-review", woke("ua-review", wake, "codex"), True)], sends()


@scenario
def wait_review_request_note_raises_nothing():
    """A review-wait line that says REVIEW READY and names one SHA raises no STUCK."""
    reset_db()
    st, screen = review_pair("ur")
    update("ur", "blocker", "Waiting on: review. REVIEW READY 1a2b3c4")
    world(screen)
    tick(st)
    wake = "dev-auto: REVIEW READY was posted on ticket ur. Review it as your ticket says."
    assert sends() == [("term-ur-review", woke("ur-review", wake, "codex"), True)], sends()


@scenario
def wait_review_verdict_after_note():
    """A verdict newer than the wait note wakes the coder and raises no STUCK."""
    reset_db()
    st, screen = review_pair("uv")
    update("uv", "blocker", NOTE_6970)
    update("uv", "finding", "REVIEW VERDICT: FIX 1a2b3c4", "codex:uv-review")
    world(screen)
    tick(st)
    wake = "dev-auto: a review verdict was posted on ticket uv. Read it there and continue."
    assert sends() == [("term-uv", woke("uv", wake), True)], sends()


@scenario
def wait_review_after_fix_without_request():
    """After a FIX verdict, a new review wait with no new request is one STUCK."""
    reset_db()
    st, screen = review_pair("uf")
    update("uf", "progress", "REVIEW READY 1a2b3c4")
    update("uf", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:uf-review")
    note = update("uf", "blocker", "Waiting on: review — review_ready again, no new SHA")
    world(screen)
    tick(st)
    stuck = review_stuck("uf", note, "has no REVIEW READY with a SHA")
    wake = (
        "term-uf",
        woke("uf", "dev-auto: a review verdict was posted on ticket uf. Read it there and continue."),
        True,
    )
    assert sorted(sends()) == sorted([wake, stuck]), sends()
    world(screen)
    tick(st)
    assert sends() == [], "that note raises nothing more"


@scenario
def wait_review_untracked_without_request_or_ticket():
    """A review wait with no request and no review ticket is one STUCK."""
    reset_db()
    ticket_row("missing")
    note = update("missing", "blocker", NOTE_6970)
    st = fresh_state(missing=rec("missing", "c1"))
    screen = local(term("term-missing", 100))
    world(screen)
    tick(st)
    assert sends() == [review_stuck("missing", note, "has no review ticket")], sends()
    world(screen)
    tick(st)
    assert sends() == []


@scenario
def wait_review_without_review_ticket():
    """The live coder row holds a missing-ticket STUCK until the review becomes trackable."""
    reset_db()
    ticket_row("un")
    update("un", "progress", "REVIEW READY 1a2b3c4", at=NOW - 90)
    note = update("un", "blocker", "Waiting on: review of 1a2b3c4", at=NOW - 80)
    st = fresh_state(un=rec("un", "c1"))
    screen = local(term("term-un", 100))

    def coder_status():
        rows = json.loads((root / "supervisor" / "status.json").read_text())["rows"]
        status = next(row["status"] for row in rows if row["ticket"] == "un")
        return status["state"], status["detail"]

    stuck = review_stuck("un", note, "has no review ticket")
    world(screen)
    tick(st)
    assert sends() == [stuck], sends()
    assert coder_status() == ("stuck", stuck[1])
    history = next(line for line in event_lines() if line.endswith(stuck[1]))
    world(screen)
    tick(st, NOW + 60)
    assert sends() == []
    assert coder_status() == ("stuck", stuck[1])

    ticket_row("un-review", agent="codex:un-review")
    st["workers"]["un-review"] = rec("un-review", "x1", agent="codex", model="gpt-6.1-sol")
    world(screen + local(term("term-un-review", 5, ident="codex")))
    tick(st, NOW + 120)
    assert coder_status() == ("waiting", "review")
    wake = "dev-auto: REVIEW READY was posted on ticket un. Review it as your ticket says."
    assert sends() == [("term-un-review", woke("un-review", wake, "codex"), True)], sends()
    assert history in event_lines()


@scenario
def wait_review_two_shas():
    """A review-wait line with REVIEW READY and two different SHAs is one STUCK."""
    reset_db()
    st, screen = review_pair("ut")
    note = update("ut", "blocker", "Waiting on: review. REVIEW READY 1a2b3c4 or 5e6f7a8b")
    world(screen)
    tick(st)
    assert sends() == [review_stuck("ut", note, "has no REVIEW READY with a SHA")], sends()


@scenario
def wait_review_request_after_stuck_wakes_reviewer():
    """A review request posted after the untracked STUCK wakes the reviewer as usual."""
    reset_db()
    st, screen = review_pair("uk")
    update("uk", "blocker", NOTE_6970)
    world(screen)
    tick(st)
    assert any(s[1].startswith("STUCK uk:") for s in sends()), sends()
    update("uk", "progress", "REVIEW READY 1a2b3c4")
    world(screen)
    tick(st)
    wake = "dev-auto: REVIEW READY was posted on ticket uk. Review it as your ticket says."
    assert sends() == [("term-uk-review", woke("uk-review", wake, "codex"), True)], sends()


@scenario
def verdict_wakes_without_a_wait_note():
    """Live (update 1492, dashboard-served-pages-2-04): a coder posted REVIEW READY with no
    Waiting on note and its FIX verdict woke nobody. A verdict someone else posts after
    the coder's last update wakes it once, FIX or PUSH, without a wait note."""
    reset_db()
    ticket_row("nw")
    update("nw", "progress", "REVIEW READY 1a2b3c4")
    st = fresh_state(nw=rec("nw", "c1"))
    screen = local(term("term-nw", 100))
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("nw", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:nw-r")
    world(screen)
    tick(st)
    text = "dev-auto: a review verdict was posted on ticket nw. Read it there and continue."
    wake = ("term-nw", woke("nw", text), True)
    assert sends() == [wake], sends()
    world(screen)
    tick(st)
    assert sends() == [], "that verdict wakes once"
    update("nw", "progress", "REVIEW READY 5e6f7a8")
    push = update("nw", "finding", "REVIEW VERDICT: PUSH 5e6f7a8", "codex:nw-r")
    world(screen)
    tick(st)
    cleared = ("term-orch", "CLEARED REVIEW nw: PUSH 5e6f7a8", True)
    # A repeat of a typed text carries its event.
    again = ("term-nw", woke("nw", f"{text} Event verdict {push}."), True)
    assert sorted(sends()) == [again, cleared], sends()
    update("nw", "finding", "REVIEW VERDICT: PUSH 5e6f7a8 received, merging")
    world(screen)
    tick(st)
    assert sends() == [], "its own receipt wakes nothing"


STANDALONE = "Outcome: one fix.\nBranch: `fix/x` · Feature branch: none (PR into dot-agents main)"


@scenario
def sdk_worker_screen_is_its_agent_and_takes_wakes():
    """Orca lists no agent for the runner, so its busy and idle lines are its agent's
    screen: no STUCK for an unknown screen, and a verdict wakes it with a typed /goal
    line, as a Claude session."""
    assert da.agent_ui("sdk", ["⏺ Bash", runner.BUSY])
    assert da.agent_ui("sdk", ["⏺ Waiting on: review recorded", runner.IDLE, "›"])
    assert not da.agent_ui("sdk", ["julian@host:~/wt$"])
    assert not da.agent_ui("sdk", [f"  {runner.IDLE}"]), "the worker's own text quoting it"
    assert not da.agent_ui("claude", [runner.IDLE])
    reset_db()
    ticket_row("sw", agent="sdk:sw")
    update("sw", "progress", "REVIEW READY 1a2b3c4", "sdk:sw")
    st = fresh_state(sw=rec("sw", "sdk1", agent="sdk", agent_id="sdk:sw", session=None))
    idle = ["⏺ Recorded REVIEW READY 1a2b3c4", runner.IDLE, "›"]
    screen = local(term("term-sw", 100, idle, ident=None))
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("sw", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:sw-r")
    world(screen)
    tick(st)
    text = "dev-auto: a review verdict was posted on ticket sw. Read it there and continue."
    assert sends() == [("term-sw", woke("sw", text, agent="sdk"), True)], sends()
    assert sends()[0][1].startswith("/goal Finish Concord ticket sw ")
    # It has no subscription limit, so a quoted limit message moves nothing.
    quoted = ["⏺ The old log says:", "  You've hit your 5-hour limit", runner.IDLE, "›"]
    world(local(term("term-sw", 100, quoted, ident=None)))
    tick(st)
    assert st["limits"] == {} and "limit_reset" not in st["workers"]["sw"], st
    assert sends() == [], sends()


def commit_in(path):
    """A real commit in the git repo at path, made there first when needed; its SHA."""
    path = Path(path)
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run([GIT, "init", "-q", str(path)], check=True, capture_output=True)
    git_at(path, "commit", "-q", "--allow-empty", "-m", "c")
    return head_of(path, "HEAD")


@scenario
def standalone_push_wake_tells_coder_to_finish():
    """Live (dev-auto-fixes-06, -07, -11 and -13): woken on its PUSH with "Read it there and
    continue", a standalone fix ticket's coder parked at review_ready or an operator wait
    until the orchestrator shipped. With no feature branch, the PUSH wake tells it to
    record the PR text and complete, and the goal it sets is not met before that."""
    reset_db()
    ticket_row("sa", notes=STANDALONE)
    ticket_row("sa-review", agent="codex:sa-review")
    st = fresh_state(sa=rec("sa", "c1"))
    sha = commit_in(st["workers"]["sa"]["worktree"])
    update("sa", "progress", f"REVIEW READY {sha[:7]}")
    update("sa", "blocker", f"Waiting on: review of {sha[:7]}")
    screen = local(term("term-sa", 100))
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("sa", "finding", f"REVIEW VERDICT: PUSH {sha}", "codex:sa-review")
    world(screen, git={}, git_bin=GIT)
    tick(st)
    typed = (
        "/goal Finish Concord ticket sa as its brief defines done, which with no feature "
        "branch means its PR title and body recorded on it and the ticket completed with "
        "finish_work outcome complete, not REVIEW READY, a review_ready outcome or a wait "
        "for the orchestrator to push, or record a new Waiting on: blocker note on it "
        "after this goal is set, then end your turn. If the Concord tools are "
        "unavailable, the goal is also met once you write the handoff to the file the "
        "handoff skill names for a session without a ticket and end your turn, without "
        "retrying them. dev-auto: REVIEW VERDICT: PUSH was posted on ticket sa, which "
        "has no feature branch. Read it, run done-check, record the PR title and body on "
        "the ticket, then complete the ticket with finish_work outcome complete. The "
        "orchestrator pushes and opens the PR."
    )
    cleared = ("term-orch", f"CLEARED REVIEW sa: PUSH {sha}", True)
    assert sorted(sends()) == sorted([("term-sa", typed, True), cleared]), sends()


@scenario
def standalone_fix_verdict_wakes_as_today():
    """A FIX verdict on a standalone ticket wakes its coder as before, and so does a FIX
    after a PUSH that told it to finish."""
    reset_db()
    ticket_row("sx", notes=STANDALONE)
    update("sx", "progress", "REVIEW READY 1a2b3c4")
    update("sx", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(sx=rec("sx", "c1"))
    screen = local(term("term-sx", 100))
    text = "dev-auto: a review verdict was posted on ticket sx. Read it there and continue."
    today = ("term-sx", woke("sx", text), True)
    update("sx", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:sx-review")
    world(screen)
    tick(st)
    assert sends() == [today], sends()
    sha = commit_in(st["workers"]["sx"]["worktree"])
    update("sx", "progress", f"REVIEW READY {sha[:7]}")
    update("sx", "blocker", f"Waiting on: review of {sha[:7]}")
    update("sx", "finding", f"REVIEW VERDICT: PUSH {sha}", "codex:sx-review")
    world(screen, git={}, git_bin=GIT)
    tick(st)
    finish = [s for s in sends() if s[0] == "term-sx"]
    assert len(finish) == 1 and "finish_work outcome complete" in finish[0][1], finish
    update("sx", "progress", "REVIEW READY 9b8c7d6, rebased")
    update("sx", "blocker", "Waiting on: review of 9b8c7d6")
    fix = update("sx", "finding", "REVIEW VERDICT: FIX 9b8c7d6 one finding", "codex:sx-review")
    world(screen)
    tick(st)
    # A repeat of a typed text carries its event.
    again = ("term-sx", woke("sx", f"{text} Event verdict {fix}."), True)
    assert sends() == [again], sends()


@scenario
def withdrawn_push_disarms_the_finish_goal():
    """A standalone coder armed to finish by a PUSH that is then superseded is disarmed on
    the next tick, and its unshown PUSH wake is not typed again. A replacement PUSH on the
    same prefix does not inherit that finish: it earns its own proof."""
    reset_db()
    ticket_row("wf", notes=STANDALONE)
    ready = update("wf", "progress", "REVIEW READY 1a2b3c4")
    push = update("wf", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:wf-review")
    proof = {"update": push, "commit": "1a2b3c4000000000000000000000000000000000", "request": ready}
    st = fresh_state(wf=rec("wf", "c1", finish=proof))
    st["workers"]["wf"]["unshown"] = [
        {"key": f"wf|verdict|{push}", "text": "finish it", "at": 0, "update": push, "output": None}
    ]
    supersede(push, update("wf", "decision", "withdrawn", "codex:wf-review"))
    world(local(term("term-wf", 100)))
    tick(st)
    assert "finish" not in st["workers"]["wf"], st["workers"]["wf"]
    assert "unshown" not in st["workers"]["wf"], st["workers"]["wf"]
    assert not [x for x in sends() if "finish it" in x[1]], sends()
    reset_db()
    ticket_row("wf", notes=STANDALONE)
    push = update("wf", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:wf-review")
    st = fresh_state(wf=rec("wf", "c1", finish={"update": push, "commit": "1a2b3c4000000000000000000000000000000000", "request": None}))
    supersede(push, update("wf", "decision", "withdrawn", "codex:wf-review"))
    update("wf", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:wf-review")
    world(local(term("term-wf", 100)))
    tick(st)
    assert "finish" not in st["workers"]["wf"], ("a replacement PUSH inherits it", st["workers"]["wf"])


@scenario
def push_on_another_sha_than_the_request_never_finishes():
    """REVIEW READY B and its wait, then a standing PUSH on A: the standalone coder gets
    the plain verdict wake, not the finish one, and is not armed to finish. A finish armed
    by A's PUSH is dropped once the coder asks for review of B. Every case is checked;
    the failing ones are listed."""
    reset_db()
    ticket_row("wb", notes=STANDALONE)
    update("wb", "progress", "REVIEW READY 5e6f7a8")
    update("wb", "blocker", "Waiting on: review of 5e6f7a8")
    st = fresh_state(wb=rec("wb", "c1"))
    screen = local(term("term-wb", 100))
    world(screen)
    tick(st)
    update("wb", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:wb-review")
    world(screen)
    tick(st)
    bad = []
    if any("finish_work outcome complete" in t for _w, t, _e in sends()):
        bad.append("told to finish on A")
    if st["workers"]["wb"].get("finish"):
        bad.append("armed to finish by A")
    reset_db()
    ticket_row("wb", notes=STANDALONE)
    ready = update("wb", "progress", "REVIEW READY 1a2b3c4")
    push = update("wb", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:wb-review")
    update("wb", "progress", "REVIEW READY 5e6f7a8")
    proof = {"update": push, "commit": "1a2b3c4000000000000000000000000000000000", "request": ready}
    st = fresh_state(wb=rec("wb", "c1", finish=proof))
    world(screen)
    tick(st)
    if st["workers"]["wb"].get("finish"):
        bad.append("finish kept after REVIEW READY B")
    assert not bad, bad


@scenario
def finish_drops_when_a_new_request_is_unproven():
    """A finish proven for a full REVIEW READY A and a full PUSH A is dropped once the coder
    posts REVIEW READY with a short prefix A shares with another commit: that request is
    unproven, so the same PUSH cannot carry the finish to it."""
    reset_db()
    ticket_row("wn", notes=STANDALONE)
    st = fresh_state(wn=rec("wn", "c1"))
    worktree = st["workers"]["wn"]["worktree"]
    a, _ = sha_twins(worktree, commit_in(worktree))
    update("wn", "progress", f"REVIEW READY {a}")
    update("wn", "blocker", f"Waiting on: review of {a}")
    update("wn", "finding", f"REVIEW VERDICT: PUSH {a}", "codex:wn-review")
    screen = local(term("term-wn", 100))
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert st["workers"]["wn"].get("finish"), ("A is proven", sends())
    update("wn", "progress", f"REVIEW READY {a[:7]}")
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert not st["workers"]["wn"].get("finish"), st["workers"]["wn"]


@scenario
def feature_branch_push_wakes_as_today():
    """A PUSH on a ticket that merges into a feature branch wakes its coder as before."""
    reset_db()
    ticket_row("fb", notes="Branch: `task/f/a` · Feature branch: `feature/f`")
    update("fb", "progress", "REVIEW READY 1a2b3c4")
    update("fb", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(fb=rec("fb", "c1"))
    screen = local(term("term-fb", 100))
    update("fb", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:fb-review")
    world(screen)
    tick(st)
    text = "dev-auto: a review verdict was posted on ticket fb. Read it there and continue."
    cleared = ("term-orch", "CLEARED REVIEW fb: PUSH 1a2b3c4", True)
    assert sorted(sends()) == sorted([("term-fb", woke("fb", text), True), cleared]), sends()


@scenario
def owner_push_receipt_is_no_verdict():
    """Live (ocr-form-spec-lockout-02 updates 405 and 407): the coder's receipt of a PUSH
    starts with the verdict prefix and raised a second CLEARED REVIEW. Only a verdict
    someone other than the ticket's owner wrote counts."""
    reset_db()
    ticket_row("rc")
    ticket_row("rc-review", agent="codex:rc-review")
    update("rc", "progress", "REVIEW READY 1a2b3c4")
    update("rc", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(rc=rec("rc", "c1"))
    screen = local(term("term-rc", 100))
    world(screen)
    tick(st)
    assert sends() == [], sends()
    push = update("rc", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:rc-r")
    receipt = (
        f"REVIEW VERDICT: PUSH 1a2b3c4 received (update {push}). Done-check passed."
    )
    update("rc", "finding", receipt)
    world(screen)
    tick(st)
    wake = "dev-auto: a review verdict was posted on ticket rc. Read it there and continue."
    assert sorted(sends()) == [
        ("term-orch", "CLEARED REVIEW rc: PUSH 1a2b3c4", True)
    ], sends()
    update("rc", "progress", "REVIEW READY 5e6f7a8")
    update("rc", "blocker", "Waiting on: review of 5e6f7a8")
    world(screen)
    tick(st)
    assert sends() == [], "its own receipt is no verdict to wake it for"
    assert wake not in notes()


@scenario
def withdrawn_push_raises_no_cleared_review_or_finish():
    """A PUSH superseded or followed by a FIX on its SHA, an indented PUSH or one misspelt
    PUSHED raises no CLEARED REVIEW, and its standalone coder is not told to finish; a
    queued CLEARED REVIEW whose PUSH is withdrawn before it is sent is dropped. Every case
    is checked; the failing ones are listed."""
    reset_db()
    ticket_row("cw", notes=STANDALONE)
    st = fresh_state(cw=rec("cw", "c1"))
    screen = local(term("term-cw", 100))
    world(screen)
    tick(st)
    bad = []

    def say(text):
        return update("cw", "finding", text, agent="codex:cw-r")

    for n, withdraw in enumerate(("superseded", "fix", "indented", "pushed")):
        sha = f"{n}a2b3c4"
        update("cw", "progress", f"REVIEW READY {sha}")
        update("cw", "blocker", f"Waiting on: review of {sha}")
        if withdraw == "superseded":
            supersede(say(f"REVIEW VERDICT: PUSH {sha}"), say("withdrawn"))
        elif withdraw == "fix":
            say(f"REVIEW VERDICT: PUSH {sha}")
            say(f"REVIEW VERDICT: FIX {sha} missed a check")
        else:
            say(f"  REVIEW VERDICT: PUSH {sha}" if withdraw == "indented" else f"REVIEW VERDICT: PUSHED {sha}")
        world(screen)
        tick(st)
        if any("CLEARED REVIEW" in t for _w, t, _e in sends()):
            bad.append(f"{withdraw}: CLEARED REVIEW")
        if any("finish_work outcome complete" in t for _w, t, _e in sends()):
            bad.append(f"{withdraw}: told to finish")
    push = say("REVIEW VERDICT: PUSH 9a8b7c6")
    st["cleared_pending"][f"cw|cleared|{push}"] = ["cw", "CLEARED REVIEW cw: PUSH 9a8b7c6", 0]
    supersede(push, say("withdrawn"))
    world(screen)
    tick(st)
    if any("CLEARED REVIEW" in t for _w, t, _e in sends()):
        bad.append("queued: CLEARED REVIEW")
    assert not bad, bad


@scenario
def two_push_rounds_on_one_sha():
    """Live (workspace-conventions-03 updates 123 and 340): two review rounds on one SHA
    each raise their own CLEARED REVIEW and wake the coder."""
    reset_db()
    ticket_row("tw")
    st = fresh_state(tw=rec("tw", "c1"))
    screen = local(term("term-tw", 100))
    cleared = ("term-orch", "CLEARED REVIEW tw: PUSH 1a2b3c4", True)
    text = "dev-auto: a review verdict was posted on ticket tw. Read it there and continue."
    for round_, ready in enumerate(("REVIEW READY 1a2b3c4", "REVIEW READY 1a2b3c4 again")):
        update("tw", "progress", ready)
        update("tw", "blocker", "Waiting on: review of 1a2b3c4")
        verdict = update("tw", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:tw-review")
        world(screen)
        tick(st)
        # The second round repeats a typed text, so it carries its event.
        wake = text if round_ == 0 else f"{text} Event verdict {verdict}."
        both = sorted([cleared, ("term-tw", woke("tw", wake), True)])
        assert sorted(sends()) == both, sends()


@scenario
def held_cleared_review_raises_one_stuck():
    """Live (dev-auto-fixes-05 update 1645): the orchestrator sat off its prompt for 79
    minutes and its CLEARED REVIEW was held each tick with no trace anywhere. It now goes
    to the events file at once, so nothing is held and no STUCK is raised; its EVENTS
    wake waits for the prompt."""
    reset_db()
    ticket_row("hb")
    update("hb", "progress", "REVIEW READY 1a2b3c4")
    update("hb", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(hb=rec("hb", "c1"))
    screen = local(term("term-hb", 100))
    world(screen)
    tick(st)
    update("hb", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:hb-r")
    stuck = (
        "dev-auto: STUCK hb: CLEARED REVIEW hb: PUSH 1a2b3c4 has not reached the "
        "orchestrator after 3 tries. Tried again each tick"
    )
    cleared = ("term-orch", "CLEARED REVIEW hb: PUSH 1a2b3c4", True)
    for n in range(4):
        world(screen, orch_tail=CLAUDE_PERMISSION)
        tick(st)
        orch = [s for s in sends() if s[0] == "term-orch"]
        assert orch == ([cleared] if n == 0 else []), (n, sends())
        assert sends(events=False) == [s for s in sends() if s[0] != "term-orch"], "no wake yet"
        assert stuck not in notes(), notes()
    world(screen)
    tick(st)
    assert [s[1][:7] for s in sends(events=False) if s[0] == "term-orch"] == ["EVENTS:"]
    world(screen)
    tick(st)
    assert sends() == [] and stuck not in notes(), "delivered once"


@scenario
def cleared_review_outlives_its_record():
    """A coder may finish its own ticket as its PUSH lands. When that tick cannot send
    its EVENTS wake, cleanup still removes its merged worktree and drops the record at
    once, so it holds no slot; the CLEARED REVIEW is in the events file once and the next
    tick types the wake."""
    reset_db()
    ticket_row("cu", status="complete")
    update("cu", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:cu-r")
    st = fresh_state(cu=rec("cu", "c1"))
    world([], send_fails=["term-orch"], git=git, gh=merged)
    tick(st)
    assert st["workers"] == {}, st["workers"]
    assert sends() == [("term-orch", "CLEARED REVIEW cu: PUSH 1a2b3c4", True)], sends()
    world([])
    tick(st)
    assert [s[1][:7] for s in sends(events=False)] == ["EVENTS:"] and sends() == [], sends()
    world([])
    tick(st)
    assert sends(events=False) == [], sends(events=False)


@scenario
def push_after_cleanup_still_clears():
    """A PUSH posted on a ticket whose record was cleaned up, or that never had one, is
    one CLEARED REVIEW. A first run starts after the ledger's newest update."""
    reset_db()
    ticket_row("ac", status="complete")
    old = update("ac", "finding", "REVIEW VERDICT: PUSH 0a0b0c0", agent="codex:ac-r")
    st = fresh_state(ac=rec("ac", "c1", since=old))
    del st["cleared_floor"]
    world([], git=git, gh=merged)
    tick(st)
    assert sends() == [] and st["workers"] == {}, (sends(), st["workers"])
    update("ac", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:ac-r")
    world([])
    tick(st)
    assert sends() == [("term-orch", "CLEARED REVIEW ac: PUSH 1a2b3c4", True)], sends()
    world([])
    tick(st)
    assert sends() == [], sends()


FEATURE_NOTES = "Branch: `task/feat/x` · Feature branch: `feature/feat`"


def head_of(checkout, ref):
    argv = [GIT, "-C", str(checkout), "rev-parse", ref]
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def feature_repo(name):
    """A real checkout on feature/feat, which origin also has."""
    main = real_main(name)
    git_at(main, "checkout", "-q", "-b", "feature/feat")
    git_at(main, "push", "-q", "origin", "feature/feat")
    return main


def task_commit(main, branch):
    """One commit on a task branch off feature/feat, left unmerged; its SHA."""
    git_at(main, "checkout", "-q", "-b", branch, "feature/feat")
    (main / branch.replace("/", "-")).write_text(branch)
    git_at(main, "add", "-A")
    git_at(main, "commit", "-q", "-m", branch)
    git_at(main, "checkout", "-q", "feature/feat")
    return head_of(main, branch)


def merge_task(main, sha):
    """The coder's `git merge --no-ff` into feature/feat; the new head."""
    git_at(main, "merge", "-q", "--no-ff", "-m", f"merge {sha[:7]}", sha)
    return head_of(main, "feature/feat")


def unpushed_sends():
    return [s for s in sends() if s[1].startswith("UNPUSHED")]


@scenario
def unpushed_merge_raises_one_event_per_head():
    """Live (dev-auto-fixes-30): three merges of cleared task branches sat unpushed for
    40 to 80 minutes; the orchestrator found them with a hand-run script. A feature
    branch ahead of origin whose head merged a PUSH-cleared SHA raises one UNPUSHED with
    ticket, host, branch, SHA and ahead count; the same head never again, a new one once."""
    reset_db()
    main = feature_repo("unpushed-head")
    task = task_commit(main, "task/feat/um")
    ticket_row("um", notes=FEATURE_NOTES)
    update("um", "finding", f"REVIEW VERDICT: PUSH {task[:7]}", agent="codex:um-r")
    st = fresh_state(um=rec("um", "c1", worktree=str(main)))
    screen = local(term("term-um", 100, worktree=str(main)))
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "cleared, not merged yet"
    head = merge_task(main, task)
    world(screen, git={}, git_bin=GIT)
    tick(st)
    text = f"UNPUSHED um: feature/feat on local at {head[:7]}, 2 commits ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "the same head raises nothing"
    fix = task_commit(main, "task/feat/um-fix")
    update("um", "finding", f"REVIEW VERDICT: PUSH {fix}", agent="codex:um-r")
    newer = merge_task(main, fix)
    world(screen, git={}, git_bin=GIT)
    tick(st)
    text = f"UNPUSHED um: feature/feat on local at {newer[:7]}, 4 commits ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()


@scenario
def unpushed_skips_pushed_uncleared_and_unread_branches():
    """A pushed branch, a head whose merged SHA no reviewer cleared, and a host that
    cannot be read raise no UNPUSHED; the remote host once readable raises it."""
    reset_db()
    main = feature_repo("unpushed-quiet")
    task = task_commit(main, "task/feat/pq")
    ticket_row("pq", notes=FEATURE_NOTES)
    update("pq", "finding", f"REVIEW VERDICT: PUSH {task[:7]}", agent="codex:pq-r")
    merge_task(main, task)
    git_at(main, "push", "-q", "origin", "feature/feat")
    st = fresh_state(pq=rec("pq", "c1", worktree=str(main)))
    screen = local(term("term-pq", 100, worktree=str(main)))
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "pushed"
    other = task_commit(main, "task/feat/other")
    update("pq", "finding", f"REVIEW VERDICT: PUSH {other} received")
    update("pq", "finding", f"REVIEW VERDICT: FIX {other}", agent="codex:pq-r")
    merge_task(main, other)
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "its head merged a SHA no reviewer cleared"
    reset_db()
    ticket_row("wu", notes=FEATURE_NOTES)
    update("wu", "finding", f"REVIEW VERDICT: PUSH {other}", agent="codex:wu-r")
    st = fresh_state(wu=rec("wu", "c1", host="Juzzark-WSL", worktree=str(main)))
    row, shown = term("term-wu", 100, worktree=str(main))
    world([("Juzzark-WSL", "term-wu", row, shown)], git={}, git_bin=GIT, probe_fails=True)
    tick(st)
    assert unpushed_sends() == [], "an unread host"
    world([("Juzzark-WSL", "term-wu", row, shown)], git={}, git_bin=GIT)
    tick(st)
    head = head_of(main, "feature/feat")
    text = f"UNPUSHED wu: feature/feat on Juzzark-WSL at {head[:7]}, 2 commits ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()


@scenario
def unpushed_fix_branch_waits_for_its_first_push():
    """With `Feature branch: none`, the ticket's own Branch holding its cleared SHA and
    not yet on origin raises one UNPUSHED."""
    reset_db()
    main = real_main("unpushed-fix")
    git_at(main, "checkout", "-q", "-b", "fix/x")
    (main / "fix").write_text("fix\n")
    git_at(main, "add", "fix")
    git_at(main, "commit", "-q", "-m", "fix")
    sha = head_of(main, "fix/x")
    ticket_row("fx", notes=STANDALONE)
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha[:7]}", agent="codex:fx-r")
    st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
    screen = local(term("term-fx", 100, worktree=str(main)))
    world(screen, git={}, git_bin=GIT)
    tick(st)
    text = f"UNPUSHED fx: fix/x on local at {sha[:7]}, 1 commit ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()
    world(screen, git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], sends()


@scenario
def unpushed_reads_the_branch_against_its_own_origin_ref():
    """Review da-3-12: the cleared fix head was on origin/development while origin's
    fix/x was absent or behind, and counting against every origin ref found nothing to
    push. The branch is read against its own origin ref; with none, a head origin has
    elsewhere is still not on origin's branch, unless origin's default branch has it."""
    for name, behind in (("unpushed-elsewhere", False), ("unpushed-behind", True), ("unpushed-merged", None)):
        reset_db()
        main = real_main(name)
        git_at(main, "checkout", "-q", "-b", "fix/x")
        (main / "fix").write_text("fix\n")
        git_at(main, "add", "fix")
        git_at(main, "commit", "-q", "-m", "fix")
        sha = head_of(main, "fix/x")
        git_at(main, "push", "-q", "origin", f"{sha}:refs/heads/development")
        if behind:
            git_at(main, "push", "-q", "origin", "staging:refs/heads/fix/x")
        if behind is None:
            # Merged into the default branch, and origin's fix/x deleted and pruned.
            git_at(main, "push", "-q", "origin", f"{sha}:refs/heads/staging")
        git_at(main, "fetch", "-q", "origin")
        git_at(main, "remote", "set-head", "origin", "staging")
        ticket_row("fx", notes=STANDALONE)
        update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
        st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
        world(local(term("term-fx", 100, worktree=str(main))), git={}, git_bin=GIT)
        tick(st)
        where = "1 commit ahead of origin" if behind else "not on origin"
        text = f"UNPUSHED fx: fix/x on local at {sha[:7]}, {where}"
        expected = [] if behind is None else [("term-orch", text, True)]
        assert unpushed_sends() == expected, (name, sends())


def sha_twins(main, tip):
    """Two full SHAs of commits on `tip`, with its tree, that share their first 7 digits:
    80000 commits make some such pair all but certain."""
    stream = "".join(
        f"commit refs/heads/many\nmark :{n}\ncommitter t <t> 0 +0000\ndata {len(str(n))}\n{n}\nfrom {tip}\n\n"
        for n in range(1, 80001)
    )
    marks = Path(main) / ".git" / "twin-marks"
    subprocess.run(
        [GIT, "-C", str(main), "fast-import", "--quiet", f"--export-marks={marks}"],
        input=stream, check=True, capture_output=True, text=True,
    )
    seen = {}
    for line in marks.read_text().splitlines():
        full = line.split()[1]
        if full[:7] in seen:
            return seen[full[:7]], full
        seen[full[:7]] = full
    raise AssertionError("no two of 80000 commits share 7 digits")


@scenario
def unpushed_needs_a_standing_push():
    """UNPUSHED needs a standing PUSH that names the head as one commit in its repo. A
    PUSH a later FIX on its SHA withdrew, a superseded one, PUSHED, or a short SHA that
    names two commits raises none, and a queued UNPUSHED whose own PUSH is withdrawn is
    dropped though another PUSH stands on the ticket, in the queue's current form or the
    three-field one before it. A new PUSH after a FIX raises it, also while its first
    notice is still queued, and a three-field notice still backed is raised again. The
    ambiguous short PUSH does not tell the standalone coder to finish, even with its full
    SHA requested; a unique short one does. A queued notice stands in for a tick whose
    send was cut short. Every case is checked; the failing ones are listed."""
    bad = []
    say = lambda text: update("fx", "finding", text, agent="codex:fx-r")  # noqa: E731
    raised = {"re-push", "re-push-queued", "legacy", "short", "ambiguous-request", "two-eligible"}
    finishing = ("ambiguous", "short", "missing-full", "tree-full", "ambiguous-request")
    cases = (
        "fix", "superseded", "pushed", "queued", "queued-other", "legacy-other", "ambiguous",
        "re-push", "re-push-queued", "legacy", "short", "missing-full", "tree-full",
        "ambiguous-request", "shared-head", "two-projects", "two-eligible", "stale-older",
    )
    for n, case in enumerate(cases):
        reset_db()
        main = real_main(f"unpushed-withdrawn-{n}")
        git_at(main, "checkout", "-q", "-b", "fix/x")
        if case in ("ambiguous", "ambiguous-request"):
            _, sha = sha_twins(main, head_of(main, "HEAD"))
            git_at(main, "reset", "-q", "--hard", sha)
        else:
            (main / "fix").write_text("fix\n")
            git_at(main, "add", "fix")
            git_at(main, "commit", "-q", "-m", "fix")
            sha = head_of(main, "fix/x")
        ticket_row("fx", notes=STANDALONE)
        st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
        screen = local(term("term-fx", 100, worktree=str(main)))
        text = f"UNPUSHED fx: fix/x at {sha[:7]}"

        def queue(*backs):
            st["cleared_pending"] = {f"fx|unpushed|{sha}": ["fx", text, 0, *backs]}
            st["unpushed"] = {"|fx": {"host": "local", "dir": str(main), "head": sha}}

        if case in ("queued-other", "legacy-other"):
            say("REVIEW VERDICT: PUSH 5e6f7a8")
        if case in ("fix", "re-push", "re-push-queued"):
            first = say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            if case == "re-push-queued":
                queue([first])
            say(f"REVIEW VERDICT: FIX {sha[:7]} missed a check")
            if case != "fix":
                say(f"REVIEW VERDICT: PUSH {sha}")
        elif case == "pushed":
            say(f"REVIEW VERDICT: PUSHED {sha[:7]}")
        elif case in ("ambiguous", "short"):
            update("fx", "progress", f"REVIEW READY {sha if case == 'ambiguous' else sha[:7]}")
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
        elif case == "ambiguous-request":
            update("fx", "progress", f"REVIEW READY {sha[:7]}")
            say(f"REVIEW VERDICT: PUSH {sha}")
        elif case in ("missing-full", "tree-full"):
            token = "deadbeef" * 5 if case == "missing-full" else head_of(main, "HEAD^{tree}")
            update("fx", "progress", f"REVIEW READY {token}")
            say(f"REVIEW VERDICT: PUSH {token}")
        elif case == "two-projects":
            # A stale notice of p1's ticket fx must not reset this project's fx at the same head.
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            here = {"host": "local", "dir": str(main), "head": sha}
            st["unpushed"] = {"|fx": here, "p1|fx": dict(here)}
            st["cleared_pending"] = {f"fx|unpushed|{sha}": ["fx", f"UNPUSHED fx: at {sha[:7]}", 0, [], "p1|fx"]}
        elif case == "two-eligible":
            # The same ticket id at the same head in two projects: two notices, neither lost.
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            ticket_row("fx", notes=STANDALONE, project="p1")
            update("fx", "finding", f"REVIEW VERDICT: PUSH {sha[:7]}", agent="codex:fx-r", project="p1")
            st["unpushed"] = {"p1|fx": {"host": "local", "dir": str(main)}}
        elif case == "stale-older":
            # A's notice, still queued, loses its PUSH after B's notice went out: B stays marked.
            older = sha
            (main / "more").write_text("more\n")
            git_at(main, "add", "more")
            git_at(main, "commit", "-q", "-m", "more")
            sha = head_of(main, "fix/x")
            withdrawn = say(f"REVIEW VERDICT: PUSH {older[:7]}")
            supersede(withdrawn, update("fx", "decision", "withdrawn", agent="codex:fx-r"))
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            st["unpushed"] = {"|fx": {"host": "local", "dir": str(main), "head": sha}}
            st["cleared_pending"] = {
                f"fx|unpushed|{older}|": ["fx", f"UNPUSHED fx: at {older[:7]}", 0, [withdrawn], "|fx"]
            }
        elif case == "shared-head":
            # fx's notice went out; fy's stale one for the same head is dropped alone.
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            st["unpushed"] = {"|fx": {"host": "local", "dir": str(main), "head": sha}}
            st["cleared_pending"] = {f"fy|unpushed|{sha}": ["fy", f"UNPUSHED fy: at {sha[:7]}", 0]}
        elif case == "legacy":
            say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            queue()
        else:
            push = say(f"REVIEW VERDICT: PUSH {sha[:7]}")
            if case != "superseded":
                queue(*([] if case == "legacy-other" else [[push]]))
            supersede(push, update("fx", "decision", "withdrawn", agent="codex:fx-r"))
        got, finish = [], False
        try:
            for _ in range(2):
                world(screen, git={}, git_bin=GIT)
                tick(st)
                got += unpushed_sends()
                finish = finish or any("finish_work outcome complete" in t for _w, t, _e in sends())
        except Exception as error:  # noqa: BLE001 - each case is reported, not the first
            bad.append(f"{case}: {type(error).__name__}: {error}")
            continue
        if bool(got) != (case in raised) or (case == "two-eligible" and len(got) != 2):
            bad.append(f"{case}: {len(got)} UNPUSHED")
        if case in finishing and finish != (case == "short"):
            bad.append(f"{case}: finish {finish}")
    assert not bad, bad


def gone_worktree(name, tid):
    """A ticket read once from its worktree, then merged, completed and its worktree and
    record gone; (checkout, state, merged head)."""
    reset_db()
    main = feature_repo(name)
    linked = tmp / name / "linked"
    git_at(main, "worktree", "add", "-q", "-b", f"task/feat/{tid}", str(linked), "feature/feat")
    (linked / tid).write_text(f"{tid}\n")
    git_at(linked, "add", tid)
    git_at(linked, "commit", "-q", "-m", tid)
    task = head_of(linked, "HEAD")
    ticket_row(tid, notes=FEATURE_NOTES)
    update(tid, "finding", f"REVIEW VERDICT: PUSH {task[:7]}", agent=f"codex:{tid}-r")
    st = fresh_state(**{tid: rec(tid, "c1", worktree=str(linked))})
    world(local(term(f"term-{tid}", 100, worktree=str(linked))), git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "not merged yet"
    head = merge_task(main, task)
    git_at(main, "worktree", "remove", str(linked))
    ticket_row(tid, status="complete", notes=FEATURE_NOTES)
    da.drop(st, tid)
    return main, st, head


@scenario
def unpushed_keeps_a_gone_worktree_through_an_unread_tick():
    """Reviews of 050e5fa, 0d2fd86 and 549ce67: an unread repo, an unread branch ref,
    and an unread ref whose cleared task alone is on origin each dropped the stored
    place of a ticket with no record, losing its event for good. Git tells a missing
    ref from an unreadable one no better, so only a read of the branch with nothing
    left to push forgets the ticket."""
    main, st, head = gone_worktree("unpushed-unread", "ur")
    away = main.with_name("away")
    main.rename(away)
    world([], git={}, git_bin=GIT)
    tick(st)
    assert unpushed_sends() == [], "its repo could not be read"
    away.rename(main)
    git_at(main, "push", "-q", "origin", "task/feat/ur")
    git_at(main, "checkout", "-q", "staging")
    refs = main / ".git" / "refs" / "heads" / "feature"
    refs.chmod(0)
    try:
        world([], git={}, git_bin=GIT)
        tick(st)
    finally:
        refs.chmod(0o755)
    assert unpushed_sends() == [], "its branch ref could not be read"
    world([], git={}, git_bin=GIT)
    tick(st)
    # Both the merge and its task, already on origin's task/feat/ur, are ahead of origin's feature/feat.
    text = f"UNPUSHED ur: feature/feat on local at {head[:7]}, 2 commits ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()
    git_at(main, "push", "-q", "origin", "feature/feat")
    world([], git={}, git_bin=GIT)
    tick(st)
    assert "|ur" not in st["unpushed"], st["unpushed"]


@scenario
def unpushed_covers_a_done_ticket_whose_worktree_is_gone():
    """A coder that merges, finishes and has its worktree removed leaves its feature
    branch on the host: it is still read there, through the repo's git dir."""
    _, st, head = gone_worktree("unpushed-gone", "gw")
    world([], git={}, git_bin=GIT)
    tick(st)
    text = f"UNPUSHED gw: feature/feat on local at {head[:7]}, 2 commits ahead of origin"
    assert unpushed_sends() == [("term-orch", text, True)], sends()


STAGING_FIX = "Branch: `fix/x` · Feature branch: none (standalone, own PR into staging)"
PR_TITLE, PR_BODY = "fix: one thing", "## Summary\n\nOne fix."
PR_TEXT = f"PR title: {PR_TITLE}\n\nPR body:\n\n{PR_BODY}"


GH_REPO = da.gh_repo


def fixture_gh_repo(url):
    """The fixtures' bare origins under tmp stand in for a GitHub repo named by their slug;
    every other URL goes through the real mapping."""
    if url.startswith(str(tmp) + "/"):
        found = da.ORIGIN_SLUG.search(url)
        return f"{found[1]}/{found[2]}"
    return GH_REPO(url)


def pushing(repos=None):
    """Turn auto_push on with these repo settings; the caller restores with pushing_off."""
    cfg["auto_push"], cfg["repos"] = True, repos or {}
    da.gh_repo = fixture_gh_repo


def pushing_off():
    cfg["auto_push"], cfg["repos"] = False, {}
    da.gh_repo = GH_REPO


def fix_repo(name, notes=STAGING_FIX, title=True, main_base=False):
    """A checkout on fix/x with one commit, cleared by its reviewer with the full SHA
    and, by default, its PR title and body recorded: (checkout, SHA, state, screen)."""
    reset_db()
    main = real_main(name)
    if main_base:
        git_at(main, "push", "-q", "origin", "HEAD:refs/heads/main")
    git_at(main, "checkout", "-q", "-b", "fix/x")
    (main / "fix").write_text("fix\n")
    git_at(main, "add", "fix")
    git_at(main, "commit", "-q", "-m", "fix")
    sha = head_of(main, "fix/x")
    ticket_row("fx", notes=notes)
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
    if title:
        update("fx", "progress", PR_TEXT)
    st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
    return main, sha, st, local(term("term-fx", 100, worktree=str(main)))


def origin_head(main, branch):
    argv = [GIT, "-C", str(main), "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
    out = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
    return out.split()[0] if out.strip() else None


PR_FIELDS = "number,baseRefName,autoMergeRequest,isCrossRepository"


def open_pr(base="staging", auto=False, number=12):
    request = {"enabledAt": "2026-11-14T00:00:00Z"} if auto else None
    return json.dumps([{"number": number, "baseRefName": base, "autoMergeRequest": request}])


def gh_key(command):
    """A delivery gh command as the stub sees it: aimed at the one repo pushing() set."""
    return f"{command} --repo {next(iter(cfg['repos']), '')}"


def history(branch, base):
    """The gh key for the branch's pull requests into its base, closed and merged too."""
    return gh_key(f"pr list --head {branch} --base {base} --state all --limit 200 --json number,state,isCrossRepository")


def gh_for(branch, base, sha, number=12, prs="[]", create=True, merge=True):
    table = {gh_key(f"pr list --head {branch} --state open --limit 200 --json {PR_FIELDS}"): [0, prs]}
    table[history(branch, base)] = [0, "[]"]
    if create:
        key = f"pr create --base {base} --head {branch} --title {PR_TITLE} --body {PR_BODY}"
        table[gh_key(key)] = [0, f"https://github.com/o/r/pull/{number}\n"]
    if merge:
        # Its checks passed and GitHub holds the merge: auto-merge at that head.
        table[gh_key(f"pr view {number} --json {da.PR_VIEW}")] = pr_view(sha)
        table[gh_key(f"pr merge {number} --auto --merge --match-head-commit {sha}")] = [0, ""]
    return table


def gh_verbs():
    """The gh pull request verbs this tick ran, past the closed-PR history read; every
    merge is an auto-merge."""
    verbs = [
        r["args"][:2]
        for r in calls("gh")
        if r["args"][:1] == ["pr"] and not (r["args"][:2] == ["pr", "list"] and "--base" in r["args"])
    ]
    for r in calls("gh"):
        if r["args"][:2] == ["pr", "merge"]:
            assert {"--auto", "--disable-auto"} & set(r["args"]), r["args"]
    return [" ".join(v) for v in verbs]


def gh_writes():
    return [v for v in gh_verbs() if v not in ("pr list", "pr view")]


def pushes():
    return [r["args"] for r in calls("git") if r["args"][:1] == ["push"]]


def delivered_lines(tid):
    log = tmp / "delivered.log"
    rows = log.read_text().splitlines() if log.exists() else []
    verbs = ("PUSHED ", "OPENED ", "AUTO-MERGE ", "MERGED ")
    return [
        r.split("\t")[2]
        for r in rows
        if r.split("\t")[1:2] == [tid] and r.split("\t")[2].startswith(verbs)
    ]


def refused_sends():
    return [s[1] for s in sends() if s[1].startswith(("PUSH REFUSED", "NO AUTO-MERGE"))]


@scenario
def auto_push_off_pushes_and_opens_nothing():
    """auto_push starts off: a cleared fix branch with its PR text raises UNPUSHED as
    before, and nothing is pushed, opened or stored."""
    main, sha, st, screen = fix_repo("auto-off")
    world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
    tick(st)
    assert da.load_config().get("auto_push", "unset") is False, "the setting starts off"
    assert origin_head(main, "fix/x") is None and pushes() == [], pushes()
    assert gh_verbs() == [] and "deliveries" not in st, (gh_verbs(), st.get("deliveries"))
    assert unpushed_sends() == [
        ("term-orch", f"UNPUSHED fx: fix/x on local at {sha[:7]}, 1 commit ahead of origin", True)
    ], sends()


@scenario
def auto_push_own_branch_into_staging_once():
    """A branch with its own PR into staging: the tick runs the repo's check, pushes
    the cleared SHA, opens the PR with the ticket's title and body and turns on
    auto-merge, each once; the next tick reads nothing and does nothing."""
    main, sha, st, screen = fix_repo("auto-staging")
    pushing({"auto-staging/origin": {"check": "test -f fix", "protected": ["main"]}})
    try:
        before = len(delivered_lines("fx"))
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert origin_head(main, "fix/x") == sha
        assert gh_verbs() == ["pr list", "pr create", "pr view", "pr merge"], gh_verbs()
        assert unpushed_sends() == [] and refused_sends() == [], sends()
        assert delivered_lines("fx")[before:] == [
            f"PUSHED fix/x at {sha} to origin",
            "OPENED pull request #12: fix/x into staging",
            f"AUTO-MERGE pull request #12 at {sha}",
        ], delivered_lines("fx")[before:]
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha, prs=open_pr(auto=True)))
        tick(st)
        assert pushes() == [] and gh_verbs() == [], (pushes(), gh_verbs())
        tick(st, NOW + da.RECHECK + 1)
        # Auto-merge is on: later reads wait for GitHub's merge and write nothing.
        assert pushes() == [] and gh_writes() == [], (pushes(), gh_verbs())
        assert len(delivered_lines("fx")) == before + 3
    finally:
        pushing_off()


@scenario
def auto_push_dot_agents_branch_into_main():
    """A dot-agents branch with `PR into dot-agents main` opens its PR into main."""
    main, sha, st, screen = fix_repo("auto-main", notes=STANDALONE, main_base=True)
    pushing({"auto-main/origin": {"protected": []}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "main", sha))
        tick(st)
        assert origin_head(main, "fix/x") == sha
        assert gh_verbs() == ["pr list", "pr create", "pr view", "pr merge"], gh_verbs()
        create = next(r["args"] for r in calls("gh") if r["args"][:2] == ["pr", "create"])
        assert create[2:4] == ["--base", "main"], create
    finally:
        pushing_off()


@scenario
def auto_push_feature_branch_opens_its_pr_once_its_tickets_are_done():
    """A task branch the coder merged --no-ff into feature/feat: the feature branch is
    pushed on each cleared merge, and the feature's one PR, into the repo's base, opens
    only once every ticket naming that feature branch is complete or closed."""
    reset_db()
    main = feature_repo("auto-feature")
    first = task_commit(main, "task/feat/fa")
    ticket_row("fa", notes=FEATURE_NOTES)
    ticket_row("fb", notes=FEATURE_NOTES)
    update("fa", "finding", f"REVIEW VERDICT: PUSH {first}", agent="codex:fa-r")
    head = merge_task(main, first)
    st = fresh_state(fa=rec("fa", "c1", worktree=str(main)))
    screen = local(term("term-fa", 100, worktree=str(main)))
    pushing({"auto-feature/origin": {"base": "staging"}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", head))
        tick(st)
        assert origin_head(main, "feature/feat") == head, "the cleared merge is pushed"
        assert gh_verbs() == ["pr list"] and refused_sends() == [], (gh_verbs(), sends())
        ticket_row("fa", status="complete", notes=FEATURE_NOTES)
        da.drop(st, "fa")
        second = task_commit(main, "task/feat/fb")
        update("fb", "finding", f"REVIEW VERDICT: PUSH {second}", agent="codex:fb-r")
        newer = merge_task(main, second)
        update("fb", "progress", PR_TEXT)
        world([], git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", newer))
        tick(st)
        assert origin_head(main, "feature/feat") == newer
        assert gh_writes() == [], "fb is still open"
        ticket_row("fb", status="complete", notes=FEATURE_NOTES)
        world([], git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", newer))
        tick(st)
        assert gh_verbs() == ["pr list", "pr create", "pr view", "pr merge"], gh_verbs()
        assert pushes() == [], "already on origin"
        world([], git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", newer, prs=open_pr(auto=True)))
        tick(st, NOW + da.RECHECK + 1)
        assert gh_writes() == [] and pushes() == [], (gh_verbs(), pushes())
    finally:
        pushing_off()


@scenario
def auto_push_delivers_a_feature_from_the_host_that_holds_its_head():
    """Live (da-logs-2): live-write-safety-07 and -03 raised UNPUSHED for feature heads on
    the remote host and were pushed by hand, while auto-push read only the first ticket's
    host, which lacked the branch. Each recorded host is read and the cleared head is
    delivered from the one holding it; an older copy elsewhere is passed over, and two
    hosts with unrelated heads refuse."""
    cases = (
        ("auto-hosts-absent", None), ("auto-hosts-older", "older"),
        ("auto-hosts-split", "split"), ("auto-hosts-nobase", "nobase"),
        ("auto-hosts-otherbase", "otherbase"),
    )
    for name, other in cases:
        reset_db()
        main = feature_repo(name)
        first = task_commit(main, "task/feat/fa")
        earlier = merge_task(main, first)
        # Origin holds the earlier feature head under another name, for the older copy.
        git_at(main, "push", "-q", "origin", f"{earlier}:refs/heads/earlier")
        task = task_commit(main, "task/feat/fb")
        head = merge_task(main, task)
        there = main.with_name("there")
        source = main.parent / "origin.git"
        if other in ("nobase", "otherbase"):
            # Its origin, under the same name, has the feature branch but no staging base,
            # or a staging base of its own that already holds its unrelated head.
            source = main.parent / "elsewhere" / name / "origin.git"
            subprocess.run([GIT, "init", "--bare", "-q", str(source)], check=True)
            git_at(main, "push", "-q", str(source), "origin/feature/feat:refs/heads/feature/feat")
        subprocess.run([GIT, "clone", "-q", str(source), str(there)], check=True)
        if other:
            git_at(there, "checkout", "-q", "-b", "feature/feat", "origin/feature/feat")
        if other == "older":
            git_at(there, "reset", "-q", "--hard", earlier)
        if other in ("split", "nobase", "otherbase"):
            (there / "stray").write_text("stray\n")
            git_at(there, "add", "stray")
            git_at(there, "commit", "-q", "-m", "stray")
        if other == "otherbase":
            git_at(there, "push", "-q", "origin", "feature/feat:refs/heads/staging")
        ticket_row("fa", notes=FEATURE_NOTES)
        ticket_row("fb", notes=FEATURE_NOTES)
        update("fa", "finding", f"REVIEW VERDICT: PUSH {first}", agent="codex:fa-r")
        update("fb", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:fb-r")
        st = fresh_state(
            fa=rec("fa", "c1", worktree=str(there)),
            fb=rec("fb", "c2", host="Juzzark-WSL", worktree=str(main)),
        )
        screens = [
            *local(term("term-fa", 100, worktree=str(there))),
            ("Juzzark-WSL", "term-fb", *term("term-fb", 100, worktree=str(main))),
        ]
        pushing({f"{name}/origin": {"base": "staging"}})
        try:
            # As real gh does, each reply ends its line, which a remote probe needs.
            table = {k: [c, out.rstrip("\n") + "\n"] for k, (c, out) in gh_for("feature/feat", "staging", head).items()}
            world(screens, git={}, git_bin=GIT, gh=table)
            tick(st)
            if other in ("split", "nobase", "otherbase"):
                assert origin_head(main, "feature/feat") != head, (name, "nothing pushed")
                stray = head_of(there, "feature/feat")
                assert refused_sends() == [
                    f"PUSH REFUSED fb: feature/feat at {task[:7]} on local, hosts: the branch has "
                    f"heads {stray[:7]} on local, {head[:7]} on Juzzark-WSL"
                ], sends()
            else:
                assert origin_head(main, "feature/feat") == head, (name, sends())
                assert st["deliveries"]["|feature/feat"]["host"] == "Juzzark-WSL"
        finally:
            pushing_off()


@scenario
def auto_push_rereads_a_feature_merged_after_its_push_verdict():
    """Review da-3-9: the PUSH landed while the feature ref was absent, or still at a head
    origin had, so the tick stored the branch as finished; the coder's later merge, with
    no ledger update, was never read. While a ticket naming the branch is open the read
    comes back after RECHECK and pushes the merge."""
    for name, absent in (("auto-reread-absent", True), ("auto-reread-onorigin", False)):
        reset_db()
        main = feature_repo(name)
        task = task_commit(main, "task/feat/fa")
        if absent:
            git_at(main, "checkout", "-q", "staging")
            git_at(main, "branch", "-q", "-D", "feature/feat")
        ticket_row("fa", notes=FEATURE_NOTES)
        update("fa", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:fa-r")
        st = fresh_state(fa=rec("fa", "c1", worktree=str(main)))
        screen = local(term("term-fa", 100, worktree=str(main)))
        pushing({f"{name}/origin": {"base": "staging"}})
        try:
            world(screen, git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", task))
            tick(st)
            assert pushes() == [], (name, pushes())
            if absent:
                git_at(main, "checkout", "-q", "-b", "feature/feat", "origin/feature/feat")
            head = merge_task(main, task)
            world(screen, git={}, git_bin=GIT, gh=gh_for("feature/feat", "staging", head))
            tick(st, NOW + da.RECHECK + 1)
            assert origin_head(main, "feature/feat") == head, (name, pushes())
        finally:
            pushing_off()


def evil_merge(main, sha):
    """A --no-ff merge of a cleared SHA that also carries a hand edit."""
    git_at(main, "merge", "-q", "--no-ff", "--no-commit", sha)
    (main / "README").write_text("edited in the merge\n")
    git_at(main, "commit", "-q", "-am", f"merge {sha[:7]}")


@scenario
def auto_push_guards_refuse_with_one_event():
    """Each guard refuses its own way: one PUSH REFUSED naming the guard, nothing
    pushed or opened, and no second event on a later read of the same head."""
    repos = {"protected": ["main"], "check": "test -f fix"}

    def short(main, sha):
        reset_db()
        ticket_row("fx", notes=STAGING_FIX)
        update("fx", "finding", "REVIEW VERDICT: PUSH deadbee", agent="codex:fx-r")
        update("fx", "progress", PR_TEXT)

    def later_commit(main, sha):
        (main / "late").write_text("late\n")
        git_at(main, "add", "late")
        git_at(main, "commit", "-q", "-m", "after the clearance")

    def diverged(main, sha):
        git_at(main, "checkout", "-q", "-b", "other", "staging")
        (main / "other").write_text("other\n")
        git_at(main, "add", "other")
        git_at(main, "commit", "-q", "-m", "other")
        git_at(main, "push", "-q", "origin", "other:refs/heads/fix/x")
        git_at(main, "checkout", "-q", "fix/x")

    def dirty(main, sha):
        (main / "stray").write_text("stray\n")

    def failing_check(main, sha):
        repos["check"] = "false"

    def mutating_check(main, sha):
        repos["check"] = "touch changed-by-check"

    def done_without_title(main, sha):
        ticket_row("fx", status="complete", notes=STAGING_FIX)

    def evil(main, sha):
        reset_db()
        git_at(main, "checkout", "-q", "-b", "feature/feat", "staging")
        git_at(main, "push", "-q", "origin", "feature/feat")
        evil_merge(main, sha)
        ticket_row("fx", notes=FEATURE_NOTES)
        update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")

    cases = [
        ("sha", short, STAGING_FIX, True),
        ("cleared", later_commit, STAGING_FIX, True),
        ("cleared", evil, STAGING_FIX, True),
        ("diverged", diverged, STAGING_FIX, True),
        ("dirty", dirty, STAGING_FIX, True),
        ("check", failing_check, STAGING_FIX, True),
        ("dirty", mutating_check, STAGING_FIX, True),
        ("base", None, "Branch: `fix/x` · Feature branch: none (own PR into main)", True),
        ("base", None, "Branch: `fix/x` · Feature branch: none", True),
        ("title", done_without_title, STAGING_FIX, False),
    ]
    try:
        for n, (guard, change, notes, title) in enumerate(cases):
            name = f"auto-guard-{n}"
            main, sha, st, screen = fix_repo(name, notes=notes, title=title)
            repos["check"] = "test -f fix"
            (main / "changed-by-check").unlink(missing_ok=True)
            if change:
                change(main, sha)
            pushing({f"{name}/origin": dict(repos, base="staging")})
            branch = "feature/feat" if change is evil else "fix/x"
            pushed = origin_head(main, branch)
            world(screen, git={}, git_bin=GIT, gh=gh_for(branch, "staging", sha))
            tick(st)
            told = refused_sends()
            assert len(told) == 1 and told[0].startswith(f"PUSH REFUSED fx: {branch} at "), (guard, sends())
            assert f" on local, {guard}: " in told[0], (guard, told)
            assert origin_head(main, branch) == pushed, (guard, "nothing pushed")
            assert gh_writes() == [], (guard, gh_verbs())
            world(screen, git={}, git_bin=GIT, gh=gh_for(branch, "staging", sha))
            tick(st, NOW + da.RECHECK + 1)
            assert refused_sends() == [], (guard, "told once", sends())
    finally:
        pushing_off()


@scenario
def auto_push_takes_only_a_standing_push():
    """A PUSH its reviewer withdraws, by a later FIX on that SHA or by superseding it,
    clears nothing: nothing is pushed or opened. A FIX on another SHA leaves it standing."""
    try:
        for n, withdraw in enumerate(("fix", "supersede", "other")):
            name = f"auto-standing-{n}"
            main, sha, st, screen = fix_repo(name)
            if withdraw == "supersede":
                (push,) = [u["id"] for u in da.read_tickets(root)["", "fx"]["updates"] if "PUSH" in u["content"]]
                supersede(push, update("fx", "decision", "PUSH withdrawn", agent="codex:fx-r"))
            else:
                other = sha if withdraw == "fix" else "deadbee"
                update("fx", "finding", f"REVIEW VERDICT: FIX {other[:9]} missed a check", agent="codex:fx-r")
            pushing({f"{name}/origin": {"protected": ["main"], "base": "staging"}})
            pushed = origin_head(main, "fix/x")
            world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
            tick(st)
            if withdraw == "other":
                assert origin_head(main, "fix/x") == sha, (withdraw, sends())
            else:
                assert origin_head(main, "fix/x") == pushed != sha, (withdraw, sends())
                assert gh_writes() == [], (withdraw, gh_verbs())
    finally:
        pushing_off()


@scenario
def auto_push_migration_leaves_auto_merge_off():
    """A PR whose diff adds a migration file is opened without auto-merge, and one
    event says so."""
    main, _, _, _ = fix_repo("auto-migration")
    (main / "db" / "migrations").mkdir(parents=True)
    (main / "db" / "migrations" / "001.sql").write_text("create table t (id int);\n")
    git_at(main, "add", "-A")
    git_at(main, "commit", "-q", "-m", "migration")
    sha = head_of(main, "fix/x")
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
    st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
    screen = local(term("term-fx", 100, worktree=str(main)))
    pushing({"auto-migration/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert origin_head(main, "fix/x") == sha
        assert gh_verbs() == ["pr list", "pr create"], gh_verbs()
        text = (
            "NO AUTO-MERGE fx: pull request 12 for fix/x adds a migration, "
            "auto-merge left off"
        )
        assert refused_sends() == [text], sends()
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha, prs=open_pr()))
        tick(st, NOW + da.RECHECK + 1)
        # Its ticket is open, so it is read again: one list of the open pull request, nothing more.
        assert gh_verbs() == ["pr list"] and refused_sends() == [], (gh_verbs(), sends())
    finally:
        pushing_off()


@scenario
def auto_push_modified_migration_registry_requires_a_human_merge():
    """A green completed feature that changes the SQLite registry needs a human merge."""
    cases = (
        (False, "CLEAN", False),
        (False, "BLOCKED", False),
        (True, "CLEAN", False),
        (True, "BLOCKED", False),
        (True, "BLOCKED", True),
    )
    for n, (migration, merge_state, armed) in enumerate(cases):
        reset_db()
        name = f"auto-registry-{n}"
        main = real_main(name)
        registry = main / "src/backend/storage/sqlite/migrations.ts"
        registry.parent.mkdir(parents=True)
        registry.write_text("export const versions = [36];\n")
        git_at(main, "add", "-A")
        git_at(main, "commit", "-q", "-m", "existing registry")
        git_at(main, "push", "-q", "origin", "staging")
        git_at(main, "checkout", "-q", "-b", "feature/feat")
        git_at(main, "checkout", "-q", "-b", "task/feat/x")
        if migration:
            registry.write_text("export const versions = [36, 37, 38];\n")
        (registry.parent / "migrations.test.ts").write_text("// Registry test fixture\n")
        git_at(main, "add", "-A")
        git_at(main, "commit", "-q", "-m", "change")
        task = head_of(main, "task/feat/x")
        git_at(main, "checkout", "-q", "feature/feat")
        head = merge_task(main, task)
        ticket_row("fx", status="complete", notes=FEATURE_NOTES)
        update("fx", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:fx-r")
        update("fx", "progress", PR_TEXT)
        st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
        screen = local(term("term-fx", 100, worktree=str(main)))
        slug = f"{name}/origin"
        pushing({slug: {"base": "staging"}})
        try:
            table = gated("feature/feat", "staging", head, merge_state=merge_state)
            if armed:
                git_at(main, "push", "-q", "origin", "feature/feat")
                st["deliveries"] = {"|feature/feat": {"told": [], "pr": 12, "auto": [12, head]}}
                table = gated("feature/feat", "staging", head, prs=open_pr(auto=True))
                table[gh_key("pr merge 12 --disable-auto")] = [0, ""]
            world(screen, git={}, git_bin=GIT, gh=table)
            tick(st)
            assert origin_head(main, "feature/feat") == head
            merges = [r["args"] for r in calls("gh") if r["args"][:2] == ["pr", "merge"]]
            if migration:
                expected = [["pr", "merge", "12", "--disable-auto", "--repo", slug]] if armed else []
                assert merges == expected, (merge_state, armed, merges)
                assert refused_sends() == [
                    "NO AUTO-MERGE fx: pull request 12 for feature/feat adds a migration, auto-merge left off"
                ], sends()
                assert armed or "pr create" in gh_verbs(), gh_verbs()
            else:
                flags = ["--auto"] if merge_state == "BLOCKED" else []
                assert merges == [[
                    "pr", "merge", "12", *flags, "--merge", "--match-head-commit", head, "--repo", slug,
                ]], merges
        finally:
            pushing_off()


@scenario
def auto_push_migration_hold_follows_the_repository_pr_and_head():
    """A hold survives a new clearing ticket, and cannot hold another repo's PR."""
    main, _, st, screen = fix_repo("auto-hold-first")
    registry = main / "src/backend/storage/sqlite/migrations.ts"
    registry.parent.mkdir(parents=True)
    registry.write_text("export const versions = [37];\n")
    git_at(main, "add", "-A")
    git_at(main, "commit", "-q", "-m", "migration")
    sha = head_of(main, "fix/x")
    git_at(main, "push", "-q", "origin", "fix/x")
    ticket_row("fx", status="complete", notes=STAGING_FIX)
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")

    other_root = tmp / "auto-hold-second"
    other_root.mkdir()
    other_origin, other = other_root / "origin.git", other_root / "checkout"
    git_at(tmp, "clone", "-q", "--bare", str(main), str(other_origin))
    git_at(tmp, "clone", "-q", "-b", "fix/x", str(other_origin), str(other))
    first_repo, second_repo = "auto-hold-first/origin", "auto-hold-second/origin"
    pushing({first_repo: {}, second_repo: {}})
    try:
        table = {
            f"pr list --head fix/x --state open --limit 200 --json {PR_FIELDS} --repo {repo}": [0, open_pr(auto=True)]
            for repo in (first_repo, second_repo)
        }
        table.update({f"pr merge 12 --disable-auto --repo {repo}": [0, ""] for repo in (first_repo, second_repo)})

        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        first_disable = [r["args"][-1] for r in calls("gh") if "--disable-auto" in r["args"]]
        assert first_disable == [first_repo], first_disable

        # A person re-armed the first PR, and another ticket now supplies its clearance.
        ticket_row("fy", status="complete", notes=STAGING_FIX)
        update("fy", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fy-r")
        ticket_row("fx", status="complete", notes=STAGING_FIX, project="other")
        update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r", project="other")
        st["workers"].update(
            fy=rec("fy", "c1", worktree=str(main)),
            fx=rec("fx", "c2", project="other", worktree=str(other)),
        )
        screen = local(term("term-fy", 100, worktree=str(main)), term("term-fx", 100, worktree=str(other)))
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st, NOW + da.RECHECK + 1)

        merges = [r["args"] for r in calls("gh") if r["args"][:2] == ["pr", "merge"]]
        assert merges == [["pr", "merge", "12", "--disable-auto", "--repo", second_repo]], (merges, sends())
        assert refused_sends() == [
            "NO AUTO-MERGE fx: pull request 12 for fix/x adds a migration, auto-merge left off"
        ], sends()
    finally:
        pushing_off()


@scenario
def auto_push_migration_hold_sees_quoted_and_renamed_paths():
    """Review da-3-2: Git quoted a root-level migrations/001_é.sql, so the pattern saw
    no path start before migrations, and a file renamed into migrations/ showed as R,
    not A. Both hold auto-merge like any added migration, and so does a name that is no
    UTF-8, which the tick reads without failing."""
    cases = (
        ("auto-mig-quoted", "quoted"), ("auto-mig-renamed", "renamed"),
        ("auto-mig-latin1", "latin1"), ("auto-mig-unicode", "unicode"),
    )
    for name, change in cases:
        main, _, _, _ = fix_repo(name)
        (main / "migrations").mkdir()
        if change == "unicode":
            # A repo whose own pattern names a non-ASCII directory.
            (main / "données").mkdir()
            (main / "données" / "001.sql").write_text("create table t (id int);\n")
            git_at(main, "add", "-A")
        elif change == "quoted":
            (main / "migrations" / "001_é.sql").write_text("create table t (id int);\n")
            git_at(main, "add", "-A")
        elif change == "renamed":
            git_at(main, "mv", "README", "migrations/002.sql")
        else:
            # Only the index can hold a Latin-1 name on a UTF-8 filesystem.
            blob = subprocess.run(
                [GIT, "-C", str(main), "hash-object", "-w", "--stdin"],
                input=b"x\n", check=True, capture_output=True,
            ).stdout.strip()
            info = b"100644," + blob + b",migrations/003_\xe9.sql"
            subprocess.run([GIT, "-C", str(main), "update-index", "--add", "--cacheinfo", info], check=True)
        git_at(main, "commit", "-q", "-m", change)
        if change == "latin1":
            argv = [GIT, "-C", str(main), "update-index", "--skip-worktree", b"migrations/003_\xe9.sql"]
            subprocess.run(argv, check=True)
        sha = head_of(main, "fix/x")
        update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
        st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
        screen = local(term("term-fx", 100, worktree=str(main)))
        pushing({f"{name}/origin": {"migrations": "(^|/)données/"} if change == "unicode" else {}})
        try:
            world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
            tick(st)
            assert gh_verbs() == ["pr list", "pr create"], (change, gh_verbs())
            text = "NO AUTO-MERGE fx: pull request 12 for fix/x adds a migration, auto-merge left off"
            assert refused_sends() == [text], (change, sends())
        finally:
            pushing_off()


@scenario
def auto_push_half_done_sequence_finishes_next_tick():
    """A push whose PR failed to open, and a push no stage was stored for, both go on
    to the PR and auto-merge on the next tick, without a second push."""
    main, sha, st, screen = fix_repo("auto-half")
    pushing({"auto-half/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha, create=False))
        tick(st)
        assert origin_head(main, "fix/x") == sha and len(pushes()) == 1, pushes()
        assert gh_verbs() == ["pr list", "pr create"], gh_verbs()
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert pushes() == [] and gh_verbs() == ["pr list", "pr create", "pr view", "pr merge"], gh_verbs()
        # Pushed, then the tick died before it stored a stage: origin already has the head.
        main, sha, st, screen = fix_repo("auto-half-crash")
        pushing({"auto-half-crash/origin": {}})
        git_at(main, "push", "-q", "origin", f"{sha}:refs/heads/fix/x")
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha, prs=open_pr()))
        tick(st)
        assert pushes() == [] and gh_verbs() == ["pr list", "pr view", "pr merge"], gh_verbs()
        assert st["deliveries"]["|fix/x"]["auto"] == [12, sha]
    finally:
        pushing_off()


@scenario
def auto_push_migration_turns_off_an_earlier_auto_merge_first():
    """Review of aca2a79: a migration head pushed onto a PR whose auto-merge an earlier
    head turned on would merge on green. The tick turns auto-merge off before that
    push; while it cannot, nothing is pushed."""
    main, first, st, screen = fix_repo("auto-migration-later")
    pushing({"auto-migration-later/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", first))
        tick(st)
        assert gh_verbs() == ["pr list", "pr create", "pr view", "pr merge"], gh_verbs()
        (main / "migrations").mkdir()
        (main / "migrations" / "002.sql").write_text("alter table t add c int;\n")
        git_at(main, "add", "-A")
        git_at(main, "commit", "-q", "-m", "migration")
        sha = head_of(main, "fix/x")
        update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
        table = gh_for("fix/x", "staging", sha, prs=open_pr(auto=True))
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        assert origin_head(main, "fix/x") == first, "auto-merge still on: no push"
        assert gh_verbs() == ["pr list", "pr merge"], gh_verbs()
        assert [t for t in refused_sends() if ", auto-merge: " in t], sends()
        table[gh_key("pr merge 12 --disable-auto")] = [0, ""]
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        assert origin_head(main, "fix/x") == sha
        disable = [r for r in calls("gh") if "--disable-auto" in r["args"]]
        assert len(disable) == 1 and gh_writes() == ["pr merge"], gh_verbs()
        text = "NO AUTO-MERGE fx: pull request 12 for fix/x adds a migration, auto-merge left off"
        assert refused_sends() == [text], sends()
    finally:
        pushing_off()


@scenario
def auto_push_invalid_migrations_pattern_is_a_refusal():
    """A repo's migrations setting that is no regex refuses the delivery before any push
    or pull request write, and the tick goes on to supervise its workers."""
    main, sha, st, screen = fix_repo("auto-bad-migrations")
    pushing({"auto-bad-migrations/origin": {"migrations": "["}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert origin_head(main, "fix/x") is None and gh_writes() == [], gh_verbs()
        (refused,) = refused_sends()
        assert refused.startswith("PUSH REFUSED fx: fix/x") and "migrations" in refused, refused
        assert "tick" in st and "status" in st["workers"]["fx"], st["workers"]["fx"]
    finally:
        pushing_off()


@scenario
def auto_push_reads_the_open_pr_base_before_each_push():
    """Review of aca2a79: an open PR on another base was found only after the push. It
    is read before every push, a stored PR number or not, and refuses with nothing
    pushed."""
    main, sha, st, screen = fix_repo("auto-pr-base")
    pushing({"auto-pr-base/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert origin_head(main, "fix/x") == sha and st["deliveries"]["|fix/x"].get("pr") == 12
        (main / "more").write_text("more\n")
        git_at(main, "add", "more")
        git_at(main, "commit", "-q", "-m", "more")
        newer = head_of(main, "fix/x")
        update("fx", "finding", f"REVIEW VERDICT: PUSH {newer}", agent="codex:fx-r")
        table = gh_for("fix/x", "staging", newer, prs=open_pr(base="main"))
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        assert origin_head(main, "fix/x") == sha, "refused before the push"
        assert gh_verbs() == ["pr list"], gh_verbs()
        told = refused_sends()
        assert told == [f"PUSH REFUSED fx: fix/x at {newer[:7]} on local, base: open pull request 12 has base main"], told
    finally:
        pushing_off()


@scenario
def auto_push_without_a_repo_policy_refuses():
    """Review of aca2a79: hr-relay's main was guarded only by an optional setting. A
    repo with no [repos] entry refuses, and an entry that names no protected bases
    still protects main."""
    try:
        main, sha, st, screen = fix_repo("auto-no-policy", notes=STANDALONE, main_base=True)
        pushing({})
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "main", sha))
        tick(st)
        told = refused_sends()
        assert told == [f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, repo: repos.auto-no-policy/origin is not set"], told
        main, sha, st, screen = fix_repo("auto-main-default", notes=STANDALONE, main_base=True)
        pushing({"auto-main-default/origin": {"check": "true"}})
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "main", sha))
        tick(st)
        told = refused_sends()
        assert told == [f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, base: main is protected"], told
        assert origin_head(main, "fix/x") is None and gh_verbs() == [], gh_verbs()
    finally:
        pushing_off()


@scenario
def auto_push_refuses_a_push_url_off_the_checked_repo():
    """Review da-3-6: origin fetched from the checked repo and pushed to a fork through
    remote.origin.pushurl, so the policy, base and tip checks read one repo and the push
    wrote another. A push URL naming another repo refuses before any write, also one
    whose last two path parts match, one that differs only by .git, one with credentials
    and one set for the branch's linked worktree alone; neither the line nor the probe's
    output carries a push URL. A relative origin URL refuses. One naming the checked repo
    delivers, and every gh call names that repo."""
    main, sha, st, screen = fix_repo("auto-push-url")
    fork = main.parent / "fork.git"
    twin = main.parent / "mirror" / "auto-push-url" / "origin.git"
    bare = main.parent / "origin"
    for repo in (fork, twin, bare):
        subprocess.run([GIT, "init", "--bare", "-q", str(repo)], check=True)
    secret = "https://dev:s3cret@example.com/o/r.git"
    line = f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, remote: origin pushes elsewhere than auto-push-url/origin"
    pushing({"auto-push-url/origin": {}})
    try:
        for n, target in enumerate((fork, twin, bare, secret), 1):
            git_at(main, "config", "remote.origin.pushurl", str(target))
            if target == secret:
                probe = subprocess.run(
                    ["sh", "-c", da.FACTS, "facts", "fix/x", "staging", sha],
                    cwd=main, capture_output=True, text=True,
                )
                assert "__facts_read" in probe.stdout and "s3cret" not in probe.stdout, probe.stdout
            st["deliveries"] = {}
            st["sent"] = {k: v for k, v in st["sent"].items() if "|refused|" not in k}
            world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
            tick(st, NOW + n)
            assert pushes() == [] and gh_verbs() == [], (target, pushes(), gh_verbs())
            assert refused_sends() == [line], (target, sends())
        # The probe reads the staging checkout; the push runs in fix/x's linked worktree,
        # whose own config points elsewhere.
        git_at(main, "config", "--unset", "remote.origin.pushurl")
        git_at(main, "checkout", "-q", "staging")
        linked = main.parent / "linked"
        git_at(main, "worktree", "add", "-q", str(linked), "fix/x")
        git_at(main, "config", "extensions.worktreeConfig", "true")
        git_at(linked, "config", "--worktree", "remote.origin.pushurl", str(fork))
        st["deliveries"] = {}
        st["sent"] = {k: v for k, v in st["sent"].items() if "|refused|" not in k}
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st, NOW + 5)
        assert pushes() == [] and gh_verbs() == [], ("linked", pushes(), gh_verbs())
        assert refused_sends() == [line], ("linked", sends())
        git_at(linked, "config", "--worktree", "--unset", "remote.origin.pushurl")
        # Relative to the probe's checkout or the push worktree, one URL names two places.
        git_at(main, "config", "remote.origin.url", "../origin.git")
        st["deliveries"] = {}
        st["sent"] = {k: v for k, v in st["sent"].items() if "|refused|" not in k}
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st, NOW + 6)
        assert pushes() == [] and refused_sends() == [
            f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, remote: origin has a relative URL"
        ], sends()
        git_at(main, "config", "remote.origin.url", str(main.parent / "origin.git"))
        assert "s3cret" not in json.dumps(st), "no push URL is stored"
        git_at(main, "config", "remote.origin.pushurl", str(main.parent / "origin.git"))
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st, NOW + 2 * da.RECHECK)
        assert origin_head(main, "fix/x") == sha, "pushed to the checked repo"
        named = [r["args"][-2:] for r in calls("gh") if r["args"][:1] == ["pr"]]
        assert named and all(n == ["--repo", "auto-push-url/origin"] for n in named), named
    finally:
        pushing_off()


@scenario
def auto_push_refuses_two_open_prs_on_one_branch():
    """Review of 15200e0: with PR 12 into staging listed first and PR 13 into main
    second, only the first was checked and the head was pushed into both. Two open pull
    requests on one branch refuse before any write, in either order."""
    try:
        for n, order in enumerate(((12, "staging"), (13, "main")), 1):
            name = f"auto-two-prs-{n}"
            main, sha, st, screen = fix_repo(name)
            pushing({f"{name}/origin": {}})
            rows = [
                {"number": 12, "baseRefName": "staging", "autoMergeRequest": None},
                {"number": 13, "baseRefName": "main", "autoMergeRequest": {"enabledAt": "x"}},
            ]
            if order[1] == "main":
                rows.reverse()
            table = gh_for("fix/x", "staging", sha, prs=json.dumps(rows))
            world(screen, git={}, git_bin=GIT, gh=table)
            tick(st)
            assert origin_head(main, "fix/x") is None, "nothing pushed"
            assert gh_verbs() == ["pr list"], gh_verbs()
            numbers = " ".join(str(r["number"]) for r in rows)
            text = (
                f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, pull-request: open pull "
                f"requests {numbers} share the branch"
            )
            assert refused_sends() == [text], sends()
    finally:
        pushing_off()


@scenario
def auto_push_records_no_merge_from_a_chain_it_failed_to_read():
    """Review of ba41885: origin's base moved between the fetch and its read, so the base
    tip named an object not fetched yet; rev-list failed into an empty chain and the
    delivery was recorded as merged. Merged needs git's proof that the head is in the
    base; without it the tick reads again."""
    main, sha, st, screen = fix_repo("auto-chain-unread")
    pushing({"auto-chain-unread/origin": {}})
    unfetched = "0123456789abcdef0123456789abcdef01234567"
    try:
        git = {"ls-remote --heads origin refs/heads/staging": [0, f"{unfetched}\trefs/heads/staging\n"]}
        world(screen, git=git, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        d = st["deliveries"]["|fix/x"]
        assert not d.get("merged") and "checked" not in d, d
        assert pushes() == [] and gh_verbs() == [], (pushes(), gh_verbs())
    finally:
        pushing_off()


@scenario
def auto_push_refuses_an_origin_gh_cannot_name_exactly():
    """Review of ba41885: git checked origin at forge.example:8443 while gh was pointed at
    forge.example, which drops the port, so another instance's owner/name could answer
    for the pull request. An origin URL that names no gh repository without losing part
    of it refuses before any read of pull requests. Review of cf815aa: so does a local
    path, whose owner/repo suffix names no GitHub repository; a GitHub URL sends gh its
    whole host/owner/repo."""
    for name in ("auto-gh-port", "auto-gh-path"):
        main, sha, st, screen = fix_repo(name)
        pushing({"o/r": {}})
        git = {"remote get-url origin": [0, "https://forge.example:8443/o/r.git\n"]}
        if name == "auto-gh-path":
            # A real mirror both URLs name, outside the fixtures' stand-in GitHub identity.
            mirror = main.parent / "mirrors" / "o" / "r.git"
            git_at(main, "clone", "-q", "--bare", str(main.parent / "origin.git"), str(mirror))
            git_at(main, "remote", "set-url", "origin", str(mirror))
            da.gh_repo, git = GH_REPO, {}
        try:
            world(screen, git=git, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
            tick(st)
            assert pushes() == [] and gh_verbs() == [], (name, pushes(), gh_verbs())
            assert refused_sends() == [
                f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, remote: origin URL maps to no gh repository"
            ], (name, sends())
        finally:
            pushing_off()
    main, sha, st, screen = fix_repo("auto-gh-github")
    pushing({"o/r": {}})
    try:
        url = "https://github.com/o/r.git\n"
        get = ["remote get-url origin", "remote get-url --push --all origin"]
        git = {k: [0, url] for k in [*get, f"-C {main} {get[1]}", f"-C . {get[1]}"]}
        table = {k.replace("--repo o/r", "--repo github.com/o/r"): v for k, v in gh_for("fix/x", "staging", sha).items()}
        world(screen, git=git, git_bin=GIT, gh=table)
        tick(st)
        assert origin_head(main, "fix/x") == sha, (pushes(), sends())
        named = [r["args"][-2:] for r in calls("gh") if r["args"][:1] == ["pr"]]
        assert named and all(n == ["--repo", "github.com/o/r"] for n in named), named
    finally:
        pushing_off()


@scenario
def auto_push_holds_a_branch_whose_last_pull_request_was_closed():
    """Live (da-logs-1): the operator closed diagnostic PR 347 unmerged and the tick
    opened PR 348 for the same branch, which merged. A branch whose last pull request
    into its base was closed unmerged is held: nothing pushed or opened, one line for
    the orchestrator. A fork's pull request from a branch of the same name, open or
    merged later, is no pull request of this branch. One merged last is delivered as
    before."""
    main, sha, st, screen = fix_repo("auto-closed-pr")
    pushing({"auto-closed-pr/origin": {}})
    try:
        fork_open = [{"number": 360, "baseRefName": "staging", "autoMergeRequest": None, "isCrossRepository": True}]
        table = gh_for("fix/x", "staging", sha, prs=json.dumps(fork_open))
        table[history("fix/x", "staging")] = [0, json.dumps([
            {"number": 340, "state": "MERGED", "isCrossRepository": False},
            {"number": 347, "state": "CLOSED", "isCrossRepository": False},
            {"number": 350, "state": "MERGED", "isCrossRepository": True},
        ])]
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        assert origin_head(main, "fix/x") is None and gh_writes() == [], gh_verbs()
        assert refused_sends() == [
            f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, closed: pull request 347 into "
            "staging was closed unmerged, reopen it or open its replacement by hand"
        ], sends()
        # A page of fork pull requests hides the branch's own history: refused, not opened.
        forks = [{"number": 400 + n, "state": "MERGED", "isCrossRepository": True} for n in range(200)]
        table[history("fix/x", "staging")] = [0, json.dumps(forks)]
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st, NOW + da.RECHECK + 1)
        assert origin_head(main, "fix/x") is None and gh_writes() == [], gh_verbs()
        assert refused_sends() == [
            f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, pull-request: 200 or more pull "
            "requests share the branch name, its history is unread"
        ], sends()
        table[history("fix/x", "staging")] = [0, json.dumps([{"number": 347, "state": "MERGED", "isCrossRepository": False}])]
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st, NOW + 2 * da.RECHECK + 2)
        assert origin_head(main, "fix/x") == sha and "pr create" in gh_verbs(), gh_verbs()
    finally:
        pushing_off()


VIEW = "pr view 12 --json state,headRefOid,mergeStateStatus,statusCheckRollup"


def pr_view(sha, checks="green", merge_state="BLOCKED", state="OPEN"):
    """`gh pr view` of PR 12 at `sha` with its check rollup green, pending or red."""
    run = {
        "green": {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
        "pending": {"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS", "conclusion": ""},
        "red": {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"},
    }[checks]
    body = {"state": state, "headRefOid": sha, "mergeStateStatus": merge_state, "statusCheckRollup": [run]}
    return [0, json.dumps(body)]


def gated(branch, base, sha, checks="green", merge_state="BLOCKED", **kw):
    """gh_for with PR 12's view, and a direct merge answered too."""
    table = gh_for(branch, base, sha, **kw)
    table[gh_key(VIEW)] = pr_view(sha, checks, merge_state)
    table[gh_key(f"pr merge 12 --merge --match-head-commit {sha}")] = [0, ""]
    return table


@scenario
def auto_push_pushes_a_branch_no_worktree_has_checked_out():
    """Live 06:22Z: a feature merge made from the task worktree left feature/feat checked
    out nowhere, and auto_push refused it. The head is pushed from the ref. A head whose
    tree is a cleared SHA's needs no check; any other runs the repo's check in a
    throwaway worktree at that head, and a failing check refuses."""
    cases = [
        ("same-tree", False, "false", None),
        ("checked", True, "test -f task-feat-ro && test -f hand", None),
        ("failing", True, "test -f nowhere", "check"),
    ]
    for name, other, check, guard in cases:
        reset_db()
        main = feature_repo(f"auto-ref-{name}")
        task = task_commit(main, "task/feat/ro")
        if other:
            # Origin moved on after the task branched: the merge's tree is no cleared SHA's.
            (main / "hand").write_text("hand\n")
            git_at(main, "add", "hand")
            git_at(main, "commit", "-q", "-m", "hand")
            git_at(main, "push", "-q", "origin", "feature/feat")
        head = merge_task(main, task)
        git_at(main, "checkout", "-q", "staging")
        ticket_row("ro", notes=FEATURE_NOTES)
        update("ro", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:ro-r")
        st = fresh_state(ro=rec("ro", "c1", worktree=str(main)))
        pushing({f"auto-ref-{name}/origin": {"base": "staging", "check": check}})
        try:
            screen = local(term("term-ro", 100, worktree=str(main)))
            world(screen, git={}, git_bin=GIT, gh=gated("feature/feat", "staging", head))
            tick(st)
            told = refused_sends()
            if guard:
                assert len(told) == 1 and f", {guard}: " in told[0], (name, told)
                assert origin_head(main, "feature/feat") != head, name
            else:
                assert told == [], (name, sends())
                assert origin_head(main, "feature/feat") == head, name
            assert "auto-ref" not in run_git_worktrees(main), "the throwaway worktree is gone"
        finally:
            pushing_off()


def run_git_worktrees(main):
    out = subprocess.run([GIT, "-C", str(main), "worktree", "list"], check=True, capture_output=True, text=True)
    return "\n".join(line for line in out.stdout.splitlines() if "/tmp" in line and "checkout" not in line)


@scenario
def auto_push_counts_commits_already_on_origin_as_accepted():
    """Live 06:16Z: a first parent that is origin's own head, uncleared by any ticket,
    refused the push as `cleared`. Commits already on origin's branch are accepted, as
    verify-merges accepts them."""
    reset_db()
    main = feature_repo("auto-on-origin")
    (main / "hand").write_text("hand\n")
    git_at(main, "add", "hand")
    git_at(main, "commit", "-q", "-m", "pushed by hand")
    git_at(main, "push", "-q", "origin", "feature/feat")
    task = task_commit(main, "task/feat/oo")
    head = merge_task(main, task)
    ticket_row("oo", notes=FEATURE_NOTES)
    update("oo", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:oo-r")
    st = fresh_state(oo=rec("oo", "c1", worktree=str(main)))
    pushing({"auto-on-origin/origin": {"base": "staging"}})
    try:
        world(local(term("term-oo", 100, worktree=str(main))), git={}, git_bin=GIT, gh=gated("feature/feat", "staging", head))
        tick(st)
        assert refused_sends() == [], sends()
        assert origin_head(main, "feature/feat") == head
    finally:
        pushing_off()


@scenario
def auto_push_fast_forwards_a_stale_local_ref():
    """Live 06:12Z: a host whose local feature ref, checked out nowhere, was behind
    origin refused as `diverged`. The ref is fast-forwarded to origin and nothing is
    refused or pushed."""
    reset_db()
    main = feature_repo("auto-stale")
    old = head_of(main, "feature/feat")
    task = task_commit(main, "task/feat/sl")
    head = merge_task(main, task)
    git_at(main, "push", "-q", "origin", "feature/feat")
    git_at(main, "checkout", "-q", "staging")
    git_at(main, "update-ref", "refs/heads/feature/feat", old)
    ticket_row("sl", notes=FEATURE_NOTES)
    update("sl", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:sl-r")
    st = fresh_state(sl=rec("sl", "c1", worktree=str(main)))
    pushing({"auto-stale/origin": {"base": "staging"}})
    try:
        world(local(term("term-sl", 100, worktree=str(main))), git={}, git_bin=GIT, gh=gated("feature/feat", "staging", head))
        tick(st)
        assert refused_sends() == [], sends()
        assert head_of(main, "feature/feat") == head and pushes() == [], pushes()
    finally:
        pushing_off()


@scenario
def auto_push_resolves_a_short_sha():
    """Live 06:38Z: a PUSH naming a short SHA refused as `sha`. A short SHA that names
    one commit in the repo is resolved there and delivered."""
    main, sha, st, screen = fix_repo("auto-short")
    reset_db()
    ticket_row("fx", notes=STAGING_FIX)
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha[:9]}", agent="codex:fx-r")
    update("fx", "progress", PR_TEXT)
    pushing({"auto-short/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gated("fix/x", "staging", sha))
        tick(st)
        assert refused_sends() == [], sends()
        assert origin_head(main, "fix/x") == sha
    finally:
        pushing_off()


@scenario
def auto_push_needs_no_title_for_an_open_pull_request():
    """Live: `title: no update holds a PR title` on a branch whose PR was already open.
    An open pull request needs no title: the head is pushed onto it."""
    main, sha, st, screen = fix_repo("auto-open-pr", title=False)
    ticket_row("fx", status="complete", notes=STAGING_FIX)
    pushing({"auto-open-pr/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gated("fix/x", "staging", sha, prs=open_pr()))
        tick(st)
        assert refused_sends() == [], sends()
        assert origin_head(main, "fix/x") == sha and "pr create" not in gh_verbs(), gh_verbs()
    finally:
        pushing_off()


@scenario
def auto_push_reads_pr_text_after_a_leading_paragraph():
    """Live (da-logs-5): gate-copy-terse-01 was refused for missing PR text while its
    owner's update held a `PR title:` and `PR body:` block after a done-check paragraph.
    A line-anchored block anywhere in an update counts; `PR title:` inside a line does not."""
    main, sha, st, screen = fix_repo("auto-pr-text-late", title=False)
    ticket_row("fx", status="complete", notes=STAGING_FIX)
    update("fx", "progress", f"Done-check: both suites pass. See PR title: later.\n\n{PR_TEXT}")
    pushing({"auto-pr-text-late/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert refused_sends() == [], sends()
        assert gh_writes() == ["pr create", "pr merge"], gh_verbs()
    finally:
        pushing_off()


@scenario
def auto_push_merges_only_once_the_pr_check_is_green():
    """Operator 07:15Z: no auto-merge while the PR check is pending or red. Pending: the
    head is pushed and the PR opened, no merge. Red: one PR RED event, no merge. Green
    and CLEAN: merged at once; green but blocked: auto-merge on at that head."""
    main, sha, st, screen = fix_repo("auto-green")
    pushing({"auto-green/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gated("fix/x", "staging", sha, checks="pending"))
        tick(st)
        assert origin_head(main, "fix/x") == sha
        assert "pr merge" not in gh_verbs(), gh_verbs()
        opened = gated("fix/x", "staging", sha, checks="red", prs=open_pr())
        world(screen, git={}, git_bin=GIT, gh=opened)
        tick(st, NOW + 30)
        assert "pr merge" not in gh_verbs(), gh_verbs()
        red = [s[1] for s in sends() if s[1].startswith("PR RED")]
        assert red == [f"PR RED fx: pull request 12 for fix/x at {sha[:7]}, check ci failed"], sends()
        world(screen, git={}, git_bin=GIT, gh=gated("fix/x", "staging", sha, merge_state="CLEAN", prs=open_pr()))
        tick(st, NOW + 60 + da.RECHECK)
        merges = [r["args"] for r in calls("gh") if r["args"][:2] == ["pr", "merge"]]
        expected = ["pr", "merge", "12", "--merge", "--match-head-commit", sha, "--repo", "auto-green/origin"]
        assert merges == [expected], merges
        assert delivered_lines("fx")[-1] == f"MERGED pull request #12 at {sha}", delivered_lines("fx")
    finally:
        pushing_off()


@scenario
def auto_push_turns_on_auto_merge_for_a_replacement_pull_request():
    """Review da-3-11: auto-merge went on for PR 12 at a green head; the operator closed
    it and opened PR 13 on the same branch, base and head, and the stored marker for that
    head kept auto-merge off PR 13 for good. The marker names its pull request."""
    main, sha, st, screen = fix_repo("auto-replaced")
    pushing({"auto-replaced/origin": {}})
    try:
        world(screen, git={}, git_bin=GIT, gh=gh_for("fix/x", "staging", sha))
        tick(st)
        assert delivered_lines("fx")[-1] == f"AUTO-MERGE pull request #12 at {sha}", delivered_lines("fx")
        table = gh_for("fix/x", "staging", sha, number=13, prs=open_pr(number=13), create=False)
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st, NOW + da.RECHECK + 1)
        assert gh_writes() == ["pr merge"], gh_verbs()
        assert delivered_lines("fx")[-1] == f"AUTO-MERGE pull request #13 at {sha}", delivered_lines("fx")
    finally:
        pushing_off()


@scenario
def auto_push_keeps_an_earlier_auto_merge_marker_on_its_pull_request():
    """Reviews of cfb09bc and 642aa6c: a state saved before the marker named its pull
    request holds the head alone, and the PR stored beside it may be a later one. With
    auto-merge on at GitHub the marker is that PR's; off, which is either an operator's
    hold or a replacement PR, nothing is merged or armed and one line asks a person. With
    auto-merge on the marker is dropped (review of ba41885) and the tick arms it afresh."""
    for name, stored, auto in (("auto-old-on", 12, True), ("auto-old-same", 12, False), ("auto-old-moved", 13, False)):
        main, sha, st, screen = fix_repo(name)
        git_at(main, "push", "-q", "origin", f"{sha}:refs/heads/fix/x")
        st["deliveries"] = {"|fix/x": {"told": [], "pr": stored, "auto": sha, "pushed": sha, "merged": False}}
        pushing({f"{name}/origin": {}})
        try:
            number = stored
            world(screen, git={}, git_bin=GIT, gh=gated("fix/x", "staging", sha, prs=open_pr(auto=auto, number=number)))
            tick(st)
            marker = st["deliveries"]["|fix/x"]["auto"]
            if auto:
                armed = [r["args"] for r in calls("gh") if r["args"][:3] == ["pr", "merge", "12"]]
                assert len(armed) == 1 and "--auto" in armed[0], (name, gh_verbs())
                assert marker == [12, sha] and refused_sends() == [], (name, marker, sends())
            else:
                assert gh_writes() == [], (name, gh_verbs())
                assert refused_sends() == [
                    f"PUSH REFUSED fx: fix/x at {sha[:7]} on local, auto-merge: auto-merge is off on "
                    f"pull request {number} and the saved marker names no pull request, turn it on or merge by hand"
                ], (name, sends())
        finally:
            pushing_off()


@scenario
def auto_push_legacy_marker_is_no_proof_the_migration_hold_ran():
    """Review of ba41885: an older tick, whose path reader missed a migration, armed
    auto-merge on PR 12 and saved the head alone. GitHub still shows that auto-merge, so
    the marker upgraded and the hold was skipped. A legacy marker is dropped instead: the
    migration hold turns auto-merge off and says so."""
    main, _, _, _ = fix_repo("auto-old-migration")
    (main / "migrations").mkdir()
    (main / "migrations" / "001_é.sql").write_text("create table t (id int);\n")
    git_at(main, "add", "-A")
    git_at(main, "commit", "-q", "-m", "migration")
    sha = head_of(main, "fix/x")
    git_at(main, "push", "-q", "origin", f"{sha}:refs/heads/fix/x")
    update("fx", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:fx-r")
    ticket_row("fx", status="complete", notes=STAGING_FIX)
    st = fresh_state(fx=rec("fx", "c1", worktree=str(main)))
    st["deliveries"] = {"|fix/x": {"told": [], "pr": 12, "auto": sha, "pushed": sha, "merged": False}}
    screen = local(term("term-fx", 100, worktree=str(main)))
    pushing({"auto-old-migration/origin": {}})
    try:
        table = gh_for("fix/x", "staging", sha, prs=open_pr(auto=True))
        table[gh_key("pr merge 12 --disable-auto")] = [0, ""]
        world(screen, git={}, git_bin=GIT, gh=table)
        tick(st)
        disable = [r["args"] for r in calls("gh") if "--disable-auto" in r["args"]]
        assert len(disable) == 1 and gh_writes() == ["pr merge"], gh_verbs()
        text = "NO AUTO-MERGE fx: pull request 12 for fix/x adds a migration, auto-merge left off"
        assert refused_sends() == [text], sends()
    finally:
        pushing_off()


@scenario
def cleanup_keeps_a_worktree_until_its_pull_request_merges():
    """Live: a complete ticket's worktree went while its PR check was still pending. A
    worktree whose delivery has an unmerged pull request is kept; once it merged, it
    goes."""
    reset_db()
    ticket_row("pk", status="complete")
    st = fresh_state(pk=rec("pk", "c1"))
    st["deliveries"] = {"|feature/feat": {"told": [], "pr": 12, "tickets": ["pk"], "merged": False}}
    world(local(term("term-pk", 100)), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "pk" in st["workers"]
    st["deliveries"]["|feature/feat"]["merged"] = True
    world([], git=git, gh=merged)
    tick(st, NOW + da.RECHECK + 1)
    assert len(calls("orca", ["worktree", "rm"])) == 1, calls("orca")


@scenario
def upgrade_keeps_unsent_verdicts():
    """A state saved before the floor existed still sends each PUSH its records had yet
    to send, once, and never one already delivered or one on a ticket with no record."""
    reset_db()
    for tid in ("ug", "nr"):
        ticket_row(tid)
    update("nr", "finding", "REVIEW VERDICT: PUSH 0a0b0c0", agent="codex:nr-r")
    sent = update("ug", "finding", "REVIEW VERDICT: PUSH 1a1b1c1", agent="codex:ug-r")
    update("ug", "finding", "REVIEW VERDICT: PUSH 2a2b2c2", agent="codex:ug-r")
    st = fresh_state(ug=rec("ug", "c1"))
    del st["cleared_floor"]
    st["sent"][f"ug|cleared|{sent}"] = 1
    screen = local(term("term-ug", 100))
    cleared = ("term-orch", "CLEARED REVIEW ug: PUSH 2a2b2c2", True)
    world(screen)
    tick(st)
    assert [s for s in sends() if s[0] == "term-orch"] == [cleared], sends()
    world(screen)
    tick(st)
    assert [s for s in sends() if s[0] == "term-orch"] == [], sends()


@scenario
def cleared_review_past_a_lost_terminal():
    """A PUSH posted after the coder's terminal went missing is still one CLEARED
    REVIEW; the lost record's own STUCK, sent before, is not repeated."""
    reset_db()
    ticket_row("lt")
    lost = "STUCK lt claude:lt: terminal term-lt is not listed. Left alone"
    st = fresh_state(lt=rec("lt", "c1", lost=lost))
    st["sent"][f"lt|lost|{st['workers']['lt']['launched_at']}"] = 1
    update("lt", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:lt-r")
    world([])
    tick(st)
    assert sends() == [("term-orch", "CLEARED REVIEW lt: PUSH 1a2b3c4", True)], sends()
    world([])
    tick(st)
    assert sends() == [], sends()


@scenario
def owner_push_text_holds_nothing():
    """The owner's own PUSH text while the orchestrator is busy is no CLEARED REVIEW:
    nothing is held, no STUCK follows, and nothing is sent once it is back."""
    reset_db()
    ticket_row("ow")
    update("ow", "finding", "REVIEW VERDICT: PUSH 1a2b3c4 received, merging")
    st = fresh_state(ow=rec("ow", "c1"))
    screen = local(term("term-ow", 100))
    for _ in range(4):
        world(screen, orch_tail=CLAUDE_PERMISSION)
        tick(st)
        assert not [n for n in notes() if "STUCK" in n], notes()
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def wait_ci():
    """Pending checks keep it waiting; finished checks wake it once."""
    reset_db()
    ticket_row("c1t")
    update("c1t", "blocker", "Waiting on: ci")
    st = fresh_state(c1t=rec("c1t", "c1"))
    world(local(term("term-c1t", 100)), gh={CHECKS: [8, "pass\npending\n"]})
    tick(st)
    assert sends() == []
    world(local(term("term-c1t", 100)), gh={CHECKS: [0, "pass\nfail\npass\n"]})
    tick(st)
    assert sends() == [
        (
            "term-c1t",
            woke(
                "c1t", "dev-auto: CI finished with 1 fail, 2 pass. Continue ticket c1t."
            ),
            True,
        )
    ]
    assert calls("gh")[0]["cwd"] == st["workers"]["c1t"]["worktree"]
    world(local(term("term-c1t", 100)), gh={CHECKS: [0, "pass\n"]})
    tick(st)
    assert sends() == []


def feature_checks(branch):
    return f"pr checks {branch} --json bucket --jq .[].bucket"


def pr_count(branch):
    return f"pr list --head {branch} --state all --json number --jq length"


@scenario
def wait_ci_feature_pr():
    """Live (dev-auto-fixes-02 update 1987): a task branch without a pull request is
    tracked on its feature branch's pull request; finished checks, passed or failed,
    wake it once."""
    reset_db()
    for tid, feature in (("fp", "feature/f"), ("fq", "feature/g")):
        ticket_row(tid, notes=f"Branch: `feat/a` · Feature branch: `{feature}`")
        update(tid, "blocker", "Waiting on: ci")
    st = fresh_state(fp=rec("fp", "c1"), fq=rec("fq", "c2"))
    terms = local(term("term-fp", 100), term("term-fq", 100))
    pending = [8, "pass\npending\n"]
    none = {pr_count("feat/a"): [0, "0\n"]}
    world(terms, gh={**none, feature_checks("feature/f"): pending, feature_checks("feature/g"): pending})
    tick(st)
    assert sends() == [], sends()
    finished = {
        **none,
        feature_checks("feature/f"): [0, "pass\nfail\n"],
        feature_checks("feature/g"): [0, "pass\npass\n"],
    }
    world(terms, gh=finished)
    tick(st)
    assert sorted(sends()) == [
        ("term-fp", woke("fp", "dev-auto: CI finished with 1 fail, 1 pass. Continue ticket fp."), True),
        ("term-fq", woke("fq", "dev-auto: CI finished with 2 pass. Continue ticket fq."), True),
    ], sends()
    world(terms, gh=finished)
    tick(st)
    assert sends() == [], sends()


@scenario
def wait_ci_named_run():
    """A CI wait that names a run id or run URL is tracked on that run, ahead of any
    pull request; prose such as "run 3 of 5" names no run and stays on its PR."""
    reset_db()
    ticket_row("rn")
    update("rn", "blocker", "Waiting on: ci run 4567890123 on the feature PR")
    ticket_row("ru")
    update("ru", "blocker", "Waiting on: ci https://github.com/o/r/actions/runs/777/job/9")
    ticket_row("rz")
    update("rz", "blocker", "Waiting on: ci (run 3 of 5)")
    st = fresh_state(rn=rec("rn", "c1"), ru=rec("ru", "c2"), rz=rec("rz", "c3"))
    terms = local(term("term-rn", 100), term("term-ru", 100), term("term-rz", 100))
    by_id = "run view 4567890123 --json status,conclusion"
    by_url = "run view 777 -R o/r --json status,conclusion"
    running = [0, json.dumps({"status": "in_progress", "conclusion": ""})]
    world(terms, gh={CHECKS: [0, "pass\n"], by_id: running, by_url: running})
    tick(st)
    assert sends() == [
        ("term-rz", woke("rz", "dev-auto: CI finished with 1 pass. Continue ticket rz."), True)
    ], sends()
    assert not [r for r in calls("gh") if r["args"][:3] == ["run", "view", "3"]], calls("gh")
    finished = {
        CHECKS: [0, "pass\n"],
        by_id: [0, json.dumps({"status": "completed", "conclusion": "failure"})],
        by_url: [0, json.dumps({"status": "completed", "conclusion": "success"})],
    }
    world(terms, gh=finished)
    tick(st)
    assert sorted(sends()) == [
        ("term-rn", woke("rn", "dev-auto: CI finished with failure on run 4567890123. Continue ticket rn."), True),
        ("term-ru", woke("ru", "dev-auto: CI finished with success on run 777. Continue ticket ru."), True),
    ], sends()
    world(terms, gh=finished)
    tick(st)
    assert sends() == [], sends()


@scenario
def wait_ci_own_pr():
    """A task branch with its own pull request is tracked there, never on the feature
    branch's."""
    reset_db()
    ticket_row("op", notes="Branch: `feat/a` · Feature branch: `feature/f`")
    update("op", "blocker", "Waiting on: ci")
    st = fresh_state(op=rec("op", "c1"))
    terms = local(term("term-op", 100))
    world(terms, gh={CHECKS: [8, "pending\n"], feature_checks("feature/f"): [0, "pass\n"]})
    tick(st)
    assert sends() == [], sends()
    world(terms, gh={CHECKS: [0, "pass\n"], feature_checks("feature/f"): [0, "fail\n"]})
    tick(st)
    assert sends() == [
        ("term-op", woke("op", "dev-auto: CI finished with 1 pass. Continue ticket op."), True)
    ], sends()
    assert not [r for r in calls("gh") if "feature/f" in r["args"]], calls("gh")


@scenario
def wait_ci_untracked():
    """No run and no readable pull request anywhere is one STUCK; a task branch whose own
    pull request reports no checks yet, in gh's error text, or whose probes time out,
    never falls back to the feature branch's."""
    reset_db()
    ticket_row("nn", notes="Branch: `feat/a` · Feature branch: `feature/f`")
    nn = update("nn", "blocker", "Waiting on: ci")
    ticket_row("nb", notes="Branch: `feat/b` · Feature branch: `feature/g`")
    nb = update("nb", "blocker", "Waiting on: ci")
    ticket_row("nt", notes="Branch: `feat/c` · Feature branch: `feature/h`")
    nt = update("nt", "blocker", "Waiting on: ci")
    st = fresh_state(nn=rec("nn", "c1"), nb=rec("nb", "c2"), nt=rec("nt", "c3"))
    terms = local(
        term("term-nn", 100),
        term("term-nb", 100, branch="refs/heads/feat/b"),
        term("term-nt", 100, branch="refs/heads/feat/c"),
    )
    gh = {
        pr_count("feat/a"): [0, "0\n"],
        pr_count("feat/b"): [0, "1\n"],
        feature_checks("feat/b"): [1, "no checks reported on the 'feat/b' branch\n"],
        pr_count("feat/c"): [124, ""],
        feature_checks("feat/c"): [124, ""],
        feature_checks("feature/h"): [0, "pass\n"],
    }
    stuck = sorted(
        ("term-orch", f"STUCK {tid}: dev-auto cannot track its Waiting on note, update {n} on the ticket", True)
        for tid, n in (("nn", nn), ("nb", nb), ("nt", nt))
    )
    world(terms, gh=gh)
    tick(st)
    assert sorted(sends()) == stuck, sends()
    world(terms, gh={**gh, feature_checks("feature/g"): [0, "pass\n"]})
    tick(st)
    assert sends() == [], sends()
    assert not [r for r in calls("gh") if {"feature/g", "feature/h"} & set(r["args"])], calls("gh")


@scenario
def wait_ci_no_checks_yet_still_wakes():
    """dev-auto-fixes-50 (b), disproved: a pull request that reports no checks yet, as
    right after a push, is the untracked STUCK once, not for good: the wait is read
    again each tick and wakes the worker once its checks finish."""
    reset_db()
    ticket_row("nc", notes="Branch: `feat/b` · Feature branch: none")
    n = update("nc", "blocker", "Waiting on: ci")
    st = fresh_state(nc=rec("nc", "c1"))
    terms = local(term("term-nc", 100, branch="refs/heads/feat/b"))
    gh = {pr_count("feat/b"): [0, "1\n"]}
    none = [1, "no checks reported on the 'feat/b' branch\n"]
    world(terms, gh={**gh, feature_checks("feat/b"): none})
    tick(st)
    text = f"STUCK nc: dev-auto cannot track its Waiting on note, update {n} on the ticket"
    assert sends() == [("term-orch", text, True)], sends()
    world(terms, gh={**gh, feature_checks("feat/b"): [8, "pending\n"]})
    tick(st)
    assert sends() == [], sends()
    world(terms, gh={**gh, feature_checks("feat/b"): [0, "pass\n"]})
    tick(st)
    text = "dev-auto: CI finished with 1 pass. Continue ticket nc."
    assert sends() == [("term-nc", woke("nc", text), True)], sends()


@scenario
def wait_ci_reads_nothing_after_its_wake():
    """dev-auto-fixes-50 (a): once a CI wait's wake is typed, later ticks read no CI for
    that note; on a remote host each read would open a terminal."""
    reset_db()
    ticket_row("cw")
    update("cw", "blocker", "Waiting on: ci")
    st = fresh_state(cw=rec("cw", "c1"))
    terms = local(term("term-cw", 100))
    world(terms, gh={CHECKS: [0, "pass\n"]})
    tick(st)
    text = "dev-auto: CI finished with 1 pass. Continue ticket cw."
    assert sends() == [("term-cw", woke("cw", text), True)], sends()
    for _ in range(2):
        world(terms, gh={CHECKS: [0, "pass\n"]})
        tick(st)
        assert sends() == [], sends()
        assert calls("gh") == [], calls("gh")
        assert not [n for n in notes() if "STUCK" in n], notes()


@scenario
def wait_process():
    """Live (dev-auto-fixes-02 update 1820 item 1): a wait on a local process wakes the
    worker once when the process is gone, at once when it never existed; a process wait
    with no pid is one STUCK."""
    reset_db()
    ticket_row("pl")
    update("pl", "blocker", "Waiting on: process 4242 (coverage run)")
    ticket_row("pn")
    update("pn", "blocker", "Waiting on: process 999999")
    ticket_row("pq")
    bare = update("pq", "blocker", "Waiting on: process soon")
    st = fresh_state(pl=rec("pl", "c1"), pn=rec("pn", "c2"), pq=rec("pq", "c3"))
    terms = local(term("term-pl", 100), term("term-pn", 100), term("term-pq", 100))
    world(terms, pids=["4242"])
    tick(st)
    assert sorted(sends()) == [
        ("term-orch", f"STUCK pq: dev-auto cannot track its Waiting on note, update {bare} on the ticket", True),
        ("term-pn", woke("pn", "dev-auto: process 999999 has exited. Continue ticket pn."), True),
    ], sends()
    assert [r["cwd"] for r in calls("ps") if r["args"][1] == "4242"] == [st["workers"]["pl"]["worktree"]]
    assert calls("orca", ["terminal", "create"]) == []
    world(terms, pids=["4242"])
    tick(st)
    assert sends() == [], "a live process gives no wake"
    world(terms, pids=[])
    tick(st)
    assert sends() == [
        ("term-pl", woke("pl", "dev-auto: process 4242 has exited. Continue ticket pl."), True)
    ], sends()
    world(terms, pids=[])
    tick(st)
    assert sends() == [], "one wake per wait"


@scenario
def wait_ticket_and_operator():
    reset_db()
    ticket_row("dep")
    ticket_row("w1")
    update("w1", "blocker", "Waiting on: ticket:dep (schema first)")
    ticket_row("op")
    needs = update("op", "blocker", "Waiting on: operator to pick the layout")
    st = fresh_state(w1=rec("w1", "c1"), op=rec("op", "c2"))
    st["orchestrator"] = None
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    world(local(term("term-w1", 100), term("term-op", 100)))
    tick(st)
    assert sends(events=False) == [], "no orchestrator pointer: nothing typed"
    assert [l.split(" ", 2)[2][:12] for l in event_lines()] == ["NEEDS YOU op"], event_lines()
    st["orchestrator"] = {"terminal": "term-orch", "host": "local", "account": None}
    ticket_row("dep", status="closed")
    world(local(term("term-w1", 100), term("term-op", 100)))
    tick(st)
    assert sorted(sends()) == [
        (
            "term-w1",
            woke("w1", "dev-auto: ticket dep is closed. Continue ticket w1."),
            True,
        ),
    ], sends()
    assert event_lines()[0].endswith(
        f"NEEDS YOU op: its worker waits on the operator, update {needs} on the ticket"
    )
    assert [s[1][:7] for s in sends(events=False) if s[0] == "term-orch"] == ["EVENTS:"]
    world(local(term("term-w1", 100), term("term-op", 100)))
    tick(st)
    assert sends() == []


@scenario
def review_copy_of_operator_wait():
    """Live (delivered.log pairs 156/160, 170/176, 213/216): a reviewer copied its coder's
    operator wait onto its review ticket and raised a second NEEDS YOU. The copy raises
    none while the coder's wait is open, nor after the coder records the answer."""
    reset_db()
    ticket_row("ow")
    ticket_row("ow-review", agent="codex:ow-review")
    needs = update("ow", "blocker", "Waiting on: operator to pick the layout")
    copy = "Waiting on: operator, the coder's layout question"
    update("ow-review", "blocker", copy, agent="codex:ow-review")
    reviewer = rec("ow-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(ow=rec("ow", "c1"), **{"ow-review": reviewer})
    screen = local(term("term-ow", 100), term("term-ow-review", 100, ident="codex"))
    world(screen)
    tick(st)
    text = (
        f"NEEDS YOU ow: its worker waits on the operator, update {needs} on the ticket"
    )
    assert sends() == [("term-orch", text, True)], sends()
    update("ow", "progress", "operator picked the grid")
    world(screen)
    tick(st)
    assert sends() == [], sends()


MENU = [
    "Approaching rate limits",
    "› 1. Switch to gpt-6-luna",
    "  2. Keep current model",
    "  3. Keep current model (never show again)",
]


@scenario
def codex_menu():
    """Codex's switch-model menu is answered with the option that keeps the model."""
    reset_db()
    ticket_row("cx", agent="codex:cx")
    st = fresh_state(cx=rec("cx", "x1", agent="codex", model="gpt-6.1-sol"))
    world(local(term("term-cx", 120, MENU, ident="codex")))
    tick(st)
    assert sends() == [("term-cx", "2", False)]


def codex_screen(model="GPT-6.1-Sol xhigh", goal="Goal achieved (45s)", above=()):
    """A live Codex 0.167.2 idle screen; its status line shows the model, effort and goal."""
    return [
        "• Claimed dev-auto-fixes-12-review and recorded blocker #2018.",
        *above,
        "  Worked for 59s • 12:50 PM",
        "› Ask Codex to do anything",
        f"  {model} · ~/orca/workspaces/.agents/dev-auto-fixes.codex-status-line"
        + " " * 47
        + goal,
        "  ← for agents · ? for shortcuts" + " " * 90 + "⚠ 2 warnings · f2 to view",
    ]


# The rate-limit switch prompt's rows, as Codex 0.167.2 words them (from its binary).
CODEX_SWITCH = [
    "  Approaching rate limits",
    "› 1. Switch to gpt-6-luna   Uses fewer credits for upcoming turns",
    "  2. Keep current model",
    (
        "  3. Keep current model (never show again)   Hide future rate limit reminders"
        " about switching models"
    ),
]


def sol_reviewer(tid, model="gpt-6.1-sol", effort="xhigh"):
    """A Codex reviewer with REVIEW READY due on its coder's ticket."""
    reset_db()
    ticket_row(tid)
    ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
    update(tid, "finding", "REVIEW READY 1a2b3c4")
    worker = rec(
        f"{tid}-review", "x1", agent="codex", model=model, effort=effort
    )
    return fresh_state(**{f"{tid}-review": worker})


@scenario
def codex_model_drift_is_stuck_once():
    """A Codex session launched as gpt-6.1-sol xhigh whose status line shows another
    model or effort gets no keystroke and one STUCK naming both, on the tick, whether
    or not a wake is due."""
    for shown in ("GPT-6-Luna medium", "GPT-6.1-Sol medium"):
        for due in (True, False):
            st = sol_reviewer("md")
            if not due:
                reset_db()
                ticket_row("md")
                ticket_row("md-review", agent="codex:md-review")
            screen = local(term("term-md-review", 5, codex_screen(shown), ident="codex"))
            world(screen)
            tick(st)
            stuck = (
                f"STUCK md-review codex:md-review: its status line shows {shown}, "
                "not the launched gpt-6.1-sol xhigh. Nothing is typed into it"
            )
            assert sends() == [("term-orch", stuck, True)], (shown, due, sends())
            world(screen)
            tick(st)
            assert sends() == [], (shown, due, sends())


@scenario
def codex_drift_after_the_ticks_read_takes_no_keys():
    """A model switch shown only on the wake's own pre-send read takes no key, Ctrl-U
    included; the next tick raises the one STUCK."""
    st = sol_reviewer("mf")
    row, screen = term("term-mf-review", 5, codex_screen(), ident="codex")
    screen["reads_left"] = 1
    screen["then"] = {"status": "running", "tail": codex_screen("GPT-6-Luna medium")}
    world(local((row, screen)))
    tick(st)
    typed = [s for s in sends(include_clear=True) if s[0] == "term-mf-review"]
    assert typed == [], typed
    tick(st)
    stuck = (
        "STUCK mf-review codex:mf-review: its status line shows GPT-6-Luna medium, "
        "not the launched gpt-6.1-sol xhigh. Nothing is typed into it"
    )
    assert ("term-orch", stuck, True) in sends(), sends()
    assert [s for s in sends(include_clear=True) if s[0] == "term-mf-review"] == []


@scenario
def stop_is_not_typed_into_a_drifted_codex():
    """After a stop, a Codex worker showing another model gets no STOP; one on its
    launched model gets it."""
    for shown, told in (("GPT-6-Luna medium", False), ("GPT-6.1-Sol xhigh", True)):
        reset_db()
        worker = rec("ms", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
        st = fresh_state(ms=worker)
        st["stopped"] = 1
        row, screen = term("term-ms", 5, codex_screen(shown), ident="codex")
        world(local((row, screen)))
        da.stop_workers(st, {"local": {"term-ms": row}})
        typed = [s for s in sends(include_clear=True) if s[0] == "term-ms"]
        expect = [("term-ms", "\x15", False), ("term-ms", da.STOP, True)] if told else []
        assert typed == expect, (shown, typed)


@scenario
def drift_while_stopped_is_reported_on_the_new_cycle():
    """While stopped a drifted Codex worker takes no key and no STUCK is raised; the
    first tick of a new cycle raises the one STUCK, still with no key, and no repeat."""
    st = sol_reviewer("mz")
    st["stopped"] = 1
    screen = local(term("term-mz-review", 5, codex_screen("GPT-6-Luna medium"), ident="codex"))
    for _ in range(2):
        world(screen)
        tick(st)
        assert [s for s in sends(include_clear=True) if s[0] == "term-mz-review"] == []
        assert not any("status line shows" in s[1] for s in sends()), sends()
    st["stopped"] = False
    stuck = (
        "STUCK mz-review codex:mz-review: its status line shows GPT-6-Luna medium, "
        "not the launched gpt-6.1-sol xhigh. Nothing is typed into it"
    )
    world(screen)
    tick(st)
    assert sends() == [("term-orch", stuck, True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def codex_on_its_launched_model_is_woken():
    """A Codex session whose status line shows its launched model and effort is woken
    as before."""
    st = sol_reviewer("mk")
    world(local(term("term-mk-review", 5, codex_screen(), ident="codex")))
    tick(st)
    text = "dev-auto: REVIEW READY was posted on ticket mk. Review it as your ticket says."
    assert sends() == [("term-mk-review", woke("mk-review", text, "codex"), True)], (
        sends()
    )


def wrapped_codex_reviewer_screen(shown, columns=None):
    lines = codex_screen()
    footer = [
        f"  {shown} · ~/orca/workspaces/hr-relay/dashboard-audit-fixes.commands-no-",
        "uncertain-text · /goal Finish Concord ticket dashboard-audit-fixes-06-review",
    ]
    if columns:
        text = "".join(footer)
        footer = [text[start : start + columns] for start in range(0, len(text), columns)]
    lines[-2:-1] = footer
    return local(term("term-wf-review", 5, lines, ident="codex"))


@scenario
def wrapped_codex_footer_allows_the_launched_route():
    text = "dev-auto: REVIEW READY was posted on ticket wf. Review it as your ticket says."
    for columns in (None, 41, 50, 52):
        st = sol_reviewer("wf", "gpt-6.2-sol", "high")
        world(wrapped_codex_reviewer_screen("GPT-6.2-Sol high", columns))
        tick(st)
        assert sends() == [("term-wf-review", woke("wf-review", text, "codex"), True)], (
            columns, sends()
        )


@scenario
def wrapped_codex_footer_refuses_a_mismatched_route():
    stuck = (
        "STUCK wf-review codex:wf-review: its status line shows GPT-6-Luna medium, "
        "not the launched gpt-6.2-sol high. Nothing is typed into it"
    )
    for columns in (None, 41, 50, 52):
        st = sol_reviewer("wf", "gpt-6.2-sol", "high")
        world(wrapped_codex_reviewer_screen("GPT-6-Luna medium", columns))
        tick(st)
        assert sends() == [("term-orch", stuck, True)], (columns, sends())
        typed = [s for s in sends(include_clear=True) if s[0] == "term-wf-review"]
        assert typed == [], (columns, typed)


@scenario
def codex_switch_menu_is_never_confirmed():
    """The switch prompt in Codex 0.167.2's wording is answered with Keep current model
    and no Enter; a switch prompt in any other wording gets no keystroke and one STUCK."""
    st = sol_reviewer("mw")
    world(local(term("term-mw-review", 5, [*CODEX_SWITCH, "", ""], ident="codex")))
    tick(st)
    assert sends() == [("term-mw-review", "2", False)], sends()
    reworded = [CODEX_SWITCH[0], CODEX_SWITCH[1], "  2. Stay on GPT-6.1-Sol"]
    st = sol_reviewer("mu")
    screen = local(term("term-mu-review", 5, reworded, ident="codex"))
    world(screen)
    tick(st)
    stuck = (
        "STUCK mu-review codex:mu-review: a switch-model menu dev-auto does not know "
        "is open on its screen. Nothing is typed into it"
    )
    assert sends() == [("term-orch", stuck, True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def wake_types_nothing_into_a_codex_menu():
    """A switch menu that opens after the tick's read takes no keys from the wake."""
    reset_db()
    ticket_row("mn", agent="codex:mn")
    worker = rec("mn", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
    st = fresh_state()
    world(local(term("term-mn", 5, CODEX_SWITCH, ident="codex")))
    assert da.emit(st, "synthetic|wake|1", "dev-auto: wake", worker=worker) is False
    assert sends(include_clear=True) == [], sends(include_clear=True)


@scenario
def codex_folder_trust_is_asking():
    """Codex's folder trust prompt in an untrusted repo is a question: no wake is typed."""
    reset_db()
    ticket_row("cd", agent="cursor:cd")
    update("cd", "progress", "REVIEW READY 1a2b3c4")
    st = fresh_state(**{"cd-review": rec("cd-review", "x1", agent="codex", since=0)})
    ticket_row("cd-review", agent="codex:cd-review")
    trust = [
        "Folder access",
        "Trust this folder? Codex can read, edit, and run files here, subject to your",
        "› 1. Trust and continue",
        "  2. Back to Agent Command Center",
        "  enter continue · esc back",
    ]
    world(local(term("term-cd-review", 90, trust, ident="codex")))
    tick(st)
    assert sends() == [
        (
            "term-orch",
            (
                "NEEDS YOU cd-review codex:cd-review: a prompt is open on its screen, "
                "which is recorded on the ticket"
            ),
            True,
        )
    ], sends()


CLAUDE_PERMISSION = [
    "⏺ Bash(rm -rf build)",
    " Bash command",
    "   rm -rf build",
    " Do you want to proceed?",
    " ❯ 1. Yes",
    "   2. Yes, and don't ask again for rm commands in /repo",
    "   3. No, and tell Claude what to do differently (esc)",
]


@scenario
def agent_prose_question_is_not_asking():
    """An idle Claude whose last message asks "Do you want to ..." has no prompt open:
    no NEEDS YOU. A permission dialog with its choice rows still is one."""
    prose = [
        "⏺ Both options work. Do you want to keep the old route as a fallback? I recorded the",
        "  question on the ticket and I am waiting.",
        "────────────────────────────────",
        "❯ ",
        "────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
    assert da.overlay(prose) is None, da.overlay(prose)
    quoted = [
        '  handler. I did not see the "Hooks need review" screen in any test.',
        "  Next: the Codex reviewer's verdict.",
        "────────────────────────────────",
        "❯",
        "────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
    assert da.overlay(quoted) is None, da.overlay(quoted)
    assert da.overlay(CLAUDE_PERMISSION) == "asking"
    reset_db()
    ticket_row("pq")
    st = fresh_state(pq=rec("pq", "c1"))
    world(local(term("term-pq", 31 * 60, prose)))
    tick(st)
    assert not any("NEEDS YOU" in text for _t, text, _e in sends()), sends()
    world(local(term("term-pq", 31 * 60, CLAUDE_PERMISSION)))
    tick(st)
    assert [text.split(":")[0] for _t, text, _e in sends()] == [
        "NEEDS YOU pq claude"
    ], sends()


@scenario
def unnumbered_permission_dialog_holds_the_wake():
    """A permission dialog whose choices carry no numbers is still open: a waiting
    verdict raises NEEDS YOU and types nothing into the worker."""
    allow = ["Allow command: rm -rf workspace", "❯ Yes", "  No"]
    assert da.overlay(allow) == "asking"
    reset_db()
    ticket_row("ud")
    update("ud", "progress", "REVIEW READY 1a2b3c4")
    update("ud", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:ud-r")
    st = fresh_state(ud=rec("ud", "c1"))
    world(local(term("term-ud", 31 * 60, allow)))
    tick(st)
    assert [row for row in sends() if row[0] == "term-ud"] == [], sends()
    assert any(
        row[0] == "term-orch" and row[1].startswith("NEEDS YOU ud ") for row in sends()
    ), sends()


@scenario
def codex_input_under_prose_question_is_not_asking():
    """An idle Codex whose last message asks "Do you want to ..." shows its input box
    and footer, not dialog choices: no NEEDS YOU, and a waiting verdict wakes it."""
    idle = [
        "Do you want to keep the current route as a fallback?",
        "› Ask Codex to do anything",
        "gpt-6.1-sol",
    ]
    assert da.overlay(idle) is None, da.overlay(idle)
    reset_db()
    ticket_row("ci", agent="codex:ci")
    update("ci", "progress", "REVIEW READY 1a2b3c4")
    update("ci", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:ci-r")
    st = fresh_state(ci=rec("ci", "x1", agent="codex", model="gpt-6.1-sol"))
    world(local(term("term-ci", 31 * 60, idle, ident="codex")))
    tick(st)
    assert not any("NEEDS YOU" in row[1] for row in sends()), sends()
    assert any(
        row[0] == "term-ci" and "review verdict was posted" in row[1] for row in sends()
    ), sends()


CLAUDE_TRUST = [
    " Accessing workspace:",
    " Quick safety check: Is this a project you created or one you trust? (Like your",
    " ❯ No, exit",
    "   Yes, I trust this folder",
    " Enter to confirm · Esc to cancel",
]


@scenario
def claude_workspace_trust_is_asking():
    """Claude's workspace trust dialog defaults to "No, exit": move to Yes and confirm."""
    reset_db()
    ticket_row("wt")
    st = fresh_state(wt=rec("wt", "c1"))
    world(local(term("term-wt", 31 * 60, CLAUDE_TRUST)))
    tick(st)
    assert sends() == [("term-wt", "\x1b[B", False), ("term-wt", "", True)], sends()
    world(local(term("term-wt", 31 * 60, CLAUDE_TRUST)))
    tick(st)
    assert sends() == [], "the trust screen is answered once"


@scenario
def trial_claude_dialog_is_not_exited():
    """Live: Claude lists no agent while its trust dialog is open, nor for ~49 s after
    launch. Neither is an exit: the dialog is answered once, a start and an unread
    screen wait, and launch will not close it. A bare shell or a lost identity proves
    nothing."""
    reset_db()
    ticket_row("cq")
    st = fresh_state(cq=rec("cq", "c1"))
    world(local(term("term-cq", 31 * 60, CLAUDE_TRUST, ident=None)))
    tick(st)
    assert created() == [] and calls("orca", ["terminal", "close"]) == []
    assert sends() == [("term-cq", "\x1b[B", False), ("term-cq", "", True)], sends()
    world(local(term("term-cq", 31 * 60, CLAUDE_TRUST, ident=None)))
    tick(st)
    assert sends() == [] and created() == [], "answered once, and never closed"
    st = fresh_state(cq=rec("cq", "c1", launched_at=NOW - 49))
    starting = ["╭───────────────────────────╮", "│ ✻ Welcome to Claude Code! │"]
    world(local(term("term-cq", 2, starting, ident=None)))
    tick(st)
    assert sends() == [] and created() == [] and "held" not in st["workers"]["cq"]
    row, _ = term("term-cq", 31 * 60, ident=None)
    orch, orch_screen = term("term-orch", 5, worktree="/repo")
    set_fixture(terminals={"local": [row, orch]}, screens={"term-orch": orch_screen})
    tick(fresh_state(cq=rec("cq", "c1")))
    assert sends() == [] and created() == [], "an unread screen is no evidence"
    old = rec("cq", "c1")
    state_file.write_text(json.dumps({"workers": {"cq": old}}))
    world(local(term("term-cq", 31 * 60, CLAUDE_TRUST, ident=None)))
    argv = ["launch", "cq", "--agent", "codex", "--model", "m"]
    done = cli(*argv, "--worktree", old["worktree"])
    assert done.returncode and "may still hold its agent" in done.stderr, done
    assert created() == [] and calls("orca", ["terminal", "close"]) == []
    state_file.unlink()
    st = fresh_state(cq=rec("cq", "c1"))
    world(local(term("term-cq", 31 * 60, ["➜  cq git:(feat/a)"], ident=None)))
    tick(st)
    assert len(created()) == 1
    st = fresh_state(cq=rec("cq", "c1"))
    world(local(term("term-cq", 31 * 60, ["Bye!"], ident=None)))
    tick(st)
    assert created() == [] and calls("orca", ["terminal", "close"]) == []
    assert [s[1].split(":")[0] for s in sends()] == ["STUCK cq claude"], sends()


LIVE_CLAUDE = [
    "  the owner's RELEASE.",
    "✻ Churned for 14s · done 3:47 PM",
    "─" * 60,
    "❯",
    "─" * 60,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]


@scenario
def b1_identity_drop_is_no_exit():
    """Live: a working Claude's row lost agentIdentity. Neither tick nor launch reads that
    as an exit: its terminal is not closed and no second agent starts."""
    reset_db()
    ticket_row("lv")
    st = fresh_state(lv=rec("lv", "c1"))
    world(local(term("term-lv", 120, LIVE_CLAUDE)))
    tick(st)
    world(local(term("term-lv", 120, LIVE_CLAUDE, ident=None)))
    tick(st)
    assert calls("orca", ["terminal", "close"]) == [] and created() == []
    state_file.write_text(json.dumps({"workers": {"lv": st["workers"]["lv"]}}))
    world(local(term("term-lv", 120, LIVE_CLAUDE, ident=None)))
    argv = ["launch", "lv", "--agent", "claude", "--model", OPUS]
    done = cli(*argv, "--worktree", st["workers"]["lv"]["worktree"])
    assert done.returncode and "may still hold its agent" in done.stderr, done
    assert calls("orca", ["terminal", "close"]) == [] and created() == []
    state_file.unlink()


@scenario
def m1_unknown_screen_is_stuck_once():
    """No agent listed, no prompt or dialog known, quiet a minute, ten minutes after launch:
    one STUCK per launch with the screen on the ticket; no keystroke, no close."""
    reset_db()
    ticket_row("uk")
    st = fresh_state(uk=rec("uk", "c1", launched_at=NOW - 599))
    starship = ["~/wt/uk on feat/a", "❯"]
    world(local(term("term-uk", 120, starship, ident=None)))
    tick(st)
    assert sends() == [], "inside its first ten minutes"
    world(local(term("term-uk", 120, starship, ident=None)))
    tick(st, NOW + 1)
    stuck = (
        "STUCK uk claude:uk: no agent is listed in its terminal and its screen is no "
        "shell prompt or known dialog. Left alone"
    )
    assert sends() == [("term-orch", stuck, True)], sends()
    assert notes() == [f"dev-auto: {stuck}\n~/wt/uk on feat/a\n❯"], notes()
    world(local(term("term-uk", 120, starship, ident=None)))
    tick(st, NOW + 3600)
    assert sends() == [], "once per launch"
    assert created() == [] and calls("orca", ["terminal", "close"]) == []
    assert f"uk|unknown|{NOW - 599}" in st["sent"]


@scenario
def usage_moves():
    """Claude resumes its session on another account; Codex moves to the next one."""
    reset_db()
    ticket_row("lc")
    st = fresh_state(lc=rec("lc", "c1"))
    world(local(term("term-lc", 120, ["You've hit your limit · resets 5pm"])))
    tick(st)
    (new,) = created()
    command = opt(new, "--command")
    assert f"CLAUDE_CONFIG_DIR={shlex.quote(str(homes['c2']))} claude " in command
    assert (
        "--resume sess-lc" in command
        and opt(new, "--worktree") == f"path:{st['workers']['lc']['worktree']}"
    )
    assert ["term-lc"] == [
        opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])
    ]
    assert (
        st["workers"]["lc"]["account"] == "c2"
        and st["workers"]["lc"]["terminal"] == "term-local-1"
    )
    assert notes() == [
        "dev-auto: moved claude:lc from pro1 to pro2 after a usage limit"
    ]
    ticket_row("lx", agent="codex:lx")
    st = fresh_state(lx=rec("lx", "x1", agent="codex", model="gpt-6.1-sol"))
    limited = ["■ You've hit your usage limit. Try again later."]
    world(local(term("term-lx", 120, limited, ident="codex")))
    tick(st)
    (new,) = created()
    command = opt(new, "--command")
    assert f"CODEX_HOME={shlex.quote(str(homes['x2']))} codex " in command
    assert (
        shlex.split(command)[-1].startswith("/goal ")
        and st["workers"]["lx"]["account"] == "x2"
    )


@scenario
def usage_move_closes_the_old_tab():
    """Live: after each WSL restart Orca restored closed workers from their resume
    records. The session closed before an account move goes with its tab, so no
    resume record of it is left."""
    reset_db()
    ticket_row("lt")
    st = fresh_state(lt=rec("lt", "c1"))
    world(local(term("term-lt", 120, ["You've hit your limit · resets 5pm"])))
    tick(st)
    assert st["workers"]["lt"]["account"] == "c2"
    assert tab_closes() == [("term-lt", True)] and resumable() == [], (tab_closes(), resumable())


@scenario
def usage_waits():
    """No free account: report once, then resume the worker after its reset; nothing is
    typed into its limit screen."""
    reset_db()
    for tid in ("lw", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
        update(tid, "blocker", "Waiting on: review. REVIEW READY 1a2b3c4")
    claude_usage("c1", 100, NOW + 3600)
    st = fresh_state(
        lw=rec("lw", "c1"), f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3")
    )
    limited = term("term-lw", 120, ["You've hit your limit · resets 5pm"])
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    world(local(limited, *idle))
    tick(st)
    (sent,) = sends()
    assert sent[0] == "term-orch" and sent[1].startswith(
        "WAITING lw claude:lw: usage limit on pro1, resets "
    ), sent
    assert created() == []
    world(local(limited, *idle))
    tick(st, NOW + 3600 + 299)
    assert sends() == []
    claude_usage("c1", 0, NOW + 9 * 3600, saved=NOW + 3600 + 300)
    world(
        local(
            term("term-lw", 120 + 3600 + 300, ["You've hit your limit · resets 5pm"]),
            *idle,
        )
    )
    tick(st, NOW + 3600 + 300)
    assert sends() == []
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == [
        "term-lw"
    ]
    (new,) = created()
    assert "--resume sess-lw" in opt(new, "--command")
    assert (
        st["workers"]["lw"]["account"] == "c1"
        and "limit_reset" not in st["workers"]["lw"]
    )
    clear_usage()


@scenario
def usage_limit_moves_on_its_first_tick_and_when_another_account_frees():
    """A limit screen is acted on the tick it appears, even with fresh output; a waiting
    worker moves to another account the tick one of its slots frees, before its reset."""
    reset_db()
    for tid in ("lw", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
        update(tid, "blocker", "Waiting on: review. REVIEW READY 1a2b3c4")
    claude_usage("c1", 100, NOW + 3600)
    limit = ["You've hit your limit · resets 5pm"]
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    st = fresh_state(lw=rec("lw", "c1"), f3=rec("f3", "c3"), f4=rec("f4", "c3"))
    world(local(term("term-lw", 5, limit), *idle[1:]), git=HR_RELAY_GIT)
    tick(st)
    (new,) = created()
    assert_ci_only_prompt(opt(new, "--command"))
    assert st["workers"]["lw"]["account"] == "c2", "moved on the first tick"
    reset_db()
    for tid in ("lw", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
        update(tid, "blocker", "Waiting on: review. REVIEW READY 1a2b3c4")
    st = fresh_state(
        lw=rec("lw", "c1"), f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3")
    )
    world(local(term("term-lw", 120, limit), *idle))
    tick(st)
    (sent,) = sends()
    assert sent[1].startswith("WAITING lw claude:lw: usage limit on pro1"), sent
    world(local(term("term-lw", 720, limit), *idle))
    tick(st, NOW + 600)
    assert created() == [], "nothing frees, nothing moves"
    st["workers"]["f2"]["lost"] = "STUCK f2: terminal gone"
    world(local(term("term-lw", 780, limit), *idle[1:]))
    tick(st, NOW + 660)
    (new,) = created()
    assert st["workers"]["lw"]["account"] == "c2", st["workers"]["lw"]
    assert "limit_reset" not in st["workers"]["lw"]
    clear_usage()


@scenario
def usage_fresh_reading_ends_a_wait_before_its_reset():
    """Live: an account came back below its limit and its waiting workers sat until the
    stored reset. A fresh reading below the limit ends a wait its full reading set, with
    one resume; a fresh full reading, or an old one, resumes nothing."""
    reset_db()
    for tid in ("fr", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        update(tid, "blocker", "Waiting on: review")
    claude_usage("c1", 100, NOW + 3600)
    st = fresh_state(
        fr=rec("fr", "c1"), f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3")
    )
    limit = ["You've hit your limit · resets 5pm"]
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    world(local(term("term-fr", 120, limit), *idle))
    tick(st)
    assert st["workers"]["fr"]["limit_reset"] == NOW + 3600, st["workers"]["fr"]

    def later(seconds, percent, saved):
        claude_usage("c1", percent, NOW + 3600, saved=saved)
        world(local(term("term-fr", 120, limit), *idle))
        tick(st, NOW + seconds)
        return created()

    assert later(300, 100, NOW + 300) == [], "a fresh full reading keeps the wait"
    assert later(1200, 20, NOW + 300) == [], "an old reading is unread, never free"
    (new,) = later(1500, 20, NOW + 1500)
    assert "--resume sess-fr" in opt(new, "--command") and sends() == [], sends()
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == ["term-fr"]
    worker = st["workers"]["fr"]
    assert worker["account"] == "c1" and "limit_reset" not in worker, worker
    clear_usage()


@scenario
def usage_reset_frees_the_account_once():
    """Live: after the operator reset an account, Orca kept its old full reading and its
    waiting workers sat until the stored reset. `usage-reset` counts the account's windows
    as reset until a newer reading: slot offers it, the next tick resumes its limited
    worker once, the limit line it was resumed from puts it back to no wait, and a newer
    full reading restores the limit. A newer reading below the limit leaves the replayed
    line answered."""
    state_file.unlink(missing_ok=True)
    state_file.write_text(json.dumps({"limits": {"c1": NOW + 3600}}))
    before = time.time()
    with redirect_stdout(io.StringIO()) as shown:
        da.main(["usage-reset", "pro1"])
    saved = json.loads(state_file.read_text())
    assert saved["limits"] == {} and before <= saved["usage_resets"]["c1"] <= time.time()
    assert shown.getvalue() == "pro1: usage counts as reset until a reading or limit screen newer than now shows it full\n"
    try:
        da.main(["usage-reset", "nobody"])
        raise AssertionError("an unknown account is refused")
    except SystemExit as error:
        assert error.code == "no account nobody in accounts.toml", error.code
    state_file.unlink()

    reset_db()
    for tid in ("ra", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        update(tid, "blocker", "Waiting on: review")
    claude_usage("c1", 100, NOW + 3600, week=100)
    st = fresh_state(
        ra=rec("ra", "c1"), f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3")
    )
    limit = ["You've hit your weekly limit · resets Nov 18, 12:59am"]
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    world(local(term("term-ra", 120, limit), *idle))
    tick(st)
    assert st["workers"]["ra"]["limit_reset"] == NOW + 86400, st["workers"]["ra"]
    pro = da.account(cfg, "c1", "local")
    try:
        da.usage_reset(st, "c1", NOW + 60)
        offered = da.slot(cfg, st["workers"], "claude", OPUS, ignore="ra", now=NOW + 120)
        assert offered and offered["id"] == "c1", "the old full reading counts as reset"
        world(local(term("term-ra", 120, limit), *idle))
        tick(st, NOW + 120)
        (new,) = created()
        assert "--resume sess-ra" in opt(new, "--command") and sends() == [], sends()
        worker = st["workers"]["ra"]
        assert worker["account"] == "c1" and "limit_reset" not in worker, worker
        replay = term("term-local-1", -60, limit, worktree=worker["worktree"])
        for seconds in (180, 240):
            world(local(replay, *idle))
            tick(st, NOW + seconds)
            assert created() == [] and sends() == [], (seconds, created(), sends())
        assert "limit_reset" not in st["workers"]["ra"], "resumed once, never back to wait"
        claude_usage("c1", 20, NOW + 3600, week=20, saved=NOW + 250)
        world(local(replay, *idle))
        tick(st, NOW + 260)
        assert created() == [] and sends() == [], "a newer free reading: still answered"
        claude_usage("c1", 100, NOW + 3600, week=100, saved=NOW + 300)
        assert da.at_limit(cfg, pro, NOW + 300), "a newer full reading restores the limit"
        assert da.slot(cfg, st["workers"], "claude", OPUS, ignore="ra", now=NOW + 300) is None
        assert da.at_limit(cfg, pro, NOW + 400) and "c1" not in st["usage_resets"], (
            "the mark goes once every reading before it is past the age limit"
        )
        world(local(replay, *idle))
        tick(st, NOW + 400)
        assert sends()[0][1].startswith("WAITING ra claude:ra: usage limit on pro1"), sends()
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_reset_answer_outlives_the_reading_fence():
    """Review: the wait a usage-reset answered blocked its row again once the mark's
    reading fence expired, so a tick delayed past it resumed nothing. The answer stays on
    the wait itself."""
    reset_db()
    ticket_row("dr", agent="codex:dr")
    codex_usage("x1", 100, 100, NOW + 3600)
    worker = rec("dr", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
    st = fresh_state(dr=worker)
    st["limits"]["x2"] = NOW + 86400
    old = [*CODEX_LIMIT_WRAPPED, *CODEX_BOX]
    try:
        world(local(term("term-dr", 120, old, ident="codex")))
        tick(st)
        assert worker["limit_reset"] == NOW + 3600, worker
        da.usage_reset(st, "x1", NOW + 60)
        codex_usage("x1", 20, 20, NOW + 3600, saved=NOW + 400)
        world(local(term("term-dr", 120, old, ident="codex")))
        tick(st, NOW + 400)
        assert st["usage_resets"] == {"x1": NOW + 60}, "a reading below full: the reset stands"
        assert len(created()) == 1 and "limit_reset" not in worker, (created(), worker)
        assert da.reset_stands(cfg, da.account(cfg, "x1", "local"), NOW + 400)
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_reset_revoked_by_a_newer_full_reading():
    """Review: a newer full reading only held off a usage-reset; once it went stale, the
    old reset resumed the wait and slot offered the row. A current reading at the limit
    revokes the reset for good, seen by a tick or by slot."""
    for seen_by in ("tick", "slot"):
        reset_db()
        ticket_row("rv", agent="codex:rv")
        codex_usage("x1", 100, 100, NOW + 3600)
        worker = rec("rv", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
        st = fresh_state(rv=worker)
        st["limits"]["x2"] = NOW + 86400
        old = [*CODEX_LIMIT_WRAPPED, *CODEX_BOX]
        try:
            world(local(term("term-rv", 120, old, ident="codex")))
            tick(st)
            da.usage_reset(st, "x1", NOW + 60)
            x1 = da.account(cfg, "x1", "local")
            assert da.reset_stands(cfg, x1, NOW + 60), st["usage_resets"]
            codex_usage("x1", 100, 100, NOW + 3600, saved=NOW + 120)
            world(local(term("term-rv", 120, old, ident="codex")))
            if seen_by == "tick":
                tick(st, NOW + 180)
                assert created() == [] and "limit_reset" in worker, (created(), worker)
            else:
                taken = da.slot(cfg, st["workers"], "codex", "gpt-6.1-sol", ignore="rv",
                                account="x1", now=NOW + 180)
                assert taken is None, taken
            assert not da.reset_stands(cfg, x1, NOW + 180), (seen_by, st.get("usage_full"))
            why = []
            taken = da.slot(cfg, st["workers"], "codex", "gpt-6.1-sol", ignore="rv",
                            account="x1", now=NOW + 500, why=why)
            assert taken is None and "limit screen until" in why[0], (seen_by, why)
            world(local(term("term-rv", 120, old, ident="codex")))
            tick(st, NOW + 500)
            assert created() == [] and "limit_reset" in worker, (seen_by, created(), worker)
        finally:
            st["usage_resets"].clear()
            clear_usage()


@scenario
def usage_reset_fences_older_readings():
    """Review: a CLI reading newer than the operator's usage-reset spent it, and Orca's
    reading from before the reset won again. Nothing read before the mark counts, full
    or free."""
    pro = da.account(cfg, "c1", "local")
    for orca_percent, file_percent, now in ((20, 100, NOW + 180.25), (100, 20, NOW + 180.5)):
        st = fresh_state()
        da.usage_reset(st, "c1", NOW + 60)
        try:
            claude_usage("c1", file_percent, NOW + 3600, saved=NOW + 120)
            before = orca_reading(orca_percent, orca_percent, updated=NOW)
            set_fixture(accounts=orca_accounts(claude=[("c1", before)]))
            assert da.peak(cfg, pro, now)[0] == file_percent, (orca_percent, da.peak(cfg, pro, now))
            offered = da.slot(cfg, {}, "claude", OPUS, now=now, account="c1")
            assert (offered is None) == (file_percent == 100), offered
        finally:
            st["usage_resets"].clear()
            clear_usage()


@scenario
def usage_reset_answers_only_the_message_it_resumed_from():
    """Review: only the first line told an answered limit from a new one, so a wrapped
    message with a new reset on a later line was ignored, and the spent reset then freed
    the new wait. The whole message counts, only a resumed Claude session replays one,
    its own output ends the answer, and the new wait stays parked."""
    reset_db()
    ticket_row("wx", agent="codex:wx")
    codex_usage("x1", 100, 100, NOW + 3600)
    worker = rec("wx", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
    st = fresh_state(wx=worker)
    st["limits"]["x2"] = NOW + 86400
    try:
        world(local(term("term-wx", 120, [*CODEX_LIMIT_WRAPPED, *CODEX_BOX], ident="codex")))
        tick(st)
        da.usage_reset(st, "x1", NOW + 60)
        world(local(term("term-wx", 120, [*CODEX_LIMIT_WRAPPED, *CODEX_BOX], ident="codex")))
        tick(st, NOW + 120)
        assert "limit_reset" not in worker and worker["terminal"] == "term-local-1", worker
        changed = [line.replace("7:28 AM", "9:28 AM") for line in CODEX_LIMIT_WRAPPED]
        assert changed != CODEX_LIMIT_WRAPPED and changed[0] == CODEX_LIMIT_WRAPPED[0]
        shown = term("term-local-1", -60, [*changed, *CODEX_BOX], ident="codex", worktree=worker["worktree"])
        world(local(shown))
        tick(st, NOW + 240)
        assert sends()[0][1].startswith("WAITING wx codex:wx: usage limit on x1"), sends()
        world(local(shown))
        tick(st, NOW + 300)
        assert created() == [] and "limit_reset" in worker, "the reset answered the old wait only"
        why = []
        row = da.slot(cfg, {"wx": worker}, "codex", "gpt-6.1-sol", ignore="wx", now=NOW + 300,
                      account="x1", why=why)
        assert row is None and "limit screen until" in why[0], ("a new screen still blocks", why)
    finally:
        st["usage_resets"].clear()
        clear_usage()

    reset_db()
    for tid in ("rb", "f2", "f3", "f4"):
        ticket_row(tid)
    for tid in ("f2", "f3", "f4"):
        update(tid, "blocker", "Waiting on: review")
    claude_usage("c1", 100, NOW + 3600, week=100)
    st = fresh_state(
        rb=rec("rb", "c1"), f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3")
    )
    limit = [CLAUDE_5H, "Usage limit reached · continuing automatically at 5am", *CLAUDE_PANEL]
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    try:
        world(local(term("term-rb", 120, limit), *idle))
        tick(st)
        da.usage_reset(st, "c1", NOW + 60)
        world(local(term("term-rb", 120, limit), *idle))
        tick(st, NOW + 120)
        worker = st["workers"]["rb"]
        assert worker["terminal"] == "term-local-1" and "limit_reset" not in worker, worker
        wt = worker["worktree"]
        later = [CLAUDE_5H, "Usage limit reached · continuing automatically at 9am", *CLAUDE_PANEL]
        world(local(term("term-local-1", -60, later, worktree=wt), *idle))
        tick(st, NOW + 180)
        assert sends()[0][1].startswith("WAITING rb claude:rb: usage limit on pro1"), sends()
        world(local(term("term-local-1", -60, later, worktree=wt), *idle))
        tick(st, NOW + 240)
        assert created() == [] and "limit_reset" in worker, "the reset answered the old wait only"
        del worker["limit_reset"]
        worker["limit_answered"] = da.limit_message(limit)
        world(local(term("term-local-1", -60, [*limit[:2], "⏺ Back to work", *CLAUDE_BOX], worktree=wt), *idle))
        tick(st, NOW + 300)
        assert "limit_answered" not in worker, "its own output ends the answer"
    finally:
        st["usage_resets"].clear()
        clear_usage()


def codex_full_wait(tid):
    """A Codex worker on x1 parked at NOW by a full reading and its limit screen; x2 is
    held by a stored screen."""
    reset_db()
    ticket_row(tid, agent=f"codex:{tid}")
    codex_usage("x1", 100, 100, NOW + 3600)
    worker = rec(tid, "x1", **CODEX)
    st = fresh_state(**{tid: worker})
    st["limits"]["x2"] = NOW + 86400
    world(local(term(f"term-{tid}", 120, CODEX_OLD, ident="codex")))
    tick(st)
    assert worker["limit_reset"] == NOW + 3600, worker
    return st, worker


def claude_full_wait(tid, screen):
    """A Claude worker on c1 parked at NOW by a full reading and this limit screen; the
    other Claude accounts are held by tickets waiting on review."""
    reset_db()
    for t in (tid, "f2", "f3", "f4"):
        ticket_row(t)
    for t in ("f2", "f3", "f4"):
        update(t, "blocker", "Waiting on: review")
    claude_usage("c1", 100, NOW + 3600, week=100)
    worker = rec(tid, "c1")
    st = fresh_state(**{tid: worker}, f2=rec("f2", "c2"), f3=rec("f3", "c3"), f4=rec("f4", "c3"))
    idle = [term(f"term-{t}", 120) for t in ("f2", "f3", "f4")]
    world(local(term(f"term-{tid}", 120, screen), *idle))
    tick(st)
    assert "limit_reset" in worker and worker.get("limit_full"), worker
    return st, worker, idle


@scenario
def usage_reset_ended_by_a_full_reading_first_seen_old():
    """Review (model R5): a full reading saved after the usage-reset ended it only while
    fresh, so one first seen past the age limit resumed the wait and slot offered the
    row. It ends the reset however old it is when first seen."""
    for seen_by in ("tick", "slot"):
        st, worker = codex_full_wait("fo")
        try:
            da.usage_reset(st, "x1", NOW + 60)
            codex_usage("x1", 100, 100, NOW + 3600, saved=NOW + 120)
            if seen_by == "tick":
                world(local(term("term-fo", 120, CODEX_OLD, ident="codex")))
                tick(st, NOW + 500)
                assert created() == [] and "limit_reset" in worker, (created(), worker)
            else:
                why = []
                taken = da.slot(cfg, st["workers"], "codex", "gpt-6.1-sol", ignore="fo",
                                account="x1", now=NOW + 500, why=why)
                assert taken is None and "limit screen until" in why[0], why
        finally:
            st["usage_resets"].clear()
            clear_usage()


@scenario
def usage_reset_over_reads_unread_without_a_reading():
    """Review (model R6): once a full reading newer than the usage-reset was seen, the
    reset's 0% came back when no reading was in hand and resumed the wait. The account
    reads unread, never free."""
    old = ["You've hit your weekly limit · resets Nov 18, 12:59am"]
    st, worker, idle = claude_full_wait("nr", old)
    try:
        da.usage_reset(st, "c1", NOW + 60)
        claude_usage("c1", 100, NOW + 3600, week=100, saved=NOW + 120)
        world(local(term("term-nr", 120, old), *idle))
        tick(st, NOW + 180)
        assert created() == [] and "limit_reset" in worker, worker
        clear_usage()
        pro = da.account(cfg, "c1", "local")
        assert da.peak(cfg, pro, NOW + 240) == (None, None), da.peak(cfg, pro, NOW + 240)
        world(local(term("term-nr", 120, old), *idle))
        tick(st, NOW + 240)
        assert created() == [] and "limit_reset" in worker, (created(), worker)
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_reset_replay_stops_counting_after_a_full_reading():
    """Review (model R6, #7506): after a resume the usage-reset made, a newer full reading
    parked the replayed limit, and once that reading went unread the replay counted as
    answered again and the wait was cleared under the same screen. A full reading newer
    than the resumed launch ends the answer for good."""
    old = ["You've hit your weekly limit · resets Nov 18, 12:59am"]
    st, worker, idle = claude_full_wait("rp", old)
    try:
        da.usage_reset(st, "c1", NOW + 60)
        world(local(term("term-rp", 120, old), *idle))
        tick(st, NOW + 120)
        assert worker["terminal"] == "term-local-1" and "limit_reset" not in worker, worker
        replay = term(worker["terminal"], -60, old, worktree=worker["worktree"])
        claude_usage("c1", 100, NOW + 3600, week=100, saved=NOW + 180)
        world(local(replay, *idle))
        tick(st, NOW + 240)
        assert sends()[0][1].startswith("WAITING rp claude:rp: usage limit on pro1"), sends()
        world(local(replay, *idle))
        tick(st, NOW + 600)
        assert created() == [] and "limit_reset" in worker, (created(), worker)
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_reset_ended_by_a_new_limit_screen():
    """Review (model R8): a new limit screen on an account the operator had reset left
    the reset standing, so the account read 0% and was offered again once its worker
    moved. A screen newer than the reset ends it, as with no reset at all."""
    for reset_first in (False, True):
        reset_db()
        ticket_row("ns", agent="codex:ns")
        worker = rec("ns", "x1", **CODEX)
        st = fresh_state(ns=worker)
        try:
            if reset_first:
                da.usage_reset(st, "x1", NOW - 1000)
            world(local(term("term-ns", 120, CODEX_OLD, ident="codex")))
            tick(st)
            assert worker["account"] == "x2", ("moved to the free account", worker)
            why = []
            row = da.slot(cfg, st["workers"], "codex", "gpt-6.1-sol", account="x1",
                          now=NOW + 10, limits=st["limits"], why=why)
            assert row is None and st["limits"].get("x1"), (reset_first, st["limits"], why)
        finally:
            st["usage_resets"].clear()


@scenario
def usage_reset_answers_a_screen_older_than_it():
    """Review (model R9): a limit screen quiet since before the usage-reset, parked only
    after it, waited until its own reset. The reset answers it: the next tick resumes it."""
    reset_db()
    ticket_row("os", agent="codex:os")
    codex_usage("x1", 100, 100, NOW + 3600, saved=NOW - 100)
    worker = rec("os", "x1", **CODEX)
    st = fresh_state(os=worker)
    st["limits"]["x2"] = NOW + 86400
    try:
        da.usage_reset(st, "x1", NOW - 10)
        world(local(term("term-os", 120, CODEX_OLD, ident="codex")))
        tick(st)
        assert "limit_reset" in worker and created() == [], worker
        world(local(term("term-os", 120, CODEX_OLD, ident="codex")))
        tick(st, NOW + 70)
        assert len(created()) == 1 and "limit_reset" not in worker, (created(), worker)
        assert worker["account"] == "x1", worker
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_replay_ignores_the_status_bar():
    """Review (model R10): the answered limit message ran on into the status bar under
    it, so after the reset the bar's new percentages made the replay a new limit and
    parked the resumed worker. Only the limit lines and their reset wording count."""
    screen = [CLAUDE_5H, *CLAUDE_STATUS_BAR]
    st, worker, idle = claude_full_wait("sb", screen)
    try:
        da.usage_reset(st, "c1", NOW + 60)
        world(local(term("term-sb", 120, screen), *idle))
        tick(st, NOW + 120)
        assert worker["terminal"] == "term-local-1" and "limit_reset" not in worker, worker
        bar = [line.replace("5h 100%", "5h 2%") for line in CLAUDE_STATUS_BAR]
        assert bar != CLAUDE_STATUS_BAR
        replay = term("term-local-1", -130, [CLAUDE_5H, *bar], worktree=worker["worktree"])
        world(local(replay, *idle))
        tick(st, NOW + 200)
        assert "limit_reset" not in worker and sends() == [], sends()
        assert da.limit_message([CLAUDE_5H, *bar]) == da.limit_message(screen)
        assert "5h 2%" not in da.limit_message([CLAUDE_5H, *bar])
    finally:
        st["usage_resets"].clear()
        clear_usage()


@scenario
def usage_relaunch_drops_the_answered_message():
    """Review (model R11): the limit message an early resume answered survived a
    relaunch, so the new session's own limit with the same wording was ignored. Every
    start drops it."""
    old = ["You've hit your weekly limit · resets Nov 18, 12:59am"]
    st, worker, idle = claude_full_wait("rl", old)
    try:
        da.usage_reset(st, "c1", NOW + 60)
        world(local(term("term-rl", 120, old), *idle))
        tick(st, NOW + 120)
        first = worker["terminal"]
        assert first == "term-local-1" and worker.get("limit_answered"), worker
        gone = term(first, -130, old, status="exited", worktree=worker["worktree"])
        world(local(gone, *idle))
        tick(st, NOW + 800)
        assert worker["launched_at"] == NOW + 800 and worker.get("relaunches") == 1, worker
        assert "--resume" not in opt(created()[-1], "--command"), "a new session"
        assert "limit_answered" not in worker, worker
        limited = term(worker["terminal"], -830, old, worktree=worker["worktree"])
        world(local(limited, *idle))
        tick(st, NOW + 900)
        assert "limit_reset" in worker, ("the new session's own limit counts", worker)
    finally:
        st["usage_resets"].clear()
        clear_usage()


CURSOR_LIMIT = [
    "Buy credits to keep going now, or upgrade your plan for more weekly usage.",
    "rateLimitReason: prepaid_balance_exhausted",
    "prepaidWallet: personal",
    "unifiedSkuWeeklyLimit: true",
    "unifiedSkuWeeklyLimitResetDays: 1",
    "chatMessage: *Weekly usage limit reached. It resets in 1 day.*",
]


@scenario
def trial_cursor_limit_blocks_slot():
    """Live: slot offered Cursor for the reviewer after the coder's screen showed Cursor's
    spent weekly limit. A limit screen marks its account until the reset it gives."""
    reset_db()
    ticket_row("cl", agent="cursor:cl")
    st = fresh_state(cl=rec("cl", "cur", agent="cursor", model="grok-4.7-high"))
    world(local(term("term-cl", 120, CURSOR_LIMIT, ident="cursor")))
    tick(st)
    assert st.get("limits", {}).get("cur") == NOW + 86400, st.get("limits")
    assert sends()[0][1].startswith("WAITING cl cursor:cl: usage limit on cur")
    world(local(term("term-cl", 180, CURSOR_LIMIT, ident="cursor")))
    tick(st, NOW + 60)
    assert st["limits"]["cur"] == NOW + 86400, "read once, not pushed forward"
    del st["workers"]["cl"]
    assert pick({}, agent="cursor", model="grok", limits=st["limits"]) is None
    later = da.slot(cfg, {}, "cursor", "grok", now=NOW + 86401, limits=st["limits"])
    assert later["label"] == "cur"
    state_file.write_text(json.dumps({"workers": {}, "limits": {"cur": NOW}}))
    done = cli("slot", "--agent", "cursor", "--model", "grok")
    assert (done.returncode, done.stdout) == (1, "no capacity\n"), done
    state_file.unlink()
    three = datetime(2033, 5, 18, 15).timestamp()
    assert da.screen_reset("You've hit your limit · resets 5pm", three) == (
        three + 7200,
        True,
    )
    # The printed zone is the clock read, whatever this machine's zone is.
    three_la = datetime(2033, 5, 18, 15, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert da.screen_reset("resets 5pm (America/Los_Angeles)", three_la + 3 * 3600) == (
        three_la + 7200 + 86400,
        True,
    )
    assert da.screen_reset("Weekly usage limit reached.", NOW) == (NOW + 86400, False)
    limit = "You've hit your usage limit. Try again later."
    assert da.screen_reset(limit, NOW) == (NOW + 5 * 3600, False)


@scenario
def screen_reset_reads_its_printed_zone():
    """dev-auto-fixes-50 (h): a limit screen's clock time is read in the zone it prints,
    such as a remote host's UTC, not this Mac's; with no zone, or one Python does not
    know, it is read here as before."""
    for name in ("Pacific/Kiritimati", "UTC"):
        zone = ZoneInfo(name)
        noon = datetime(2033, 5, 18, 12, tzinfo=zone).timestamp()
        five = datetime(2033, 5, 18, 17, tzinfo=zone).timestamp()
        text = f"You've hit your limit · resets 5pm ({name})"
        assert da.screen_reset(text, noon) == (five, True), (name, da.screen_reset(text, noon))
        dated = f"resets Jan 2 at 8pm ({name})"
        jan2 = datetime(2034, 1, 2, 20, tzinfo=zone).timestamp()
        assert da.screen_reset(dated, noon) == (min(jan2, noon + da.MAX_WAIT), True), name
    here = datetime(2033, 5, 18, 12).timestamp()
    for text in ("resets 5pm", "resets 5pm (Mars/Olympus_Mons)"):
        assert da.screen_reset(text, here) == (here + 5 * 3600, True), text


@scenario
def screen_reset_takes_the_next_of_a_repeated_hour():
    """At 1:45 a.m. before the fall-back, a screen's 1:30 a.m. that day is the repeated
    hour's second 1:30, 45 minutes ahead, never a year or a day later."""
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 1, 1, 45, tzinfo=zone).timestamp()
    second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1).timestamp()
    assert second - now == 45 * 60
    for text in (
        "resets Nov 1 at 1:30am (America/Los_Angeles)",
        "resets 1:30am (America/Los_Angeles)",
    ):
        assert da.screen_reset(text, now) == (second, True), (text, da.screen_reset(text, now))


@scenario
def limits_read_once_from_own_text():
    """A screen's reset is read once and capped at 7 days; quoted limit text marks nothing;
    an account with usage files is marked from a screen too; an unread reset says so on
    the ticket; at the reset the worker is relaunched, never typed into."""
    reset_db()
    ticket_row("cr", agent="cursor:cr")

    def cursor(screen, **kw):
        st = fresh_state(cr=rec("cr", "cur", agent="cursor", model="g"))
        world(local(term("term-cr", 120, screen, ident="cursor")), **kw)
        return st

    st = cursor(CURSOR_LIMIT, orch_tail=CLAUDE_PERMISSION)
    for later in (NOW, NOW + 3600, NOW + 7200):
        world(
            local(term("term-cr", 120, CURSOR_LIMIT, ident="cursor")),
            orch_tail=CLAUDE_PERMISSION,
        )
        tick(st, later)
    assert st["limits"] == {"cur": NOW + 86400}, st["limits"]
    quoted = ['  the reviewer saw "usage limit reached · resets in 1 day"', "› "]
    st = cursor(quoted)
    tick(st)
    assert st["limits"] == {} and sends() == [] and created() == []
    st = cursor(["*Weekly usage limit reached. It resets in 400 days.*"])
    tick(st)
    assert st["limits"] == {"cur": NOW + 7 * 86400}, st["limits"]
    st = cursor(["Usage limit reached."])
    tick(st)
    assert sends()[0][1].startswith("WAITING cr cursor:cr: usage limit on cur, resets")
    assert notes()[-1].endswith("assumed 5 hours."), notes()
    world(local(term("term-cr", 6 * 3600, ["Usage limit reached."], ident="cursor")))
    tick(st, NOW + 5 * 3600 + 300)
    assert sends() == [] and len(created()) == 1, sends()
    ticket_row("cs")
    claude_usage("c1", 95, NOW + 3600)
    st = fresh_state(cs=rec("cs", "c1"))
    world(local(term("term-cs", 120, ["You've hit your limit · resets 5pm"])))
    tick(st)
    assert clock(st["limits"]["c1"]) == (17, 0) and st["workers"]["cs"]["account"] == "c2"
    clear_usage()


# Live layouts (R4): Claude Code 2.1.322 under its plugin panel or a one-line status bar,
# Codex 0.167.2 above its prompt and footer. Blank lines are kept as Orca returns them.
CLAUDE_BOX = [
    "─" * 148,
    "❯",
    "─" * 148,
    "  [PONYTAIL]",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]
CLAUDE_PANEL = [
    "",
    "off lite [ full ] ultra" + " " * 89 + "Copy Review Audit Board Compact [-]",
    "─" * 143,
    "cost" + " " * 43 + "tokens" + " " * 41 + "agent time",
    "$0.00" + " " * 42 + "0" + " " * 46 + "1s",
    "last $0.00 · ⌀ $0.00" + " " * 27 + "2 turns" + " " * 40 + "⌀ 1s / turn",
    "",
    " " * 126 + "◎ /goal active (31m)",
    "",
    "",
    *CLAUDE_BOX,
]
CLAUDE_STATUS_BAR = ["", "  opus@max · 5h 100% · 7d 41%", "", "", *CLAUDE_BOX]
CLAUDE_5H = "  ⎿  You've hit your 5-hour limit · resets 5am (America/Los_Angeles)"
CLAUDE_CONTINUING = "Usage limit reached · continuing automatically at 5am · esc to cancel"
CODEX_LIMIT = (
    "■ You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
    "again at 7:28 AM."
)
CODEX_BOX = [
    "",
    "› Ask Codex to do anything",
    "",
    "  GPT-6.1-Sol xhigh · ~/orca/workspaces/.agents/dev-auto-fixes.limit-screens",
]
# The same message wrapped at 60 columns: its reset is on the fourth line.
CODEX_LIMIT_WRAPPED = [
    "■ You’ve hit your usage limit. Upgrade to Pro",
    "  (https://chatgpt.com/explore/pro), visit",
    "  https://chatgpt.com/codex/settings/usage to purchase more",
    "  credits or try again at 7:28 AM.",
]
CODEX = {"agent": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}
CODEX_OLD = [*CODEX_LIMIT_WRAPPED, *CODEX_BOX]
# Wrapped at 80 columns: "try" and "again at 7:28 AM." fall on different lines.
CODEX_LIMIT_WRAPPED_80 = [
    "■ You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro),",
    "  visit https://chatgpt.com/codex/settings/usage to purchase more credits or try",
    "  again at 7:28 AM.",
]
CODEX_GOAL_LIMITED = "  Goal hit usage limits (/goal resume)"


def clock(epoch):
    stamp = datetime.fromtimestamp(epoch)
    return stamp.hour, stamp.minute


@scenario
def claude_and_codex_limit_screens_recognised():
    """C4: a limit line well above the input box, in the agent's own wording (Codex's
    ’ included), marks the worker limited and gives its reset; Codex's goal status alone
    marks it too, with the assumed 5 hours."""
    claude_work = ["⏺ Running the full suite before REVIEW READY.", "  ⎿  Running…"]
    codex_work = ["• Ran python3 -m pytest", "  └ 412 passed in 31.2s", ""]
    for agent, account, screen, reset in (
        ("claude", "c1", [*claude_work, CLAUDE_5H, *CLAUDE_PANEL], (5, 0)),
        ("claude", "c1", [*claude_work, CLAUDE_5H, *CLAUDE_STATUS_BAR], (5, 0)),
        ("claude", "c1", [*claude_work, CLAUDE_CONTINUING, *CLAUDE_PANEL], (5, 0)),
        ("codex", "x2", [*codex_work, CODEX_LIMIT, *CODEX_BOX], (7, 28)),
        ("codex", "x2", [*codex_work, *CODEX_LIMIT_WRAPPED, *CODEX_BOX], (7, 28)),
        ("codex", "x2", [*codex_work, *CODEX_LIMIT_WRAPPED_80, *CODEX_BOX], (7, 28)),
        ("codex", "x2", [*codex_work, *CODEX_BOX, CODEX_GOAL_LIMITED], None),
    ):
        reset_db()
        ticket_row("ls")
        # Codex launched as the model and effort its status line shows.
        model = "gpt-6.1-sol" if agent == "codex" else "m"
        st = fresh_state(ls=rec("ls", account, agent=agent, model=model, effort="xhigh"))
        world(local(term("term-ls", 120, screen, ident=agent)))
        tick(st)
        marked = st["limits"].get(account)
        assert marked and st["workers"]["ls"]["account"] != account, (screen, st)
        if reset:
            assert 0 < marked - NOW <= 86400 and clock(marked) == reset, screen
        else:
            assert marked == NOW + 5 * 3600, screen
    # The limit line sits 12 lines above the input box, or 6 with a status bar.
    assert (CLAUDE_PANEL.index("❯"), CLAUDE_STATUS_BAR.index("❯")) == (11, 5)


@scenario
def sdk_budget_line_is_one_stuck():
    """A runner that stopped at its budget cap exits to its shell under the fixed budget
    line. That line is one STUCK naming it, once per launch, and the exit is never
    relaunched, which would spend past the cap."""
    cost = "· sdk worker: error_max_budget_usd, 0.40 USD this turn, 20.05 of 20 USD on sb"
    shell = "julian@host:~/wt/sb$"
    # Review of e23fd91: a terminal wraps rows wider than itself, and the old 86-character
    # budget line no longer matched a whole row there, so the capped worker was relaunched.
    narrow = [cost[i : i + 40] for i in range(0, len(cost), 40)]
    for status, tail in (
        ("running", [cost, runner.BUDGET, shell]),
        ("exited", ["⏺ Bash", cost, runner.BUDGET]),
        ("running", [*narrow, runner.BUDGET, shell]),
        # A turn with no API usage stops the runner the same way, under its own row.
        ("running", ["· sdk worker: no API usage: success, is_error True", runner.FAILED, shell]),
        ("running", ["· sdk worker: unpriced usage: no transcript", runner.UNPRICED, shell]),
    ):
        reset_db()
        ticket_row("sb", agent="sdk:sb")
        st = fresh_state(sb=rec("sb", "sdk1", agent="sdk", agent_id="sdk:sb", session=None))
        world(local(term("term-sb", 100, tail, status=status, ident=None)))
        tick(st)
        said = next(r for r in (runner.FAILED, runner.UNPRICED, runner.BUDGET) if r in tail)
        said = said.removeprefix("· ")
        stuck = f"STUCK sb sdk:sb: its runner stopped: {said}"
        assert sends() == [("term-orch", stuck, True)], (status, sends())
        assert notes() == [f"dev-auto: {stuck}"], notes()
        assert created() == [] and calls("orca", ["terminal", "close"]) == [], status
        world(local(term("term-sb", 160, tail, status=status, ident=None)))
        tick(st)
        assert sends() == [] and created() == [], (status, sends())
    assert all(len(row) <= 40 for row in (runner.BUSY, runner.IDLE, runner.BUDGET, runner.FAILED))


@scenario
def sdk_exit_without_a_stop_line_is_one_stuck():
    """da-3-13, da-docs-6: a runner killed past the fast-exit window, its terminal exited
    or back at its shell with no fixed stop line on screen, is one STUCK as any sdk stop
    and is never relaunched."""
    for status, tail in (
        ("exited", ["⏺ Bash", "Killed: 9"]),
        ("running", ["Killed: 9", "julian@host:~/wt/sx$"]),
    ):
        reset_db()
        ticket_row("sx", agent="sdk:sx")
        st = fresh_state(sx=rec("sx", "sdk1", agent="sdk", agent_id="sdk:sx", session=None))
        world(local(term("term-sx", 100, tail, status=status, ident=None)))
        tick(st)
        stuck = "STUCK sx sdk:sx: its runner stopped: sdk worker: exited with no stop line on its screen"
        assert sends() == [("term-orch", stuck, True)], (status, sends())
        assert created() == [] and calls("orca", ["terminal", "close"]) == [], status
        world(local(term("term-sx", 160, tail, status=status, ident=None)))
        tick(st, NOW + 60)
        assert sends() == [] and created() == [], (status, sends())

@scenario
def limit_screen_outranks_usage_reading():
    """C4c: a limit line on a screen quiet for a minute moves the worker, or parks it
    until the screen's reset when no account is free, though its usage reading is below
    the limit."""
    reset_db()
    ticket_row("lr")
    claude_usage("c1", 20, NOW + 3600)
    st = fresh_state(lr=rec("lr", "c1"))
    world(local(term("term-lr", 120, [CLAUDE_5H, *CLAUDE_PANEL])))
    tick(st)
    assert st["workers"]["lr"]["account"] == "c2" and len(created()) == 1
    clear_usage()
    ticket_row("lx")
    codex_usage("x1", 20, 30, NOW + 3600)
    st = fresh_state(lx=rec("lx", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh"))
    st["limits"]["x2"] = NOW + 86400
    world(local(term("term-lx", 120, [CODEX_LIMIT, *CODEX_BOX], ident="codex")))
    tick(st)
    assert sends()[0][1].startswith("WAITING lx codex:lx: usage limit on x1"), sends()
    parked = st["workers"]["lx"]["limit_reset"]
    assert clock(parked) == (7, 28) and 0 < parked - NOW <= 86400, "the screen's reset"
    assert created() == []
    clear_usage()


# Live 2026-11-14: a weekly-limit dialog as `orca terminal read --screen` returned it.
FOOTER_ONLY = [" Enter to confirm · Esc to cancel"]
# Live 2026-11-14 (Claude Code's limit screen since then): a ⚠ before the wording.
CLAUDE_WARNING = [
    "⚠ Usage limit reached · limit resets 4am",
    "Continuing automatically at 4am · Esc to cancel",
]


def limit_transcript(session, text, at, home):
    """A synthetic Claude transcript: a user turn, then, unless text is None, one
    assistant entry written at `at`."""
    folder = home / "projects" / "-wt"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")
    entries = [{"type": "user", "timestamp": stamp, "message": {"role": "user", "content": "go"}}]
    if text is not None:
        content = [{"type": "text", "text": text}]
        entries.append(
            {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": content}}
        )
    (folder / f"{session}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def clear_transcripts():
    for home in (*homes.values(), tmp / "wslhome" / ".claude"):
        shutil.rmtree(home / "projects", ignore_errors=True)


@scenario
def footer_only_dialog_reads_its_transcript_limit():
    """Live 2026-11-14: a weekly-limit dialog showed only its footer and sat unseen for an
    hour. A settled footer-only frame whose transcript ends in a limit is handled exactly
    as that limit screen, the date-form reset included, on this Mac or another host, and
    nothing is typed into it."""
    day = datetime.fromtimestamp(NOW + 2 * 86400)
    weekly = f"You've hit your weekly limit · resets {day:%b} {day.day} at 8pm"
    for text, reset in (
        (weekly, (day.month, day.day, 20, 0)),
        ("You've hit your 5-hour limit · resets 5pm", (None, None, 17, 0)),
    ):
        seen = []
        for screen, written in (([f"  ⎿  {text}", *CLAUDE_PANEL], "Working."), (FOOTER_ONLY, text)):
            reset_db()
            ticket_row("fd")
            clear_transcripts()
            limit_transcript("sess-fd", written, NOW - 120, homes["c1"])
            st = fresh_state(fd=rec("fd", "c1"))
            world(local(term("term-fd", 120, screen)))
            tick(st)
            mine = [s for s in sends() if s[0] == "term-fd"]
            seen.append((st["limits"], st["workers"]["fd"]["account"], len(created()), notes(), mine))
        assert seen[0] == seen[1], (text, seen)
        assert seen[1][1] == "c2" and seen[1][2] == 1 and seen[1][4] == [], seen[1]
        marked = datetime.fromtimestamp(st["limits"]["c1"])
        got = (marked.month, marked.day) if reset[0] else (None, None)
        assert (*got, marked.hour, marked.minute) == reset, (text, marked)
    # On another host the transcript is read there, under its CLI's default folder.
    reset_db()
    ticket_row("fr")
    clear_transcripts()
    limit_transcript("sess-fr", weekly, NOW - 120, tmp / "wslhome" / ".claude")
    st = fresh_state(fr=rec("fr", "c3", host="Juzzark-WSL"))
    row, shown = term("term-fr", 120, FOOTER_ONLY)
    world([("Juzzark-WSL", "term-fr", row, shown)], probe_env={"HOME": str(tmp / "wslhome")})
    tick(st)
    assert sends()[0][1].startswith("WAITING fr claude:fr: usage limit on max@wsl"), sends()
    assert clock(st["workers"]["fr"]["limit_reset"]) == (20, 0), st["workers"]["fr"]
    # The frame's transcript is read once: each remote read opens a probe terminal.
    world([("Juzzark-WSL", "term-fr", row, shown)], probe_env={"HOME": str(tmp / "wslhome")})
    tick(st, NOW + 60)
    probes = [r for r in calls("orca", ["terminal", "create"]) if "dev-auto probe" in r["args"]]
    assert probes == [] and sends() == [], (probes, sends())
    dec30 = datetime(2033, 12, 30, 12).timestamp()
    assert da.screen_reset("resets Jan 2 at 8pm", dec30) == (
        datetime(2034, 1, 2, 20).timestamp(),
        True,
    )
    clear_transcripts()


@scenario
def footer_only_dialog_without_a_limit_is_one_stuck():
    """A settled footer-only frame that its transcript does not explain, or whose
    transcript is unread, is one STUCK naming the open dialog, once per frame; so is one
    whose transcript still ends in a limit older than the frame with no limit wait saved
    (da-3-15), and nothing is typed into any of them. Limit screens and dialogs with
    choice rows behave as before."""
    stuck = (
        "STUCK fd {}:fd: a dialog shows only its footer, Enter to confirm or Esc to cancel, "
        "and its transcript shows no usage limit. Nothing is typed into it"
    )
    old = "You've hit your 5-hour limit · resets 5pm"
    for agent, written, at in (
        ("claude", "Running the suite.", NOW - 120),
        ("claude", None, NOW - 120),
        ("claude", "", None),
        ("codex", old, NOW - 120),
    ):
        reset_db()
        ticket_row("fd", agent=f"{agent}:fd")
        clear_transcripts()
        if at:
            limit_transcript("sess-fd", written, at, homes["c1" if agent == "claude" else "x1"])
        account = "c1" if agent == "claude" else "x1"
        st = fresh_state(fd=rec("fd", account, agent=agent, model="m"))
        for later in (NOW, NOW + 60):
            # The same frame: term() dates its last output from NOW.
            world(local(term("term-fd", 120, FOOTER_ONLY, ident=agent)))
            tick(st, later)
            want = [("term-orch", stuck.format(agent), True)] if later == NOW else []
            assert sends() == want, (agent, written, later, sends())
            assert st["limits"] == {} and created() == [], (agent, st)
    reset_db()
    ticket_row("fd")
    clear_transcripts()
    limit_transcript("sess-fd", old, NOW - 120 - 3600, homes["c1"])
    st = fresh_state(fd=rec("fd", "c1"))
    world(local(term("term-fd", 120, FOOTER_ONLY)))
    tick(st)
    old_stuck = [("term-orch", stuck.format("claude"), True)]
    assert sends() == old_stuck and created() == [] and st["limits"] == {}, (sends(), st)
    # Not settled yet: nothing is read or raised.
    limit_transcript("sess-fd", "Working.", NOW - 20, homes["c1"])
    world(local(term("term-fd", 20, FOOTER_ONLY)))
    tick(st)
    assert sends() == [] and created() == [], sends()
    # A dialog with choice rows is answered as before; its transcript is not read.
    limit_transcript("sess-fd", old, NOW - 120, homes["c1"])
    st = fresh_state(fd=rec("fd", "c1"))
    world(local(term("term-fd", 31 * 60, CLAUDE_TRUST)))
    tick(st)
    assert sends() == [("term-fd", "\x1b[B", False), ("term-fd", "", True)], sends()
    assert st["limits"] == {}, st["limits"]
    clear_transcripts()


@scenario
def transcript_limit_lifted_only_by_the_reading_that_showed_it():
    """LIMIT MODEL L9, G1: a limit seen only in the transcript holds its account though a
    reading shows it below full. Later readings below full, unread and stale ones lift
    nothing until a fresh one has shown the account at its limit; then a fresh one below
    full frees it, and a wait parked under a full reading resumes on its own account."""
    reset_db()
    ticket_row("fd")
    clear_transcripts()
    claude_usage("c1", 20, NOW + 3600)
    limit_transcript("sess-fd", "You've hit your 5-hour limit · resets 5pm", NOW - 120, homes["c1"])
    st = fresh_state(fd=rec("fd", "c1", model=SONNET))
    world(local(term("term-fd", 120, FOOTER_ONLY)))
    tick(st)
    reset = st["limits"]["c1"]
    assert reset > NOW and st["workers"]["fd"]["account"] == "c2", st

    def offered(now):
        why = []
        found = da.slot(cfg, {}, "claude", SONNET, now=now, limits=st["limits"], account="c1", why=why)
        return found is not None, why

    ok, why = offered(NOW)
    assert not ok and f"pro1: usage limit until {da.stamp(reset)}" in why, why
    del st["workers"]["fd"]
    world(local())
    for at in (NOW + 200, NOW + 400):
        claude_usage("c1", 20, NOW + 3600, saved=at)
        tick(st, at)
        assert not offered(at)[0] and "c1" not in st.get("limit_full", {}), ("never shown full", at)
    clear_usage()
    tick(st, NOW + 500)
    assert not offered(NOW + 500)[0], "unread"
    claude_usage("c1", 95, NOW + 3600, saved=NOW + 600)
    tick(st, NOW + 600)
    assert not offered(NOW + 600)[0] and st.get("limit_full", {}).get("c1"), "a later reading at the limit"
    claude_usage("c1", 20, NOW + 3600, saved=NOW + 600)
    tick(st, NOW + 1000)
    assert not offered(NOW + 1000)[0], "a stale reading below full"
    claude_usage("c1", 20, NOW + 3600, saved=NOW + 1000)
    tick(st, NOW + 1000)
    assert offered(NOW + 1000)[0] and st["limits"].get("c1") is None, st["limits"]
    assert "c1" not in st.get("limit_full", {}), st.get("limit_full")
    # Parked with no other account free: under a full reading a fresh one below full ends
    # the wait on its own account that tick (R7); under a low one the wait stays.
    for five in (95, 20):
        reset_db()
        ticket_row("fd")
        clear_transcripts()
        claude_usage("c1", five, NOW + 3600)
        limit_transcript("sess-fd", "You've hit your 5-hour limit · resets 5pm", NOW - 120, homes["c1"])
        st = fresh_state(fd=rec("fd", "c1", model=SONNET))
        st["limits"].update(c2=NOW + 86400, c3=NOW + 86400)
        world(local(term("term-fd", 120, FOOTER_ONLY)))
        tick(st)
        assert sends()[0][1].startswith("WAITING fd claude:fd: usage limit on pro1"), sends()
        parked = st["workers"]["fd"]["limit_reset"]
        claude_usage("c1", 20, NOW + 3600, saved=NOW + 400)
        world(local(term("term-fd", 120, FOOTER_ONLY)))
        tick(st, NOW + 400)
        worker = st["workers"]["fd"]
        if five == 95:
            assert len(created()) == 1 and worker["account"] == "c1", (created(), worker)
            assert "limit_reset" not in worker and "c1" not in st["limits"], (worker, st["limits"])
        else:
            assert created() == [] and worker["limit_reset"] == parked, worker
            assert st["limits"]["c1"] >= parked, st["limits"]
    clear_usage()
    clear_transcripts()


@scenario
def parked_under_a_low_reading_is_freed_after_a_later_full_one():
    """A worker parked under a low reading whose next fresh reading is full, and a later
    one below full while the same limit stays on its screen, resumes on its own account:
    the full reading backs its wait as it backs the account's hold."""
    reset_db()
    ticket_row("fd")
    clear_transcripts()
    claude_usage("c1", 20, NOW + 3600)
    limit_transcript("sess-fd", "You've hit your 5-hour limit · resets 5pm", NOW - 120, homes["c1"])
    st = fresh_state(fd=rec("fd", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 86400, c3=NOW + 86400)
    world(local(term("term-fd", 120, FOOTER_ONLY)))
    tick(st)
    assert sends()[0][1].startswith("WAITING fd claude:fd: usage limit on pro1"), sends()
    for at, five in ((NOW + 400, 95), (NOW + 800, 20)):
        claude_usage("c1", five, NOW + 3600, saved=at)
        world(local(term("term-fd", 120, FOOTER_ONLY)))
        tick(st, at)
    worker = st["workers"]["fd"]
    assert len(created()) == 1 and worker["account"] == "c1", (created(), worker)
    assert "limit_reset" not in worker and "c1" not in st["limits"], (worker, st["limits"])
    clear_usage()
    clear_transcripts()



@scenario
def wait_saved_before_sightings_is_freed_after_its_full_reading():
    """A wait saved before sightings were kept, with no full mark, takes its first fresh
    full reading as its own, its saved reset unmoved, so a later fresh reading below
    full resumes it on its own account."""
    reset_db()
    ticket_row("fd")
    clear_transcripts()
    limit_transcript("sess-fd", "You've hit your 5-hour limit · resets 5pm", NOW - 120, homes["c1"])
    st = fresh_state(fd=rec("fd", "c1", model=SONNET, limit_reset=NOW + 3600))
    st["limits"].update(c2=NOW + 86400, c3=NOW + 86400)
    for at, five in ((NOW, 95), (NOW + 400, 20)):
        claude_usage("c1", five, NOW + 3600, saved=at)
        world(local(term("term-fd", 120, FOOTER_ONLY)))
        tick(st, at)
        if five == 95:
            worker = st["workers"]["fd"]
            assert worker["limit_reset"] == NOW + 3600 and worker.get("limit_full"), worker
    worker = st["workers"]["fd"]
    assert len(created()) == 1 and worker["account"] == "c1", (created(), worker)
    clear_usage()
    clear_transcripts()


@scenario
def low_readings_never_end_a_screen_wait():
    """LIMIT MODEL rule 4, G1: a wait parked by a limit alone, its reading below full,
    keeps its reset and its account's hold through later fresh, stale and unread
    readings below full, on a row the operator resets or not, on a limit screen and on a
    footer-only frame. It never resumes onto its limited account early."""
    limit = "You've hit your 5-hour limit · resets in 1 hour"
    row = da.account(cfg, "c1", "local")
    for operator in (True, False):
        for footer in (False, True):
            reset_db()
            ticket_row("lo")
            clear_transcripts()
            clear_usage()
            claude_usage("c1", 20, NOW + 7200)
            screen = [f"  ⎿  {limit}", *CLAUDE_PANEL]
            if footer:
                limit_transcript("sess-lo", limit, NOW - 120, homes["c1"])
                screen = FOOTER_ONLY
            row["weekly_reset_by_operator"] = operator
            try:
                st = fresh_state(lo=rec("lo", "c1", model=SONNET))
                st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
                world(local(term("term-lo", 120, screen)))
                tick(st)
                worker = st["workers"]["lo"]
                assert worker["limit_reset"] == NOW + 3600 == st["limits"]["c1"], (operator, footer, st)
                for at, saved in ((NOW + 400, NOW + 400), (NOW + 1000, NOW + 400), (NOW + 1600, None), (NOW + 2000, NOW + 2000)):
                    clear_usage()
                    if saved:
                        claude_usage("c1", 20, NOW + 7200, saved=saved)
                    world(local(term("term-lo", 120, screen)))
                    tick(st, at)
                    assert created() == [], (operator, footer, at, created())
                    assert worker["limit_reset"] == NOW + 3600 == st["limits"]["c1"], (operator, footer, at, st)
                    found = da.slot(cfg, {}, "claude", SONNET, now=at, limits=st["limits"], account="c1")
                    assert found is None, (operator, footer, at)
            finally:
                row["weekly_reset_by_operator"] = True
    clear_usage()
    clear_transcripts()


@scenario
def footer_wait_survives_a_repaint():
    """LIMIT MODEL E2b, G4: a worker parked on a footer-only frame whose frame is redrawn
    after the transcript's limit is still that wait: after its reset it resumes."""
    reset_db()
    ticket_row("fw")
    clear_transcripts()
    limit_transcript("sess-fw", "You've hit your 5-hour limit · resets in 1 hour", NOW - 120, homes["c1"])
    st = fresh_state(fw=rec("fw", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    world(local(term("term-fw", 120, FOOTER_ONLY)))
    tick(st)
    assert st["workers"]["fw"]["limit_reset"] == NOW + 3600, st["workers"]["fw"]
    at = NOW + 3600 + da.RESUME_AFTER + 60
    # Redrawn long after the entry: the same limit, never STUCK.
    world(local(term("term-fw", NOW - at + 120, FOOTER_ONLY)))
    tick(st, at)
    assert len(created()) == 1 and st["workers"]["fw"]["account"] == "c1", (created(), st)
    assert [s for s in sends() if s[1].startswith("STUCK")] == [], sends()
    clear_transcripts()


@scenario
def park_holds_its_account_to_its_wait():
    """LIMIT MODEL L5, G5: a park whose full reading resets later than its screen holds
    its account to the wait it wrote, so no new worker takes the account in between."""
    reset_db()
    ticket_row("ph")
    claude_usage("c1", 100, NOW + 3600, week=100, week_reset=NOW + 2 * 86400)
    st = fresh_state(ph=rec("ph", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    world(local(term("term-ph", 120, ["  ⎿  You've hit your 5-hour limit · resets in 1 hour", *CLAUDE_PANEL])))
    tick(st)
    assert st["workers"]["ph"]["limit_reset"] == NOW + 2 * 86400 == st["limits"]["c1"], st
    clear_usage()
    found = da.slot(cfg, {}, "claude", SONNET, now=NOW + 3601, limits=st["limits"], account="c1")
    assert found is None, "the hold reaches the wait"


@scenario
def new_limit_on_a_full_wait_needs_its_own_reading():
    """LIMIT MODEL L4, G6: a wait parked under a full reading that then shows a different
    limit while its reading is low takes the new reset and drops its full mark, so a
    later reading below full ends neither the wait nor the hold."""
    day = datetime.fromtimestamp(NOW + 2 * 86400)
    weekly = f"  ⎿  You've hit your weekly limit · resets {day:%b} {day.day} at 8pm"
    reset_db()
    ticket_row("nl")
    claude_usage("c1", 95, NOW + 3600)
    st = fresh_state(nl=rec("nl", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    world(local(term("term-nl", 120, ["  ⎿  You've hit your 5-hour limit · resets in 1 hour", *CLAUDE_PANEL])))
    tick(st)
    worker = st["workers"]["nl"]
    assert worker["limit_full"] and st["limits"]["c1"] == NOW + 3600, st
    later = da.screen_reset(weekly, NOW + 600)[0]
    for at in (NOW + 600, NOW + 700):
        claude_usage("c1", 20, NOW + 3600, saved=at)
        world(local(term("term-nl", NOW - at + 120, [weekly, *CLAUDE_PANEL])))
        tick(st, at)
        assert created() == [] and worker["limit_reset"] == later == st["limits"]["c1"], (at, st)
        assert "limit_full" not in worker and "c1" not in st.get("limit_full", {}), (at, st)
    clear_usage()


@scenario
def usage_reset_answers_new_limits_older_than_it():
    """LIMIT MODEL L2 and L4 under a standing usage-reset, G7: a footer-only limit drawn
    before the operator's usage-reset holds nothing and parks, and the reset resumes it;
    a new limit on a parked worker drawn before the reset neither holds its account nor
    keeps the wait."""
    reset_db()
    ticket_row("ur")
    clear_transcripts()
    limit_transcript("sess-ur", "You've hit your 5-hour limit · resets in 1 hour", NOW - 120, homes["c1"])
    st = fresh_state(ur=rec("ur", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    worker = st["workers"]["ur"]
    try:
        da.usage_reset(st, "c1", NOW - 10)
        world(local(term("term-ur", 120, FOOTER_ONLY)))
        tick(st)
        assert "limit_reset" in worker and "c1" not in st["limits"], (worker, st["limits"])
        world(local(term("term-ur", 120, FOOTER_ONLY)))
        tick(st, NOW + 70)
        assert len(created()) == 1 and worker["account"] == "c1", (created(), worker)
    finally:
        st["usage_resets"].clear()
    reset_db()
    ticket_row("ur")
    st = fresh_state(ur=rec("ur", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    worker = st["workers"]["ur"]
    day = datetime.fromtimestamp(NOW + 2 * 86400)
    weekly = f"  ⎿  You've hit your weekly limit · resets {day:%b} {day.day} at 8pm"
    try:
        world(local(term("term-ur", 120, ["  ⎿  You've hit your 5-hour limit · resets in 1 hour", *CLAUDE_PANEL])))
        tick(st)
        assert worker["limit_reset"] == NOW + 3600 == st["limits"]["c1"], st
        da.usage_reset(st, "c1", NOW + 50)
        # Drawn at NOW + 20, before the reset.
        world(local(term("term-ur", -20, [weekly, *CLAUDE_PANEL])))
        tick(st, NOW + 100)
        assert "c1" not in st["limits"], st["limits"]
        assert len(created()) == 1 and worker["account"] == "c1", (created(), worker)
    finally:
        st["usage_resets"].clear()
    clear_transcripts()


@scenario
def staggered_limits_keep_the_latest():
    """Review of 0acbb49: two workers share an account and hit its limit minutes apart.
    The later one's sighting and its later reset stand, so a reading taken before it
    frees nothing and the account stays out until the later reset."""
    reset_db()
    for tid in ("sa", "sb"):
        ticket_row(tid)
    clear_transcripts()
    claude_usage("c1", 20, NOW + 3600, saved=NOW + 50)
    st = fresh_state(sa=rec("sa", "c1", model=SONNET), sb=rec("sb", "c1", model=SONNET))
    busy = ["⏺ Running the full suite.", "  ⎿  Running…", *CLAUDE_BOX]
    world(local(term("term-sa", 120, CLAUDE_WARNING), term("term-sb", 5, busy)))
    tick(st)
    first = st["limits"]["c1"]
    assert clock(first) == (4, 0) and st["workers"]["sa"]["account"] == "c2", st
    del st["workers"]["sa"]
    day = datetime.fromtimestamp(NOW + 2 * 86400)
    weekly = f"  ⎿  You've hit your weekly limit · resets {day:%b} {day.day} at 8pm"
    # Drawn at NOW + 100, handled at NOW + 180.
    world(local(term("term-sb", -100, [weekly, *CLAUDE_PANEL])))
    tick(st, NOW + 180)
    later = da.screen_reset(weekly, NOW + 180)[0]
    assert st["workers"]["sb"]["account"] == "c2" and later > first, st
    assert st["limits"]["c1"] == later and "c1" not in st.get("limit_full", {}), st
    del st["workers"]["sb"]
    world(local())
    tick(st, NOW + 310)
    found = da.slot(cfg, {}, "claude", SONNET, now=NOW + 310, limits=st["limits"], account="c1")
    assert found is None and st["limits"]["c1"] == later, "the reading predates sb's limit"
    clear_usage()
    found = da.slot(cfg, {}, "claude", SONNET, now=first + 60, limits=st["limits"], account="c1")
    assert found is None, "the earlier reset frees nothing"


@scenario
def parked_worker_takes_a_later_limit():
    """Review of 25ef078: a worker parked on one limit shows a different, later one in
    the same terminal, during its old reset's grace or after it. The later reset holds
    both the account and the wait, so nothing resumes on it early; the same limit
    on later ticks is read once."""
    day = datetime.fromtimestamp(NOW + 2 * 86400)
    weekly = f"  ⎿  You've hit your weekly limit · resets {day:%b} {day.day} at 8pm"
    later = da.screen_reset(weekly, NOW)[0]
    for after in (60, da.RESUME_AFTER + 1):
        reset_db()
        ticket_row("pw")
        clear_usage()
        st = fresh_state(pw=rec("pw", "c1", model=SONNET))
        st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
        world(local(term("term-pw", 120, CLAUDE_WARNING)))
        tick(st)
        first = st["workers"]["pw"]["limit_reset"]
        assert clock(first) == (4, 0) and sends()[0][1].startswith("WAITING pw"), sends()
        for at in (first + after, first + after + da.RESUME_AFTER):
            world(local(term("term-pw", NOW - at + 120, [weekly, *CLAUDE_PANEL])))
            tick(st, at)
            assert created() == [] and st["workers"]["pw"]["limit_reset"] == later, (after, st)
            assert st["limits"]["c1"] == later, st
            found = da.slot(cfg, {}, "claude", SONNET, now=at, limits=st["limits"], account="c1")
            assert found is None, (after, at)


@scenario
def parked_wait_keeps_one_reset():
    """Review of f98949d: a wait saved by the code before sightings joins its account's
    hold at its saved reset and is never reread, and a later reading below full still
    frees both; a panel shown or hidden under an unchanged limit reads nothing new."""
    limit = "  ⎿  You've hit your 5-hour limit · resets in 1 hour"
    reset_db()
    ticket_row("pw")
    clear_usage()
    claude_usage("c1", 20, NOW + 7200)
    # As 1151e8c saved it: the wait, no sighting, and no hold for a read account.
    st = fresh_state(pw=rec("pw", "c1", model=SONNET, limit_reset=NOW + 3600))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    world(local(term("term-pw", 60, [limit, *CLAUDE_PANEL])))
    tick(st, NOW + 60)
    assert st["limits"]["c1"] == NOW + 3600 == st["workers"]["pw"]["limit_reset"], st
    assert da.slot(cfg, {}, "claude", SONNET, now=NOW + 60, limits=st["limits"], account="c1") is None
    claude_usage("c1", 20, NOW + 7200, saved=NOW + 400)
    world(local(term("term-pw", -280, [limit, *CLAUDE_PANEL])))
    tick(st, NOW + 400)
    assert st["limits"]["c1"] == NOW + 3600 == st["workers"]["pw"]["limit_reset"], st
    assert da.slot(cfg, {}, "claude", SONNET, now=NOW + 400, limits=st["limits"], account="c1") is None
    # Saved under a full reading, a fresh one below full ends it on its own account in
    # the first tick; one saved with no reset is over and holds nothing.
    for saved in ({"limit_reset": NOW + 3600, "limit_full": True}, {"limit_reset": None}):
        reset_db()
        ticket_row("pw")
        claude_usage("c1", 20, NOW + 7200)
        st = fresh_state(pw=rec("pw", "c1", model=SONNET, **saved))
        st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
        world(local(term("term-pw", 60, [limit, *CLAUDE_PANEL])))
        tick(st, NOW + 60)
        assert len(created()) == 1 and st["workers"]["pw"]["account"] == "c1", (saved, st)
        assert "c1" not in st["limits"], (saved, st["limits"])
    reset_db()
    ticket_row("pw")
    clear_usage()
    st = fresh_state(pw=rec("pw", "c1", model=SONNET))
    st["limits"].update(c2=NOW + 9 * 86400, c3=NOW + 9 * 86400)
    for at, screen in ((NOW, [limit, *CLAUDE_PANEL]), (NOW + 60, [limit]), (NOW + 120, [limit, *CLAUDE_PANEL])):
        world(local(term("term-pw", NOW - at + 120, screen)))
        tick(st, at)
        assert st["workers"]["pw"]["limit_reset"] == NOW + 3600 == st["limits"]["c1"], (at, st)
    assert created() == [], created()


@scenario
def claude_warning_limit_screen_recognised():
    """Live 2026-11-14, dev-auto-fixes-49's first launch: Claude's limit screen as two
    lines with a ⚠ before the wording. It is a limit screen with the 4am reset, and it
    keeps its account, one the operator resets, from new workers though its reading is
    low (review of feb4f1a)."""
    for screen, low in ((CLAUDE_WARNING, False), ([*CLAUDE_WARNING, *CLAUDE_PANEL], True)):
        reset_db()
        ticket_row("lw")
        if low:
            claude_usage("c1", 20, NOW + 3600)
        st = fresh_state(lw=rec("lw", "c1", model=SONNET))
        world(local(term("term-lw", 120, screen)))
        tick(st)
        assert st["workers"]["lw"]["account"] == "c2" and len(created()) == 1, st
        assert clock(st["limits"]["c1"]) == (4, 0), st["limits"]
        assert [s for s in sends() if s[0] == "term-lw"] == [], sends()
        assert da.account(cfg, "c1", "local")["weekly_reset_by_operator"]
        found = da.slot(cfg, {}, "claude", SONNET, now=NOW, limits=st["limits"], account="c1")
        assert found is None, (low, found)
        clear_usage()


PACIFIC = ZoneInfo("America/Los_Angeles")


def claude_weekly_screen(day):
    """Live 2026-11-15 (dev-auto-fixes-56): Claude Code's weekly-limit screen under a goal,
    its limit line above two ⏺ status lines and its menu, the reset on `day` at 9am."""
    return [
        "❯ dev-auto: continue ticket wl.",
        f"  ⎿  You've hit your weekly limit · resets {day} at 9am (America/Los_Angeles)",
        "⏺ Usage limit reached · continuing automatically at 4am · esc to cancel",
        "⏺ Goal paused · usage limit reached · send a message after it resets to continue",
        "",
        "✻ Worked for 0s · done 7:00 PM",
        "",
        " What do you want to do?",
        "",
        " ❯ 1. Stop and wait for limit to reset",
        "   3. Switch to usage credits",
        "",
        " Enter to confirm · Esc to cancel",
    ]


@scenario
def claude_weekly_screen_under_status_lines_recognised():
    """dev-auto-fixes-56: the weekly-limit screen, its ⏺ status lines under the limit line,
    is a limit screen in one tick: its account is held until the weekly reset in its
    printed zone, not the earlier 4am, slot refuses it until then, and the worker moves."""
    reset_db()
    clear_usage()
    ticket_row("wl")
    later = datetime.fromtimestamp(NOW, PACIFIC) + timedelta(days=1)
    reset = later.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    st = fresh_state(wl=rec("wl", "c1", model=SONNET))
    world(local(term("term-wl", 120, claude_weekly_screen(later.strftime("%b %-d")))))
    tick(st)
    assert st["limits"].get("c1") == reset, (st["limits"], da.stamp(reset))
    assert st["workers"]["wl"]["account"] == "c2" and len(created()) == 1, st["workers"]
    assert [s for s in sends() if s[0] == "term-wl"] == [], sends()
    for at, offered in ((reset - 60, None), (reset + 1, "c1")):
        found = da.slot(cfg, {}, "claude", SONNET, now=at, limits=st["limits"], account="c1")
        assert (found and found["id"]) == offered, (da.stamp(at), found)


@scenario
def claude_weekly_screen_read_after_its_status_clock():
    """dev-auto-fixes-56 review: first read at 5am, after the status line's 4am passed but
    before the dated 9am weekly reset, either line first: the dated reset holds the account
    and the waiting worker, every other Claude account held, not tomorrow's 4am."""
    morning = datetime.fromtimestamp(NOW, PACIFIC) + timedelta(days=1)
    now = morning.replace(hour=5, minute=0, second=0, microsecond=0).timestamp()
    reset = morning.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    screen = claude_weekly_screen(morning.strftime("%b %-d"))
    for lines in (screen, [screen[0], screen[2], screen[1], *screen[3:]]):
        reset_db()
        clear_usage()
        ticket_row("wl")
        st = fresh_state(wl=rec("wl", "c1", model=SONNET))
        st["limits"].update(c2=now + 9 * 86400, c3=now + 9 * 86400)
        world(local(term("term-wl", 120, lines)))
        tick(st, now)
        assert st["workers"]["wl"]["account"] == "c1" and not created(), st["workers"]
        held = (st["limits"].get("c1"), st["workers"]["wl"].get("limit_reset"))
        assert held == (reset, reset), (lines[1], [da.stamp(at or 0) for at in held])
        found = da.slot(cfg, {}, "claude", SONNET, now=reset + 1, limits=st["limits"], account="c1")
        assert found and found["id"] == "c1", found


@scenario
def claude_weekly_reset_wordings_read_in_their_zone():
    """dev-auto-fixes-56: the three live reset wordings, read at 7pm Pacific on Nov 15,
    and the latest reset when the screen also prints an earlier one."""
    now = datetime(2026, 11, 15, 19, tzinfo=PACIFIC).timestamp()
    for text, hour in (
        ("You've hit your weekly limit · resets Nov 16 at 9am (America/Los_Angeles)", (16, 9)),
        ("You've hit your weekly limit · resets 4am (America/Los_Angeles)", (16, 4)),
        ("You've hit your weekly limit · resets Nov 16 at 2pm (America/Los_Angeles)", (16, 14)),
        (" ".join(claude_weekly_screen("Nov 16")[1:3]), (16, 9)),
        (" ".join(claude_weekly_screen("Nov 16")[2:0:-1]), (16, 9)),
    ):
        want = datetime(2026, 11, *hour, tzinfo=PACIFIC).timestamp()
        assert da.screen_reset(text, now) == (want, True), (text, da.screen_reset(text, now))


# Concord failing on a Claude screen, in the documented wording: a tool result, and the
# notification Claude Code shows under its input box when it gives up reconnecting.
CONCORD_TOOL_GONE = [
    '⏺ concord - update_work (MCP)(task_id: "cd", kind: "progress", content: "Tests pass")',
    "  ⎿  Error: No such tool available: mcp__concord__update_work",
    "⏺ The Concord tools are unavailable, so the progress note was not recorded.",
]
CONCORD_DISCONNECTED = 'MCP server "concord" disconnected · open /mcp to reconnect'
CONCORD_FOOTER = [*CLAUDE_BOX[:-1], CLAUDE_BOX[-1] + " " * 40 + CONCORD_DISCONNECTED]
# Live Claude Code 2.1.325 on Linux and codex-cli 0.169.0 against a concord server that
# exits mid-call: Claude prints no call line above the error and draws ● for ⏺.
CLAUDE_CONNECTION_CLOSED = [
    "● Recording the merge on the ticket.",
    "  ⎿  Error: Connection closed",
    '● Failed: the update_work call returned "Connection closed," and the concord MCP',
    "  server didn't connect. I didn't retry.",
    "✻ Crunched for 3s · done 5:04 PM",
]
CLAUDE_RECONNECTED = ["❯ /mcp reconnect concord", "  ⎿  Successfully reconnected to concord"]
CLAUDE_RECONNECT_FAILED = ["❯ /mcp reconnect concord", "  ⎿  Failed to reconnect to concord"]
CODEX_TRANSPORT_CLOSED = [
    "• Failed concord.inspect_work",
    "  └",
    "    Caused by:",
    "        Transport closed",
    "    + Show details",
    "• Concord is unreachable, so the verdict is not recorded.",
]
RECONNECT = [("term-cd", "\x15", False), ("term-cd", "/mcp reconnect concord", True)]


def concord_stuck(tid, agent, why, remedy, host="local", terminal=None):
    return (
        f"STUCK {tid} {agent}:{tid} on {host} {terminal or f'term-{tid}'}: its Concord tools fail on its "
        f"screen{why} and it has recorded nothing since. Remedy: {remedy}"
    )


CLAUDE_STUCK = concord_stuck(
    "cd",
    "claude",
    " after /mcp reconnect concord",
    "reconnect concord from /mcp, or relaunch it. dev-auto types nothing more",
)


@scenario
def claude_concord_lost_idle_gets_one_reconnect():
    """An idle Claude worker whose screen shows its Concord tools failing, in Linux's or
    the Mac's wording, its call scrolled off or not, gets /mcp reconnect concord typed
    once after a cleared input, and nothing else that tick. The reconnect's success on
    its screen, or a new update from it, clears the failure: no STUCK, nothing more."""
    for failing in (
        CLAUDE_CONNECTION_CLOSED,
        CONCORD_TOOL_GONE,
        CONCORD_TOOL_GONE[1:],
    ):
        for cleared in ("reconnected", "update"):
            reset_db()
            ticket_row("cd")
            update("cd", "progress", "Claimed.")
            st = fresh_state(cd=rec("cd", "c1"))
            world(local(term("term-cd", 120, [*failing, *CLAUDE_BOX])))
            tick(st)
            assert sends(include_clear=True) == RECONNECT, (failing, sends(True))
            if cleared == "reconnected":
                after = [*failing, *CLAUDE_RECONNECTED, *CLAUDE_BOX]
            else:
                update("cd", "progress", "Concord is back.")
                after = [*failing, *CLAUDE_BOX]
            for later in (60, 120, 180):
                world(local(term("term-cd", 120, after)))
                tick(st, NOW + later)
                assert sends() == [], (failing, cleared, later, sends())
            # Once the screen stops showing it, a new failure gets its own reconnect.
            world(local(term("term-cd", 120, ["● Running the suite.", *CLAUDE_BOX])))
            tick(st, NOW + 240)
            world(local(term("term-cd", 120, [*failing, *CLAUDE_BOX])))
            tick(st, NOW + 300)
            assert sends(include_clear=True) == RECONNECT, (failing, cleared, sends(True))
    reset_db()
    ticket_row("cd")
    st = fresh_state(cd=rec("cd", "c1"))
    world(local(term("term-cd", 120, ["● Running the suite.", *CONCORD_FOOTER])))
    tick(st)
    assert sends(include_clear=True) == RECONNECT, sends(True)


@scenario
def claude_concord_reconnect_failing_raises_one_stuck():
    """A screen still failing on the tick after the reconnect, its reconnect refused or
    unanswered, with no update from the worker, raises one STUCK naming ticket, host and
    remedy, and types nothing more, then or later."""
    for after in (
        [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_RECONNECT_FAILED, *CLAUDE_BOX],
        [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX],
    ):
        reset_db()
        ticket_row("cd")
        st = fresh_state(cd=rec("cd", "c1"))
        world(local(term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX])))
        tick(st)
        assert sends(include_clear=True) == RECONNECT, sends(True)
        world(local(term("term-cd", 120, after)))
        tick(st, NOW + 60)
        assert sends() == [("term-orch", CLAUDE_STUCK, True)], sends()
        assert notes() == [f"dev-auto: {CLAUDE_STUCK}"], notes()
        for later in (120, 180):
            world(local(term("term-cd", 120, after)))
            tick(st, NOW + later)
            assert sends(include_clear=True) == [], (later, sends(True))


@scenario
def claude_concord_lost_in_a_turn_waits_until_idle():
    """A Claude worker still in its turn, by recent output or by its spinner when the
    output is stale, its screen showing Concord failing, gets no Esc and nothing typed and
    raises no STUCK; once it is idle the reconnect is its one message that tick."""
    reset_db()
    ticket_row("cd")
    update("cd", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(cd=rec("cd", "c1"))
    screen = [*CLAUDE_CONNECTION_CLOSED, "✻ Hullaballooing… (5m 15s · ↓13.1k tokens)", *CLAUDE_BOX]
    for later in (0, 60, 120):
        world(local(term("term-cd", 5 - later, screen)))
        tick(st, NOW + later)
        assert sends(include_clear=True) == [], (later, sends(True))
    # Its output is stale but its spinner still shows the turn: still nothing.
    for later in (180, 240):
        world(local(term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_MID_TURN])))
        tick(st, NOW + later)
        assert sends(include_clear=True) == [], (later, sends(True))
    update("cd", "finding", "REVIEW VERDICT: FIX 1a2b3c4", agent="codex:cd-review")
    world(local(term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX])))
    tick(st, NOW + 300)
    assert sends(include_clear=True) == RECONNECT, sends(True)


CODEX_STUCK = concord_stuck(
    "cd-review",
    "codex",
    " again after its relaunch",
    "check the ssh link from its host to the ledger, then relaunch it. dev-auto "
    "relaunches it no more",
    terminal="term-local-1",
)


@scenario
def codex_transport_closed_relaunched_once_with_a_note():
    """A Codex reviewer whose Concord call failed with Transport closed is left alone while
    its turn runs, stale output or not; once idle it is relaunched once, after one note on its ticket naming its ticket and the coder's and
    the re-run rule. A healthy new session raises nothing; a second loss raises one STUCK
    on the next tick and no second relaunch."""
    reset_db()
    ticket_row("cd-review", agent="codex:cd-review")
    st = fresh_state(
        **{"cd-review": rec("cd-review", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")}
    )
    screen = [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX]
    working = [*CODEX_TRANSPORT_CLOSED, "• Working (8s • esc to interrupt)", *CODEX_BOX]
    for later in (-120, -60):
        world(local(term("term-cd-review", 300, working, ident="codex")))
        tick(st, NOW + later)
        assert created() == [] and calls("orca", ["terminal", "close"]) == [], later
    world(local(term("term-cd-review", 120, screen, ident="codex")))
    tick(st)
    assert len(created()) == 1, created()
    new = created()[0]
    assert "previous session on this ticket exited" in opt(new, "--command")
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == ["term-cd-review"]
    concord_notes = [text for text in notes() if "Concord" in text]
    assert len(concord_notes) == 1, notes()
    note_text = concord_notes[0]
    assert note_text.startswith("dev-auto: codex:cd-review's Concord transport closed"), note_text
    assert "Read ticket cd-review and the coder's ticket cd first" in note_text, note_text
    assert "re-run only the checks of a SHA that has no verdict" in note_text, note_text
    assert sends() == [], sends()
    handle = st["workers"]["cd-review"]["terminal"]
    fresh = ["• Reading ticket cd-review.", '• Called concord.inspect_work({"task_id":"cd-review"})', *CODEX_BOX]
    world(local(term(handle, 120, fresh, ident="codex", worktree=str(tmp / "wt" / "cd-review"))))
    tick(st, NOW + 60)
    assert sends() == [] and created() == [], (sends(), created())
    for later in (120, 180, 240):
        world(local(term(handle, 120, screen, ident="codex", worktree=str(tmp / "wt" / "cd-review"))))
        tick(st, NOW + later)
        expected = [("term-orch", CODEX_STUCK, True)] if later == 180 else []
        assert sends() == expected, (later, sends())
        assert created() == [], (later, created())


@scenario
def codex_transport_closed_soon_after_launch_is_relaunched():
    """dev-auto-fixes-50 (c): a Codex session whose Concord transport closes 5 minutes
    after launch is relaunched; its terminal did not exit, so no "exited within 10
    minutes" STUCK and no hold that would refuse a later exit its relaunch. A relaunch
    refused for capacity posts its ticket note once, not every tick."""
    reset_db()
    ticket_row("cf", agent="codex:cf")
    young = rec("cf", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh", launched_at=NOW - 300)
    st = fresh_state(cf=young)
    screen = [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX]
    world(local(term("term-cf", 120, screen, ident="codex")))
    tick(st)
    assert len(created()) == 1, (created(), sends())
    assert not [s for s in sends() if "STUCK" in s[1]], sends()
    assert "held" not in st["workers"]["cf"], st["workers"]["cf"]

    reset_db()
    ticket_row("cg", agent="codex:cg")
    st = fresh_state(cg=rec("cg", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh"))
    st["limits"].update(x1=NOW + 3600, x2=NOW + 3600)
    concord_notes = []
    for later in (0, 60, 120):
        world(local(term("term-cg", 120, screen, ident="codex")))
        tick(st, NOW + later)
        assert created() == [], (later, created())
        concord_notes += [text for text in notes() if "Concord transport closed" in text]
    assert len(concord_notes) == 1, concord_notes


LEDGER_DOWN = (
    "STUCK Juzzark-WSL: its ssh self-check to the ledger fails, so the Concord tools of "
    "its workers cannot be repaired. Remedy: restore the ssh link from Juzzark-WSL to the "
    "ledger. dev-auto types and relaunches nothing there until the check passes"
)


@scenario
def unreachable_ledger_host_types_and_relaunches_nothing():
    """Concord failing on two idle workers on a host whose own ssh self-check to the
    ledger fails: nothing is typed or relaunched, and one STUCK names the host, on that
    tick and none later; the host is probed once for both, and again only after five
    minutes. Once the check passes, each worker gets its repair."""
    reset_db()
    ticket_row("cd")
    ticket_row("ce", agent="codex:ce")
    st = fresh_state(
        cd=rec("cd", "c3", host="Juzzark-WSL"),
        ce=rec("ce", "xw", host="Juzzark-WSL", agent="codex", model="gpt-6.1-sol", effort="xhigh"),
    )
    claude = term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX])
    codex = term("term-ce", 120, [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX], ident="codex")
    terms = [("Juzzark-WSL", "term-cd", *claude), ("Juzzark-WSL", "term-ce", *codex)]
    for later in (0, 60, 120):
        world(terms, ssh_fail=True)
        tick(st, NOW + later)
        expected = [("term-orch", LEDGER_DOWN, True)] if later == 0 else []
        assert sends() == expected, (later, sends())
        assert [s for s in sends(True) if s[0] != "term-orch"] == [], (later, sends(True))
        assert created() == [], (later, created())
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        assert not {"term-cd", "term-ce"} & set(closed), (later, closed)
        assert len(selfcheck_calls()) == (1 if later == 0 else 0), (later, selfcheck_calls())
    world(terms)
    tick(st, NOW + 300)
    assert sends(include_clear=True) == RECONNECT, sends(True)
    assert len(created()) == 1 and opt(created()[0], "--title") == "ce codex", created()


SUPERVISOR_DOWN = (
    "STUCK local: the Concord call of dev-auto itself to the ledger fails, so it repairs "
    "the Concord tools of no worker. Remedy: check concord-mcp and the ledger here. "
    "dev-auto types and relaunches nothing until the call works"
)



@scenario
def local_ledger_outage_after_its_recovery_is_a_new_stuck():
    """A local Concord call that passes ends its own outage although the host's
    self-check then fails, so a later local outage raises its STUCK again."""
    reset_db()
    ticket_row("cd")
    st = fresh_state(cd=rec("cd", "c3", host="Juzzark-WSL"))
    claude = term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX])
    terms = [("Juzzark-WSL", "term-cd", *claude)]
    for later, fixture, told in (
        (0, {"tool_error": "Transport closed"}, SUPERVISOR_DOWN),
        (300, {"ssh_fail": True}, LEDGER_DOWN),
        (600, {"tool_error": "Transport closed"}, SUPERVISOR_DOWN),
    ):
        world(terms, **fixture)
        tick(st, NOW + later)
        assert sends() == [("term-orch", told, True)], (later, sends())


@scenario
def supervisor_concord_failure_types_and_relaunches_nothing():
    """Concord failing on two idle workers while the supervisor's own Concord call fails
    too: nothing is typed or relaunched, and one STUCK names the host, on that tick and
    none later. Once its call works again, each worker gets its repair."""
    reset_db()
    ticket_row("cd")
    ticket_row("ce", agent="codex:ce")
    st = fresh_state(
        cd=rec("cd", "c1"),
        ce=rec("ce", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh"),
    )
    claude = term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX])
    codex = term("term-ce", 120, [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX], ident="codex")
    for later in (0, 60, 120):
        world(local(claude, codex), tool_error="Transport closed")
        tick(st, NOW + later)
        expected = [("term-orch", SUPERVISOR_DOWN, True)] if later == 0 else []
        assert sends() == expected, (later, sends())
        assert [s for s in sends(True) if s[0] != "term-orch"] == [], (later, sends(True))
        assert created() == [] and calls("orca", ["terminal", "close"]) == [], later
    world(local(claude, codex))
    tick(st, NOW + 300)
    assert sends(include_clear=True) == RECONNECT, sends(True)
    assert len(created()) == 1 and opt(created()[0], "--title") == "ce codex", created()


@scenario
def supervisor_concord_failure_is_probed_once_per_recheck():
    """During one ledger outage the supervisor's own Concord call is tried once per
    LEDGER_RECHECK, not once per worker: two idle workers showing the failure, and a third
    that shows it on the next tick, make one probe until the recheck time passes."""
    reset_db()
    for tid in ("cd", "cf"):
        ticket_row(tid)
    ticket_row("ce", agent="codex:ce")
    st = fresh_state(
        cd=rec("cd", "c1"),
        ce=rec("ce", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh"),
        cf=rec("cf", "c2"),
    )
    failing = [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_BOX]
    claude = term("term-cd", 120, failing)
    codex = term("term-ce", 120, [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX], ident="codex")
    probes, real = [], da.concord_call

    def counted(tool, args, *rest, **kw):
        if tool == "inspect_work":
            probes.append(args.get("task_id"))
        return real(tool, args, *rest, **kw)

    da.concord_call = counted
    try:
        per_tick = []
        for later in (0, 60, 120):
            third = term("term-cf", 120, failing if later else CLAUDE_BOX)
            world(local(claude, codex, third), tool_error="Transport closed")
            probes.clear()
            tick(st, NOW + later)
            per_tick.append(len(probes))
        assert per_tick == [1, 0, 0], per_tick
        world(local(claude, codex, term("term-cf", 120, failing)), tool_error="Transport closed")
        probes.clear()
        tick(st, NOW + da.LEDGER_RECHECK)
        assert len(probes) == 1, probes
    finally:
        da.concord_call = real


@scenario
def limited_claude_repaired_when_it_resumes():
    """A Claude worker parked on its limit screen with Concord failing above it gets
    nothing typed; once the limit is off its screen it gets the reconnect."""
    reset_db()
    ticket_row("cd")
    claude_usage("c1", 20, NOW + 3600)
    st = fresh_state(cd=rec("cd", "c1", limit_reset=NOW + 3600))
    # Every other Claude account is held, so the parked worker has nowhere to move.
    st["limits"].update(c2=NOW + 3600, c3=NOW + 3600)
    limited = [*CLAUDE_CONNECTION_CLOSED, CLAUDE_CONTINUING, *CLAUDE_PANEL]
    world(local(term("term-cd", 120, limited)))
    tick(st)
    assert sends(include_clear=True) == [] and created() == [], sends(True)
    world(local(term("term-cd", 120, [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_PANEL])))
    tick(st, NOW + 60)
    assert sends(include_clear=True) == RECONNECT, sends(True)
    clear_usage()


@scenario
def cursor_and_grok_concord_lost_raise_one_stuck():
    """A Cursor or Grok worker whose screen shows Claude's or Codex's Concord failure gets
    no keystroke and raises one STUCK on the next tick, none later."""
    for agent, box in (("cursor", "› "), ("grok", "Ask Grok anything")):
        for failing in (CONCORD_TOOL_GONE, CODEX_TRANSPORT_CLOSED):
            reset_db()
            ticket_row("cd", agent=f"{agent}:cd")
            st = fresh_state(cd=rec("cd", "c1", agent=agent, model="m"))
            stuck = concord_stuck(
                "cd", agent, "", "restart its Concord server or relaunch it. dev-auto types nothing"
            )
            for later in (0, 60, 120, 180):
                world(local(term("term-cd", 120, [*failing, box], ident=agent)))
                tick(st, NOW + later)
                expected = [("term-orch", stuck, True)] if later == 60 else []
                assert sends() == expected, (agent, later, sends())
                assert [s for s in sends(True) if s[0] != "term-orch"] == [], sends(True)


@scenario
def claude_concord_recovered_before_first_tick_gets_its_wake():
    """An old failure above a later Concord call that worked is history, though the
    worker recorded its recovery before the supervisor first saw the screen: no STUCK,
    and its review verdict wakes it."""
    reset_db()
    ticket_row("cd")
    update("cd", "progress", "Concord is back; ready for review.")
    update("cd", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(cd=rec("cd", "c1"))
    screen = [
        *CONCORD_TOOL_GONE,
        '⏺ concord - update_work (MCP)(task_id: "cd", kind: "blocker")',
        "  ⎿  Recorded blocker on cd.",
        "⏺ Concord is back; waiting for review.",
        *CLAUDE_BOX,
    ]
    world(local(term("term-cd", 5, screen)))
    tick(st)
    update("cd", "finding", "REVIEW VERDICT: FIX 1a2b3c4", agent="codex:cd-review")
    world(local(term("term-cd", 5, screen)))
    tick(st, NOW + 60)
    wake = "dev-auto: a review verdict was posted on ticket cd. Read it there and continue."
    assert sends() == [("term-cd", woke("cd", wake), True)], sends()


@scenario
def concord_wording_spares_healthy_and_other_agents():
    """A healthy Claude screen that only mentions MCP and quotes the failure wording in
    prose, a fenced example (its intro scrolled off or not) or its unsent input raises
    nothing; a Codex screen showing Claude's failure wording is unaffected."""
    quoted = [
        "⏺ The MCP documentation gives this example:",
        "```text",
        CONCORD_DISCONNECTED,
        "```",
        "⏺ Concord is healthy; continuing the tests.",
    ]
    fenced_error = [
        "⏺ The documentation gives this example tool result:",
        "```text",
        CONCORD_TOOL_GONE[1],
        "```",
        "⏺ Concord is healthy; continuing the tests.",
    ]
    typed = [f"❯ Explain this message: {CONCORD_DISCONNECTED}" if line == "❯" else line for line in CLAUDE_BOX]
    healthy = [
        "⏺ Reading how worker_command registers the concord MCP server.",
        "  ⎿  Read skills/dev-auto/scripts/dev_auto.py (40 lines)",
        "⏺ A lost server shows `No such tool available: mcp__concord__update_work` and",
        f"  {CONCORD_DISCONNECTED}; /mcp reconnect concord is the remedy.",
    ]
    for agent, screen, ident in (
        ("claude", [*healthy, *CLAUDE_BOX], "claude"),
        ("claude", [*quoted, *CLAUDE_BOX], "claude"),
        ("claude", [*fenced_error, *CLAUDE_BOX], "claude"),
        ("claude", [*fenced_error[1:], *CLAUDE_BOX], "claude"),
        ("claude", ["⏺ Concord is healthy; ready for the next question.", *typed], "claude"),
        ("codex", [*CONCORD_TOOL_GONE, CONCORD_DISCONNECTED, *CODEX_BOX], "codex"),
    ):
        reset_db()
        ticket_row("cd", agent=f"{agent}:cd")
        model = "gpt-6.1-sol" if agent == "codex" else "m"
        st = fresh_state(cd=rec("cd", "c1", agent=agent, model=model, effort="xhigh"))
        for later in (0, 60, 120):
            world(local(term("term-cd", 5, screen, ident=ident)))
            tick(st, NOW + later)
            assert sends() == [], (agent, later, sends())


@scenario
def concord_new_wording_spares_healthy_idle_screens():
    """Idle screens whose last Concord call worked, whose failure result belongs to
    another tool's call (a quoted Concord error included), or that quote the Linux and Codex failure wording in prose, a fence or
    the input, raise nothing and get nothing typed."""
    recovered_linux = [
        *CLAUDE_CONNECTION_CLOSED,
        "● Called concord (ctrl+o to expand)",
        "● Concord is back; continuing the tests.",
    ]
    quoted_linux = [
        "● A dead server leaves this under the call, and the reconnect can fail too:",
        "```text",
        CLAUDE_CONNECTION_CLOSED[1],
        CLAUDE_RECONNECT_FAILED[1],
        "```",
        "● Concord is healthy; it says Transport closed only in Codex.",
    ]
    typed = ["❯ Explain: ⎿  Error: Connection closed" if line == "❯" else line for line in CLAUDE_BOX]
    recovered_codex = [
        *CODEX_TRANSPORT_CLOSED,
        '• Called concord.update_work({"task_id":"cd","kind":"progress"})',
        "• Recorded the progress note.",
    ]
    prose_codex = [
        "• A dead concord shows Failed concord.inspect_work, then Transport closed under",
        "  Caused by. The relaunch is the remedy.",
    ]
    other_server = [
        '● slack - search (MCP)(query: "release")',
        "  ⎿  Error: Connection closed",
        "● Slack failed; Concord is healthy.",
    ]
    quoted_fixture = [
        "⏺ Bash(cat fixture.txt)",
        "  ⎿  Error: No such tool available: mcp__concord__update_work",
        "⏺ This is the captured failure fixture; Concord is healthy.",
    ]
    other_mcp = [
        "⏺ mcp__slack__search",
        "  ⎿  Error: Connection closed",
        "⏺ Slack failed; Concord is healthy.",
    ]
    for agent, screen in (
        ("claude", [*other_server, *CLAUDE_BOX]),
        ("claude", [*quoted_fixture, *CLAUDE_BOX]),
        ("claude", [*other_mcp, *CLAUDE_BOX]),
        ("claude", ["● Fetch(https://example.com)", "  ⎿  Error: Connection closed", *CLAUDE_BOX]),
        ("claude", [*recovered_linux, *CLAUDE_BOX]),
        ("claude", [*quoted_linux, *CLAUDE_BOX]),
        ("claude", [*quoted_linux[1:], *CLAUDE_BOX]),
        ("claude", ["● Concord is healthy.", *typed]),
        ("claude", [*CODEX_TRANSPORT_CLOSED, *CLAUDE_BOX]),
        ("codex", [*recovered_codex, *CODEX_BOX]),
        ("codex", [*prose_codex, *CODEX_BOX]),
        ("codex", [*CLAUDE_CONNECTION_CLOSED, *CLAUDE_RECONNECT_FAILED, *CODEX_BOX]),
    ):
        reset_db()
        ticket_row("cd", agent=f"{agent}:cd")
        model = "gpt-6.1-sol" if agent == "codex" else "m"
        st = fresh_state(cd=rec("cd", "c1", agent=agent, model=model, effort="xhigh"))
        for later in (0, 60, 120):
            world(local(term("term-cd", 120, screen, ident=agent)))
            tick(st, NOW + later)
            assert sends(include_clear=True) == [] and created() == [], (agent, later, sends(True))


@scenario
def agent_prose_about_limits_moves_nobody():
    """S5: output that opens with "limit reached", on an account with unread usage, is
    not a limit screen: no close, no move, no mark."""
    reset_db()
    ticket_row("lp", agent="cursor:lp")
    prose = [
        "⏺ Notes on the supervisor's limit handling:",
        "  limit reached screens go through usage_limit(), which moves or parks.",
        "  Usage limit reached handling keeps its policy.",
        "› ",
    ]
    st = fresh_state(lp=rec("lp", "cur", agent="cursor", model="g"))
    world(local(term("term-lp", 120, prose, ident="cursor")))
    tick(st)
    assert calls("orca", ["terminal", "close"]) == [] and created() == []
    assert st["limits"] == {} and sends() == [], (st["limits"], sends())


@scenario
def silent_worker():
    """One nudge, then STUCK with the last screen on the ticket; output resets it."""
    reset_db()
    ticket_row("s1")
    st = fresh_state(s1=rec("s1", "c1"))
    world(local(term("term-s1", 29 * 60)))
    tick(st)
    assert sends() == []
    screen = ["Edited src/a.ts", "❯ "]
    world(local(term("term-s1", 31 * 60, screen)))
    tick(st)
    assert sends() == [
        (
            "term-s1",
            woke(
                "s1",
                "dev-auto: no output for 30 minutes while ticket s1 is open. "
                "Continue, or record a Waiting on: blocker note on it.",
            ),
            True,
        )
    ]
    world(local(term("term-s1", 41 * 60, screen)))
    tick(st, NOW + 600)
    assert sends() == []
    world(local(term("term-s1", 61 * 60, screen)))
    tick(st, NOW + 1800)
    ((who, text, _),) = sends()
    assert (who, text) == (
        "term-orch",
        (
            "STUCK s1 claude:s1 on local term-s1: silent 30 minutes after a nudge. "
            "Its last screen is on the ticket"
        ),
    ), text
    assert notes()[-1].endswith("last screen is on the ticket\nEdited src/a.ts\n❯")
    world(local(term("term-s1", 61 * 60, screen)))
    tick(st, NOW + 1800)
    assert sends() == []
    world(local(term("term-s1", -1800, screen)))
    tick(st, NOW + 1800 + 31 * 60)
    assert [s[0] for s in sends()] == ["term-s1"], "new silence after output: a nudge"


NUDGE_S1 = (
    "dev-auto: no output for 30 minutes while ticket s1 is open. "
    "Continue, or record a Waiting on: blocker note on it."
)


@scenario
def nudge_echo_is_not_worker_output():
    """A worker whose only output after its nudge is the nudge's own echo gets one STUCK
    silent_minutes after the nudge, never a fresh nudge, and never a second STUCK."""
    reset_db()
    ticket_row("s1")
    st = fresh_state(s1=rec("s1", "c1"))
    screen = ["Edited src/a.ts", "❯ "]
    world(local(term("term-s1", 31 * 60, screen)), echo_at=int(NOW * 1000))
    tick(st)
    assert sends() == [("term-s1", woke("s1", NUDGE_S1), True)], sends()
    echoed = local(term("term-s1", 0, screen))
    world(echoed)
    tick(st, NOW + 600)
    assert sends() == [], "the echo is not output and the nudge is not yet due"
    world(echoed)
    tick(st, NOW + 1800)
    assert [s[:2] for s in sends()] == [
        (
            "term-orch",
            (
                "STUCK s1 claude:s1 on local term-s1: silent 30 minutes after a nudge. "
                "Its last screen is on the ticket"
            ),
        )
    ], sends()
    for later in (1860, 3600, 5400):
        world(echoed)
        tick(st, NOW + later)
        assert sends() == [], (later, sends())


@scenario
def real_output_after_nudge_clears_it():
    """Output past the nudge's echo clears the nudge: no STUCK, and the next nudge comes
    only a new silent_minutes after that output."""
    reset_db()
    ticket_row("s1")
    st = fresh_state(s1=rec("s1", "c1"))
    screen = ["Edited src/a.ts", "❯ "]
    world(local(term("term-s1", 31 * 60, screen)), echo_at=int(NOW * 1000))
    tick(st)
    assert [s[0] for s in sends()] == ["term-s1"]
    worked = local(term("term-s1", -300, ["Edited src/b.ts", "❯ "]))
    for later in (600, 1800, 300 + 29 * 60):
        world(worked)
        tick(st, NOW + later)
        assert sends() == [], (later, sends())
    world(worked, echo_at=int((NOW + 300 + 31 * 60) * 1000))
    tick(st, NOW + 300 + 31 * 60)
    # A repeat of a typed text carries its event.
    again = f"{NUDGE_S1} Event nudge {int(NOW) + 300}."
    assert sends() == [("term-s1", woke("s1", again), True)], sends()


def nudge_of(tid):
    return (
        f"dev-auto: no output for 30 minutes while ticket {tid} is open. "
        "Continue, or record a Waiting on: blocker note on it."
    )


@scenario
def reviewer_after_its_verdict_gets_no_nudge():
    """Live (delivered.log 11-11..11-14): 154 of 170 silence nudges went to reviewers
    waiting on their coder. A reviewer whose verdict answers every review request is
    not nudged, after a PUSH or a FIX; one whose coder asks again and then goes silent
    mid-review is nudged as before."""
    reset_db()
    ticket_row("rv")
    ticket_row("rv-review", agent="codex:rv-review")
    reviewer = rec("rv-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"rv-review": reviewer})
    update("rv", "progress", "REVIEW READY 1a2b3c4")
    update("rv", "blocker", "Waiting on: review of 1a2b3c4")
    update("rv", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:rv-review")
    update("rv", "progress", "fixing the finding")
    silent = local(term("term-rv-review", 7200, ident="codex"))
    world(silent)
    tick(st)
    assert sends() == [], "a reviewer after its FIX waits for the next request"
    update("rv", "progress", "REVIEW READY 5e6f7a8")
    world(silent)
    tick(st)
    assert [s[0] for s in sends()] == ["term-rv-review"], sends()
    world(silent)
    tick(st, NOW + 60)
    assert [s[1] for s in sends()] == [woke("rv-review", nudge_of("rv-review"), "codex")], (
        "silent mid-review: nudged as today"
    )
    push = update("rv", "finding", "REVIEW VERDICT: PUSH 5e6f7a8 no findings", "codex:rv-review")
    st["cleared_floor"] = push  # the orchestrator's CLEARED REVIEW is not under test
    later = local(term("term-rv-review", 0, ident="codex"))
    for wait in (120, 1800 + 120, 7200):
        world(later)
        tick(st, NOW + wait)
        assert sends() == [], (wait, sends())



@scenario
def reviewed_all_needs_a_live_verdict_for_each_requested_sha():
    """A verdict on B leaves a request for A open, and so does A's verdict once superseded:
    its reviewer still has work and is not taken for done."""
    reset_db()
    ticket_row("ra")
    update("ra", "progress", "REVIEW READY 1a2b3c4")
    update("ra", "progress", "REVIEW READY 5e6f7a8")
    update("ra", "finding", "REVIEW VERDICT: FIX 5e6f7a8 one finding", "codex:ra-review")
    assert not da.reviewed_all(da.read_tickets(root)["", "ra"]), "A is still open"
    push = update("ra", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:ra-review")
    assert da.reviewed_all(da.read_tickets(root)["", "ra"]), "both answered"
    supersede(push, update("ra", "decision", "withdrawn", "codex:ra-review"))
    assert not da.reviewed_all(da.read_tickets(root)["", "ra"]), "A's verdict withdrawn"

@scenario
def coder_completion_wakes_a_reviewer_with_no_live_wait():
    """Live (#7784): dev-auto-fixes-29 completed while its reviewer's latest update was a
    progress note after its Waiting on note, so no wait was live and no completion wake
    came. The coder ticket completing wakes its reviewer once, wait note or not."""
    reset_db()
    ticket_row("cd")
    ticket_row("cd-review", agent="codex:cd-review")
    reviewer = rec("cd-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"cd-review": reviewer})
    update("cd-review", "blocker", "Waiting on: ticket:cd to complete", agent="codex:cd-review")
    # No request or verdict: a PUSH as the latest would make the review done, closed and not woken.
    update("cd-review", "progress", "Reviewed; blocker stands", agent="codex:cd-review")
    ticket_row("cd", status="complete")
    screen = local(term("term-cd-review", 100, ident="codex"))
    world(screen)
    tick(st)
    done = "dev-auto: ticket cd is complete. Continue ticket cd-review."
    assert sends() == [("term-cd-review", woke("cd-review", done, "codex"), True)], sends()
    for wait in (60, 3600):
        world(local(term("term-cd-review", 0, ident="codex")))
        tick(st, NOW + wait)
        assert sends() == [], (wait, sends())


@scenario
def parked_limit_screen_gets_no_nudge():
    """C4c: a worker parked on Claude's limit screen, silent past the nudge time with its
    usage reading below the limit, is never typed into."""
    reset_db()
    ticket_row("pn")
    claude_usage("c1", 20, NOW + 3600)
    st = fresh_state(pn=rec("pn", "c1", limit_reset=NOW + 3600))
    # Every other Claude account is held, so the parked worker has nowhere to move.
    st["limits"].update(c2=NOW + 3600, c3=NOW + 3600)
    world(local(term("term-pn", 31 * 60, [CLAUDE_CONTINUING, *CLAUDE_PANEL])))
    tick(st)
    assert sends() == [] and created() == [], sends()
    assert st["workers"]["pn"]["limit_reset"] == NOW + 3600
    clear_usage()


@scenario
def exited_relaunch():
    """An agent back at its shell: relaunch from the handoff; a fast exit is STUCK."""
    reset_db()
    ticket_row("e1")
    st = fresh_state(e1=rec("e1", "c1"))
    world(local(term("term-e1", 300, ["% "], ident=None)), git=HR_RELAY_GIT)
    tick(st)
    (new,) = created()
    prompt = shlex.split(opt(new, "--command"))[-1]
    assert_ci_only_prompt(prompt)
    assert (
        prompt.startswith("/goal ")
        and "previous session on this ticket exited" in prompt
    )
    assert opt(new, "--worktree") == f"path:{st['workers']['e1']['worktree']}"
    assert (
        "--session-id" in opt(new, "--command")
        and st["workers"]["e1"]["session"] != "sess-e1"
    )
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == [
        "term-e1"
    ]
    assert st["workers"]["e1"]["terminal"] == "term-local-1"
    shell = term("term-local-1", 30, ["% "], ident=None, worktree=str(tmp / "wt/e1"))
    world(local(shell))
    tick(st, NOW + 30)
    assert created() == [] and sends() == [], "an agent not yet seen gets a minute"
    world(local(shell))
    tick(st, NOW + 60)
    assert created() == [] and sends()[0][1].startswith(
        "STUCK e1 claude:e1: its terminal exited within 10"
    )


def worker_status(st, tid):
    shown = st["workers"][tid].get("status") or {}
    return shown.get("state"), shown.get("detail"), shown.get("since")


def noted(update_id, at):
    """Give an update the time Concord would have written."""
    con = sqlite3.connect(db)
    stamped = datetime.fromtimestamp(at).astimezone().isoformat()
    con.execute("UPDATE task_updates SET created_at = ? WHERE id = ?", (stamped, update_id))
    con.commit()
    con.close()


@scenario
def status_working_then_quiet():
    """Fresh output is working, a silence under silent_minutes is quiet; since stays while
    nothing changes. The tick records when it ended and how long it took."""
    reset_db()
    ticket_row("sw")
    st = fresh_state(sw=rec("sw", "c1"))
    world(local(term("term-sw", 10)))
    tick(st)
    shown = {"state": "working", "detail": "", "since": NOW, "seen": NOW, "output_at": NOW - 10}
    assert st["workers"]["sw"].get("status") == shown, st["workers"]["sw"].get("status")
    ended = st.get("tick") or {"at": 0, "seconds": -1}
    assert NOW <= ended["at"] < NOW + 60 and 0 <= ended["seconds"] < 60, ended
    world(local(term("term-sw", -50)))
    tick(st, NOW + 60)
    shown.update(seen=NOW + 60, output_at=NOW + 50)
    assert st["workers"]["sw"].get("status") == shown, st["workers"]["sw"].get("status")
    world(local(term("term-sw", -50)))
    tick(st, NOW + 600)
    assert worker_status(st, "sw") == ("quiet", "", NOW + 600), worker_status(st, "sw")
    assert sends() == [], sends()


@scenario
def status_waiting_shows_target_and_note_time():
    """A wait shows its target, since the note's time; a new target moves since."""
    reset_db()
    ticket_row("sv")
    ticket_row("sv-review", agent="codex:sv-review")
    ticket_row("sx")
    update("sv", "progress", "REVIEW READY abc1234")
    noted(update("sv", "blocker", "Waiting on: review of abc1234"), NOW - 300)
    st = fresh_state(sv=rec("sv", "c1"))
    world(local(term("term-sv", 7200)))
    tick(st)
    assert worker_status(st, "sv") == ("waiting", "review", NOW - 300), worker_status(st, "sv")
    world(local(term("term-sv", 7200)))
    tick(st, NOW + 60)
    assert worker_status(st, "sv") == ("waiting", "review", NOW - 300), worker_status(st, "sv")
    noted(update("sv", "blocker", "Waiting on: ticket:sx for its schema"), NOW + 90)
    world(local(term("term-sv", 7200)))
    tick(st, NOW + 120)
    assert worker_status(st, "sv") == ("waiting", "ticket:sx", NOW + 90), worker_status(st, "sv")
    assert sends() == [], sends()


@scenario
def answered_review_wait_with_output_is_working():
    """Live 2026-11-15, dev-auto-fixes-56: a FIX verdict landed after the coder's review
    wait and its wake was typed; the coder made output, yet it still read waiting on
    review. Fresh output after an answered wait is working; the wake is typed as before."""
    reset_db()
    ticket_row("aw")
    ticket_row("aw-review", agent="codex:aw-review")
    update("aw", "progress", "REVIEW READY 1a2b3c4")
    noted(update("aw", "blocker", "Waiting on: review of 1a2b3c4"), NOW - 300)
    st = fresh_state(aw=rec("aw", "c1"))
    world(local(term("term-aw", 10)))
    tick(st)
    assert worker_status(st, "aw") == ("waiting", "review", NOW - 300), worker_status(st, "aw")
    update("aw", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:aw-review")
    world(local(term("term-aw", 7200)))
    tick(st, NOW + 60)
    text = "dev-auto: a review verdict was posted on ticket aw. Read it there and continue."
    assert sends() == [("term-aw", woke("aw", text), True)], sends()
    assert worker_status(st, "aw") == ("waiting", "review", NOW - 300), worker_status(st, "aw")
    world(local(term("term-aw", -110)))
    tick(st, NOW + 120)
    assert worker_status(st, "aw") == ("working", "", NOW + 120), worker_status(st, "aw")
    assert sends() == [], sends()


@scenario
def reviewer_woken_after_its_wait_is_working():
    """Live 2026-11-15, dev-auto-fixes-58-review: the reviewer's wait on its coder stood
    while it ran its checks for a REVIEW READY it was woken for after that note. Fresh
    output after the request is working; the wake is typed once as before."""
    reset_db()
    ticket_row("rw")
    ticket_row("rw-review", agent="codex:rw-review")
    noted(update("rw-review", "blocker", "Waiting on: ticket:rw", "codex:rw-review"), NOW - 600)
    reviewer = rec("rw-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"rw-review": reviewer})
    world(local(term("term-rw-review", 10, ident="codex")))
    tick(st)
    shown = worker_status(st, "rw-review")
    assert shown == ("waiting", "ticket:rw", NOW - 600), shown
    update("rw", "progress", "REVIEW READY 1a2b3c4")
    world(local(term("term-rw-review", 7200, ident="codex")))
    tick(st, NOW + 60)
    text = "dev-auto: REVIEW READY was posted on ticket rw. Review it as your ticket says."
    assert sends() == [("term-rw-review", woke("rw-review", text, "codex"), True)], sends()
    world(local(term("term-rw-review", -110, ident="codex")))
    tick(st, NOW + 120)
    shown = worker_status(st, "rw-review")
    assert shown == ("working", "", NOW + 120), shown
    assert sends() == [], sends()


@scenario
def reviewer_woken_by_coder_completion_is_working():
    """A reviewer waiting on its coder's ticket is woken once that ticket completes; fresh
    output after that wake is working, and the wake is typed once as before."""
    reset_db()
    ticket_row("cw")
    ticket_row("cw-review", agent="codex:cw-review")
    noted(update("cw-review", "blocker", "Waiting on: ticket:cw", "codex:cw-review"), NOW - 600)
    reviewer = rec("cw-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"cw-review": reviewer})
    ticket_row("cw", status="complete")
    world(local(term("term-cw-review", 7200, ident="codex")))
    tick(st)
    text = "dev-auto: ticket cw is complete. Continue ticket cw-review."
    assert sends() == [("term-cw-review", woke("cw-review", text, "codex"), True)], sends()
    shown = worker_status(st, "cw-review")
    assert shown == ("waiting", "ticket:cw", NOW - 600), shown
    world(local(term("term-cw-review", -110, ident="codex")))
    tick(st, NOW + 120)
    shown = worker_status(st, "cw-review")
    assert shown == ("working", "", NOW + 120), shown
    assert sends() == [], sends()
    # The completion wake answers no later wait on another ticket.
    ticket_row("cdep")
    noted(update("cw-review", "blocker", "Waiting on: ticket:cdep", "codex:cw-review"), NOW + 130)
    world(local(term("term-cw-review", -140, ident="codex")))
    tick(st, NOW + 150)
    shown = worker_status(st, "cw-review")
    assert shown == ("waiting", "ticket:cdep", NOW + 130), shown
    assert sends() == [], sends()
    # Nor a new wait on its coder once that ticket is reopened.
    ticket_row("cw")
    note = update("cw-review", "blocker", "Waiting on: ticket:cw for its reopened work", "codex:cw-review")
    noted(note, NOW + 160)
    world(local(term("term-cw-review", -170, ident="codex")))
    tick(st, NOW + 180)
    shown = worker_status(st, "cw-review")
    assert shown == ("waiting", "ticket:cw", NOW + 160), shown
    assert sends() == [], sends()


@scenario
def status_asking_names_the_prompt():
    """An open question on the screen is asking."""
    reset_db()
    ticket_row("sa", agent="codex:sa")
    st = fresh_state(sa=rec("sa", "x1", agent="codex"))
    trust = [
        "Folder access",
        "Trust this folder? Codex can read, edit, and run files here, subject to your",
        "› 1. Trust and continue",
        "  2. Back to Agent Command Center",
        "  enter continue · esc back",
    ]
    world(local(term("term-sa", 90, trust, ident="codex")))
    tick(st)
    assert worker_status(st, "sa") == ("asking", "question", NOW), worker_status(st, "sa")


@scenario
def status_limited_shows_the_reset():
    """A usage-limit screen is limited, with its reset."""
    reset_db()
    ticket_row("sl")
    claude_usage("c1", 20, NOW + 3600)
    st = fresh_state(sl=rec("sl", "c1", limit_reset=NOW + 3600))
    # Every other Claude account is held, so the parked worker has nowhere to move.
    st["limits"].update(c2=NOW + 3600, c3=NOW + 3600)
    world(local(term("term-sl", 31 * 60, [CLAUDE_CONTINUING, *CLAUDE_PANEL])))
    tick(st)
    assert worker_status(st, "sl") == ("limited", da.stamp(NOW + 3600), NOW), worker_status(st, "sl")
    clear_usage()


@scenario
def status_nudged_since_its_nudge():
    """A nudged worker still silent is nudged, since the nudge."""
    reset_db()
    ticket_row("sn")
    st = fresh_state(sn=rec("sn", "c1", nudged_at=NOW - 600))
    world(local(term("term-sn", 31 * 60)))
    tick(st)
    assert worker_status(st, "sn") == ("nudged", "", NOW - 600), worker_status(st, "sn")
    assert sends() == [], sends()
    ticket_row("sf")
    st = fresh_state(sf=rec("sf", "c1"))
    world(local(term("term-sf", 31 * 60)))
    tick(st)
    assert [s[0] for s in sends()] == ["term-sf"], "the nudge is sent this tick"
    assert worker_status(st, "sf") == ("nudged", "", NOW), worker_status(st, "sf")
    # The nudge's own echo, after the tick began, is no answer; output past it is.
    ticket_row("sg")
    st = fresh_state(sg=rec("sg", "c1"))
    screen = ["Edited src/a.ts", "❯ "]
    world(local(term("term-sg", 31 * 60, screen)), echo_at=int((NOW + 1) * 1000))
    tick(st)
    assert st["workers"]["sg"]["nudge_echo"] == NOW + 1, st["workers"]["sg"]
    assert worker_status(st, "sg") == ("nudged", "", NOW), worker_status(st, "sg")
    world(local(term("term-sg", -1, screen)))
    tick(st, NOW + 600)
    assert sends() == [], sends()
    assert worker_status(st, "sg") == ("nudged", "", NOW), worker_status(st, "sg")
    world(local(term("term-sg", -500, screen)))
    tick(st, NOW + 620)
    assert worker_status(st, "sg") == ("quiet", "", NOW + 620), worker_status(st, "sg")


@scenario
def status_stuck_carries_its_text_while_it_stands():
    """A raised STUCK is on the record while it stands; output ends it."""
    reset_db()
    ticket_row("sk")
    st = fresh_state(sk=rec("sk", "c1", nudged_at=NOW - 1800))
    world(local(term("term-sk", 61 * 60)))
    tick(st)
    text = (
        "STUCK sk claude:sk on local term-sk: silent 30 minutes after a nudge. "
        "Its last screen is on the ticket"
    )
    assert worker_status(st, "sk") == ("stuck", text, NOW), worker_status(st, "sk")
    world(local(term("term-sk", 61 * 60)))
    tick(st, NOW + 60)
    assert worker_status(st, "sk") == ("stuck", text, NOW), worker_status(st, "sk")
    world(local(term("term-sk", -100)))
    tick(st, NOW + 120)
    assert worker_status(st, "sk") == ("working", "", NOW + 120), worker_status(st, "sk")


@scenario
def status_unanswered_wake_stuck_stands_until_the_worker_answers():
    """A STUCK raised once stays on the record until the worker updates or outputs."""
    st, _wake = pending_wake("sp")
    tick(st, NOW + 200)
    (text,) = orchestrator_stucks("sp")
    assert worker_status(st, "sp") == ("stuck", text, NOW + 200), worker_status(st, "sp")
    log_file.write_text("")
    tick(st, NOW + 400)
    assert orchestrator_stucks("sp") == [], "raised once"
    assert worker_status(st, "sp") == ("stuck", text, NOW + 200), worker_status(st, "sp")
    noted(update("sp", "progress", "reading the verdict", "codex:sp"), NOW + 450)
    tick(st, NOW + 500)
    assert worker_status(st, "sp")[0] == "quiet", worker_status(st, "sp")


@scenario
def status_cleanup_stuck_stands_until_rechecked():
    """A done record's cleanup STUCK stays through the cached ticks; a recheck that no
    longer raises it ends it."""
    reset_db()
    ticket_row("sz", status="complete")
    dirty = {STATUS: [0, "?? new.ts\n"]}
    st = finished_cleanup("sz", False, change=dirty, gh=no_pr)
    text = "STUCK sz: ticket is complete but its worktree is dirty. Not removed"
    assert worker_status(st, "sz") == ("stuck", text, NOW), worker_status(st, "sz")
    finished_cleanup("sz", False, change=dirty, gh=no_pr, st=st, now=NOW + 60)
    assert worker_status(st, "sz") == ("stuck", text, NOW), worker_status(st, "sz")
    finished_cleanup("sz", False, gh=opened, st=st, now=NOW + 960)
    shown = ("done", "its worktree is not removed", NOW + 960)
    assert worker_status(st, "sz") == shown, worker_status(st, "sz")


@scenario
def status_lost_exited_and_no_agent():
    """A lost record is lost, an exited terminal exited, a bare shell before its agent
    shows no-agent."""
    reset_db()
    for tid in ("so", "se", "sb"):
        ticket_row(tid)
    lost = "STUCK so claude:so: terminal term-so is not listed. Left alone"
    st = fresh_state(
        so=rec("so", "c1", lost=lost),
        se=rec("se", "c2"),
        sb=rec("sb", "c3", launched_at=NOW - 30),
    )
    shell = {"ident": None, "tail": ["% "]}
    world(local(term("term-se", 300, **shell), term("term-sb", 5, **shell)))
    tick(st)
    assert worker_status(st, "so") == ("lost", lost, NOW), worker_status(st, "so")
    assert worker_status(st, "se") == ("exited", "", NOW), worker_status(st, "se")
    assert worker_status(st, "sb") == ("no-agent", "", NOW), worker_status(st, "sb")


@scenario
def status_done_says_why_it_is_kept():
    """A complete ticket whose record stays is done, with the reason."""
    reset_db()
    ticket_row("sd", status="complete")
    st = finished_cleanup("sd", live=False, gh=opened)
    assert worker_status(st, "sd") == ("done", "its worktree is not removed", NOW), worker_status(st, "sd")
    # A terminal cleanup closes keeps the output time this tick listed for it.
    dirty = {STATUS: [0, "?? new.ts\n"]}
    ticket_row("sc", status="complete")
    st = finished_cleanup("sc", True, change=dirty, gh=no_pr)
    assert st["workers"]["sc"]["closed"], st["workers"]["sc"]
    assert st["workers"]["sc"]["status"]["output_at"] == NOW - 100, st["workers"]["sc"]["status"]
    ticket_row("sq")
    st = fresh_state(sq=rec("sq", "c1"))
    world(local(term("term-sq", 10)))
    tick(st)
    ticket_row("sq", status="complete")
    world(local(term("term-sq", -50)), git={**git, **dirty}, gh=no_pr)
    tick(st, NOW + 60)
    assert st["workers"]["sq"]["closed"], st["workers"]["sq"]
    assert st["workers"]["sq"]["status"]["output_at"] == NOW + 50, st["workers"]["sq"]["status"]


@scenario
def status_closed_review_is_done():
    """A review ticket dev-auto closes for its gone reviewer is done that tick, from no
    status, a quiet one or the STUCK of a close Concord refused; a refusal stays stuck."""
    shown = ("done", "its review ticket was closed; cleanup follows", NOW)
    st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
    world([], git=git, gh=merged)
    tick(st)
    assert status_of("gv-review") == "complete"
    assert worker_status(st, "gv-review") == shown, worker_status(st, "gv-review")
    st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
    quiet = {"state": "quiet", "detail": "", "since": NOW - 600, "seen": NOW - 60, "output_at": NOW - 900}
    st["workers"]["gv-review"]["status"] = quiet
    world([], git=git, gh=merged)
    tick(st)
    assert worker_status(st, "gv-review") == shown, worker_status(st, "gv-review")
    assert st["workers"]["gv-review"]["status"]["output_at"] == NOW - 900
    st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
    world([], git=git, gh=merged, finish_refused="task version is stale")
    tick(st)
    assert worker_status(st, "gv-review")[0] == "stuck", worker_status(st, "gv-review")
    world([], git=git, gh=merged)
    tick(st, NOW + 60)
    shown = ("done", "its review ticket was closed; cleanup follows", NOW + 60)
    assert worker_status(st, "gv-review") == shown, worker_status(st, "gv-review")


@scenario
def status_unread_host_keeps_old_statuses():
    """A tick that cannot list a host, or read a screen, leaves its statuses as they were;
    an unread first screen still gives one from the listed output time."""
    reset_db()
    ticket_row("sr")
    st = fresh_state(sr=rec("sr", "c1"))
    row, screen = term("term-sr", 10)
    world(local((row, {**screen, "unread": True})))
    tick(st)
    shown = {"state": "working", "detail": "", "since": NOW, "seen": NOW, "output_at": NOW - 10}
    assert st["workers"]["sr"].get("status") == shown, st["workers"]["sr"].get("status")
    row, screen = term("term-sr", 600)
    world(local((row, {**screen, "unread": True})))
    tick(st, NOW + 60)
    assert st["workers"]["sr"]["status"] == shown, "an unread screen changes nothing"
    reset_db()
    ticket_row("su")
    st = fresh_state(su=rec("su", "c1"))
    world(local(term("term-su", 10)))
    tick(st)
    before = st["workers"]["su"].get("status")
    assert before, "a status after a read tick"
    set_fixture(terminals={"local": "down"}, screens={})
    tick(st, NOW + 60)
    assert st["workers"]["su"].get("status") == before, st["workers"]["su"].get("status")


@scenario
def relaunch_closes_the_old_tab():
    """An exited worker's old session goes with its tab before the relaunch, so a restart
    restores no second copy of it."""
    reset_db()
    ticket_row("et")
    st = fresh_state(et=rec("et", "c1"))
    world(local(term("term-et", 300, ["% "], ident=None)))
    tick(st)
    assert st["workers"]["et"]["terminal"] == "term-local-1"
    assert tab_closes() == [("term-et", True)] and resumable() == [], (tab_closes(), resumable())


@scenario
def refused_close_starts_no_replacement():
    """da-3-5: Orca refusing to close the old terminal stops the relaunch of an exited
    worker, the Concord repair of a Codex one and the move of a usage-limited Claude one:
    nothing starts and the record keeps its terminal and account."""
    reset_db()
    ticket_row("ec")
    st = fresh_state(ec=rec("ec", "c1"))
    world(local(term("term-ec", 300, ["% "], ident=None)), close_fails=["term-ec"])
    tick(st)
    worker = st["workers"]["ec"]
    assert created() == [] and worker["terminal"] == "term-ec", (created(), worker)
    assert "relaunches" not in worker, worker
    world(local(term("term-ec", 360, ["% "], ident=None)))
    tick(st, NOW + 60)
    assert len(created()) == 1, created()

    reset_db()
    ticket_row("cd-review", agent="codex:cd-review")
    st = fresh_state(
        **{"cd-review": rec("cd-review", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")}
    )
    screen = [*CODEX_TRANSPORT_CLOSED, *CODEX_BOX]
    world(local(term("term-cd-review", 120, screen, ident="codex")), close_fails=["term-cd-review"])
    tick(st)
    worker = st["workers"]["cd-review"]
    assert created() == [] and worker["terminal"] == "term-cd-review", (created(), worker)

    reset_db()
    ticket_row("um")
    claude_usage("c1", 100, NOW + 3600)
    st = fresh_state(um=rec("um", "c1"))
    world(local(term("term-um", 120, ["You've hit your limit · resets 5pm"])), close_fails=["term-um"])
    tick(st)
    worker = st["workers"]["um"]
    assert created() == [], created()
    assert (worker["terminal"], worker["account"], worker["session"]) == ("term-um", "c1", "sess-um"), worker
    clear_usage()

@scenario
def shells_close_with_their_tab():
    """dev-auto-fixes-50 (j): a launch closes the shell its old agent exited to, and cleanup
    closes the worktree's spare shells, each with its tab, so a restart restores none."""
    reset_db()
    ticket_row("sh")
    # launch reads the wall clock, not the scenarios' NOW.
    old = rec("sh", "c1", launched_at=time.time() - 7200)
    state_file.write_text(json.dumps({"workers": {"sh": old}}))
    world(local(term("term-sh", 31 * 60, ["➜  sh git:(feat/a)"], ident=None)))
    done = cli("launch", "sh", "--agent", "claude", "--model", OPUS, "--worktree", old["worktree"])
    assert done.returncode == 0, done
    assert ("term-sh", True) in tab_closes() and resumable() == [], (tab_closes(), resumable())
    state_file.unlink()

    reset_db()
    ticket_row("sp", status="closed")
    prompt = ["➜  sp git:(julianthant/sp)"]
    shells = [{"terminal": "term-spare", "pty": "pty-term-spare", "lines": prompt}]
    st = fresh_state(sp=rec("sp", "c1", shells=shells))
    spare = term("term-spare", 300, prompt, ident=None, worktree=st["workers"]["sp"]["worktree"])
    world(local(term("term-sp", 100), spare), git=git, gh=merged)
    tick(st)
    assert sorted(tab_closes()) == [("term-sp", True), ("term-spare", True)], tab_closes()
    assert resumable() == [] and "sp" not in st["workers"], (resumable(), st["workers"])


@scenario
def b1_rebinds_after_restart():
    """After a restart a worker is followed to the new handle of the pty it launched in."""
    reset_db()
    ticket_row("hs")
    st = fresh_state(hs=rec("hs", "c1"))
    moved = term("term-NEW", 31 * 60, pty="pty-term-hs")
    world(local(moved), orch="term-orch-new")
    tick(st)
    assert created() == [], "no second agent in a live worktree"
    assert st["workers"]["hs"]["terminal"] == "term-NEW"
    assert [s[0] for s in sends()] == ["term-NEW"], "supervised under its new handle"


@scenario
def stopped_cycle_stops_a_worker_under_its_new_handle():
    """da-2-11: during a stopped cycle a worker Orca lists under a new handle, with the pty
    it launched in, is followed there and told STOP once."""
    reset_db()
    ticket_row("hz")
    st = fresh_state(hz=rec("hz", "c1"))
    st["stopped"] = int(NOW - 60)
    st["relay"] = {"used": True, "by": "term-orch-old", "reset": NOW + 900}
    moved = term("term-NEWZ", 31 * 60, pty="pty-term-hz")
    for later in (0, 60):
        world(local(moved))
        tick(st, NOW + later)
        stops = [s for s in sends() if s[1] == da.STOP]
        assert stops == ([("term-NEWZ", da.STOP, True)] if later == 0 else []), (later, sends())
    assert st["workers"]["hz"]["terminal"] == "term-NEWZ", st["workers"]["hz"]



@scenario
def stopped_cycle_rechecks_each_unshown_stop():
    """da-2-11: two workers whose screens keep their frame during a stopped cycle: the tick
    waits for one recheck, and the other worker's STOP is rechecked and typed once more on
    the next stopped tick."""
    reset_db()
    for tid in ("sa", "sb"):
        ticket_row(tid)
    st = fresh_state(sa=rec("sa", "c1"), sb=rec("sb", "c2"))
    st["stopped"] = int(NOW - 60)
    st["relay"] = {"used": True, "by": "term-orch-old", "reset": NOW + 900}
    stops = []
    for later in (0, 60):
        world(local((term("term-sa", 31 * 60)[0], unshown()), (term("term-sb", 31 * 60)[0], unshown())))
        # Each tick is its own run, with its own one recheck wait.
        st.waited = False
        with recheck_wait():
            tick(st, NOW + later)
        stops += [s[0] for s in sends() if s[1] == da.STOP]
    assert stops.count("term-sa") == 2 and stops.count("term-sb") == 2, stops


@scenario
def stopped_cycle_stop_waits_for_its_listing():
    """da-2-11: a STOP not yet shown is not rechecked on a stopped tick whose terminal list
    failed for its host, though its screen reads; once the list shows its row, it is."""
    reset_db()
    ticket_row("sl")
    worker = rec("sl", "c1")
    st = fresh_state(sl=worker)
    st["stopped"] = int(NOW - 60)
    st["relay"] = {"used": True, "by": "term-orch-old", "reset": NOW + 900}
    key = f"sl|stop|{st['stopped']}"
    for sent in (f"stopped|{st['stopped']}", f"stopped|{st['stopped']}|full", key):
        st["sent"][sent] = int(NOW)
    worker["unshown"] = [{"key": key, "text": da.STOP, "typed": da.STOP, "seen": 0, "at": time.time() - 60, "update": 0, "output": int((NOW - 31 * 60) * 1000)}]
    for later, listed in ((0, False), (60, True)):
        world(local((term("term-sl", 31 * 60)[0], unshown())))
        if not listed:
            data = fixture()
            data["terminals"]["local"] = "down"
            fixture_file.write_text(json.dumps(data))
        st.waited = False
        with recheck_wait():
            tick(st, NOW + later)
        stops = [s for s in sends(include_clear=True) if s[0] == "term-sl"]
        assert (stops == []) if not listed else ((("term-sl", da.STOP, True)) in stops), (later, stops)

@scenario
def n11_lost_record_stays_lost():
    """Q2: a record left alone is not adopted by a later restart, nor its worktree removed."""
    reset_db()
    ticket_row("la", status="complete")
    ticket_row("lb")
    update("lb", "blocker", "Waiting on: review")
    st = fresh_state(la=rec("la", "c1"), lb=rec("lb", "c2"))
    op = term("term-op1", 100, worktree=str(tmp / "wt" / "la"))
    world(local(op, term("term-lb", 100)), git=git, gh=merged)
    tick(st)
    op = term("term-op1-new", 100, worktree=str(tmp / "wt" / "la"))
    lb = term("term-lb-new", 100, pty="pty-term-lb")
    world(local(op, lb), orch="term-orch-new", git=git, gh=merged)
    tick(st)
    assert calls("orca", ["terminal", "close"]) == [], "the operator's session stays"
    assert calls("orca", ["worktree", "rm"]) == [], "its worktree stays"
    assert "la" in st["workers"], "kept while the operator's session holds its worktree"
    assert st["workers"]["lb"]["terminal"] == "term-lb-new", "lb follows its pty"


@scenario
def n11_two_closed_tabs_on_a_remote():
    """Q3: both workers' tabs on one host gone and an operator session in a lane worktree:
    not a restart, nothing adopted, closed or removed."""
    reset_db()
    ticket_row("wa", status="complete")
    ticket_row("wb")
    update("wb", "blocker", "Waiting on: review")
    st = fresh_state(
        wa=rec("wa", "c3", host="Juzzark-WSL"), wb=rec("wb", "c3", host="Juzzark-WSL")
    )
    op = term("term-op-wsl", 100, worktree=str(tmp / "wt" / "wa"))
    world([("Juzzark-WSL", "term-op-wsl", *op)], git=git, gh=merged)
    tick(st)
    assert calls("orca", ["terminal", "close"]) == [], "the operator's session stays"
    assert calls("orca", ["worktree", "rm"]) == [] and "wa" in st["workers"]
    assert sorted(x[1].split(":")[0] for x in sends()) == ["STUCK wa", "STUCK wb claude"]
    assert any(x[1].endswith("other terminals term-op-wsl. Not removed") for x in sends())


@scenario
def idle_shell_in_a_recorded_worktree_is_closed():
    """A plain shell at its prompt in a recorded worktree, quiet 10 minutes and held by no
    record, is closed; a recent one, one elsewhere and the record's own stay."""
    reset_db()
    ticket_row("is")
    st = fresh_state(**{"is": rec("is", "c1")})
    wt = str(tmp / "wt" / "is")
    world(
        local(
            term("term-is", 5),
            term("term-old", 700, ["% "], ident=None, worktree=wt),
            term("term-new", 100, ["% "], ident=None, worktree=wt),
            term("term-away", 700, ["% "], ident=None, worktree=str(tmp / "elsewhere")),
        )
    )
    tick(st)
    assert closes() == [("term-old", "local")], closes()


@scenario
def orphan_worktree_swept_once_merged():
    """A worktree dev-auto owned whose record is gone and whose ticket is complete is
    removed with its idle shell once merged; while an open ticket names it, it stays."""
    reset_db()
    ticket_row("ow", status="complete")
    wt = tmp / "wt" / "ow"
    wt.mkdir(parents=True, exist_ok=True)
    ticket_row("keep", notes=f"Continue in {wt}")
    st = fresh_state()
    st["worktrees"] = {
        f"local|{wt}": {
            "host": "local",
            "worktree": str(wt),
            "ticket": "ow",
            "project": "",
            "at": 0,
            "owned_branch": "feat/a",
            "owned_common": f"{tmp / 'repo'}/.git",
        }
    }
    shell = term("term-ow", 700, ["% "], ident=None, worktree=str(wt))
    world(local(shell), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [], "named by an open ticket"
    ticket_row("keep", status="complete")
    world(local(shell), git=git, gh=merged)
    tick(st, NOW + da.RECHECK)
    assert len(calls("orca", ["worktree", "rm"])) == 1, calls("orca", ["worktree", "rm"])
    assert ("term-ow", "local") in closes() and st["worktrees"] == {}, closes()


def orphan(tid, host="local", notes=None, **entry):
    """A complete ticket's worktree dev-auto owned, its record gone."""
    reset_db()
    ticket_row(tid, status="complete", notes=notes)
    wt = tmp / "wt" / tid
    wt.mkdir(parents=True, exist_ok=True)
    st = fresh_state()
    st["worktrees"] = {
        f"{host}|{wt}": {
            "host": host,
            "worktree": str(wt),
            "ticket": tid,
            "project": "",
            "at": 0,
            "owned_branch": "feat/a",
            "owned_common": f"{tmp / 'repo'}/.git",
            **entry,
        }
    }
    return st, wt


@contextmanager
def during_check(**change):
    """The fixture takes these values once CHECK has run, as when the operator opens a
    pane in the worktree while it fetches."""
    real = da.run_on

    def run_on(host, worktree, argv, *rest, **kw):
        done = real(host, worktree, argv, *rest, **kw)
        if argv[:3] == ["sh", "-c", da.CHECK]:
            fixture_file.write_text(json.dumps({**fixture(), **change}))
        return done

    da.run_on = run_on
    try:
        yield
    finally:
        da.run_on = real


@scenario
def orphan_on_another_branch_or_detached_is_kept():
    """An orphan whose launch owned task/feat/oa, or whose entry predates recording that,
    now shows another merged branch, even one its brief names, or a detached checkout of
    origin's default branch with untracked files: it stays."""
    for owned in ("task/feat/oa", None):
        for answers in ({}, DETACHED):
            st, wt = orphan("oa", notes="Branch: `feat/a`", owned_branch=owned)
            world(local(), git={**git, **answers}, gh=merged, untracked=True)
            tick(st)
            assert calls("orca", ["worktree", "rm"]) == [] and st["worktrees"], (owned, answers)


@scenario
def orphan_sweep_relists_its_terminals_after_check():
    """CHECK takes seconds. An agent opened in the orphan meanwhile, its idle shell put to
    work, or a shell close Orca refuses keeps the worktree for a later tick."""
    for case in ("agent", "busy", "refused"):
        st, wt = orphan("os")
        shell = term("term-os", 700, ["% "], ident=None, worktree=str(wt))
        if case == "agent":
            world(local(shell, term("term-op", 5, worktree=str(wt))))
        else:
            world(local((shell[0], {"status": "running", "tail": ["% make test", "building"]})))
        after = fixture() if case != "refused" else {}
        refused = ["term-os"] if case == "refused" else []
        world(local(shell), git=git, gh=merged, close_fails=refused)
        with during_check(**{k: after[k] for k in ("terminals", "screens") if k in after}):
            tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and st["worktrees"], case


@scenario
def checkout_changed_during_check_is_kept():
    """CHECK takes seconds. Another branch checked out, a new commit or a new file
    meanwhile: neither the sweep nor cleanup removes the worktree on CHECK's stale answer."""
    changes = (
        {"rev-parse --abbrev-ref HEAD": [0, "feat/b\n"]},
        {"rev-parse HEAD": [0, "beef\n"]},
        {STATUS: [0, "?? new.txt\n"]},
    )
    for change in changes:
        st, wt = orphan("oc")
        world(local(), git=git, gh=merged)
        with during_check(git={**git, **change}):
            tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and st["worktrees"], change
        reset_db()
        ticket_row("cc", status="complete")
        st = fresh_state(cc=rec("cc", "c1"))
        world(local(term("term-cc", 100)), git=git, gh=merged)
        with during_check(git={**git, **change}):
            tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and "cc" in st["workers"], change


@contextmanager
def during_close(**change):
    """The fixture takes these values once a shell close returns, as when the operator
    checks out a branch or opens an agent while dev-auto closes the worktree's shells."""
    real = da.close_terminal

    def close_terminal(host, handle):
        done = real(host, handle)
        fixture_file.write_text(json.dumps({**fixture(), **change}))
        return done

    da.close_terminal = close_terminal
    try:
        yield
    finally:
        da.close_terminal = real


@scenario
def change_during_shell_close_is_kept():
    """Closing the worktree's shells takes Orca calls. A branch switched or an agent opened
    meanwhile, or a refused close, keeps the worktree, in the sweep and in cleanup; so does
    cleanup's own worker handle listed again then."""
    for case in ("switch", "agent", "refused"):
        st, wt = orphan("sc")
        shell = term("term-sc", 700, ["% "], ident=None, worktree=str(wt))
        agent = term("term-new", 5, worktree=str(wt))
        world(local(shell, agent), git=git, gh=merged)
        change = {
            "switch": {"git": {**git, "rev-parse --abbrev-ref HEAD": [0, "feat/b\n"]}},
            "agent": {"terminals": fixture()["terminals"]},
            "refused": {},
        }[case]
        world(local(shell), git=git, gh=merged, close_fails=["term-sc"] if case == "refused" else [])
        with during_close(**change):
            tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and st["worktrees"], case
        reset_db()
        ticket_row("cs", status="complete")
        st = fresh_state(cs=rec("cs", "c1"))
        cs_shell = term("term-cs-sh", 700, ["% "], ident=None, worktree=str(tmp / "wt" / "cs"))
        cs_agent = term("term-cs-new", 5, worktree=str(tmp / "wt" / "cs"))
        # Its own terminal is already gone, so the first close is the shell's.
        world(local(cs_shell, cs_agent), git=git, gh=merged)
        change = {
            "switch": {"git": {**git, "rev-parse --abbrev-ref HEAD": [0, "feat/b\n"]}},
            "agent": {"terminals": fixture()["terminals"]},
            "refused": {},
        }[case]
        refused = ["term-cs-sh"] if case == "refused" else []
        world(local(cs_shell), git=git, gh=merged, close_fails=refused)
        with during_close(**change):
            tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and "cs" in st["workers"], case
    # The worker's own saved handle, in neither listing before, is listed again meanwhile.
    reset_db()
    ticket_row("cs", status="complete")
    st = fresh_state(cs=rec("cs", "c1"))
    cs_shell = term("term-cs-sh", 700, ["% "], ident=None, worktree=str(tmp / "wt" / "cs"))
    world(local(term("term-cs", 100, worktree=str(tmp / "wt" / "cs"))))
    back = {"terminals": fixture()["terminals"]}
    world(local(cs_shell), git=git, gh=merged)
    with during_close(**back):
        tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "cs" in st["workers"]


@scenario
def path_reused_by_another_repository_is_kept():
    """The launch owned feat/a of one repository; its path now holds another repository's
    clean, merged feat/a. Neither the sweep nor cleanup removes it or deletes its branch."""
    st, wt = orphan("pr", owned_common="/elsewhere/repo/.git")
    world(local(), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and st["worktrees"]
    reset_db()
    ticket_row("rp", status="complete")
    st = fresh_state(rp=rec("rp", "c1", owned_common="/elsewhere/repo/.git"))
    world(local(term("term-rp", 100)), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "rp" in st["workers"]
    assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []
    assert [s[1] for s in sends()] == [
        f"STUCK rp: ticket is complete but its worktree holds the repository {tmp / 'repo'}/.git, "
        "not its own /elsewhere/repo/.git. Not removed"
    ], sends()


@scenario
def orphan_kept_when_its_remote_worktree_cannot_be_shown():
    """A remote host lists its terminals, then its `orca worktree show` times out and its
    probe fails: the orphan is kept, and removed once the host answers again. Only Orca's
    selector_not_found forgets it."""
    st, wt = orphan("or", host=WSL)
    world([], git=git, gh=merged, worktree_show="fails", probe_fails=True)
    tick(st)
    assert f"{WSL}|{wt}" in st["worktrees"], st["worktrees"]
    world([], git=git, gh=merged)
    tick(st, NOW + da.RECHECK)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and st["worktrees"] == {}, st["worktrees"]


@scenario
def n14_pty_learned_from_its_handle():
    """A pty missed at launch is learned from the worker's own handle, so it can still
    be cleaned up later."""
    reset_db()
    ticket_row("lp")
    st = fresh_state(lp=rec("lp", "c1", pty=None))
    world(local(term("term-lp", 100)))
    tick(st)
    ticket_row("lp", status="complete")
    world(local(term("term-lp", 100)), git=git, gh=merged)
    tick(st)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "lp" not in st["workers"]


@scenario
def n1_closed_tab_is_not_a_restart():
    """A closed tab while other handles stay listed: the operator's own session in that
    worktree is never adopted, typed into, closed or removed with the worktree."""
    reset_db()
    ticket_row("fa", status="complete")
    st = fresh_state(fa=rec("fa", "c1"))
    op = term("term-operator-claude", 100, worktree=str(tmp / "wt" / "fa"))
    world(local(op), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["terminal", "close"]) == [], "the operator's terminal stays"
    assert calls("orca", ["worktree", "rm"]) == [], "and so does its worktree"
    text = (
        "STUCK fa: ticket is complete but its worktree holds other terminals "
        "term-operator-claude. Not removed"
    )
    assert "fa" in st["workers"] and sends() == [("term-orch", text, True)], sends()


def finished_cleanup(tid, live, *extra, change=None, gh=merged, st=None, now=NOW):
    """One tick of complete ticket tid's cleanup, its own terminal listed or already gone."""
    st = st or fresh_state(**{tid: rec(tid, "c1")})
    mine = [term(f"term-{tid}", 100)] if live else []
    world(local(*mine, *extra), git={**git, **(change or {})}, gh=gh)
    tick(st, now)
    return st


@scenario
def done_worktree_removed_when_its_terminal_is_gone():
    """Live: finished worktrees piled up on the Mac and on WSL, as a finished ticket whose
    terminal had already exited had its record dropped with its worktree never checked.
    A gone terminal skips only the close: a merged, clean worktree and its branch go, and
    so does a lost worker's."""
    reset_db()
    ticket_row("gc", status="complete")
    st = finished_cleanup("gc", live=False)
    assert calls("orca", ["terminal", "close"]) == [], "a gone terminal is never closed"
    removed = [opt(r, "--worktree") for r in calls("orca", ["worktree", "rm"])]
    assert removed == [f"path:{tmp / 'wt' / 'gc'}"], removed
    deleted = [r["args"] for r in calls("git") if r["args"][0] == "update-ref"]
    assert deleted == [["update-ref", "-d", "refs/heads/feat/a", "c0ffee1"]], deleted
    assert st["workers"] == {} and sends() == [], (st["workers"], sends())
    ticket_row("gl", status="closed")
    lost = "STUCK gl claude:gl: terminal term-gl is not listed. Left alone"
    st = finished_cleanup("gl", False, st=fresh_state(gl=rec("gl", "c2", lost=lost)))
    assert calls("orca", ["terminal", "close"]) == []
    assert len(calls("orca", ["worktree", "rm"])) == 1 and st["workers"] == {}


def cleanup_refusals(live):
    """Dirty, unmerged, shared and unreachable worktrees of a complete ticket: today's
    STUCK once, nothing removed, the record kept. Only a listed own terminal is closed."""
    reset_db()
    ticket_row("gr", status="complete")
    wt = str(tmp / "wt" / "gr")
    operator = term("term-op", 100, worktree=wt)
    head = "STUCK gr: ticket is complete but its worktree"
    for extra, change, gh, text in (
        ((), {STATUS: [0, "?? new.ts\n"]}, no_pr, f"{head} is dirty. Not removed"),
        ((), {}, no_pr, f"{head} is unmerged. Not removed"),
        ((operator,), {}, merged, f"{head} holds other terminals term-op. Not removed"),
        ((), {STATUS: [128, ""]}, merged, f"{head} could not be checked"),
    ):
        st = finished_cleanup("gr", live, *extra, change=change, gh=gh)
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        assert closed == (["term-gr"] if live else []), closed
        assert calls("orca", ["worktree", "rm"]) == [] and "gr" in st["workers"]
        assert sends() == [("term-orch", text, True)], sends()
        # Its own terminal is closed by now; a check 16 minutes on repeats no STUCK.
        finished_cleanup("gr", False, *extra, change=change, gh=gh, st=st, now=NOW + 960)
        assert calls("orca", ["terminal", "close"]) == [], "never closed again"
        assert calls("orca", ["worktree", "rm"]) == [] and sends() == [], sends()
        assert "gr" in st["workers"]


@scenario
def done_refusals_when_its_terminal_is_gone():
    """A gone terminal leaves the worktree to the same checks and STUCKs as a live one."""
    cleanup_refusals(live=False)


@scenario
def done_refusals_when_its_terminal_is_live():
    """A live terminal is closed once, and its worktree then gets today's checks and STUCKs."""
    cleanup_refusals(live=True)


@scenario
def done_terminal_listed_again_is_closed_before_removal():
    """A terminal gone at one tick and listed again at a later one is still its own: it is
    closed through its launch pty before the worktree goes, never skipped as closed."""
    reset_db()
    ticket_row("gb", status="complete")
    st = finished_cleanup("gb", False, change={STATUS: [0, "?? new.ts\n"]}, gh=no_pr)
    dirty = "STUCK gb: ticket is complete but its worktree is dirty. Not removed"
    assert [x[1] for x in sends()] == [dirty], sends()
    finished_cleanup("gb", True, st=st, now=NOW + 960)
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    verbs = [r["args"][:2] for r in rows if r["cmd"] == "orca"]
    closes = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closes == ["term-gb"], closes
    assert verbs.index(["terminal", "close"]) < verbs.index(["worktree", "rm"]), verbs
    assert "gb" not in st["workers"]


SDK_SHELL = "julian@host:~/wt/se$"


def sdk_done(tail, on_eof=None, st=None, now=NOW, source=None):
    """One tick of complete sdk ticket se's cleanup with its runner's screen."""
    if st is None:
        reset_db()
        ticket_row("se", status="complete", agent="sdk:se")
        st = fresh_state(se=rec("se", "sdk1", agent="sdk", agent_id="sdk:se", session=None))
    row, screen = term("term-se", 100, tail, ident=None)
    if on_eof:
        screen["on_eof"] = on_eof
    if source:
        screen["source"] = source
    world(local((row, screen)), git=git, gh=merged)
    tick(st, now)
    return st


def verbs():
    return [r["args"][:2] for r in calls("orca")]


@scenario
def done_sdk_runner_ends_its_input_before_its_tab_closes():
    """Live (dashboard-backend-cpu-01, 2026-11-15): cleanup closed a complete ticket's idle
    sdk runner with its tab, the SIGHUP ended its session with no result, and the runner
    left an open spend hold that filled the month. An idle runner gets end of input, its
    exit to the shell is awaited, and only then does its tab close and the worktree go."""
    idle = ["⏺ Recorded the PR text and completed se", runner.IDLE, "›"]
    st = sdk_done(idle, on_eof={"tail": [*idle, SDK_SHELL]})
    typed = [(t, text) for t, text, _ in sends(include_clear=True) if t == "term-se"]
    assert typed == [("term-se", "\x15"), ("term-se", "\x04")], sends(include_clear=True)
    assert all(not enter for t, _, enter in sends(include_clear=True) if t == "term-se")
    assert tab_closes() == [("term-se", True)], tab_closes()
    order = verbs()
    eof = next(i for i, r in enumerate(calls("orca")) if "\x04" in r["args"])
    assert eof < order.index(["terminal", "close"]) < order.index(["worktree", "rm"]), order
    assert "se" not in st["workers"] and [x for x in sends() if x[0] == "term-orch"] == []
    # A runner that already exited, to its shell or with its terminal, gets no keys.
    for tail, status in (([*idle, SDK_SHELL], "running"), (idle, "exited")):
        reset_db()
        ticket_row("se", status="complete", agent="sdk:se")
        st = fresh_state(se=rec("se", "sdk1", agent="sdk", agent_id="sdk:se", session=None))
        row, screen = term("term-se", 100, tail, status=status, ident=None)
        world(local((row, screen)), git=git, gh=merged)
        tick(st)
        assert sends(include_clear=True) == [] and tab_closes() == [("term-se", True)], status


@scenario
def done_sdk_runner_busy_is_left_for_a_later_tick():
    """A runner mid-turn is never sent end of input or closed: a later tick finds it idle."""
    busy = ["⏺ Bash", runner.BUSY]
    st = sdk_done(busy)
    assert sends(include_clear=True) == [] and calls("orca", ["terminal", "close"]) == []
    assert calls("orca", ["worktree", "rm"]) == [] and "se" in st["workers"]
    # Text typed at its prompt is still idle; Ctrl-U clears it before the Ctrl-D.
    typed = [runner.IDLE, "› half a line"]
    sdk_done(typed, on_eof={"tail": [*typed, SDK_SHELL]}, st=st, now=NOW + 60)
    assert [x[1] for x in sends(include_clear=True)] == ["\x15", "\x04"], sends(True)
    assert tab_closes() == [("term-se", True)] and "se" not in st["workers"]


@scenario
def done_sdk_runner_idle_after_an_ignored_reset_still_ends():
    """sdk_worker.py prints IDLE once; a /clear, /reset or /new typed after it is ignored
    with its own row and a new prompt, and the runner is still idle. Both screens it
    leaves, with and without the typed command echoed, get end of input."""
    ignored = f"{runner.PROMPT}· sdk worker: ignored /clear".rstrip()
    echoed = [runner.IDLE, "› /reset", "· sdk worker: ignored /reset", "›"]
    for tail in ([runner.IDLE, ignored, "›"], echoed):
        st = sdk_done(tail, on_eof={"tail": [*tail, SDK_SHELL]})
        assert [x[1] for x in sends(include_clear=True)] == ["\x15", "\x04"], tail
        assert tab_closes() == [("term-se", True)] and "se" not in st["workers"], tail
    # Other text after IDLE is not this state: the runner went on to a turn.
    st = sdk_done([runner.IDLE, "› fix the build", "›"])
    assert sends(include_clear=True) == [] and calls("orca", ["terminal", "close"]) == []


@scenario
def done_sdk_runner_on_an_unavailable_screen_is_left():
    """A screen Orca could not read gives its accumulated stream, whose IDLE row may be
    history. It is no proof of an idle runner, so nothing is sent or closed until a later
    tick reads the screen."""
    idle = ["⏺ done", runner.IDLE, "›"]
    st = sdk_done(idle, source="screen-unavailable")
    assert sends(include_clear=True) == [] and calls("orca", ["terminal", "close"]) == []
    assert "ended" not in st["workers"]["se"] and "se" in st["workers"]
    sdk_done(idle, on_eof={"tail": [*idle, SDK_SHELL]}, st=st, now=NOW + 60)
    assert [x[1] for x in sends(include_clear=True)] == ["\x15", "\x04"], sends(True)
    assert tab_closes() == [("term-se", True)] and "se" not in st["workers"]


@scenario
def done_sdk_runner_that_will_not_exit_is_one_stuck_and_keeps_its_terminal():
    """End of input that does not end the runner within SDK_EXIT seconds is one STUCK
    naming the spend hold a close would leave, and the terminal stays. The tick waits
    once, under SDK_EXIT, and a later tick sends nothing again: it closes the tab once
    the runner is gone."""
    idle = ["⏺ done", runner.IDLE, "›"]
    with recheck_wait() as clock:
        st = sdk_done(idle)
        assert 0 < sum(clock.slept) <= da.SDK_EXIT, clock.slept
        typed = [x[1] for x in sends(include_clear=True) if x[0] == "term-se"]
        assert typed == ["\x15", "\x04"], typed
        stuck = (
            f"STUCK se sdk:se: its runner did not exit {da.SDK_EXIT}s after end of input, "
            "and closing its terminal would leave its spend reservation held open. Terminal kept"
        )
        assert [x for x in sends() if x[0] == "term-orch"] == [("term-orch", stuck, True)]
        assert notes() == [f"dev-auto: {stuck}"], notes()
        assert calls("orca", ["terminal", "close"]) == [] and "se" in st["workers"]
        # The next tick has a fresh state, which has not waited.
        st.waited, clock.slept = False, []
        sdk_done(idle, st=st, now=NOW + 60)
        assert sends(include_clear=True) == [] and calls("orca", ["terminal", "close"]) == []
        assert clock.slept == [], "a runner past its deadline is not waited on again"
    sdk_done([*idle, SDK_SHELL], st=st, now=NOW + 120)
    assert sends(include_clear=True) == [] and tab_closes() == [("term-se", True)]
    assert "se" not in st["workers"]


@scenario
def done_sdk_runner_waits_once_per_tick():
    """The state lock is held for one wait at most: a tick that already waited sends end
    of input without waiting, and the next tick closes the runner once it has exited."""
    idle = ["⏺ done", runner.IDLE, "›"]
    reset_db()
    ticket_row("se", status="complete", agent="sdk:se")
    st = fresh_state(se=rec("se", "sdk1", agent="sdk", agent_id="sdk:se", session=None))
    st.waited = True
    with recheck_wait() as clock:
        sdk_done(idle, st=st)
    assert clock.slept == [] and [x[1] for x in sends(include_clear=True)] == ["\x15", "\x04"]
    assert calls("orca", ["terminal", "close"]) == [] and notes() == [], notes()
    st.waited = False
    sdk_done([*idle, SDK_SHELL], st=st, now=NOW + 60)
    assert tab_closes() == [("term-se", True)] and "se" not in st["workers"]


def closes():
    """Each terminal close as (handle, host)."""
    return [
        (opt(r, "--terminal"), opt(r, "--environment") if "--environment" in r["args"] else "local")
        for r in calls("orca", ["terminal", "close"])
    ]


def tab_closes():
    """Each terminal close as (handle, whether it closed the whole tab)."""
    return [(opt(r, "--terminal"), "--tab" in r["args"]) for r in calls("orca", ["terminal", "close"])]


def resumable(host="local"):
    """Handles whose resume record Orca keeps after their close."""
    return fixture().get("resume", {}).get(host, [])


def on(host, *items):
    return [(host, row["handle"], row, screen) for row, screen in items]


@scenario
def finished_pair_closes_both_terminals_on_every_host():
    """Live: runs-pause-served-values-01 and its review ticket on Juzzark-WSL were both
    complete with terminals open. A complete coder ticket and its complete review ticket
    in one worktree each close their own terminal, here and on a remote host, neither
    record stays, and the merged worktree goes with the last of them."""
    for host in ("local", "Juzzark-WSL"):
        reset_db()
        ticket_row("fp", status="complete")
        ticket_row("fp-review", status="complete", agent="codex:fp-review")
        wt = str(tmp / "wt" / "fp")
        review = rec("fp-review", "x1", agent="codex", host=host, worktree=wt, owns_worktree=False)
        st = fresh_state(fp=rec("fp", "c3", host=host), **{"fp-review": review})
        coder = term("term-fp", 100, worktree=wt)
        reviewer = term("term-fp-review", 100, ident="codex", worktree=wt)
        world(on(host, coder, reviewer), git=git, gh=merged)
        tick(st)
        # A remote worktree's checks run in probe terminals, which close as they finish.
        workers = [c for c in closes() if not c[0].startswith(f"term-{host}-")]
        assert workers == [("term-fp", host), ("term-fp-review", host)], (host, closes())
        assert removed_with()[1] == [f"path:{wt}"], (host, removed_with())
        assert st["workers"] == {} and sends() == [], (host, st["workers"], sends())


@scenario
def remote_refused_close_is_one_stuck_then_retried():
    """On a remote host a refused close of a complete ticket's terminal is one STUCK; the
    close is tried again each tick with no second STUCK, and goes once Orca takes it."""
    reset_db()
    ticket_row("rc", status="complete")
    st = fresh_state(rc=rec("rc", "c3", host="Juzzark-WSL", owns_worktree=False))
    mine = term("term-rc", 100)
    stuck = []
    for _ in range(2):
        world(on("Juzzark-WSL", mine), close_fails=["term-rc"])
        tick(st)
        assert closes() == [("term-rc", "Juzzark-WSL")], closes()
        assert "rc" in st["workers"] and not st["workers"]["rc"].get("closed")
        stuck.append([s[1] for s in sends()])
    failed = "STUCK rc: ticket is complete but closing its terminal term-rc failed. Tried again each tick"
    assert stuck == [[failed], []], stuck
    world(on("Juzzark-WSL", mine))
    tick(st)
    assert closes() == [("term-rc", "Juzzark-WSL")] and "rc" not in st["workers"]


@scenario
def finished_worker_leaves_no_tab_or_resume_record():
    """The last recorded terminal of a kept worktree goes with its tab: no terminal, tab
    or resume record of it stays for a restart to bring back."""
    for host in ("local", "Juzzark-WSL"):
        reset_db()
        ticket_row("ft", status="complete")
        st = fresh_state(ft=rec("ft", "c3", host=host, owns_worktree=False))
        world(on(host, term("term-ft", 100)))
        tick(st)
        assert tab_closes() == [("term-ft", True)], (host, tab_closes())
        wt = str(tmp / "wt" / "ft")
        left = [r for r in fixture()["terminals"].get(host, []) if r["worktreePath"] == wt]
        assert left == [] and resumable(host) == [] and st["workers"] == {}, (host, left)


@scenario
def shared_tab_closes_only_the_pane():
    """A finished coder whose tab also holds its live reviewer's pane closes only its own
    pane and says so: the reviewer and its tab stay. Unlisted tabs are never closed whole."""
    reset_db()
    ticket_row("fs", status="complete")
    ticket_row("fs-review", agent="codex:fs-review")
    wt = str(tmp / "wt" / "fs")
    review = rec("fs-review", "x1", agent="codex", worktree=wt, owns_worktree=False)
    st = fresh_state(fs=rec("fs", "c1"), **{"fs-review": review})
    coder = term("term-fs", 100, tab="tab-shared")
    reviewer = term("term-fs-review", 100, ident="codex", worktree=wt, tab="tab-shared")
    world(local(coder, reviewer))
    text = tick_text(st)
    assert tab_closes() == [("term-fs", False)], tab_closes()
    assert "term-fs: its tab also holds term-fs-review; closed the pane only" in text, text
    assert [r["handle"] for r in fixture()["terminals"]["local"]] == ["term-fs-review", "term-orch"]
    set_fixture(terminals={"local": "down"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        da.close_terminal("local", "term-x")
    assert tab_closes() == [("term-x", False)], tab_closes()
    assert "term-x: its tab could not be listed; closed the pane only" in buf.getvalue()


@scenario
def refused_tab_close_is_reported_as_today():
    """A tab close Orca refuses is one STUCK with today's text, and tried again each tick."""
    reset_db()
    ticket_row("rt", status="complete")
    st = fresh_state(rt=rec("rt", "c3", host="Juzzark-WSL", owns_worktree=False))
    mine = term("term-rt", 100)
    world(on("Juzzark-WSL", mine), close_fails=["term-rt"])
    tick(st)
    assert tab_closes() == [("term-rt", True)], tab_closes()
    failed = "STUCK rt: ticket is complete but closing its terminal term-rt failed. Tried again each tick"
    assert [s[1] for s in sends()] == [failed] and not st["workers"]["rt"].get("closed")


@scenario
def done_terminal_named_by_its_handle_is_closed():
    """A complete ticket's terminal is closed when its own handle names it, even when Orca
    now shows another pty under that handle (seen after an Orca restart); with neither
    its handle nor its launch pty listed nothing is closed."""
    reset_db()
    ticket_row("rn", status="complete")
    st = fresh_state(rn=rec("rn", "c3", host="Juzzark-WSL", pty="pty-of-another-terminal"))
    world(on("Juzzark-WSL", term("term-rn", 100)), git=git, gh=merged)
    tick(st)
    assert ("term-rn", "Juzzark-WSL") in closes(), closes()
    assert not any("not provably" in s[1] for s in sends()), sends()


def context_transcript(home, folder, session, tokens):
    """A Claude transcript whose last assistant entry used `tokens` of context."""
    path = home / "projects" / folder / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {"input_tokens": 1000, "cache_read_input_tokens": tokens - 1500, "cache_creation_input_tokens": 500, "output_tokens": 50}
    entry = {"type": "assistant", "timestamp": "2026-11-17T06:00:00Z", "message": {"usage": usage, "content": []}}
    path.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(entry) + "\n")
    return path


def clear_transcripts():
    for home in homes.values():
        shutil.rmtree(home / "projects", ignore_errors=True)


@scenario
def worker_at_the_context_limit_restarts_from_its_handoff():
    """A Claude coder whose transcript shows 300k context or more and whose Handoff: update
    of this launch is on its ticket is closed with its tab once quiet and started fresh on
    the same ticket, worktree, model and account, reading that handoff, never resumed;
    one RESTARTED event names the context and the handoff."""
    reset_db()
    clear_transcripts()
    ticket_row("cr", notes="Tier: hard")
    hid = update("cr", "progress", "Handoff: done X, next Y", at=NOW - 300)
    st = fresh_state(cr=rec("cr", "c2"))
    context_transcript(homes["c2"], "-wt-cr", "sess-cr", 310_000)
    world(local(term("term-cr", 120)), git=HR_RELAY_GIT)
    tick(st)
    assert tab_closes()[:1] == [("term-cr", True)], tab_closes()
    (made,) = created()
    command = opt(made, "--command")
    assert "--resume" not in command and f"handoff #{hid}" in command, command
    assert_ci_only_prompt(command)
    new = st["workers"]["cr"]
    assert (new["account"], new["model"], new["worktree"]) == ("c2", OPUS, str(tmp / "wt" / "cr")), new
    assert "context" not in new and new["session"] != "sess-cr", new
    restarted = [l for l in event_lines() if "RESTARTED cr" in l]
    assert len(restarted) == 1 and restarted[0].endswith(f"RESTARTED cr coder: context 310k, handoff #{hid}"), restarted
    clear_transcripts()


@scenario
def context_restart_waits_for_a_refused_close():
    """da-2-2: Orca refuses to close the quiet session at its context handoff. Nothing
    starts: the record keeps its terminal, pty and session, and the next tick closes it
    and restarts."""
    reset_db()
    clear_transcripts()
    ticket_row("cf", notes="Tier: hard")
    update("cf", "progress", "Handoff: done X, next Y", at=NOW - 300)
    st = fresh_state(cf=rec("cf", "c2"))
    context_transcript(homes["c2"], "-wt-cf", "sess-cf", 310_000)
    world(local(term("term-cf", 120)), git=git, close_fails=["term-cf"])
    tick(st)
    kept = st["workers"]["cf"]
    assert created() == [], created()
    assert (kept["terminal"], kept["pty"], kept["session"]) == ("term-cf", "pty-term-cf", "sess-cf"), kept
    assert not [l for l in event_lines() if "RESTARTED cf" in l], event_lines()
    world(local(term("term-cf", 120)), git=git)
    tick(st, NOW + 60)
    assert tab_closes()[:1] == [("term-cf", True)] and len(created()) == 1, (tab_closes(), created())
    clear_transcripts()


@scenario
def context_handoff_with_its_pane_gone_restarts():
    """da-2-4: a Claude worker at the context limit whose handoff is on its ticket and
    whose pane is already gone is restarted from that handoff, never marked lost: alone,
    beside another missing worker (no Orca-restart sweep takes it), and with no free slot,
    where it waits unlost until one frees."""
    for other in (False, True):
        reset_db()
        clear_transcripts()
        (root / "supervisor" / "events.md").unlink(missing_ok=True)
        ticket_row("cg", notes="Tier: hard")
        hid = update("cg", "progress", "Handoff: next", at=NOW - 300)
        workers = {"cg": rec("cg", "c2")}
        if other:
            ticket_row("co")
            workers["co"] = rec("co", "c1")
        st = fresh_state(**workers)
        context_transcript(homes["c2"], "-wt-cg", "sess-cg", 310_000)
        world(local(), git=git)
        tick(st)
        assert "lost" not in st["workers"]["cg"], (other, st["workers"]["cg"])
        assert len(created()) == 1, (other, created())
        restarted = [l for l in event_lines() if "RESTARTED cg" in l]
        assert len(restarted) == 1 and restarted[0].endswith(f"handoff #{hid}"), restarted
        if other:
            assert "lost" in st["workers"]["co"], st["workers"]["co"]
    reset_db()
    clear_transcripts()
    ticket_row("cn", notes="Tier: hard")
    update("cn", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(cn=rec("cn", "c2"))
    st["limits"] = {account: NOW + 9999 for account in ("c1", "c2", "c3")}
    context_transcript(homes["c2"], "-wt-cn", "sess-cn", 310_000)
    world(local(), git=git)
    tick(st)
    assert created() == [] and "lost" not in st["workers"]["cn"], st["workers"]["cn"]
    assert any("NO CAPACITY cn" in l for l in event_lines()), event_lines()
    st["limits"] = {}
    world(local(), git=git)
    tick(st, NOW + 60)
    assert len(created()) == 1 and "lost" not in st["workers"]["cn"], (created(), st["workers"]["cn"])
    clear_transcripts()


@scenario
def context_handoff_beside_an_unknown_claude_pane_is_not_restarted():
    """A context handoff whose own pane is gone by handle and pty, while a live Claude pane
    no record holds runs in its worktree (Orca may have renamed both), is one STUCK and no
    second session, tick after tick."""
    reset_db()
    clear_transcripts()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    ticket_row("cu", notes="Tier: hard")
    update("cu", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(cu=rec("cu", "c2"))
    context_transcript(homes["c2"], "-wt-cu", "sess-cu", 310_000)
    renamed = term("term-x9", 600, ["❯ "], worktree=str(tmp / "wt" / "cu"), pty="pty-x9")
    world(local(renamed), git=git)
    tick(st)
    tick(st, NOW + 60)
    assert created() == [], created()
    assert "lost" not in st["workers"]["cu"], st["workers"]["cu"]
    stuck = [s for s in sends() if s[1].startswith("STUCK cu")]
    assert len(stuck) == 1 and "term-x9" in stuck[0][1], sends()
    clear_transcripts()


@scenario
def renamed_pane_with_no_identity_gets_no_second_worker():
    """A live Claude pane under a new handle and pty whose Orca identity expired is no proof
    its worker is gone: the context restart, the limit_handoff stop, a proposal's launch
    and `resume` over a stale handle all leave it and start nothing. A bare shell there
    a minute after launch is proof, and the context restart goes on."""
    claude_screen = ["✻ Working… (3s · esc to interrupt)"]

    def renamed(tail):
        return term("term-x8", 600, tail, ident=None, worktree=str(tmp / "wt" / "cv"), pty="pty-x8")

    reset_db()
    clear_transcripts()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    ticket_row("cv", notes="Tier: hard")
    update("cv", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(cv=rec("cv", "c2"))
    context_transcript(homes["c2"], "-wt-cv", "sess-cv", 310_000)
    world(local(renamed(claude_screen)), git=git)
    tick(st)
    assert created() == [] and "lost" not in st["workers"]["cv"], (created(), st["workers"]["cv"])
    assert any(s[1].startswith("STUCK cv") and "term-x8" in s[1] for s in sends()), sends()
    world(local(renamed(["% "])), git=git)
    tick(st, NOW + 60)
    assert len(created()) == 1, created()

    reset_db()
    clear_usage()
    ticket_row("cv", notes="Tier: hard")
    update("cv", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(cv=rec("cv", "c1"))
    world(local(renamed(claude_screen)))
    tick_on(st)
    assert "stopped" not in st["workers"]["cv"] and not st.get("proposals"), st["workers"]["cv"]
    assert "lost" not in st["workers"]["cv"], st["workers"]["cv"]
    # Once that pane exits, the handoff is stopped with one proposal.
    world(local((renamed(claude_screen)[0], {"status": "exited", "tail": []})))
    tick_on(st, NOW + 60)
    assert "stopped" in st["workers"]["cv"] and list(st.get("proposals", {})) == ["cv"], st["workers"]["cv"]

    reset_db()
    ticket_row("cv")
    state_file.write_text(json.dumps({"workers": {"cv": rec("cv", "c2")}}))
    world(local(renamed(claude_screen)))
    done = cli("resume", "cv")
    assert done.returncode != 0 and "term-x8" in done.stderr and not created(), done
    state_file.write_text(json.dumps({"workers": {"cv": rec("cv", "c2", stopped={"why": "x", "at": NOW, "handoff": None})}}))
    world(local(renamed(claude_screen)))
    try:
        launch_on("cv", "claude", OPUS)
    except SystemExit as error:
        assert "term-x8" in str(error.code), error.code
    else:
        raise AssertionError("the launch ran beside a live renamed pane")
    assert created() == [], created()
    state_file.unlink()
    clear_transcripts()


@scenario
def restarted_session_drops_the_old_wake_state():
    """da-2-7: a wake still waiting for its recheck when the worker is restarted at its
    context handoff belongs to the old pane: the fresh session's record has no unshown,
    wake or missed entry, so nothing is rechecked or retyped into the new pane."""
    reset_db()
    clear_transcripts()
    ticket_row("dw", notes="Tier: hard")
    update("dw", "progress", "Handoff: next", at=NOW - 300)
    pending = {"key": "dw|wake|1", "text": "dev-auto: a wake", "at": time.time(), "output": 0, "update": 0}
    st = fresh_state(dw=rec("dw", "c2", unshown=[pending], wake={"key": "dw|wake|1"}))
    # This tick already spent its one recheck wait.
    st.waited = True
    context_transcript(homes["c2"], "-wt-dw", "sess-dw", 310_000)
    world(local(term("term-dw", 120)), git=git)
    tick(st)
    new = st["workers"]["dw"]
    assert len(created()) == 1 and new["terminal"] != "term-dw", (created(), new)
    assert not {"unshown", "wake", "missed"} & set(new), new
    clear_transcripts()

@scenario
def worker_at_the_context_limit_without_a_handoff_is_asked_once():
    """A settled worker is asked at the first observation of the context limit."""
    reset_db()
    clear_transcripts()
    ticket_row("cn", notes="Tier: hard")
    st = fresh_state(cn=rec("cn", "c1"))
    context_transcript(homes["c1"], "-wt-cn", "sess-cn", 300_000)
    asks = []
    for when in (NOW, NOW + 15 * 60, NOW + 16 * 60):
        world(local(term("term-cn", 5, ["❯"])), git=git)
        tick(st, when)
        asks.append([s[1] for s in sends(events=False) if s[0] == "term-cn" and "context is at 300k" in s[1]])
        # Resuming under a new launch time keeps the same session's ask.
        st["workers"]["cn"]["launched_at"] = when
    assert [len(a) for a in asks] == [1, 0, 0], asks
    assert asks[0][0].startswith("/goal Write the handoff of Concord ticket cn"), asks
    assert created() == [] and tab_closes() == []
    clear_transcripts()


@scenario
def context_handoff_waits_for_the_turn_and_defers_stale_sessions():
    reset_db()
    clear_transcripts()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    ticket_row("busy")
    ticket_row("age-review")
    busy = rec("busy", "c1")
    stale = rec("age-review", "c2", claude_input=None)
    st = fresh_state(busy=busy, **{"age-review": stale})
    context_transcript(homes["c1"], "-wt-busy", busy["session"], 300_000)
    context_transcript(homes["c2"], "-wt-age-review", stale["session"], 300_000)
    old = term("term-age-review", 1)
    world(local(old, term("term-busy", 1, ["❯", "✻ Working… (3s · esc to interrupt)"])))
    tick(st)
    assert not any(s[0] in ("term-age-review", "term-busy") for s in sends(include_clear=True))
    event = [line for line in event_lines() if "FRESH SESSION age-review" in line]
    assert len(event) == 1 and "context is at 300k" in event[0] and stale["session"] in event[0], event
    world(local(old, term("term-busy", 1)))
    tick(st, NOW + 20)
    worker_sends = [s for s in sends() if s[0] in ("term-age-review", "term-busy")]
    assert len(worker_sends) == 1 and worker_sends[0][0] == "term-busy", worker_sends
    assert "/goal Write the handoff of Concord ticket busy" in worker_sends[0][1]
    assert [line for line in event_lines() if "FRESH SESSION age-review" in line] == event
    assert created() == [] and tab_closes() == []
    clear_transcripts()


@scenario
def context_handoff_survives_a_lower_reading_after_a_busy_turn():
    reset_db()
    clear_transcripts()
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    ticket_row("low")
    ticket_row("low-review")
    coder = rec("low", "c1")
    reviewer = rec("low-review", "c2")
    st = fresh_state(low=coder, **{"low-review": reviewer})
    busy = ["❯", "✻ Working… (3s · esc to interrupt)"]
    with recheck_wait() as clock:
        coder["claude_input"] = [coder["session"], clock.time()]
        reviewer["claude_input"] = [reviewer["session"], clock.time() - 3599]
        context_transcript(homes["c1"], "-wt-low", coder["session"], 300_000)
        context_transcript(homes["c2"], "-wt-low-review", reviewer["session"], 300_000)
        world(local(term("term-low", 1, busy), term("term-low-review", 1, busy)))
        tick(st)
        assert not any(s[0] in ("term-low", "term-low-review") for s in sends(include_clear=True))
        st.save()
        st = da.State(json.loads(st.path.read_text()), st.path)
        clock.now += 2
        context_transcript(homes["c1"], "-wt-low", coder["session"], 299_000)
        context_transcript(homes["c2"], "-wt-low-review", reviewer["session"], 299_000)
        for when in (NOW + 2, NOW + 4):
            world(local(term("term-low", 1), term("term-low-review", 1)))
            tick(st, when)
            worker_sends = [s for s in sends() if s[0] in ("term-low", "term-low-review")]
            assert [s[0] for s in worker_sends] == (["term-low"] if when == NOW + 2 else []), worker_sends
            if worker_sends:
                assert "context reached 300k" in worker_sends[0][1], worker_sends
            events = [line for line in event_lines() if "FRESH SESSION low-review" in line]
            assert len(events) == 1 and reviewer["session"] in events[0], events
            assert not created() and not tab_closes()
    clear_transcripts()


@scenario
def orchestrator_at_its_context_limit_is_told_relay_once():
    """The orchestrator's registered session's transcript at 400k or more: RELAY typed
    into it once and written to events.md; it is never closed."""
    reset_db()
    clear_transcripts()
    st = fresh_state()
    st["orchestrator"].update(account="c1", session="orch-session")
    place = str(tmp / "repo")
    context_transcript(homes["c1"], re.sub(r"[^A-Za-z0-9]", "-", place), "orch-session", 405_000)
    typed = []
    for when in (NOW, NOW + 60):
        world([], git=git, orch_worktree=place)
        tick(st, when)
        typed.append([s[1] for s in sends(events=False) if s[1].startswith("RELAY: context")])
    assert [len(t) for t in typed] == [1, 0] and typed[0][0].startswith("RELAY: context 405k."), typed
    assert any("RELAY: context 405k." in l for l in event_lines()), event_lines()
    assert tab_closes() == [] and st["orchestrator"]["terminal"] == "term-orch"
    clear_transcripts()


@scenario
def orchestrator_context_is_read_from_its_own_session_only():
    """da-logs-3: a successor sharing its predecessor's project folder, whose old transcript
    is newer and at 630k, reads its own registered session's 175k and gets no RELAY; with
    no registered session nothing is read and no RELAY is typed. set-orchestrator records
    the session its Claude Code runs."""
    reset_db()
    clear_transcripts()
    place = str(tmp / "repo")
    folder = re.sub(r"[^A-Za-z0-9]", "-", place)
    context_transcript(homes["c1"], folder, "succ-session", 175_000)
    old = context_transcript(homes["c1"], folder, "pred-session", 630_000)
    os.utime(old, (time.time() + 60, time.time() + 60))
    for session, context in (("succ-session", 175_000), (None, None)):
        st = fresh_state()
        st["orchestrator"].update(account="c1", session=session)
        world([], git=git, orch_worktree=place)
        tick(st)
        relays = [s for s in sends(events=False) if s[1].startswith("RELAY")]
        assert relays == [] and st["orchestrator"].get("context") == context, (session, relays, st["orchestrator"])
    clear_transcripts()
    path = root / "supervisor" / "state.json"
    saved = path.read_bytes() if path.exists() else None
    try:
        done = cli("set-orchestrator", "term-succ", "--account", "c1", CLAUDE_CODE_SESSION_ID="succ-session")
        assert done.returncode == 0, done.stderr
        assert json.loads(path.read_text())["orchestrator"]["session"] == "succ-session", path.read_text()
    finally:
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(saved)


@scenario
def status_md_and_the_roadmap_render_one_status_file():
    """Each tick writes status.json and status.md from it: one row per record with role,
    agent, model, effort, account, host, state, minutes, context and phase, plus the
    orchestrator's row. The roadmap's agent rows read the same file and agree with
    status.md field by field."""
    reset_db()
    clear_transcripts()
    stamp_at = datetime.fromtimestamp(NOW - 600, UTC).isoformat()
    ticket_row("sa", notes="Coder: claude · Reviewer: codex · Tier: hard")
    ticket_row("sa-review", agent="codex:sa-review")
    update("sa", "blocker", "Waiting on: review 1a2b3c4d", at=NOW - 600)
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET updated_at = ?", (stamp_at,))
    con.commit()
    con.close()
    context_transcript(homes["c1"], "-wt-sa", "sess-sa", 123_456)
    st = fresh_state(
        sa=rec("sa", "c1"),
        **{"sa-review": rec("sa-review", "x1", agent="codex", model="gpt-6.1-sol", worktree=str(tmp / "wt" / "sa"))},
    )
    st["orchestrator"]["account"] = "c2"
    world(local(term("term-sa", 5, ["❯"]), term("term-sa-review", 5, ["❯"], ident="codex", worktree=str(tmp / "wt" / "sa"))), git=git)
    tick(st)
    folder = root / "supervisor"
    data = json.loads((folder / "status.json").read_text())
    table = [l for l in (folder / "status.md").read_text().splitlines() if l.startswith("| ") and not l.startswith("| ticket")]
    head = da.STATUS_COLUMNS
    shown = [dict(zip(head, [c.strip() for c in l.strip("|").split("|")])) for l in table]
    by = {r["ticket"]: r for r in shown}
    assert set(by) == {"sa", "sa-review", "-"}, shown
    assert by["-"]["role"] == "orchestrator" and by["-"]["account"] == "pro2", by["-"]
    assert by["sa"]["context"] == "123k" and by["sa-review"]["context"] == "-", by
    assert (by["sa"]["agent"], by["sa"]["model"], by["sa"]["account"]) == ("claude", OPUS, "pro1"), by["sa"]
    rm = da.ROADMAP
    now = datetime.fromtimestamp(NOW, UTC)
    tickets = rm.classify(rm.read_ledger(), now)
    workers = rm.read_workers()
    for t in tickets:
        t["worker"] = workers.get(t["task_id"])
    page = {a["key"]: (a, g) for g in rm.agents(tickets, now)["worktrees"] for a in g["agents"]}
    assert set(page) == {"sa", "sa-review"}, page
    for tid, (a, g) in page.items():
        row = by[tid]
        got = {
            "role": a["role"],
            "agent": a["agent"],
            "model": a["model"],
            "effort": a["effort"],
            "account": a["account"],
            "state": a["state"],
            "context": "-" if a["context"] is None else f"{a['context'] // 1000}k",
            "phase": g["phase"],
        }
        assert got == {k: row[k] for k in got}, (tid, got, row)
    assert {r["ticket"] for r in data["rows"]} == {"sa", "sa-review", None}, data
    clear_transcripts()


@scenario
def status_and_roadmap_keep_exited_workers_without_counting_their_slots():
    reset_db()
    for tid in ("live", "held", "left"):
        ticket_row(tid)
    now = datetime.fromtimestamp(NOW, UTC)
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE tasks SET updated_at = ?", (now.isoformat(),))
    con.close()
    st = fresh_state(
        live=rec("live", "c1", status={"state": "working"}),
        held=rec("held", "c1", held=True, status={"state": "stuck", "detail": "relaunch held"}),
        left=rec("left", "c1", exited_at=NOW - 60, status={"state": "exited"}),
    )
    da.write_status(cfg, root, st, da.read_tickets(root), NOW)
    rm = da.ROADMAP
    workers = rm.read_workers()
    tickets = rm.classify(rm.read_ledger(), now)
    for ticket in tickets:
        ticket["worker"] = workers[ticket["task_id"]]
    listing = rm.running_text(rm.running(tickets, now))
    assert listing[0] == "RUNNING NOW · 1 worker: local 1 (1 coder, 0 reviewers)", listing
    assert any("STUCK" in line and "held" in line for line in listing[1:]), listing
    assert any("EXITED" in line and "left" in line for line in listing[1:]), listing
    assert {tid: row["live_slot"] for tid, row in workers.items()} == {
        "live": True, "held": False, "left": False,
    }, workers
    agents = rm.agents(tickets, now, emails={"c1": "test@example.test"})
    assert agents["counts"] == {"working": 1, "stuck": 1, "exited": 1}, agents


@scenario
def status_survives_a_wait_on_a_ticket_with_no_id():
    """A coder's `Waiting on: ticket:` names no ticket, so it is no phase of its own: the
    tick still writes status.json, the row in its ordinary phase."""
    reset_db()
    clear_transcripts()
    ticket_row("se", notes="Coder: claude · Reviewer: codex · Tier: hard")
    update("se", "blocker", "Waiting on: ticket:")
    st = fresh_state(se=rec("se", "c1"))
    status_file = root / "supervisor" / "status.json"
    status_file.unlink(missing_ok=True)
    world(local(term("term-se", 5, ["❯"])), git=git)
    tick(st)
    rows = {r["ticket"]: r for r in json.loads(status_file.read_text())["rows"]}
    assert rows["se"]["phase"] == "coding", rows["se"]


FILL_TAIL = '[fill]\nenabled = true\nrepo = "/repo"\n'


def ready_row(tid, notes):
    """A ready ticket: proposed and unowned."""
    ticket_row(tid, status="proposed", notes=notes)
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET agent_id = NULL WHERE task_id = ?", (tid,))
    con.commit()
    con.close()


@contextmanager
def fake_launch():
    """cmd_launch recorded as (ticket, agent, model, effort, worktree, host), each one
    leaving a live record whose terminal the next world() lists."""
    launched, real = [], da.cmd_launch

    def launch(args, locked=None, cfg=None):
        launched.append((args.ticket, args.agent, args.model, args.effort, args.worktree, args.host))
        locked["workers"][args.ticket] = rec(args.ticket, "c3", agent=args.agent, model=args.model, host=args.host)

    da.cmd_launch = launch
    try:
        yield launched
    finally:
        da.cmd_launch = real


def fill_tick(conf, st, *terms, **extra):
    world(on("Juzzark-WSL", *terms), git=git, gh=merged, **extra)
    da.tick(conf, root, st, da.read_tickets(root), NOW)


@scenario
def fill_launches_up_to_target_then_stops_at_memory_refusal():
    """With [fill] on, a host below its target_tickets gets one ready ticket per tick on
    a new worktree, its Tier line picking the model, until the target; a blocked ticket,
    an operator step and another host's ticket wait; a host short of memory gets
    none and no worktree."""
    reset_db()
    conf = da.load_config(memory_accounts(wsl="target_tickets = 2\n", tail=FILL_TAIL))
    for tid in ("fa", "fb", "fc"):
        ready_row(tid, f"Coder: claude · Reviewer: codex · Tier: hard · Host: Juzzark-WSL or the main Mac")
    ready_row("fd", "Coder: claude · Tier: hard · Host: Juzzark-WSL · Blocked by: fz")
    ticket_row("fz", status="active")
    ready_row("fe", "Coder: claude · Tier: hard · Host: Juzzark-WSL (operator step)")
    ready_row("ff", "Coder: claude · Tier: hard · Host: main Mac")
    st = fresh_state()
    live = [term(f"term-{t}", 5) for t in ("fa", "fb")]
    with fake_launch() as launched:
        for _ in range(3):
            fill_tick(conf, st, *live)
        assert [l[0] for l in launched] == ["fa", "fb"], launched
        assert launched[0] == ("fa", "claude", OPUS, "medium", "/wt/feat.fa", "Juzzark-WSL"), launched
        filled = [l for l in event_lines() if "FILLED" in l]
        assert len(filled) == 2 and "FILLED fa on Juzzark-WSL" in filled[0], filled
        conf["hosts"]["Juzzark-WSL"]["target_tickets"] = 3
        low = {"availableMemory": 2**30, "freeMemory": 2**30, "availableMemorySource": "proc-meminfo"}
        fill_tick(conf, st, *live, memory={"Juzzark-WSL": low})
        assert [l[0] for l in launched] == ["fa", "fb"], launched
        assert not [r for r in calls("orca") if "create" in r["args"]], "no worktree while short of memory"


@scenario
def fill_uses_the_coder_tuple_in_each_tier():
    conf = da.load_config(memory_accounts(wsl="target_tickets = 1\n", tail=FILL_TAIL))
    conf["fill"]["models"].update({
        "hard": ["codex", "gpt-6.2-astra", "high"],
        "ui": ["claude", OPUS, "high"],
        "codex": ["codex", "gpt-6.2-sol", "xhigh"],
    })
    cases = (
        ("ba", "medium", "codex", "gpt-6.2-sol", "medium"),
        ("bb", "hard", "claude", OPUS, "medium"),
        ("bc", "ui", "claude", SONNET, "medium"),
    )
    picks = []
    for tid, tier, kind, model, effort in cases:
        reset_db()
        ready_row(tid, f"Coder: {kind} ({model}, effort {effort}) · Tier: {tier}")
        with fake_launch() as launched:
            fill_tick(conf, fresh_state())
        picks.extend(row[:4] for row in launched)
    assert picks == [(tid, kind, model, effort) for tid, _, kind, model, effort in cases], picks


@scenario
def fill_matches_a_whole_host_name_and_accepts_no_host_line():
    reset_db()
    conf = da.load_config(memory_accounts(tail=FILL_TAIL))
    conf["hosts"]["Mac2"]["target_tickets"] = 1
    conf["hosts"]["Mac20"] = {**conf["hosts"]["Mac2"], "target_tickets": 1}
    account = next(row for row in conf["account"] if row["id"] == "xw")
    conf["account"] += [
        {**account, "id": "xmac2", "host": "Mac2"},
        {**account, "id": "xmac20", "host": "Mac20"},
    ]
    ready_row("ha", "Coder: codex · Tier: medium · Host: Mac20 or the main Mac")
    ready_row("hb", "Coder: codex · Tier: medium")
    st = fresh_state()
    with fake_launch() as launched:
        fill_tick(conf, st)
    assert [(row[0], row[-1]) for row in launched] == [("hb", "Mac2"), ("ha", "Mac20")], launched


@scenario
def fill_rechecks_ticket_eligibility_around_worktree_creation():
    brief = "Coder: codex · Tier: medium · Host: Juzzark-WSL"
    conf = da.load_config(memory_accounts(wsl="target_tickets = 1\n", tail=FILL_TAIL))
    make, fresh = da.fill_worktree, da.fresh_tools
    for when in ("before", "during", "startup"):
        for change in ("claimed", "assigned", "complete"):
            reset_db()
            ready_row("fh", brief)
            snapshot = da.read_tickets(root)
            worktree = tmp / "wt" / "fh"
            worktree.mkdir(parents=True, exist_ok=True)

            def replace():
                changes = {
                    "claimed": ("active", "claude:operator", None),
                    "assigned": ("proposed", None, "claude:operator"),
                    "complete": ("complete", None, None),
                }
                with sqlite3.connect(db) as store:
                    store.execute(
                        "UPDATE tasks SET status = ?, agent_id = ?, assigned_agent_id = ?",
                        changes[change],
                    )

            created_worktrees = []

            def create(conf, host, ticket):
                created_worktrees.append(ticket["task_id"])
                if when == "during":
                    replace()
                return str(worktree), None

            world(on("Juzzark-WSL"), git=git, gh=merged)
            if when == "before":
                replace()
            if when == "startup":
                da.fresh_tools = lambda *args: replace()
            da.fill_worktree = create
            st = fresh_state()
            try:
                da.fill(conf, st, snapshot, NOW)
            finally:
                da.fill_worktree, da.fresh_tools = make, fresh
            assert created_worktrees == ([] if when == "before" else ["fh"]), (when, change)
            assert not created(), (when, change, created())
            assert not ledger_calls("transfer_work"), (when, change)
            assert "fh" not in st["workers"], (when, change, st["workers"])
            if when != "before":
                kept = st["fill_worktrees"].get("fh")
                assert kept == ["Juzzark-WSL", str(worktree)], (when, change, kept)
                ready_row("fh", brief)
                world(on("Juzzark-WSL"), git=git, gh=merged)
                da.fill(conf, st, da.read_tickets(root), NOW + 60)
                (made,) = created()
                assert opt(made, "--worktree") == f"path:{worktree}", (when, change, made)
                assert not calls("orca", ["worktree", "create"]), (when, change)
                assert "fh" not in st["fill_worktrees"], (when, change)


@scenario
def fill_creates_the_worktree_on_its_host():
    """The worktree is created on the host with --environment before the subcommand,
    from feature/<feature> on task/<feature>/<lane>, or from the Branch line's base
    when the ticket has no feature branch."""
    reset_db()
    conf = da.load_config(memory_accounts(wsl="target_tickets = 2\n", tail=FILL_TAIL))
    ready_row("ga", "Coder: claude · Tier: hard · Host: Juzzark-WSL · Feature branch: none · Branch: `fix/ga` from `main`")
    st = fresh_state()
    with fake_launch() as launched:
        fill_tick(conf, st)
    (made,) = [r for r in calls("orca") if "create" in r["args"]]
    assert made["args"][:4] == ["--environment", "Juzzark-WSL", "worktree", "create"], made
    assert (opt(made, "--branch"), opt(made, "--base-branch"), opt(made, "--repo")) == ("fix/ga", "main", "/repo")
    assert launched and launched[0][4] == "/wt/feat.ga", launched


@scenario
def fill_skips_a_ticket_whose_owned_paths_overlap_a_live_one():
    """A ready ticket whose owned paths overlap a live ticket's waits; the next ready one
    that does not overlap starts."""
    reset_db()
    conf = da.load_config(memory_accounts(wsl="target_tickets = 3\n", tail=FILL_TAIL))
    ticket_row("oa", notes="Tier: hard · Owned paths: `src/a`")
    ready_row("ob", "Coder: claude · Tier: hard · Host: Juzzark-WSL · Owned paths: `src/a/b.py`")
    ready_row("oc", "Coder: claude · Tier: hard · Host: Juzzark-WSL · Owned paths: `src/c`")
    st = fresh_state(oa=rec("oa", "c3", host="Juzzark-WSL"))
    with fake_launch() as launched:
        fill_tick(conf, st, term("term-oa", 5))
    assert [l[0] for l in launched] == ["oc"], launched


@scenario
def fill_launches_a_reviewer_for_an_open_review_request():
    """With [fill] on, a coder's review request with no review ticket publishes one and
    launches its reviewer on the coder's host, on the first kind of the Reviewer line
    with a slot; a Claude reviewer runs Sonnet at medium."""
    reset_db()
    conf = da.load_config(memory_accounts(tail=FILL_TAIL))
    ticket_row("ra", notes="Coder: codex · Reviewer: claude or codex · Tier: medium")
    update("ra", "progress", "REVIEW READY 1a2b3c4", agent="codex:ra")
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET agent_id = 'codex:ra' WHERE task_id = 'ra'")
    con.commit()
    con.close()
    st = fresh_state(ra=rec("ra", "x1", agent="codex", model="m"))
    with fake_launch() as launched:
        world(local(term("term-ra", 5, ident="codex")), git=git, gh=merged)
        da.tick(conf, root, st, da.read_tickets(root), NOW)
        assert launched == [("ra-review", "claude", SONNET, "medium", st["workers"]["ra"]["worktree"], "local")], launched
        assert ("", "ra-review") in da.read_tickets(root)
        assert any("FILLED ra-review on local" in l for l in event_lines()), event_lines()


@scenario
def fill_uses_the_reviewer_tuple_and_records_the_selected_kind():
    cases = (
        ("medium", "claude", OPUS, "medium", " or codex"),
        ("hard", "codex", "gpt-6.2-sol", "xhigh", " or claude"),
    )
    conf = da.load_config(memory_accounts(tail=FILL_TAIL))
    conf["fill"]["models"]["codex"] = ["codex", "gpt-6.1-sol", "medium"]
    for tier, kind, model, effort, fallback in cases:
        reset_db()
        ticket_row("rc", agent="codex:rc", notes=(
            f"Coder: codex · Reviewer: {kind} ({model}, effort {effort}){fallback} · Tier: {tier}"
        ))
        update("rc", "progress", "REVIEW READY 1a2b3c4", agent="codex:rc")
        st = fresh_state(rc=rec("rc", "x1", agent="codex"))
        world(local(term("term-rc", 5, ident="codex")), git=git, gh=merged)
        with fake_launch() as launched:
            da.fill(conf, st, da.read_tickets(root), NOW)
        assert [row[:4] for row in launched] == [("rc-review", kind, model, effort)], launched
        (claim,) = ledger_calls("start_work")
        assert claim["kind"] == kind, claim


@scenario
def fill_retries_its_review_ticket_when_memory_recovers():
    reset_db()
    ticket_row("rr", agent="codex:rr", notes="Coder: codex · Reviewer: claude · Tier: medium")
    update("rr", "progress", "REVIEW READY 1a2b3c4", agent="codex:rr")
    wt = tmp / "wt" / "rr"
    wt.mkdir(parents=True, exist_ok=True)
    st = fresh_state(rr=rec("rr", "x1", agent="codex", worktree=str(wt)))
    conf = da.load_config(memory_accounts(tail=FILL_TAIL))
    real_memory = da.memory_refusal
    refusal = "local: memory under its floor; no worker starts there"

    def fill_once(readings):
        world(local(term("term-rr", 5, ident="codex")), git=git, gh=merged)
        da.memory_refusal = lambda conf, host: (next(readings, None), [])
        try:
            da.fill(conf, st, da.read_tickets(root), NOW)
        finally:
            da.memory_refusal = real_memory

    fill_once(iter([refusal]))
    assert ("", "rr-review") not in da.read_tickets(root)
    assert not created()
    fill_once(iter([None, refusal]))
    review = da.read_tickets(root)["", "rr-review"]
    assert review["status"] == "proposed" and review["agent_id"] is None, review
    assert not created()
    assert st["fill_failed"] == {}, st["fill_failed"]
    fill_once(iter([]))
    assert len(created()) == 1 and "rr-review" in st["workers"], created()
    assert not ledger_calls("start_work"), "reused the originally published ticket"


@contextmanager
def ledger_locked():
    """The store under another writer's exclusive lock; a read fails at once instead of
    after SQLite's 5 seconds, and the retries do not sleep."""
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    old = vars(da).get("LEDGER_TIMEOUT"), vars(da).get("LEDGER_WAIT")
    da.LEDGER_TIMEOUT, da.LEDGER_WAIT = 0, 0
    try:
        yield holder
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()
        da.LEDGER_TIMEOUT, da.LEDGER_WAIT = old


@scenario
def ledger_locked_tick_waits_and_touches_nothing():
    """A tick whose opening ledger read stays locked through its retries stops with one
    line naming the ledger and leaves every record as it was; a lock that lifts between
    retries reads both tables in one snapshot."""
    reset_db()
    ticket_row("lk")
    saved = {"workers": {"lk": rec("lk", "c1")}, "sent": {"lk|x": 1}}
    state_file.write_text(json.dumps(saved))
    world(local(term("term-lk", 5)))
    try:
        with ledger_locked():
            stopped = None
            try:
                da.cmd_tick(None)
            except SystemExit as stop:
                stopped = str(stop.code)
            assert stopped and "ledger" in stopped, stopped
        data = json.loads(state_file.read_text())
        assert data["workers"] == saved["workers"] and data["sent"] == saved["sent"], data
        assert not calls("orca"), calls("orca")
        with ledger_locked() as holder:
            real = da.time.sleep
            da.time.sleep = lambda seconds: holder.rollback()
            try:
                assert ("", "lk") in da.read_tickets(root)
            finally:
                da.time.sleep = real
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def fill_ledger_read_failure_is_tried_again_next_tick():
    """A ledger read that fails inside one auto-fill launch, its owner read in the real
    launch for a coder and for a review ticket already published, is reported and leaves
    that ticket to the next tick: nothing goes on the fill-failed list and the tick
    finishes its other work. The next tick launches into the worktree already made, and
    launches the published review ticket."""
    reset_db()
    conf = da.load_config(memory_accounts(wsl="target_tickets = 1\n", tail=FILL_TAIL))
    ready_row("la", "Coder: claude · Tier: hard · Host: Juzzark-WSL")
    ticket_row("lr", notes="Coder: codex · Reviewer: claude or codex · Tier: medium")
    update("lr", "progress", "REVIEW READY 1a2b3c4", agent="codex:lr")
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET agent_id = 'codex:lr' WHERE task_id = 'lr'")
    con.commit()
    con.close()
    st = fresh_state(lr=rec("lr", "x1", agent="codex", model="m"))

    real_owner, reads = da.ledger_owner, []

    def locked(at, project, task):
        # The review ticket's release reads it once; the launch's read after it is locked.
        reads.append(task)
        if task == "la" or reads.count(task) > 1:
            raise sqlite3.OperationalError("database is locked")
        return real_owner(at, project, task)

    da.ledger_owner = locked
    try:
        world(local(term("term-lr", 5, ident="codex")), git=git, gh=merged)
        buf = io.StringIO()
        with redirect_stdout(buf):
            da.tick(conf, root, st, da.read_tickets(root), NOW)
        told = buf.getvalue()
    finally:
        da.ledger_owner = real_owner
    assert reads == ["la", "lr-review", "lr-review"], reads
    assert st.get("fill_failed", {}) == {}, st["fill_failed"]
    assert "la:" in told and "lr-review:" in told and "database is locked" in told, told
    assert "tick" in st and "status" in st["workers"]["lr"], st["workers"]["lr"]
    assert owner_of("lr-review") is None, "published and released before the failed read"
    assert len([r for r in calls("orca") if "worktree" in r["args"] and "create" in r["args"]]) == 1
    with fake_launch() as launched:
        world(local(term("term-lr", 5, ident="codex")), git=git, gh=merged)
        da.tick(conf, root, st, da.read_tickets(root), NOW + 60)
    assert [(l[0], l[4]) for l in launched] == [("la", "/wt/feat.la"), ("lr-review", st["workers"]["lr"]["worktree"])], launched
    assert not [r for r in calls("orca") if "worktree" in r["args"] and "create" in r["args"]], "the kept worktree"
    assert owner_of("lr-review") is None, "released for its reviewer"


@scenario
def fill_kept_worktree_waits_for_its_host():
    """A ticket two hosts may run whose launch on the first failed on a ledger read keeps
    that host's worktree: the second host neither takes it nor makes another, and the
    next tick launches on the first host into the kept worktree."""
    reset_db()
    conf = da.load_config(memory_accounts(wsl="target_tickets = 1\n", tail=FILL_TAIL))
    conf["hosts"]["Mac2"]["target_tickets"] = 1
    ready_row("lh", "Coder: claude · Tier: hard · Host: Juzzark-WSL or Mac2")
    st = fresh_state()
    first = next(iter(da.fill_hosts(conf)))

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    real_owner, da.ledger_owner = da.ledger_owner, locked
    try:
        fill_tick(conf, st)
    finally:
        da.ledger_owner = real_owner
    (kept,) = st["fill_worktrees"].values()
    assert kept[0] == first and st.get("fill_failed", {}) == {}, (st["fill_worktrees"], st.get("fill_failed"))
    assert len([r for r in calls("orca") if "worktree" in r["args"] and "create" in r["args"]]) == 1, calls("orca")
    with fake_launch() as launched:
        fill_tick(conf, st)
    assert [(l[0], l[4], l[5]) for l in launched] == [("lh", kept[1], first)], launched
    assert not [r for r in calls("orca") if "worktree" in r["args"] and "create" in r["args"]], "the kept worktree"


@scenario
def reviewer_none_raises_one_orchestrator_review_event():
    """A coder ticket whose Reviewer line is none raises one REVIEW event per request for
    the orchestrator, and its review wait is never a no-review-ticket STUCK."""
    reset_db()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    ticket_row("rz", notes="Coder: claude · Reviewer: none · Tier: medium")
    update("rz", "blocker", "Waiting on: review 1a2b3c4d")
    st = fresh_state(rz=rec("rz", "c1"))
    for when in (NOW, NOW + 60):
        world(local(term("term-rz", 5, ["❯"])), git=git, gh=merged)
        tick(st, when)
    assert sum("REVIEW rz 1a2b3c4d: orchestrator review" in l for l in event_lines()) == 1, event_lines()
    assert not any("no review ticket" in l for l in event_lines()), event_lines()


@scenario
def review_done_once_its_coder_is_complete_with_a_push():
    """A review whose coder ticket is complete with a PUSH as its latest verdict is done:
    a running reviewer's terminal is closed and its ticket finished, and one that exited
    soon after launch is cleaned, never a fast-exit STUCK."""
    for screen, ident, launched in ((["❯"], "codex", NOW - 7200), (["% "], None, NOW - 60)):
        reset_db()
        ticket_row("rp", status="complete")
        update("rp", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:rp-review")
        ticket_row("rp-review", agent="codex:rp-review")
        reviewer = rec("rp-review", "x1", agent="codex", model="m", launched_at=launched)
        st = fresh_state(**{"rp-review": reviewer})
        world(local(term("term-rp-review", 100, screen, ident=ident)), git=git, gh=merged)
        tick(st)
        assert ("term-rp-review", "local") in closes(), (ident, closes())
        assert not any("STUCK" in s[1] for s in sends()), sends()
        assert da.read_tickets(root)[("", "rp-review")]["status"] == "complete"


@scenario
def b1_lost_handle_is_unknown():
    """No terminal with its pty, however many agents are in the worktree: STUCK once, and
    for that ticket nothing launched, cleaned or typed. A finished ticket beside it with
    no terminal has its merged worktree removed."""
    reset_db()
    ticket_row("hs")
    ticket_row("hd", status="complete")
    st = fresh_state(hs=rec("hs", "c1"), hd=rec("hd", "c2"))
    twins = [term(f"term-{n}", 31 * 60, worktree=str(tmp / "wt" / "hs")) for n in "ab"]
    world(local(*twins), git=git, gh=merged)
    tick(st)
    removed = [opt(r, "--worktree") for r in calls("orca", ["worktree", "rm"])]
    assert created() == [] and removed == [f"path:{tmp / 'wt' / 'hd'}"], removed
    assert calls("orca", ["terminal", "close"]) == []
    assert "hd" not in st["workers"], "a finished ticket's merged worktree goes with it"
    assert sorted(s[1] for s in sends()) == [
        "STUCK hs claude:hs: terminal term-hs is not listed and no listed terminal has its pty. Left alone until a new launch",
    ], sends()
    world(local(*twins), git=git, gh=merged)
    tick(st, NOW + 7200)
    assert sends() == [] and created() == [], "reported once"


def launch_line(tid, rec_):
    """The launch command a gone worker's STUCK hands the orchestrator, as typed."""
    tail = f" --effort {rec_['effort']}" if rec_.get("effort") else ""
    return (
        f"python3 {da.HERE / 'dev_auto.py'} launch {tid} --agent {rec_['agent']} "
        f"--model {rec_['model']}{tail} --worktree {rec_['worktree']} --host {rec_['host']}"
    )


@scenario
def lost_terminal_stuck_carries_its_launch_command():
    """Live (114 STUCKs, 68 tickets, 2026-11-12 to 11-14): a worker whose terminal is gone
    was relaunched, held for a later wave or dropped, a different step by day. With
    stuck_launch_command on, its one STUCK carries the filled launch command; nothing is
    launched, and later ticks raise nothing more. No effort is no --effort."""
    for tid, effort in (("lc", "high"), ("le", None)):
        reset_db()
        ticket_row(tid)
        lost = rec(tid, "c1", effort=effort)
        st = fresh_state(**{tid: lost})
        with settings(stuck_launch_command=True):
            world([])
            tick(st)
            text = (
                f"STUCK {tid} claude:{tid}: terminal term-{tid} is not listed and no listed "
                f"terminal has its pty. Left alone until a new launch: "
                f"{launch_line(tid, lost)}"
            )
            assert sends() == [("term-orch", text, True)], sends()
            assert created() == [] and calls("orca", ["terminal", "close"]) == []
            world([])
            tick(st, NOW + 600)
        assert sends() == [] and created() == [], "reported once"
    reset_db()
    ticket_row("lq")
    odd = rec("lq", "c1", worktree=str(tmp / "wt" / "l+q"))
    st = fresh_state(lq=odd)
    with settings(stuck_launch_command=True):
        world([])
        tick(st)
    text = (
        "STUCK lq claude:lq: terminal term-lq is not listed and no listed terminal has its "
        "pty. Left alone until a new launch. Its launch command is on the ticket"
    )
    assert sends() == [("term-orch", text, True)], sends()
    assert notes() == [f"dev-auto: {text}\n{launch_line('lq', odd)}"], notes()


@scenario
def exited_worker_stuck_carries_its_launch_command():
    """A worker whose terminal exited too often waits for a new launch: with
    stuck_launch_command on, its one STUCK carries the filled command, and the tick
    starts nothing. Off, as it starts, the STUCK is today's."""
    reset_db()
    ticket_row("fz")
    held = rec("fz", "c1", relaunches=3)
    st = fresh_state(fz=held)
    with settings(stuck_launch_command=True):
        world(local(term("term-fz", 600, status="exited")))
        tick(st)
        text = (
            "STUCK fz claude:fz: its terminal exited after 3 relaunches. It waits for a new "
            f"launch: {launch_line('fz', held)}"
        )
        assert created() == [] and sends() == [("term-orch", text, True)], sends()
        world(local(term("term-fz", 600, status="exited")))
        tick(st, NOW + 600)
    assert created() == [] and sends() == [], "reported once"
    assert cfg.get("stuck_launch_command") is False
    ticket_row("fo")
    st = fresh_state(fo=rec("fo", "c1", relaunches=3))
    world(local(term("term-fo", 600, status="exited")))
    tick(st)
    text = "STUCK fo claude:fo: its terminal exited after 3 relaunches. It waits for a new launch"
    assert sends() == [("term-orch", text, True)], sends()


def gone_reviewer(coder_status, verdicts, host="local", lost=None):
    """Coder ticket gv and review ticket gv-review, whose Codex reviewer's terminal is not
    listed; the reviewer posted these verdicts on gv, oldest first."""
    reset_db()
    ticket_row("gv", status=coder_status)
    ticket_row("gv-review", agent="codex:gv-review")
    for verdict in verdicts:
        update("gv", "finding", f"REVIEW VERDICT: {verdict}", agent="codex:gv-review")
    extra = {"lost": lost} if lost else {}
    worker = rec(
        "gv-review",
        "x1",
        agent="codex",
        host=host,
        worktree=str(tmp / "wt" / "gv"),
        owns_worktree=False,
        **extra,
    )
    return fresh_state(**{"gv-review": worker})


def status_of(tid):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT status FROM tasks WHERE task_id = ?", (tid,)).fetchone()[0]
    finally:
        con.close()


def finishes():
    """Each finish_work call as (its arguments, the identity it ran as)."""
    return [
        (r["call"]["arguments"], r["env"].get("CONCORD_AGENT_ID"))
        for r in calls("concord-mcp")
        if r["call"]["name"] == "finish_work"
    ]


def progress_lines():
    return [
        a["content"] for a in ledger_calls("update_work") if a.get("kind") == "progress"
    ]


@scenario
def gone_reviewer_after_push_closes_its_review_ticket():
    """Live: dependencies-refresh-01-review's reviewer recorded PUSH, its terminal ended,
    and dev-auto raised STUCK lost until the orchestrator closed the ticket by hand. A
    review ticket whose terminal is gone, whose coder ticket is complete and whose latest
    verdict there is PUSH is closed as its owner with one progress line, no STUCK, here
    and on a remote host, whether the terminal went this tick or before."""
    lost = "STUCK gv-review codex:gv-review: terminal term-gv-review is not listed. Left alone"
    for host, already in (("local", None), ("Juzzark-WSL", None), ("local", lost)):
        st = gone_reviewer("complete", ["FIX 0a0b0c0", "PUSH 1a2b3c4"], host, already)
        world([], git=git, gh=merged)
        tick(st)
        assert finishes() == [
            (
                {
                    "task_id": "gv-review",
                    "expected_version": 1,
                    "outcome": "complete",
                    "what_changed": (
                        "Closed by dev-auto: the reviewer's terminal is gone, gv is "
                        "complete and its latest verdict is REVIEW VERDICT: PUSH 1a2b3c4."
                    ),
                },
                "codex:gv-review",
            )
        ], (host, finishes())
        assert status_of("gv-review") == "complete"
        (line,) = progress_lines()
        assert line.startswith("dev-auto: closed gv-review as codex:gv-review: its terminal"), line
        assert "REVIEW VERDICT: PUSH 1a2b3c4" in line and "gv is complete" in line, line
        assert not any("STUCK" in s[1] for s in sends()), (host, sends())
        world([], git=git, gh=merged)
        tick(st)
        assert finishes() == [] and progress_lines() == [], (host, finishes())
        assert "gv-review" not in st["workers"], "the closed ticket's record goes"


@scenario
def gone_reviewer_left_alone_until_its_coder_is_done_and_pushed():
    """The same gone reviewer with its coder still open, with no verdict, or with a FIX as
    the latest verdict is today's lost STUCK, once, and its ticket is not closed."""
    for coder_status, verdicts in (
        ("active", ["PUSH 1a2b3c4"]),
        ("closed", ["PUSH 1a2b3c4"]),
        ("complete", []),
        ("complete", ["PUSH 1a2b3c4", "FIX 2b3c4d5"]),
    ):
        st = gone_reviewer(coder_status, verdicts)
        said = []
        for when in (NOW, NOW + 600):
            world([], git=git, gh=merged)
            tick(st, when)
            said.append([s[1] for s in sends() if not s[1].startswith("CLEARED")])
        lost = (
            "STUCK gv-review codex:gv-review: terminal term-gv-review is not listed and no "
            "listed terminal has its pty. Left alone until a new launch"
        )
        assert said == [[lost], []], (coder_status, verdicts, said)
        assert finishes() == [] and status_of("gv-review") == "active"


@scenario
def lost_reviewer_listed_again_after_a_push_is_closed_as_done():
    """A reviewer reported lost on an earlier tick and now listed again, by its handle or
    under a new handle with its launch pty, whose coder ticket is complete with a PUSH:
    the review is done, so its terminal is closed and its review ticket finished, here
    and on a remote host."""
    lost = "STUCK gv-review codex:gv-review: terminal term-gv-review is not listed. Left alone"
    wt = str(tmp / "wt" / "gv")
    for host in ("local", "Juzzark-WSL"):
        for handle in ("term-gv-review", "term-gv-review-new"):
            st = gone_reviewer("complete", ["PUSH 1a2b3c4"], host, lost)
            listed = term(handle, 100, ident="codex", worktree=wt, pty="pty-term-gv-review")
            world(on(host, listed), git=git, gh=merged)
            tick(st)
            assert len(finishes()) == 1 and closes() == [(handle, host)], (host, handle, finishes(), closes())
            assert status_of("gv-review") == "complete", (host, handle)


@scenario
def gone_reviewer_close_refused_is_one_stuck():
    """A close Concord refuses is one STUCK naming the review ticket, with Concord's answer
    on the ticket; it is tried again each tick with no second STUCK."""
    st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
    said, noted = [], []
    for when in (NOW, NOW + 60):
        world([], git=git, gh=merged, finish_refused="task version is stale")
        tick(st, when)
        assert len(finishes()) == 1 and status_of("gv-review") == "active"
        said.append([s[1] for s in sends() if not s[1].startswith("CLEARED")])
        noted.append(any("task version is stale" in n for n in notes()))
    failed = (
        "STUCK gv-review: its reviewer terminal is gone after REVIEW VERDICT: PUSH on gv, "
        "which is complete, but closing the review ticket failed. Tried again each tick"
    )
    assert said == [[failed], []] and noted == [True, False], (said, noted)
    world([], git=git, gh=merged)
    tick(st, NOW + 120)
    assert len(finishes()) == 1 and status_of("gv-review") == "complete"
    assert len(progress_lines()) == 1


@scenario
def live_reviewer_kept_until_its_review_ticket_closes():
    """A live reviewer whose coder is complete with PUSH: while Concord refuses the review
    ticket's close, or the review ticket is another agent's, its terminal and record stay.
    The refused close is tried again next tick, and then the terminal closes."""
    wt = str(tmp / "wt" / "gv")
    listed = term("term-gv-review", 100, ident="codex", worktree=wt)
    for owner, refused in (("codex:gv-review", "task version is stale"), ("claude:other", None)):
        st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
        ticket_row("gv-review", agent=owner)
        world(local(listed), git=git, gh=merged, finish_refused=refused)
        tick(st)
        assert closes() == [] and "gv-review" in st["workers"], (owner, closes())
    st = gone_reviewer("complete", ["PUSH 1a2b3c4"])
    world(local(listed), git=git, gh=merged, finish_refused="task version is stale")
    tick(st)
    world(local(listed), git=git, gh=merged)
    tick(st, NOW + 60)
    assert status_of("gv-review") == "complete" and ("term-gv-review", "local") in closes(), closes()


@scenario
def b1_cut_short_list():
    """A truncated listing is no evidence that a worker exited."""
    reset_db()
    ticket_row("tr")
    st = fresh_state(tr=rec("tr", "c1"))
    world(local(), truncated=True)
    tick(st)
    assert created() == [] and sends() == []
    assert "--limit" in calls("orca", ["terminal", "list"])[0]["args"]


@scenario
def b1_launch_needs_a_listing():
    """launch keeps a record whose host cannot list, and refuses a live replacement."""
    reset_db()
    ticket_row("lk", status="proposed", title="served pages / lk")
    old = rec("lk", "x1", agent="codex", model="gpt-6.1-sol", agent_id="codex:lk")
    argv = ["launch", "lk", "--agent", "codex", "--model", "gpt-6.1-sol"]
    argv += ["--worktree", old["worktree"]]
    for terminals, why in (
        ({"local": "down"}, "terminal list failed"),
        (
            {"local": [term("term-r2", 5, ident="codex", worktree=old["worktree"])[0]]},
            "already runs in term-r2",
        ),
    ):
        state_file.write_text(json.dumps({"workers": {"lk": old}}))
        set_fixture(terminals=terminals)
        done = cli(*argv)
        assert done.returncode != 0 and why in done.stderr, done
        assert created() == [] and calls("orca", ["terminal", "close"]) == []
        assert json.loads(state_file.read_text())["workers"]["lk"] == old
    state_file.unlink()


@scenario
def b2_shell_is_exited():
    """An agent that exited leaves its shell running: nothing is typed, it is relaunched."""
    reset_db()
    ticket_row("sh")
    update("sh", "blocker", "Waiting on: review")
    update("sh", "finding", "REVIEW VERDICT: FIX", agent="codex:sh-review")
    st = fresh_state(sh=rec("sh", "c1"))
    world(local(term("term-sh", 31 * 60, ["julian@mac sh % "], ident=None)))
    tick(st)
    assert [s for s in sends() if s[0] == "term-sh"] == [], "never typed into a shell"
    assert len(created()) == 1


@scenario
def b2_no_ticket_text_typed():
    """Wakes and events are fixed lines; ticket text and screens stay on the ticket."""
    reset_db()
    ticket_row("inj")
    update("inj", "blocker", "Waiting on: review")
    update("inj", "finding", "REVIEW VERDICT: FIX `touch pwned` $(id)", agent="c:r")
    ticket_row("op2")
    update("op2", "blocker", "Waiting on: operator; rm -rf `pwd`")
    ticket_row("sl")
    st = fresh_state(inj=rec("inj", "c1"), op2=rec("op2", "c2"), sl=rec("sl", "c3"))
    st["workers"]["sl"]["nudged_at"] = NOW - 3600
    screen = ["$(touch pwned)", "line two"]
    world(
        local(
            term("term-inj", 100),
            term("term-op2", 100),
            term("term-sl", 3 * 3600, screen),
        )
    )
    tick(st)
    texts = [s[1] for s in sends()]
    assert len(texts) == 3, texts
    for text in texts:
        assert "\n" not in text and not set(text) & set("`$;|&<>()'\""), text


@scenario
def b2_orchestrator_shell_holds_events():
    """An event goes to the file at once; its EVENTS wake waits while the orchestrator's
    terminal is a shell, then goes through once."""
    reset_db()
    ticket_row("oq")
    update("oq", "blocker", "Waiting on: operator")
    st = fresh_state(oq=rec("oq", "c1"))
    world(local(term("term-oq", 100)), orch_ident=None)
    tick(st)
    assert sends(events=False) == [] and [s[1][:8] for s in sends()] == ["NEEDS YO"]
    world(local(term("term-oq", 100)))
    tick(st)
    assert [s[1][:7] for s in sends(events=False)] == ["EVENTS:"], sends(events=False)
    world(local(term("term-oq", 100)))
    tick(st)
    assert sends(events=False) == []


@scenario
def m1_cleanup_spares_other_terminals():
    """A merged worktree that holds a terminal dev-auto did not launch is not removed."""
    reset_db()
    ticket_row("k9", status="complete")
    st = fresh_state(k9=rec("k9", "c1"))
    shell = term(
        "term-operator", 100, ident=None, worktree=str(tmp / "wt" / "k9") + "/"
    )
    world(local(term("term-k9", 100), shell), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "k9" in st["workers"]
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-k9"], "only its own terminal"
    assert sends() == [
        (
            "term-orch",
            (
                "STUCK k9: ticket is complete but its worktree holds other terminals "
                "term-operator. Not removed"
            ),
            True,
        )
    ], sends()
    ticket_row("k2", status="closed")
    ticket_row("k3")
    update("k3", "blocker", "Waiting on: review")
    pair = str(tmp / "wt" / "k2")
    st = fresh_state(
        k2=rec("k2", "c1"), k3=rec("k3", "x1", agent="codex", worktree=pair + "/")
    )
    reviewer = term("term-k3", 100, ident="codex", worktree=pair)
    world(local(term("term-k2", 100), reviewer), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [], "a shared worktree stays"
    assert "k2" not in st["workers"] and "k3" in st["workers"]


@scenario
def trial_default_shell_closed_with_worktree():
    """Live: `orca worktree create` opened a default shell, so cleanup refused for "other
    terminals". Plain shells with no agent go with the worktree; a shell that is not at
    its prompt, or an agent, still blocks the removal."""
    reset_db()
    ticket_row("ds", status="proposed", title="trial / ds", notes="Branch: `feat/a`")
    state_file.unlink(missing_ok=True)
    wt = str(tmp / "wt" / "ds")
    Path(wt).mkdir(parents=True, exist_ok=True)
    prompt = ["➜  ds git:(julianthant/ds)"]
    default = term("term-default", 300, prompt, ident=None, worktree=wt)
    own = {"rev-parse --abbrev-ref HEAD": [0, "feat/a\n"], "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"]}
    world(local(default), worktree_show=just_created(), git=own)
    da.main(["set-orchestrator", "term-orch"])
    da.main(["launch", "ds", "--agent", "claude", "--model", OPUS, "--worktree", wt])
    shells = [{"terminal": "term-default", "pty": "pty-term-default", "lines": prompt}]
    assert json.loads(state_file.read_text())["workers"]["ds"].get("shells") == shells
    ticket_row("ds", status="closed", title="trial / ds")
    world(local(term("term-local-1", 100, worktree=wt), default), git=git, gh=merged)
    da.main(["tick"])
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-local-1", "term-default"], closed
    assert len(calls("orca", ["worktree", "rm"])) == 1 and sends() == []
    state_file.unlink()

    running = ["➜  ds git:(julianthant/ds) npm test", "> vitest"]
    busy_default = term("term-default", 5, running, ident=None, worktree=wt)
    st = fresh_state(ds=rec("ds", "c1", worktree=wt, shells=shells))
    world(local(term("term-ds", 100, worktree=wt), busy_default), git=git, gh=merged)
    tick(st)
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-ds"], "only its own terminal"
    assert calls("orca", ["worktree", "rm"]) == [] and "ds" in st["workers"]
    assert sends()[0][1].endswith("other terminals term-default. Not removed"), sends()
    used = ["➜  ds git:(julianthant/ds) ls", "a.txt", *prompt]
    used_default = term("term-default", 5, used, ident=None, worktree=wt)
    operator = term("term-op", 50, prompt, ident=None, worktree=wt)
    for terms, closed_shells in (
        ((default, operator), ["term-default", "term-op"]),
        ((used_default,), ["term-default"]),
    ):
        st = fresh_state(ds=rec("ds", "c1", worktree=wt, shells=shells))
        world(local(term("term-ds", 100, worktree=wt), *terms), git=git, gh=merged)
        tick(st)
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        assert closed == ["term-ds", *closed_shells], closed
        assert len(calls("orca", ["worktree", "rm"])) == 1 and "ds" not in st["workers"]

    ticket_row("ds-review")
    st = fresh_state(
        ds=rec("ds", "c1", worktree=wt, shells=shells),
        **{"ds-review": rec("ds-review", "x1", agent="codex", worktree=wt)},
    )
    reviewer = term("term-ds-review", 100, ident="codex", worktree=wt)
    world(local(term("term-ds", 100, worktree=wt), reviewer, default))
    tick(st)
    assert "ds" not in st["workers"]
    assert st["workers"]["ds-review"].get("shells") == shells


@scenario
def b2_operator_shell_never_recorded():
    """An operator's shell is never recorded as Orca's default shell: not in a worktree
    older than a minute, not beside another terminal. At removal it is a plain shell
    with no agent, so cleanup closes it with the worktree."""
    reset_db()
    state_file.unlink(missing_ok=True)
    wt = tmp / "wt" / "os"
    wt.mkdir(parents=True, exist_ok=True)
    old = time.time() - 3 * 86400
    os.utime(wt, (old, old))
    mine = ["➜  os git:(feat/os) vim notes.md", "zsh: suspended  vim notes.md"]
    mine.append("➜  os git:(feat/os) ✗")
    operator = term("term-operator", 3 * 86400, mine, ident=None, worktree=str(wt))
    world(local(operator), git={"rev-parse --abbrev-ref HEAD": [0, "feat/a\n"], "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"]})
    da.main(["set-orchestrator", "term-orch"])
    for tid in ("os", "ot"):
        ticket_row(tid, status="proposed", title=f"trial / {tid}", notes="Branch: `feat/a`")
    argv = ["--agent", "claude", "--model", OPUS, "--worktree", str(wt)]
    da.main(["launch", "os", *argv])
    assert json.loads(state_file.read_text())["workers"]["os"]["shells"] == []
    ticket_row("os", status="closed", title="trial / os")
    world(
        local(term("term-local-1", 100, worktree=str(wt)), operator), git=git, gh=merged
    )
    da.main(["tick"])
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-local-1", "term-operator"], closed
    assert len(calls("orca", ["worktree", "rm"])) == 1 and sends() == []
    fresh = tmp / "wt" / "ot"
    fresh.mkdir(parents=True, exist_ok=True)
    prompt = ["➜  ot git:(feat/ot)"]
    pair = [
        term(h, 5, prompt, ident=None, worktree=str(fresh))
        for h in ("term-d", "term-o")
    ]
    world(local(*pair), worktree_show=just_created())
    da.main(
        ["launch", "ot", "--agent", "claude", "--model", OPUS, "--worktree", str(fresh)]
    )
    assert json.loads(state_file.read_text())["workers"]["ot"]["shells"] == []
    state_file.unlink()


@scenario
def m2_menu_before_wake():
    """The switch-model menu is answered at once and nothing else is typed that tick."""
    reset_db()
    ticket_row("mr", agent="codex:mr")
    update("mr", "blocker", "Waiting on: review", agent="codex:mr")
    update("mr", "finding", "REVIEW VERDICT: FIX x", agent="claude:mr-review")
    st = fresh_state(mr=rec("mr", "x1", agent="codex", model="gpt-6.1-sol"))
    world(local(term("term-mr", 10, MENU, ident="codex")))
    tick(st)
    assert sends() == [("term-mr", "2", False)], sends()


@scenario
def m3_relaunch_keeps_events():
    """Old clearances are not re-announced: not on a first launch, not on a relaunch."""
    reset_db()
    ticket_row("rp")
    update("rp", "finding", "REVIEW VERDICT: PUSH 0ad0ad0", agent="codex:rp-review")
    state_file.unlink(missing_ok=True)
    set_fixture()
    da.main(["set-orchestrator", "term-orch"])
    wt = str(tmp / "wt" / "rp")
    Path(wt).mkdir(parents=True, exist_ok=True)
    argv = ["launch", "rp", "--agent", "claude", "--model", OPUS, "--worktree", wt]
    da.main(argv)
    live = local(term("term-local-1", 5, worktree=wt))
    world(live)
    da.main(["tick"])
    assert sends() == [], "a PUSH from before the launch is old news"
    update("rp", "finding", "REVIEW VERDICT: PUSH abcdef1", agent="codex:rp-review")
    world(live)
    da.main(["tick"])
    verdict = "dev-auto: a review verdict was posted on ticket rp. Read it there and continue."
    assert sorted(sends()) == [
        ("term-local-1", woke("rp", verdict), True),
        ("term-orch", "CLEARED REVIEW rp: PUSH abcdef1", True),
    ], sends()
    data = json.loads(state_file.read_text())
    data["workers"]["rp"]["launched_at"] -= 120
    state_file.write_text(json.dumps(data))
    world(
        local(term("term-local-1", 5, ["➜  rp git:(feat/a)"], ident=None, worktree=wt))
    )
    da.main(argv)
    world(live)
    da.main(["tick"])
    assert sends() == [], "a relaunch keeps what was delivered"
    state_file.unlink()


CODER_LINE = "Coder: claude (claude-opus-5-5, effort high)"


def reviewer_pair(tid, kind, model, line, tier_name):
    """A coder ticket whose Reviewer line is `line`, its Claude coder and a `kind` reviewer
    on its review ticket, launched in one worktree; returns the saved workers and the
    worktree."""
    reset_db()
    ticket_row(tid, notes=f"{CODER_LINE} · {line} · Tier: {tier_name}")
    ticket_row(f"{tid}-review", status="proposed", title=f"feat / {tid}-review")
    state_file.unlink(missing_ok=True)
    set_fixture()
    da.main(["set-orchestrator", "term-orch"])
    wt = str(tmp / "wt" / tid)
    Path(wt).mkdir(parents=True, exist_ok=True)
    da.main(["launch", tid, "--agent", "claude", "--model", OPUS, "--worktree", wt])
    da.main(["launch", f"{tid}-review", "--agent", kind, "--model", model, "--worktree", wt])
    return json.loads(state_file.read_text())["workers"], wt


@scenario
def review_launch_on_another_host_than_its_coder_is_refused():
    """A review ticket launches only on its coder record's host: elsewhere, one line names
    both hosts and no terminal is created."""
    reset_db()
    ticket_row("ph", notes=f"{CODER_LINE} · Reviewer: codex · Tier: medium")
    ticket_row("ph-review", status="proposed", title="feat / ph-review")
    coder = rec("ph", "c3", host="Juzzark-WSL")
    state_file.write_text(json.dumps({"workers": {"ph": coder}}))
    set_fixture()
    wt = str(tmp / "wt" / "ph")
    done = cli("launch", "ph-review", "--agent", "codex", "--model", "m", "--worktree", wt)
    assert done.returncode != 0 and done.stderr.strip() == (
        "ph-review refused: its coder ph runs on Juzzark-WSL, not local; a coder and "
        "its reviewer run on the same host"
    ), done
    assert created() == []
    state_file.unlink()


@scenario
def review_launch_checks_both_published_and_current_coder_placement():
    reset_db()
    ticket_row("pl", notes="Reviewer: codex · Tier: medium")
    paths = [tmp / "wt" / name for name in ("published", "current", "other")]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    published, current, other = map(str, paths)
    set_fixture()
    assert cli("review-ticket", "pl", "--worktree", published).returncode == 0
    args = ("launch", "pl-review", "--agent", "codex", "--model", "m", "--worktree")
    try:
        done = cli(*args, other)
        assert done.returncode and published in done.stderr and not created(), done
        coder = rec("pl", "x1", agent="codex", worktree=published)
        state_file.write_text(json.dumps({"workers": {"pl": coder}}))

        def shift(conf, host):
            coder.update(host="Juzzark-WSL", worktree=current)
            state_file.write_text(json.dumps({"workers": {"pl": coder}}))
            return None, []

        real_memory = da.memory_refusal
        da.memory_refusal = shift
        try:
            try:
                da.main([*args, published])
            except SystemExit as stop:
                assert "Juzzark-WSL" in str(stop.code), stop
            else:
                raise AssertionError("review launched after the coder changed hosts")
        finally:
            da.memory_refusal = real_memory
        assert not created()
        done = cli(*args, other, "--host", "Juzzark-WSL")
        assert done.returncode and current in done.stderr and not created(), done
        assert cli(*args, current, "--host", "Juzzark-WSL").returncode == 0
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def direct_launch_uses_named_effort_and_refuses_a_different_reviewer_tuple():
    reset_db()
    ticket_row("dr", notes="Coder: codex (gpt-6.2-sol, effort medium) · "
               "Reviewer: claude (claude-opus-5-5, effort medium) · Tier: medium")
    ready_row("dr-review", "Tier: medium")
    wt = tmp / "wt" / "dr"
    wt.mkdir(parents=True, exist_ok=True)
    set_fixture()
    try:
        done = cli("launch", "dr", "--agent", "codex", "--model", "gpt-6.2-sol", "--worktree", str(wt))
        assert done.returncode == 0, done
        coder = json.loads(state_file.read_text())["workers"]["dr"]
        assert (coder["model"], coder["effort"]) == ("gpt-6.2-sol", "medium"), coder
        args = ("launch", "dr-review", "--agent", "claude", "--worktree", str(wt), "--model")
        for model, effort in ((SONNET, "medium"), (OPUS, "high")):
            refused = cli(*args, model, "--effort", effort)
            assert refused.returncode and "Reviewer" in refused.stderr, refused
        assert len(created()) == 1, created()
        done = cli(*args[:-1])
        assert done.returncode == 0, done
        reviewer = json.loads(state_file.read_text())["workers"]["dr-review"]
        assert (reviewer["model"], reviewer["effort"]) == (OPUS, "medium"), reviewer
        ready_row("dr2", "Coder: codex (gpt-6.2-sol, effort medium)")
        override = ("launch", "dr2", "--agent", "codex", "--model", "gpt-6.2-sol",
                    "--effort", "high", "--worktree", str(wt))
        refused = cli(*override)
        assert refused.returncode and "Coder" in refused.stderr, refused
        done = cli(*override, "--override-route")
        assert done.returncode == 0, done
        assert json.loads(state_file.read_text())["workers"]["dr2"]["effort"] == "high"
    finally:
        state_file.unlink(missing_ok=True)


def reviewer_runs_its_review(tid, kind, model, line, tier_name):
    """A `kind` reviewer the Reviewer line names launches, REVIEW READY wakes it once, its
    PUSH clears and wakes the coder, and the finished pair is closed. Codex's switch-model
    words on its screen are output, so no Codex-only key is typed into it."""
    workers, wt = reviewer_pair(tid, kind, model, line, tier_name)
    review = f"{tid}-review"
    reviewer = workers[review]
    assert (reviewer["agent"], reviewer["model"]) == (kind, model), reviewer
    assert len(created()) == 2, created()
    coder_term = term(workers[tid]["terminal"], 5, worktree=wt)
    reviewer_term = term(reviewer["terminal"], 5, MENU, ident=kind, worktree=wt)
    update(tid, "progress", "REVIEW READY 1a2b3c4")
    update(tid, "blocker", "Waiting on: review of 1a2b3c4")
    ready = f"dev-auto: REVIEW READY was posted on ticket {tid}. Review it as your ticket says."
    world(local(coder_term, reviewer_term))
    da.main(["tick"])
    assert sends() == [(reviewer["terminal"], woke(review, ready, kind), True)], sends()
    world(local(coder_term, reviewer_term))
    da.main(["tick"])
    assert sends() == [], "REVIEW READY wakes the reviewer once"
    update(tid, "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent=reviewer["agent_id"])
    world(local(coder_term, reviewer_term))
    da.main(["tick"])
    verdict = f"dev-auto: a review verdict was posted on ticket {tid}. Read it there and continue."
    assert sorted(sends()) == [
        (workers[tid]["terminal"], woke(tid, verdict), True),
        ("term-orch", f"CLEARED REVIEW {tid}: PUSH 1a2b3c4", True),
    ], sends()
    ticket_row(tid, status="complete")
    ticket_row(review, status="complete", agent=reviewer["agent_id"])
    world(local(coder_term, reviewer_term), git=git, gh=merged)
    da.main(["tick"])
    assert (reviewer["terminal"], "local") in closes(), closes()
    assert review not in json.loads(state_file.read_text())["workers"]
    state_file.unlink()


@scenario
def claude_reviewer_runs_a_ui_review():
    """A ui coder ticket whose Reviewer line names claude gets a Claude review session."""
    line = "Reviewer: claude (claude-sonnet-5-5, effort medium)"
    reviewer_runs_its_review("rvcl", "claude", SONNET, line, "ui")


@scenario
def grok_and_cursor_reviewers_run_on_this_mac():
    """A medium or easy coder ticket whose Reviewer line names grok or cursor gets that
    session on this Mac; a Grok reviewer on a remote host is refused as before."""
    line = "Reviewer: codex (gpt-6.1-sol, effort xhigh) or grok (grok-4.7, effort xhigh)"
    reviewer_runs_its_review("rvgr", "grok", "grok-4.7", line, "medium")
    line = "Reviewer: codex (gpt-6.1-sol, effort xhigh) or cursor (grok-4.7-xhigh)"
    reviewer_runs_its_review("rvcu", "cursor", "grok-4.7-xhigh", line, "easy")
    done = cli(
        *("launch", "rvgr-review", "--agent", "grok", "--model", "grok-4.7"),
        *("--worktree", str(tmp), "--host", "Juzzark-WSL"),
    )
    assert done.returncode and "this Mac" in done.stderr, done


@scenario
def review_launch_on_a_kind_its_reviewer_line_does_not_name_is_refused():
    """A review ticket launched on an agent kind its coder ticket's Reviewer line does not
    name, or whose coder ticket has no Reviewer line, is refused before any terminal is
    created; the kind the line names launches."""
    reset_db()
    line = "Reviewer: codex (gpt-6.1-sol, effort xhigh)"
    ticket_row("rn", notes=f"{CODER_LINE} · {line} · Tier: hard")
    ticket_row("rn-review", status="proposed", title="feat / rn-review")
    state_file.unlink(missing_ok=True)
    set_fixture()
    wt = str(tmp / "wt" / "rn")
    Path(wt).mkdir(parents=True, exist_ok=True)
    for kind, model in (("claude", SONNET), ("grok", "grok-4.7"), ("cursor", "grok-4.7-xhigh")):
        done = cli("launch", "rn-review", "--agent", kind, "--model", model, "--worktree", wt)
        refused = f"rn-review refused: rn's Reviewer line names codex, not {kind}: {line}"
        assert (done.returncode, done.stderr.strip()) == (1, refused), done
    ticket_row("rn", notes=f"{CODER_LINE} · Tier: hard")
    done = cli("launch", "rn-review", "--agent", "codex", "--model", "m", "--worktree", wt)
    assert (done.returncode, done.stderr.strip()) == (1, "rn-review refused: rn has no Reviewer line"), done
    assert created() == [], created()
    ticket_row("rn", notes=f"{CODER_LINE} · {line} · Tier: hard")
    done = cli("launch", "rn-review", "--agent", "codex", "--model", "gpt-6.1-sol", "--worktree", wt)
    assert done.returncode == 0 and len(created()) == 1, (done, created())
    state_file.unlink()


@scenario
def review_launch_reads_never_and_parentheses_on_the_reviewer_line():
    """dev-auto-fixes-50 (g): a kind after "never" on the Reviewer line is refused, and a
    kind named in words inside its parentheses (live: dev-auto-fixes-19) may launch; a
    model id there, such as cursor's grok-4.7-xhigh, still names no kind."""
    never = "Reviewer: claude (claude-sonnet-5-5, effort medium), never codex or grok"
    fallback = "Reviewer: codex (gpt-6.1-sol, effort xhigh; a Claude reviewer on WSL if no Codex is free)"
    model = "Reviewer: codex (gpt-6.1-sol, effort xhigh) or cursor (grok-4.7-xhigh)"
    cases = (
        (never, "codex", "m", "claude"),
        (never, "grok", "grok-4.7", "claude"),
        (never, "claude", SONNET, None),
        (fallback, "claude", SONNET, None),
        (model, "grok", "grok-4.7", "codex or cursor"),
    )
    for line, kind, model_id, refused in cases:
        reset_db()
        ticket_row("rw", notes=f"{CODER_LINE} · {line} · Tier: medium")
        ticket_row("rw-review", status="proposed", title="feat / rw-review")
        state_file.unlink(missing_ok=True)
        set_fixture()
        wt = str(tmp / "wt" / "rw")
        Path(wt).mkdir(parents=True, exist_ok=True)
        done = cli("launch", "rw-review", "--agent", kind, "--model", model_id, "--worktree", wt)
        if refused:
            text = f"rw-review refused: rw's Reviewer line names {refused}, not {kind}: {line}"
            assert (done.returncode, done.stderr.strip()) == (1, text), (line, kind, done)
        else:
            assert done.returncode == 0 and len(created()) == 1, (line, kind, done)
    state_file.unlink(missing_ok=True)


@scenario
def m4_exits_are_bounded():
    """A fast exit waits for a new launch, and so does a worker relaunched too often."""
    reset_db()
    ticket_row("fx")
    st = fresh_state(fx=rec("fx", "c1", launched_at=NOW - 60))
    world(local(term("term-fx", 30, status="exited")))
    tick(st)
    assert created() == [] and sends()[0][1].startswith("STUCK fx claude:fx: its ")
    world(local(term("term-fx", 600, status="exited")))
    tick(st, NOW + 600)
    assert created() == [] and sends() == [], "it waits for a new launch"
    ticket_row("fy")
    st = fresh_state(fy=rec("fy", "c1", relaunches=3))
    world(local(term("term-fy", 600, status="exited")))
    tick(st)
    assert created() == [] and sends() == [
        (
            "term-orch",
            (
                "STUCK fy claude:fy: its terminal exited after 3 relaunches. "
                "It waits for a new launch"
            ),
            True,
        )
    ], sends()


@scenario
def m5_slots_count_live_agents():
    """Live agents dev-auto did not launch hold slots; its own are counted once."""
    sessions = homes["c1"] / "sessions"
    sessions.mkdir()
    try:
        (sessions / "111.json").write_text(json.dumps({"pid": 111, "sessionId": "s-x"}))
        (sessions / "999.json").write_text(json.dumps({"pid": 999, "sessionId": "s-y"}))
        set_fixture(ps="111 claude --model claude-opus-5-5 ORCA_TERMINAL_HANDLE=t-x\n")
        assert pick({}) == "pro2", "an Opus worker from outside fills a Pro account"
        (sessions / "111.json").write_text(json.dumps({"pid": 111, "sessionId": "s-a"}))
        mine = {"a": rec("a", "c1", model=SONNET, session="s-a")}
        set_fixture(ps="111 claude --model claude-sonnet-5-5 CLAUDE_CONFIG_DIR=/x y\n")
        assert pick(mine, model=SONNET) == "pro1", (
            "its own session is not counted twice"
        )
    finally:
        shutil.rmtree(sessions)
    full = {"a": rec("a", "x1", agent="codex"), "b": rec("b", "x1", agent="codex")}
    line = f"222 codex -m m ORCA_TERMINAL_HANDLE=t-c CODEX_HOME={homes['x2']} TERM=x\n"
    set_fixture(ps=line + "333 node /opt/bin/cursor-agent ORCA_TERMINAL_HANDLE=t-u\n")
    assert pick(full, agent="codex", model="m") is None, "x2 holds a live Codex"
    assert pick({}, agent="cursor", model="grok") is None, "cur holds a live Cursor"
    full["c"] = rec("c", "x2", agent="codex", terminal="t-c")
    assert len(da.foreign(cfg, full)["x2"]) == 0, "a record's own terminal"
    assert (
        da.env_value(
            "x ORCA_TERMINAL_HANDLE=t1 CODEX_HOME=/a b/home TERM=x", "CODEX_HOME"
        )
        == "/a b/home"
    )


@scenario
def m5_default_codex_row_counts_only_its_own_home():
    """A Codex row with no home is the CLI's default ~/.codex: a session on another row's
    explicit profile is charged to that row alone, and one with CODEX_HOME unset, or set
    to the default path, to the default row alone."""
    x2 = da.account_row(cfg, "x2")
    on = {**cfg, "account": [{**x2, "id": "xd", "label": "xd", "home": ""}, x2]}
    profile = f"222 codex -m m ORCA_TERMINAL_HANDLE=t-p CODEX_HOME={homes['x2']} TERM=x\n"
    set_fixture(ps=profile)
    assert da.foreign(on, {}) == {"xd": [], "x2": ["codex"]}, da.foreign(on, {})
    default = f"444 codex -m m ORCA_TERMINAL_HANDLE=t-e CODEX_HOME={Path.home() / '.codex'}\n"
    set_fixture(ps="333 codex -m m ORCA_TERMINAL_HANDLE=t-d TERM=x\n" + default)
    assert da.foreign(on, {}) == {"xd": ["codex", "codex"], "x2": []}, da.foreign(on, {})


@scenario
def m6_relay_clears_the_pointer():
    """After RELAY, events wait for the successor instead of waking the old session."""
    reset_db()
    ticket_row("o5")
    update("o5", "blocker", "Waiting on: operator")
    claude_usage("c3", 96, NOW + 900)
    st = fresh_state(o5=rec("o5", "c1"))
    st["orchestrator"]["account"] = "c3"
    world(local(term("term-o5", 100)))
    tick(st)
    assert [s[1].split(":")[0] for s in sends(events=False)] == ["RELAY"], sends()
    assert st["orchestrator"] is None and st["relay"]["by"] == "term-orch"
    assert any(" NEEDS YOU o5" in l for l in event_lines()), "it waits in the file"
    st["orchestrator"] = {"terminal": "term-orch2", "host": "local", "account": "c1"}
    world(local(term("term-o5", 100)), orch="term-orch2")
    tick(st)
    assert [(s[0], s[1].split(":")[0]) for s in sends(events=False)] == [
        ("term-orch2", "EVENTS")
    ], sends(events=False)
    clear_usage()


@scenario
def m7_sighup_keeps_state():
    """A tick killed by SIGHUP mid-launch saves what it did: no repeat, no orphan."""
    import signal
    import time

    reset_db()
    ticket_row("ha")
    ticket_row("hb")
    real = time.time()
    workers = {
        "ha": rec("ha", "c1", launched_at=real - 7200),
        "hb": rec("hb", "c2", launched_at=real - 7200),
    }
    data = fresh_state(**workers)
    state_file.write_text(json.dumps(data))
    (root / "supervisor" / "events.md").unlink(missing_ok=True)

    def row(handle, ident, wt, quiet=31 * 60):
        return {
            "handle": handle,
            "branch": "refs/heads/feat/a",
            "lastOutputAt": int((real - quiet) * 1000),
            "agentIdentity": ident,
            "worktreePath": str(tmp / "wt" / wt),
        }

    orch = row("term-orch", "claude", "repo")
    running = {"status": "running", "tail": ["❯"]}
    exited = {"status": "exited", "tail": []}
    set_fixture(
        terminals={
            "local": [orch, row("term-ha", "claude", "ha"), row("term-hb", None, "hb")]
        },
        screens={"term-orch": running, "term-ha": running, "term-hb": exited},
        hang_create=True,
    )
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "dev_auto.py"), "tick"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if calls("orca", ["terminal", "create"]):
            break
        time.sleep(0.05)
    assert [s[0] for s in sends()] == ["term-ha"], sends()
    proc.send_signal(signal.SIGHUP)
    assert proc.wait(timeout=20) == 1, "SIGHUP exits through the state writer"
    set_fixture(
        terminals={
            "local": [
                orch,
                row("term-ha", "claude", "ha"),
                row("term-new", "claude", "hb", 0),
            ]
        },
        screens={"term-orch": running, "term-ha": running, "term-new": running},
    )
    done = cli("tick")
    assert done.returncode == 0, done
    assert created() == [], "nothing launched twice"
    assert [l.split(" ", 2)[2].split(":")[0] for l in event_lines()] == ["STUCK hb claude"]
    assert [x[1][:7] for x in sends(events=False)] == ["EVENTS:"], sends(events=False)
    assert json.loads(state_file.read_text())["workers"]["hb"]["terminal"] is None
    state_file.unlink()


@scenario
def m8_questions_are_left_alone():
    """An open question or permission prompt gets no keystroke; the orchestrator hears."""
    reset_db()
    ticket_row("aq")
    st = fresh_state(aq=rec("aq", "c1"))
    question = ["Which layout?", "❯ 1. Grid", "  2. List", "? 1 question"]
    world(local(term("term-aq", 31 * 60, question)))
    tick(st)
    assert sends() == [
        (
            "term-orch",
            (
                "NEEDS YOU aq claude:aq: a prompt is open on its screen, "
                "which is recorded on the ticket"
            ),
            True,
        )
    ], sends()
    assert notes()[-1].endswith("Which layout?\n❯ 1. Grid\n  2. List\n? 1 question")
    ticket_row("ap")
    update("ap", "blocker", "Waiting on: operator")
    st = fresh_state(ap=rec("ap", "c2"))
    world(local(term("term-ap", 100)), orch_tail=CLAUDE_PERMISSION)
    tick(st)
    typed = sends(events=False, include_clear=True)
    assert typed == [], "nor is the orchestrator's answered: its EVENTS wake waits"
    assert [s[1][:11] for s in sends()] == ["NEEDS YOU a"], sends()


@scenario
def remote_branch_checked():
    """A remote branch ahead of the merged head keeps everything; one merged in goes."""
    reset_db()
    ticket_row("rb", status="complete")
    ahead = {
        **git,
        "ls-remote --heads origin refs/heads/feat/a": [0, "def\trefs/heads/feat/a\n"],
        "merge-base --is-ancestor def abc": [1, ""],
    }
    st = fresh_state(rb=rec("rb", "c1"))
    world(local(term("term-rb", 100)), git=ahead, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "rb" in st["workers"]
    assert sends() == [
        (
            "term-orch",
            "STUCK rb: ticket is complete but its worktree is remote-ahead. Not removed",
            True,
        )
    ], sends()
    st = fresh_state(rb=rec("rb", "c1"))
    inside = {**ahead, "merge-base --is-ancestor def abc": [0, ""]}
    world(local(term("term-rb", 100)), git=inside, gh=merged)
    tick(st)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "rb" not in st["workers"]


@scenario
def trial_plain_git_remote_cleanup():
    """Live: a plain bare origin has no gh, so cleanup read "could not be checked". git
    decides instead: HEAD and the remote branch in the remote default branch's tip. On a
    GitHub origin a failed gh stays unchecked, since git cannot see a squash merge."""
    reset_db()
    ticket_row("pg", status="closed")
    plain = {
        **git,
        "remote get-url origin": [0, "/Users/x/dev-auto-trial/origin.git\n"],
        "ls-remote --heads origin refs/heads/feat/a": [
            0,
            "c0ffee1\trefs/heads/feat/a\n",
        ],
        "ls-remote origin HEAD": [0, "c0ffee1\tHEAD\n"],
        "merge-base --is-ancestor HEAD c0ffee1": [0, ""],
        "merge-base --is-ancestor c0ffee1 c0ffee1": [0, ""],
    }
    unpushed = {**plain, "merge-base --is-ancestor HEAD c0ffee1": [1, ""]}
    github = {**plain, "remote get-url origin": git["remote get-url origin"]}
    for fixture_git, removed, why in (
        (unpushed, 0, "its worktree is unmerged. Not removed"),
        (plain, 1, ""),
        (github, 0, "its worktree could not be checked"),
    ):
        st = fresh_state(pg=rec("pg", "c1"))
        world(local(term("term-pg", 100)), git=fixture_git, gh={})
        tick(st)
        assert len(calls("orca", ["worktree", "rm"])) == removed, (fixture_git, sends())
        assert ("pg" in st["workers"]) == (not removed)
        assert removed or why in sends()[0][1], sends()
    assert calls("gh"), "a GitHub origin asks gh first"


def feature_cleanup(tid, title, pushed, gh=None, owned=True, **answers):
    """Tick a closed ticket on branch task/feat/<tid> whose feature branch feature/feat is
    pushed or only local; gh fails unless given, so only the feature branch proves a merge."""
    reset_db()
    ticket_row(tid, status="closed", title=title)
    branch = f"task/feat/{tid}"
    tip = "f00d" if not pushed else "beef"
    found = [0, f"{tip}\trefs/heads/feature/feat\n" if pushed else ""]
    fixture_git = {
        **git,
        "rev-parse --abbrev-ref HEAD": [0, f"{branch}\n"],
        f"ls-remote --heads origin refs/heads/{branch}": [0, ""],
        "ls-remote --heads origin refs/heads/feature/feat": found,
        "rev-parse -q --verify refs/heads/feature/feat": [0, "f00d\n"],
        f"merge-base --is-ancestor HEAD {tip}": [0, ""],
        f"update-ref -d refs/heads/{branch} c0ffee1": [0, ""],
        **answers,
    }
    st = fresh_state(**{tid: rec(tid, "c1", owned_branch=branch if owned else None)})
    world(local(term(f"term-{tid}", 100)), git=fixture_git, gh=gh or {})
    tick(st)
    return st


@scenario
def feature_branch_merge_local():
    """A ticket branch merged --no-ff into a feature branch with no remote yet is merged:
    terminal, worktree and branch go."""
    st = feature_cleanup("fl", "feat / fl", pushed=False)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "fl" not in st["workers"]
    (deleted,) = [r for r in calls("git") if r["args"][0] == "update-ref"]
    assert deleted["args"] == ["update-ref", "-d", "refs/heads/task/feat/fl", "c0ffee1"]


@scenario
def feature_branch_merge_pushed():
    """Merged into origin's feature branch, with the ticket branch pushed at that tip."""
    pushed = {
        "ls-remote --heads origin refs/heads/task/feat/fp": [0, "c0ffee1\trefs/x\n"],
        "merge-base --is-ancestor c0ffee1 beef": [0, ""],
    }
    st = feature_cleanup("fp", "feat / fp", pushed=True, **pushed)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "fp" not in st["workers"]


@scenario
def trial2_merge_local_before_push():
    """Live: merged --no-ff into the local feature branch before origin's got the merge
    read as unmerged. Either feature branch holding every commit is merged; neither, or
    the pushed ticket branch past both, still refuses."""
    local_only = {
        "merge-base --is-ancestor HEAD beef": [1, ""],
        "merge-base --is-ancestor HEAD f00d": [0, ""],
        "ls-remote --heads origin refs/heads/task/feat/fm": [0, "c0ffee1\trefs/x\n"],
        "merge-base --is-ancestor c0ffee1 f00d": [0, ""],
    }
    st = feature_cleanup("fm", "feat / fm", pushed=True, **local_only)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "fm" not in st["workers"]
    for change, verdict in (
        ({"merge-base --is-ancestor HEAD f00d": [1, ""]}, "unmerged"),
        ({"merge-base --is-ancestor c0ffee1 f00d": [1, ""]}, "remote-ahead"),
    ):
        st = feature_cleanup("fm", "feat / fm", pushed=True, **{**local_only, **change})
        assert calls("orca", ["worktree", "rm"]) == [] and "fm" in st["workers"]
        assert sends()[0][1].endswith(f"its worktree is {verdict}. Not removed"), (
            sends()
        )


@scenario
def feature_branch_commit_after_merge():
    """A commit on the ticket branch after its merge keeps everything: STUCK unmerged."""
    after = {"merge-base --is-ancestor HEAD f00d": [1, ""]}
    st = feature_cleanup("fa", "feat / fa", pushed=False, **after)
    assert calls("orca", ["worktree", "rm"]) == [] and "fa" in st["workers"]
    assert sends()[0][1].endswith("its worktree is unmerged. Not removed"), sends()


@scenario
def done_record_holds_no_slot():
    """A finished ticket kept for unmerged work: its own terminal is closed at once, its
    record stays for the worktree and holds no slot, and a later tick reports nothing."""
    after = {"merge-base --is-ancestor HEAD f00d": [1, ""]}
    st = feature_cleanup("fs", "feat / fs", pushed=False, **after)
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-fs"], closed
    assert calls("orca", ["worktree", "rm"]) == [] and "fs" in st["workers"]
    assert sends()[0][1].endswith("its worktree is unmerged. Not removed"), sends()
    world(local(), git=git, gh={})
    tick(st)
    assert "fs" in st["workers"] and sends() == [], sends()
    set_fixture()
    claude_usage("c1", 10, NOW + 600)
    assert pick(st["workers"]) == "pro1", "pro1 holds no Opus for a finished ticket"
    clear_usage()


@scenario
def task_branch_without_commits_counts_as_merged():
    """C6b: a document-only ticket's task branch has no commits of its own and no feature
    branch exists anywhere. HEAD inside origin's default branch counts as merged."""
    none = {
        "ls-remote --heads origin refs/heads/feature/feat": [0, ""],
        "rev-parse -q --verify refs/heads/feature/feat": [1, ""],
        "ls-remote origin HEAD": [0, "beadf00d\tHEAD\n"],
        "merge-base --is-ancestor HEAD beadf00d": [0, ""],
    }
    st = feature_cleanup("fd", "feat / fd", pushed=False, **none)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "fd" not in st["workers"]
    assert sends() == [], sends()


@scenario
def task_branch_with_own_commits_is_kept():
    """Commits of its own in neither the feature branch nor origin's default branch: the
    worktree and branch are never removed."""
    own = {
        "ls-remote --heads origin refs/heads/feature/feat": [0, ""],
        "rev-parse -q --verify refs/heads/feature/feat": [1, ""],
        "ls-remote origin HEAD": [0, "beadf00d\tHEAD\n"],
        "merge-base --is-ancestor HEAD beadf00d": [1, ""],
    }
    st = feature_cleanup("fk", "feat / fk", pushed=False, **own)
    assert calls("orca", ["worktree", "rm"]) == [] and "fk" in st["workers"]
    assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []
    assert sends()[0][1].endswith("its worktree is unmerged. Not removed"), sends()


@scenario
def declared_task_branch_of_another_feature_is_checked():
    """Live 2026-11-17: assistant-send-shortcut-ci's Branch line named a task branch whose
    feature differs from its title's, and every cleanup raised "could not be checked". The
    owned task branch names the feature CHECK tests it against, recorded or from the
    brief."""
    branch = "task/send-shortcut/lc"
    notes = f"Branch: `{branch}` from `feature/send-shortcut`"
    fixture_git = {
        **git,
        "rev-parse --abbrev-ref HEAD": [0, f"{branch}\n"],
        f"ls-remote --heads origin refs/heads/{branch}": [0, ""],
        "ls-remote --heads origin refs/heads/feature/send-shortcut": [
            0,
            "f00d\trefs/heads/feature/send-shortcut\n",
        ],
        "rev-parse -q --verify refs/heads/feature/send-shortcut": [0, "f00d\n"],
        "merge-base --is-ancestor HEAD f00d": [0, ""],
        f"update-ref -d refs/heads/{branch} c0ffee1": [0, ""],
    }
    reset_db()
    ticket_row("lc", status="closed", title="assistant / lc", notes=notes)
    st = fresh_state(lc=rec("lc", "c1", owns_worktree=True, owned_branch=branch))
    world(local(term("term-lc", 100)), git=fixture_git, gh={})
    tick(st)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "lc" not in st["workers"], sends()
    assert sends() == [], sends()
    # A record from before launches recorded their branch: the brief allows this branch,
    # but its launch may have owned another one, so it is checked and kept.
    reset_db()
    ticket_row("lc", status="closed", title="assistant / lc", notes=notes)
    st = fresh_state(lc=rec("lc", "c1", owns_worktree=True, owned_branch=None))
    world(local(term("term-lc", 100)), git=fixture_git, gh={})
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [] and "lc" in st["workers"], sends()
    assert [s[1] for s in sends()] == [
        f"STUCK lc: ticket is closed but its worktree has {branch} checked out, not its own "
        "branch, which its launch did not record. Not removed"
    ], sends()


DETACHED = {
    **git,
    "rev-parse --abbrev-ref HEAD": [0, "HEAD\n"],
    STATUS: [0, "?? results.txt\n"],
    "status --porcelain --untracked-files=no": [0, ""],
    "ls-remote --heads origin refs/heads/HEAD": [0, ""],
    "ls-remote origin HEAD": [0, "beadf00d\tHEAD\n"],
    "merge-base --is-ancestor HEAD beadf00d": [0, ""],
}


def detached_cleanup(tid, host="local", gh=None, record=None, **answers):
    """Tick a complete report ticket whose worktree is a detached checkout, on that host."""
    reset_db()
    ticket_row(tid, status="complete", title=f"feat / {tid}")
    st = fresh_state(**{tid: rec(tid, "c3", host=host, **{"owned_branch": None, **(record or {})})})
    row, screen = term(f"term-{tid}", 100)
    show = {"worktree": {"head": "c0ffee1"}}
    world(
        [(host, f"term-{tid}", row, screen)],
        git={**DETACHED, **answers},
        gh=gh or {},
        worktree_show=show,
        untracked=True,
    )
    tick(st)
    return st


@scenario
def detached_report_checkout_is_kept_quietly():
    """Live: ci-pipeline-07 and -17, report tickets in detached checkouts, raised "its
    worktree is unmerged". A launch into a detached checkout does not own it: its terminal
    closes and its record goes with no STUCK, here and on a remote host, and the worktree,
    its untracked report and every branch stay."""
    for host in ("local", "Juzzark-WSL"):
        st = detached_cleanup("dr", host, record={"owns_worktree": False})
        assert sends() == [] and "dr" not in st["workers"], (host, sends())
        assert calls("orca", ["worktree", "rm"]) == [], host
        assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []


@scenario
def detached_checkout_with_own_commits_is_kept():
    """A detached HEAD outside origin's default branch holds commits of its own: STUCK
    unmerged, nothing removed."""
    flag = " --json headRefOid --jq .[].headRefOid"
    gh = {
        "pr list --head HEAD --state merged" + flag: [0, ""],
        "pr list --head HEAD --state open --json number --jq .[].number": [0, ""],
    }
    own = {STATUS: [0, ""], "merge-base --is-ancestor HEAD beadf00d": [1, ""]}
    st = detached_cleanup("du", gh=gh, **own)
    assert calls("orca", ["worktree", "rm"]) == [] and "du" in st["workers"]
    assert sends() == [
        ("term-orch", "STUCK du: ticket is complete but its worktree is unmerged. Not removed", True)
    ], sends()


@scenario
def detached_task_checkout_keeps_its_untracked_files():
    """A worker launched on its task branch detaches at a commit on origin's default branch
    and leaves untracked files there: cleanup keeps the worktree and its files, as it does
    for a record from before launches recorded their branch, with or without owns_worktree."""
    for record in (
        {"owns_worktree": True, "owned_branch": "task/feat/dk"},
        {"owns_worktree": True},
        {},
    ):
        st = detached_cleanup("dk", record=record)
        assert calls("orca", ["worktree", "rm"]) == [] and "dk" in st["workers"], record


@scenario
def finished_review_ticket_raises_nothing():
    """Live: runs-pause-stop-state-02-review on Juzzark-WSL raised "complete but its worktree
    could not be checked" once its coder's worktree was gone. A review ticket never has
    commits, and a worktree that no longer exists holds nothing to clean: its terminal
    closes and its record goes, with no STUCK, here and on a remote host."""
    for host in ("local", "Juzzark-WSL"):
        reset_db()
        ticket_row("rv-review", status="complete", title="feat / rv-review")
        gone = tmp / "wt" / "gone-rv"
        worker = rec("rv-review", "x1", agent="codex", host=host, worktree=str(gone))
        shutil.rmtree(gone)
        st = fresh_state(**{"rv-review": worker})
        row, screen = term("term-rv-review", 100, ident="codex", worktree=str(gone))
        world(
            [(host, "term-rv-review", row, screen)],
            git=git,
            gh=merged,
            worktree_show="missing",
            probe_fails=True,
        )
        tick(st)
        assert sends() == [] and notes() == [], (host, sends(), notes())
        assert "rv-review" not in st["workers"] and calls("orca", ["worktree", "rm"]) == []
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        assert closed == ["term-rv-review"], (host, closed)


@scenario
def unreachable_worktree_is_one_stuck():
    """A worktree Orca still lists that no probe reaches is one STUCK: none again on later
    ticks, after the recheck interval, or when its head moves."""
    reset_db()
    ticket_row("ur", status="complete")
    st = fresh_state(ur=rec("ur", "c3", host="Juzzark-WSL"))
    row, screen = term("term-ur", 100)
    show = {"worktree": {"head": "c0ffee1"}}
    world([("Juzzark-WSL", "term-ur", row, screen)], worktree_show=show, probe_fails=True)
    tick(st)
    stuck = "STUCK ur: ticket is complete but its worktree could not be checked"
    assert sends() == [("term-orch", stuck, True)], sends()
    assert len(notes()) == 1 and stuck in notes()[0], notes()
    for later, head in ((60, "c0ffee1"), (da.RECHECK + 60, "c0ffee1"), (2 * da.RECHECK, "d00d")):
        world([], worktree_show={"worktree": {"head": head}}, probe_fails=True)
        tick(st, NOW + later)
        assert sends() == [] and notes() == [], (later, sends(), notes())
    assert "ur" in st["workers"] and calls("orca", ["worktree", "rm"]) == []


@scenario
def feature_branch_names_another_feature():
    """The title's feature and the branch's disagree: no guess, not even from a merged
    PR. STUCK unchecked."""
    pr = {MERGED_PRS.replace("feat/a", "task/feat/fo"): [0, "abc\n"]}
    st = feature_cleanup("fo", "other / fo", pushed=False, gh=pr, owned=False)
    assert calls("orca", ["worktree", "rm"]) == [] and "fo" in st["workers"]
    assert sends()[0][1].endswith("its worktree could not be checked"), sends()


@scenario
def grok_worker():
    """Grok: its launch line carries the ledger identity in the env its user-scope
    Concord server inherits; slots come from its row, and a live Grok dev-auto did not
    launch holds one; it runs on this Mac only."""
    set_fixture()
    ticket = {"task_id": "t-1", "title": "served pages / Settings wiring"}
    worker = rec(
        "t-1",
        "gk",
        agent="grok",
        model="grok-4.7",
        effort="xhigh",
        agent_id="grok:settings-wiring",
    )
    text = da.brief(worker, ticket)
    command = da.worker_command(cfg, root, worker, da.account(cfg, "gk", "local"), text)
    entry = run_command(command)
    assert entry["cmd"] == "grok", entry
    concord = {k: v for k, v in entry["env"].items() if k.startswith("CONCORD_")}
    assert concord == {
        "CONCORD_REPO_ROOT": str(root),
        "CONCORD_AGENT_ID": "grok:settings-wiring",
        "CONCORD_AGENT_KIND": "grok",
        "CONCORD_TELEMETRY_DISABLED": "1",
        "CONCORD_NO_UPDATE_CHECK": "1",
    }, concord
    a = entry["args"]
    assert a == [
        "-m",
        "grok-4.7",
        "--effort",
        "xhigh",
        "--permission-mode",
        "bypassPermissions",
        "--trust",
        "--no-leader",
        text,
    ], a
    assert text.startswith("/goal Finish Concord ticket t-1"), text
    set_fixture()
    assert pick({}, agent="grok", model="grok-4.7") == "gk"
    one = {"a": rec("a", "gk", agent="grok", model="grok-4.7")}
    assert pick(one, agent="grok", model="grok-4.7") == "gk"
    two = {**one, "b": rec("b", "gk", agent="grok", model="grok-4.7")}
    assert pick(two, agent="grok", model="grok-4.7") is None, "row slots"
    set_fixture(ps="444 grok -m grok-4.7 --trust go ORCA_TERMINAL_HANDLE=t-g\n")
    assert pick(one, agent="grok", model="grok-4.7") is None, "a live Grok holds one"
    set_fixture(ps="444 grok -m grok-4.7 go ORCA_TERMINAL_HANDLE=term-a\n")
    assert pick(one, agent="grok", model="grok-4.7") == "gk", "its own worker"
    set_fixture()
    done = cli("slot", "--agent", "grok", "--model", "grok-4.7")
    assert (done.returncode, done.stdout.split("\t")[0]) == (0, "gk"), done
    done = cli(
        *("launch", "t-1", "--agent", "grok", "--model", "grok-4.7"),
        *("--worktree", str(tmp), "--host", "Juzzark-WSL"),
    )
    assert done.returncode and "this Mac" in done.stderr, done


@scenario
def grok_wake_sets_goal():
    """A Grok worker's wake sets its goal again with /goal before the wake line."""
    reset_db()
    ticket_row("gw")
    ticket_row("gw-review", agent="grok:gw-review")
    reviewer = rec("gw-review", "gk", agent="grok", model="grok-4.7")
    st = fresh_state(**{"gw-review": reviewer})
    update("gw", "finding", "REVIEW READY 1a2b3c4")
    world(local(term("term-gw-review", 5, ident="grok")))
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket gw. Review it as your ticket says."
    )
    typed = f"/goal {da.goal_line('gw-review')} {wake}"
    assert sends() == [("term-gw-review", typed, True)], sends()


CLAUDE_CONCORD_LOST = (
    "If the Concord tools are unavailable, the goal is also met once you write the "
    "handoff to the file the handoff skill names for a session without a ticket and end "
    "your turn, without retrying them."
)


@scenario
def claude_goal_writes_handoff_when_concord_is_lost():
    """A Claude worker's goal, at launch and on a wake, tells it to write the handoff
    file and stop when Concord is unavailable; Codex, Cursor and Grok goals never do."""
    goal = da.goal_line("gh", "T")
    assert da.armed({"agent": "claude"}, goal, "go").endswith(f" {CLAUDE_CONCORD_LOST} go")
    told = da.brief({"agent": "claude"}, {"task_id": "gh", "title": "T"})
    assert CLAUDE_CONCORD_LOST in told, told
    for agent in ("codex", "cursor", "grok"):
        typed = da.armed({"agent": agent}, goal, "go")
        assert "Concord tools are unavailable" not in typed, (agent, typed)


@scenario
def codex_goal_never_asks_for_a_repeated_wait_note():
    """C7: Codex's goal is met by an unanswered Waiting on note and says to mark the goal
    complete; Claude's keeps the new Waiting on note."""
    goal = da.goal_line("gq", "T")
    assert da.armed({"agent": "claude"}, goal, "go") == (
        "/goal Finish Concord ticket gq (T) as its brief defines done, or record a new "
        "Waiting on: blocker note on it after this goal is set, then end your turn. "
        f"{CLAUDE_CONCORD_LOST} go"
    )
    codex = da.armed({"agent": "codex"}, goal, "go")
    assert codex == (
        "/goal Finish Concord ticket gq (T) as its brief defines done, or until it holds "
        "a Waiting on: blocker note that nothing has answered yet. Record such a note "
        "only when none is open, then mark this goal complete and end your turn. go"
    ), codex
    ticket = {"task_id": "gq", "title": "T"}
    told = da.brief({"agent": "codex"}, ticket)
    assert "record a new" not in told and "never repeat a Waiting on note" in told, told
    assert told.startswith(codex.removesuffix(" go")), told


@scenario
def pair_reviewer_woken():
    """REVIEW READY on the coder's ticket wakes the reviewer under <ticket>-review once."""
    reset_db()
    ticket_row("pc")
    ticket_row("pc-review", agent="cursor:pc-review")
    reviewer = rec("pc-review", "cur", agent="cursor", model="grok-4.7-high")
    st = fresh_state(**{"pc-review": reviewer})
    screen = local(term("term-pc-review", 5, ident="cursor"))
    update("pc", "finding", "REVIEW READY 1a2b3c4")
    world(screen)
    tick(st)
    text = (
        "dev-auto: REVIEW READY was posted on ticket pc. Review it as your ticket says."
    )
    wake = woke("pc-review", text, "cursor")
    assert sends() == [("term-pc-review", wake, True)], sends()
    world(screen)
    tick(st)
    assert sends() == []
    second = update("pc", "finding", "REVIEW READY 5e6f7a8")
    world(screen)
    tick(st)
    # A repeat of a typed text carries its event.
    again = woke("pc-review", f"{text} Event ready {second}.", "cursor")
    assert sends() == [("term-pc-review", again, True)]


@scenario
def ready_wake_is_the_ticks_only_wake():
    """A quiet reviewer with no wait note, woken for REVIEW READY, gets no silence nudge
    in the same tick."""
    reset_db()
    ticket_row("nr")
    ticket_row("nr-review", agent="codex:nr-review")
    reviewer = rec("nr-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"nr-review": reviewer})
    update("nr", "progress", "REVIEW READY 1a2b3c4")
    world(local(term("term-nr-review", 7200, ident="codex")))
    tick(st)
    text = (
        "dev-auto: REVIEW READY was posted on ticket nr. Review it as your ticket says."
    )
    assert sends() == [("term-nr-review", woke("nr-review", text, "codex"), True)], (
        sends()
    )


@scenario
def trial2_reviewed_sha_no_wake():
    """Live: the reviewer was woken for a REVIEW READY it had already given its verdict
    on. A verdict for another SHA still leaves the wake."""
    reset_db()
    ticket_row("pv")
    ticket_row("pv-review", agent="codex:pv-review")
    reviewer = rec("pv-review", "x1", agent="codex", model="gpt-6.1-sol")
    screen = local(term("term-pv-review", 5, ident="codex"))
    update("pv", "progress", "REVIEW READY aa41913ba190 Changed two files.")
    push = update(
        "pv", "finding", "REVIEW VERDICT: PUSH aa41913 no findings", "codex:pv-review"
    )
    st = fresh_state(**{"pv-review": reviewer})
    st["cleared_floor"] = push  # that verdict was cleared before this history
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("pv", "progress", "REVIEW READY 5e6f7a8")
    world(screen)
    tick(st)
    assert [t for t, _, _ in sends()] == ["term-pv-review"], sends()


@scenario
def trial_limited_reviewer_woken_after_reset():
    """Live: the Cursor reviewer sat at its weekly limit when REVIEW READY was posted, and
    a wait with no readable usage never ended. It is relaunched at the screen's reset,
    then woken for the READY."""
    reset_db()
    ticket_row("tr", agent="codex:tr")
    ticket_row("tr-review", agent="cursor:tr-review")
    st = fresh_state(
        **{"tr-review": rec("tr-review", "cur", agent="cursor", model="g", since=0)}
    )
    world(local(term("term-tr-review", 120, CURSOR_LIMIT, ident="cursor")))
    tick(st)
    ready = "REVIEW READY 52167903148d45fd6231111af3e47843a7f1d158"
    update("tr", "progress", ready, agent="codex:tr")
    assert st["workers"]["tr-review"].get("limit_reset") == NOW + 86400
    late = NOW + 86400 + 300
    world(local(term("term-tr-review", 86400 + 400, CURSOR_LIMIT, ident="cursor")))
    tick(st, late)
    assert sends() == [] and len(created()) == 1, "relaunched, never typed into"
    wt = st["workers"]["tr-review"]["worktree"]
    shown = term("term-local-1", -86295, ["› "], ident="cursor", worktree=wt)
    world(local(shown))
    tick(st, late + 60)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket tr. Review it as your ticket says."
    )
    assert sends() == [("term-local-1", woke("tr-review", wake, "cursor"), True)], (
        sends()
    )


@scenario
def projects_kept_apart():
    """Tickets are keyed by (project, task_id): another project's updates never leak in."""
    reset_db()
    ticket_row("dup")
    ticket_row("dup", project="other")
    update("dup", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", project="other")
    st = fresh_state(dup=rec("dup", "c1"))
    world(local(term("term-dup", 5)))
    tick(st)
    assert sends() == []
    tickets = da.read_tickets(root)
    assert [len(tickets[(p, "dup")]["updates"]) for p in ("", "other")] == [0, 1]
    done = cli("launch", "dup", "--agent", "claude", "--model", OPUS, "--worktree", "x")
    assert done.returncode and "several projects" in done.stderr, done


@scenario
def minor_cleanup_refusals():
    """A failed git or gh step, the main checkout or a moved branch never loses work."""
    reset_db()
    ticket_row("km", status="complete")
    for change, gh, word in (
        ({STATUS: [128, ""]}, merged, "could not be checked"),
        ({}, {MERGED_PRS: [1, ""]}, "could not be checked"),
        (
            {
                "rev-parse --path-format=absolute --git-common-dir": [
                    0,
                    f"{tmp / 'wt' / 'km'}/.git\n",
                ]
            },
            merged,
            "is the main checkout",
        ),
    ):
        answers = {**git, **change}
        common = answers["rev-parse --path-format=absolute --git-common-dir"][1].strip()
        st = fresh_state(km=rec("km", "c1", owned_common=common))
        world(local(term("term-km", 100)), git=answers, gh=gh)
        tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and "km" in st["workers"]
        assert word in sends()[0][1], sends()
    st = fresh_state(km=rec("km", "c1"))
    moved = {**git, "update-ref -d refs/heads/feat/a c0ffee1": [1, ""]}
    world(local(term("term-km", 100)), git=moved, gh=merged)
    tick(st)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "km" not in st["workers"]
    assert sends()[0][1].startswith(
        "STUCK km: its worktree was removed, but its branch"
    )


@scenario
def refused_cleanup_checked_once_until_something_changes():
    """C9, R17: five ticks after a cleanup STUCK run its check once. A new commit, a ticket
    update, another terminal in the worktree, or 15 minutes runs it again; the
    supervisor's own note does not."""
    reset_db()
    ticket_row("rc", status="complete")
    st = fresh_state(rc=rec("rc", "c1"))
    head = {}

    def checks(now, *extra):
        world(local(term("term-rc", 100), *extra), git={**git, **head}, gh=no_pr)
        tick(st, now)
        return sum(" ".join(r["args"]) == STATUS for r in calls("git"))

    assert checks(NOW) == 1
    assert sends()[0][1].endswith("its worktree is unmerged. Not removed"), sends()
    assert [checks(NOW + 60 * n) for n in range(1, 5)] == [0, 0, 0, 0]
    head["rev-parse HEAD"] = [0, "c0ffee2\n"]
    assert [checks(NOW + 300), checks(NOW + 360)] == [1, 0], "a new commit"
    update("rc", "progress", "pushed the rest")
    assert [checks(NOW + 420), checks(NOW + 480)] == [1, 0], "a ticket update"
    update("rc", "finding", "dev-auto: STUCK rc", agent=da.SUPERVISOR)
    assert checks(NOW + 540) == 0, "the supervisor's own note"
    shell = term("term-op", 50, ["$ "], ident=None, worktree=str(tmp / "wt" / "rc"))
    assert [checks(NOW + 600, shell), checks(NOW + 660, shell)] == [1, 0], "a terminal"
    assert checks(NOW + 600 + 15 * 60, shell) == 1, "15 minutes"
    assert "rc" in st["workers"]
    agent = term("term-op", 50, ["$ "], ident="claude", worktree=str(tmp / "wt" / "rc"))
    later = NOW + 600 + 16 * 60
    # An agent in that terminal refuses before the check; once it leaves, the check runs.
    assert [checks(later, agent), checks(later + 60, shell)] == [0, 1], "its agent left"

    # A remote head is read from Orca's worktree record, with no probe terminal.
    ticket_row("rr", status="complete")
    st = fresh_state(rr=rec("rr", "c3", host="Juzzark-WSL"))
    mine = term("term-rr", 100)

    def remote(now, sha):
        shown = {"worktree": {"head": sha}}
        world([("Juzzark-WSL", "term-rr", *mine)], git=git, gh=no_pr, worktree_show=shown)
        tick(st, now)
        return sum(" ".join(r["args"]) == STATUS for r in calls("git"))

    assert [remote(NOW, "c0ffee1"), remote(NOW + 60, "c0ffee1")] == [1, 0]
    assert remote(NOW + 120, "c0ffee2") == 1, "a new commit on the host"


@scenario
def minor_limit_text_and_reset():
    """Limit text above later output is content; a wake waits for the last reset."""
    reset_db()
    ticket_row("lt")
    st = fresh_state(lt=rec("lt", "c1"))
    old = ["You've hit your limit · resets 5pm", *[f"⏺ line {n}" for n in range(6)]]
    world(local(term("term-lt", 120, old)))
    tick(st)
    assert created() == [] and sends() == [], "limit text above later output"
    claude_usage("c1", 20, NOW + 3600)
    world(local(term("term-lt", 120, ["grep says: hit your limit"])))
    tick(st)
    assert created() == [] and sends() == [], "output quoting the limit"
    clear_usage()
    for tid in ("lv", "g2", "g3", "g4"):
        ticket_row(tid)
        update(tid, "blocker", "Waiting on: review")
    claude_usage("c1", 100, NOW + 3600, week=95)
    st = fresh_state(
        lv=rec("lv", "c1"), g2=rec("g2", "c2"), g3=rec("g3", "c3"), g4=rec("g4", "c3")
    )
    limit = term("term-lv", 120, ["You've hit your limit · resets 5pm"])
    idle = [term(f"term-{t}", 120) for t in ("g2", "g3", "g4")]
    world(local(limit, *idle))
    tick(st)
    assert st["workers"]["lv"]["limit_reset"] == NOW + 86400, st["workers"]["lv"]
    world(local(term("term-lv", 5, ["You've hit your limit · resets 5pm"]), *idle))
    tick(st, NOW + 60)
    assert "limit_reset" in st["workers"]["lv"], "a redraw keeps the wait"
    clear_usage()


@scenario
def minor_waits_and_receipts():
    """Untrackable waits are STUCK once; an early verdict counts; refused sends retry."""
    reset_db()
    ticket_row("uw")
    note = update("uw", "blocker", "Waiting on: Codex reviewer verdict")
    ticket_row("nc")
    ci = update("nc", "blocker", "Waiting on: ci")
    ticket_row("ev")
    ticket_row("ev-review", agent="codex:ev-review")
    update("ev", "progress", "REVIEW READY 1111aaa")
    update("ev", "finding", "REVIEW VERDICT: FIX", agent="codex:ev-review")
    update("ev", "blocker", "Waiting on: review")
    st = fresh_state(uw=rec("uw", "c1"), nc=rec("nc", "c2"), ev=rec("ev", "c3"))
    terms = local(term("term-uw", 100), term("term-nc", 100), term("term-ev", 100))
    world(terms, gh={CHECKS: [1, ""]})
    tick(st)
    assert sorted(sends()) == [
        (
            "term-ev",
            woke(
                "ev",
                "dev-auto: a review verdict was posted on ticket ev. Read it there and continue.",
            ),
            True,
        ),
        (
            "term-orch",
            f"STUCK nc: dev-auto cannot track its Waiting on note, update {ci} on the ticket",
            True,
        ),
        (
            "term-orch",
            f"STUCK uw: dev-auto cannot track its Waiting on note, update {note} on the ticket",
            True,
        ),
    ], sends()
    world(terms, gh={CHECKS: [1, ""]})
    tick(st)
    assert sends() == []
    ticket_row("rx")
    update("rx", "blocker", "Waiting on: operator")
    st = fresh_state(rx=rec("rx", "c1"))
    world(local(term("term-rx", 100)), send_refused=["term-orch"])
    tick(st)
    assert [s[1][:11] for s in sends()] == ["NEEDS YOU r"], sends()
    assert st["events_new"] == 1, "a refused EVENTS wake stays pending"
    st = fresh_state(gone=rec("gone", "c1"))
    world(local())
    tick(st)
    assert "gone" not in st["workers"], "a missing ticket's record goes once reported"


@scenario
def minor_round_two():
    """N3 handles kept after a rebind, N4 a read verdict, N5 a failed launch, N7 an
    untypable id, N8 a symlinked worktree, N10 a reused session pid."""
    held = {
        "a": rec("a", "x2", agent="codex", terminal="term-new", handles=["term-old"])
    }
    set_fixture(
        ps=f"222 codex ORCA_TERMINAL_HANDLE=term-old CODEX_HOME={homes['x2']}\n"
    )
    assert da.foreign(cfg, held)["x2"] == [], "a rebound worker is counted once"
    sessions = homes["c1"] / "sessions"
    sessions.mkdir()
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "sessionId": "s-old"}))
    set_fixture(ps="77 /usr/bin/python3 server.py\n")
    assert da.foreign(cfg, {})["c1"] == [], "a reused pid is not a Claude session"
    shutil.rmtree(sessions)

    reset_db()
    ticket_row("ov")
    update("ov", "finding", "REVIEW VERDICT: PUSH aaaaaaa", agent="codex:ov-review")
    update("ov", "progress", "saw the PUSH, adding a follow-up commit")
    update("ov", "blocker", "Waiting on: review of bbbbbbb")
    st = fresh_state(ov=rec("ov", "c1", since=0))
    world(local(term("term-ov", 100)))
    tick(st)
    assert [x for x in sends() if x[0] == "term-ov"] == [], "a read verdict is old"

    ticket_row("fl", status="proposed", title="f / fl")
    wt = str(tmp / "wt" / "fl")
    Path(wt).mkdir(parents=True, exist_ok=True)
    set_fixture()
    argv = ["launch", "fl", "--agent", "claude", "--model", OPUS, "--worktree", wt]
    done = cli(*argv, ORCA_CLI_COMMAND="false")
    assert (
        done.returncode == 1
        and "fl" not in json.loads(state_file.read_text())["workers"]
    )
    done = cli("launch", "a;b", "--agent", "claude", "--model", OPUS, "--worktree", wt)
    assert done.returncode and "will not type" in done.stderr, done
    state_file.unlink()

    ticket_row("sy", status="complete")
    link = tmp / "linked"
    link.symlink_to(tmp / "wt")
    st = fresh_state(sy=rec("sy", "c1"))
    shell = term("term-op-sy", 100, ident=None, worktree=str(link / "sy"))
    world(local(term("term-sy", 100), shell), git=git, gh=merged)
    tick(st)
    assert calls("orca", ["worktree", "rm"]) == [], (
        "a symlinked spelling is the same path"
    )


@scenario
def minor_resets_sum_their_units():
    """'try again in 3 days 4 hours' and 'resets in 2 hours 30 minutes' read in full."""
    assert da.screen_reset("Try again in 3 days 4 hours.", NOW) == (
        NOW + 3 * 86400 + 4 * 3600,
        True,
    )
    assert da.screen_reset("resets in 2 hours 30 minutes", NOW)[0] == NOW + 9000


@scenario
def minor_status_line_is_no_shell():
    """A status line such as 'opus@max: 87%' is no shell prompt; real prompts still are."""
    for line in ("me@mac 42%", "opus@max: 87%", "julian@x.com: 100%"):
        assert not da.SHELL.fullmatch(line), line
    for line in ("julian@mac sh %", "root@3f2a1b9c:/app#", "julian@Juzzark:~/x$", "%"):
        assert da.SHELL.fullmatch(line), line
    screen = {"status": "running", "lines": ["opus@max: 87%"], "source": "screen"}
    assert not da.exited(rec("st", "c1"), {}, screen, NOW)


@scenario
def minor_stream_tail_is_no_proof():
    """When Orca cannot render the screen, its stream tail never proves a bare shell."""
    reset_db()
    ticket_row("su")
    st = fresh_state(su=rec("su", "c1"))
    row, screen = term("term-su", 31 * 60, ["% "], ident=None)
    world(local((row, {**screen, "source": "screen-unavailable"})))
    tick(st)
    assert created() == [] and calls("orca", ["terminal", "close"]) == []


@scenario
def minor_default_shell_needs_orca_created_at():
    """Only Orca's createdAt dates a worktree: a new folder without it, or a createdAt in
    the future, records no default shell."""
    reset_db()
    state_file.unlink(missing_ok=True)
    wt = tmp / "wt" / "dn"
    wt.mkdir(parents=True, exist_ok=True)
    prompt = ["➜  dn git:(feat/dn)"]
    shell = term("term-dn", 5, prompt, ident=None, worktree=str(wt))
    ticket_row("dn", status="proposed", title="trial / dn")
    argv = ["launch", "dn", "--agent", "codex", "--model", "m", "--worktree", str(wt)]
    for show in ({}, just_created(-60)):
        state_file.unlink(missing_ok=True)
        world(local(shell), worktree_show=show)
        da.main(argv)
        assert json.loads(state_file.read_text())["workers"]["dn"]["shells"] == [], show
    state_file.unlink()


@scenario
def minor_limit_needs_its_own_message():
    """A bulleted line about the limit marks nothing, and the reset comes from the limit
    message, not from other output on the screen."""
    reset_db()
    ticket_row("cm", agent="cursor:cm")
    for screen, limits in (
        (["- Usage limit reached handling is in usage_limit()."], {}),
        (
            ["Fixed: resets in 400 days parse.", "*Weekly usage limit reached.*"],
            {"cur": NOW + 86400},
        ),
    ):
        st = fresh_state(cm=rec("cm", "cur", agent="cursor", model="g"))
        world(local(term("term-cm", 120, screen, ident="cursor")))
        tick(st)
        assert st["limits"] == limits, (screen, st["limits"])


@scenario
def minor_earlier_state_is_read():
    """Records saved by earlier versions tick without a crash: a Claude worker with a
    Codex account, a limit wait saved as None, a plain shell stored with no screen.
    That shell is still closed with the worktree."""
    reset_db()
    for tid in ("o1", "o2"):
        ticket_row(tid)
    ticket_row("o3", status="complete")
    old = rec("o1", "c1", codex_account="x1", codex_home=str(homes["x1"]))
    waited = rec("o2", "cur", agent="cursor", model="g", limit_reset=None)
    shells = [{"terminal": "term-o3-shell", "pty": "pty-term-o3-shell"}]
    st = fresh_state(o1=old, o2=waited, o3=rec("o3", "c2", shells=shells))
    wt3 = str(tmp / "wt" / "o3")
    world(
        local(
            term("term-o1", 300, ["% "], ident=None),
            term("term-o2", 120, ["Usage limit reached."], ident="cursor"),
            term("term-o3", 100),
            term("term-o3-shell", 100, ["% "], ident=None, worktree=wt3),
        ),
        git=git,
        gh=merged,
    )
    tick(st)
    assert len(created()) == 2
    assert "term-o3-shell" in [
        opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])
    ]


@scenario
def host_down():
    """An unreachable host's workers are left alone."""
    reset_db()
    ticket_row("h1")
    st = fresh_state(h1=rec("h1", "c3", host="Juzzark-WSL"))
    set_fixture(terminals={"Juzzark-WSL": "down"})
    tick(st)
    assert created() == [] and sends() == []


WSL = "Juzzark-WSL"
RESTART = f"STUCK {WSL}: Orca restarted and lost 2 workers: ra rb. Each is left alone until a new launch"


def wsl_down():
    """The orchestrator listed as usual; Juzzark-WSL's terminal list fails."""
    world([])
    data = fixture()
    data["terminals"][WSL] = "down"
    set_fixture(**data)


def wsl_pair():
    reset_db()
    ticket_row("ra")
    ticket_row("rb")
    return fresh_state(ra=rec("ra", "c3", host=WSL), rb=rec("rb", "c3", host=WSL))


@scenario
def outage_is_one_stuck():
    """One failed listing raises nothing; a host that keeps failing is one STUCK naming it and
    its worker count, then nothing until it answers, and the next outage is a new STUCK. Its
    workers get no event meanwhile."""
    st = wsl_pair()
    said = []
    for step, down in enumerate([True, True, True, False, True, True]):
        if down:
            wsl_down()
        else:
            world(on(WSL, term("term-ra", 100), term("term-rb", 100)))
        tick(st, NOW + 60 * step)
        typed = sends()
        if down:
            assert all(s[0] == "term-orch" for s in typed), typed
        said.append([s[1] for s in typed if s[0] == "term-orch"])
    stuck = f"STUCK {WSL}: its terminal list failed twice in a row, its 2 workers wait until it answers"
    assert said == [[], [stuck], [], [], [], [stuck]], said
    assert not any(r.get("lost") for r in st["workers"].values())


@scenario
def restart_closes_restored_tabs_of_finished_worktrees():
    """At an Orca restart, an agent or bare shell Orca restored into the worktree of a
    finished ticket that no open record owns is closed; one in an open record's worktree
    that is a shell stays."""
    st = wsl_pair()
    done_wt = str(tmp / "wt" / "fin")
    st["worktrees"] = {f"{WSL}|{done_wt}": {"host": WSL, "worktree": done_wt, "ticket": "fin", "at": 0}}
    world(
        on(
            WSL,
            term("term-r1", 100, ["% "], ident=None, worktree=done_wt),
            term("term-r2", 100, ident="codex", worktree=done_wt),
            term("term-r3", 100, ["% "], ident=None, worktree=str(tmp / "wt" / "ra")),
        )
    )
    tick(st)
    assert sorted(h for h, _host in closes()) == ["term-r1", "term-r2"], closes()


@scenario
def restart_is_one_stuck():
    """A host that answers again with every recorded handle gone is one STUCK listing its lost
    tickets, not one per ticket, and nothing on later ticks; the same when the restart fell
    between two ticks and no failed listing was seen."""
    for seen_down in (True, False):
        st = wsl_pair()
        if seen_down:
            wsl_down()
            tick(st)
            assert sends() == []
        world([])
        tick(st, NOW + 60)
        assert [s[1] for s in sends()] == [RESTART], (seen_down, sends())
        assert all(st["workers"][t].get("lost") for t in ("ra", "rb"))
        world([])
        tick(st, NOW + 120)
        assert sends() == [] and created() == [], "reported once"


@scenario
def sent_forgets_events_that_are_over():
    """dev-auto-fixes-50 (i): st["sent"] keeps no key whose event is over: an earlier
    interval's CHECK-IN, an outage the host answered after, a restart STUCK once told,
    and a CLEARED REVIEW once told. None of them is told again."""
    def kinds(st):
        return sorted(k for k in st["sent"] if k.split("|")[1] in ("down", "restart", "cleared") or k.startswith("checkin|"))

    st = wsl_pair()
    for step in range(2):
        wsl_down()
        tick(st, NOW + 60 * step)
    assert [k for k in st["sent"] if "|down|" in k], st["sent"]
    world(on(WSL, term("term-ra", 100), term("term-rb", 100)))
    tick(st, NOW + 120)
    assert kinds(st) == [], st["sent"]
    wsl_down()
    tick(st, NOW + 180)
    world([])
    tick(st, NOW + 240)
    assert [s[1] for s in sends()] == [RESTART], sends()
    assert kinds(st) == [], st["sent"]
    world([])
    tick(st, NOW + 300)
    assert sends() == [], sends()

    reset_db()
    ticket_row("sk", status="complete")
    st = fresh_state()
    update("sk", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", agent="codex:sk-review")
    world([])
    tick(st)
    assert sends() == [("term-orch", "CLEARED REVIEW sk: PUSH 1a2b3c4", True)], sends()
    every = {**cfg, "checkin_minutes": 30}
    for n in range(1, 4):
        world([])
        da.tick(every, root, st, da.read_tickets(root), NOW + n * 1800)
        assert [s[1][:9] for s in sends()] == ["CHECK-IN:"], (n, sends())
        assert kinds(st) == [], (n, kinds(st))
    # trial2_checkin reads every CHECK-IN in this shared log.
    (st.path.parent / "delivered.log").unlink(missing_ok=True)


@scenario
def restart_of_a_lone_worker_after_an_outage():
    """One worker on a host that failed to list and came back without it is a restart too."""
    reset_db()
    ticket_row("ra")
    st = fresh_state(ra=rec("ra", "c3", host=WSL))
    wsl_down()
    tick(st)
    world([])
    tick(st, NOW + 60)
    lone = f"STUCK {WSL}: Orca restarted and lost 1 workers: ra. Each is left alone until a new launch"
    assert [s[1] for s in sends()] == [lone], sends()


@scenario
def one_gone_terminal_keeps_its_own_stuck():
    """A worker whose terminal is gone while the host's other worker is listed keeps today's
    per-ticket STUCK, and an agent copy in its worktree is not closed."""
    st = wsl_pair()
    stray = term("term-copy", 100, worktree=st["workers"]["ra"]["worktree"])
    world(on(WSL, term("term-rb", 100), stray))
    tick(st)
    assert [s[1] for s in sends() if s[0] == "term-orch"] == [
        "STUCK ra claude:ra: terminal term-ra is not listed and no listed terminal has its pty. Left alone until a new launch",
    ], sends()
    assert closes() == []


@scenario
def restart_closes_restored_agents_once():
    """At a detected restart each unrecorded agent terminal in a recorded worktree on that host
    is closed with its tab, once; a shell there, an agent in another worktree and a recorded
    terminal are left alone. A close Orca refuses is named in the restart's STUCK."""
    st = wsl_pair()
    wa, wb = (st["workers"][t]["worktree"] for t in ("ra", "rb"))
    ticket_row("ro")
    st["workers"]["ro"] = rec("ro", "c3", host=WSL, worktree=wa, lost="STUCK ro: lost earlier")
    st["sent"][f"ro|lost|{NOW - 7200}"] = 1
    rows = on(
        WSL,
        term("term-copy-a", 100, worktree=wa),
        term("term-copy-b", 100, ident="codex", worktree=wb),
        term("term-shell-a", 100, ident=None, worktree=wa),
        term("term-elsewhere", 100, worktree=str(tmp / "wt" / "other")),
        term("term-ro", 100, worktree=wa),
    )
    world(rows, close_fails=["term-copy-b"])
    tick(st)
    assert tab_closes() == [("term-copy-a", True), ("term-copy-b", True)], tab_closes()
    assert [s[1] for s in sends()] == [f"{RESTART}. Closing term-copy-b failed"], sends()
    world(rows)
    tick(st, NOW + 60)
    assert closes() == [] and sends() == [], "swept once"


@scenario
def restart_spares_a_rebound_lost_record():
    """Two workers vanish in a restart while a third, marked lost earlier, is listed again
    under a new handle with its launch pty: that pane is its own, never a restored copy."""
    st = wsl_pair()
    wa = st["workers"]["ra"]["worktree"]
    ticket_row("ro")
    st["workers"]["ro"] = rec("ro", "c3", host=WSL, worktree=wa, lost="STUCK ro: lost earlier")
    st["sent"][f"ro|lost|{NOW - 7200}"] = 1
    world(on(WSL, term("term-ro-new", 100, worktree=wa, pty="pty-term-ro")))
    tick(st)
    assert ("term-ro-new", WSL) not in closes(), closes()


def finished(hosts, **extra):
    """One tick in which cleanup closes each complete ticket's terminal ({ticket: host}), on
    feat/a, and drops its record. A record as launched holds no branch."""
    reset_db()
    workers = {}
    for tid, host in hosts.items():
        ticket_row(tid, status="complete")
        workers[tid] = rec(tid, "c3", host=host, **extra)
    st = fresh_state(**workers)
    rows = [(host, f"term-{t}", *term(f"term-{t}", 100)) for t, host in hosts.items()]
    world(rows, git=git, gh=merged)
    tick(st)
    assert sorted(worker_closes()) == sorted((f"term-{t}", h) for t, h in hosts.items()), closes()
    assert st["workers"] == {}, st["workers"]
    return st


def wt(tid):
    return str(tmp / "wt" / tid)


def worker_closes():
    """closes() without the probe terminals a worktree's checks run in."""
    return [c for c in closes() if not c[0].startswith(("term-local-", f"term-{WSL}-"))]


@scenario
def revived_pane_of_a_finished_ticket_is_closed_with_its_tab():
    """Orca revives a finished ticket's agent pane after its record dropped: under its launch
    ptyId, or as a new pane in the same worktree on the same branch. Each is closed with its
    tab within one tick, on every host, with one line each."""
    for host in ("local", WSL):
        st = finished({"rv": host})
        rows = on(
            host,
            term("term-rv-revived", 100, worktree=str(tmp / "wt" / "moved"), pty="pty-term-rv"),
            term("term-rv-copy", 100, worktree=wt("rv")),
        )
        world(rows)
        text = tick_text(st, NOW + 60)
        assert sorted(tab_closes()) == [("term-rv-copy", True), ("term-rv-revived", True)], (host, tab_closes())
        for handle in ("term-rv-copy", "term-rv-revived"):
            assert f"rv: closed revived {handle} on {host}; its ticket is complete" in text, text
        log_file.write_text("")
        tick(st, NOW + 120)
        assert closes() == [], (host, closes())


@scenario
def retired_branch_is_the_one_its_terminal_shows_at_cleanup():
    """A record whose branch went stale since it was last supervised retires the branch its
    own terminal shows at cleanup, so a revived pane on that branch is still closed."""
    for host in ("local", WSL):
        st = finished({"rv": host}, branch="feat/old")
        world(on(host, term("term-rv-copy", 100, worktree=wt("rv"))))
        tick(st, NOW + 60)
        assert closes() == [("term-rv-copy", host)], (host, closes())


@scenario
def borrowed_worktree_ties_revived_panes_only_by_their_launch():
    """In a worktree the ticket did not own, only its own launch's pty ties a revived pane:
    an agent there on the same branch may be the owner's and stays."""
    st = finished({"rb": "local"}, owns_worktree=False)
    world(local(
        term("term-rb-revived", 100, worktree=wt("rb"), pty="pty-term-rb"),
        term("term-rb-copy", 100, worktree=wt("rb")),
    ))
    tick(st, NOW + 60)
    assert closes() == [("term-rb-revived", "local")], closes()


@scenario
def panes_listed_at_cleanup_are_not_tied_by_worktree():
    """A coder leaving its live reviewer's worktree: an unrecorded agent pane already open
    there was not revived later and stays, also when Orca lists it again under a new handle
    with its pty; a copy Orca revives afterwards goes."""
    for host in ("local", WSL):
        reset_db()
        ticket_row("pv", status="complete")
        ticket_row("pv-review", agent="codex:pv-review")
        st, terms, worktree = pair("pv", host=host)
        visitor = term("term-visitor", 100, worktree=worktree)
        world(on(host, *terms.values(), visitor))
        tick(st)
        assert worker_closes() == [("term-pv", host)], (host, closes())
        rehandled = term("term-visitor-revived", 100, worktree=worktree, pty="pty-term-visitor")
        copy = term("term-pv-copy", 100, worktree=worktree)
        world(on(host, terms["pv-review"], rehandled, copy))
        tick(st, NOW + 60)
        assert worker_closes() == [("term-pv-copy", host)], (host, closes())


@scenario
def a_pane_seen_at_any_retirement_is_not_tied_by_worktree():
    """Coder and reviewer retire from one merged worktree: a visitor pane listed when the
    coder retired, gone while the reviewer retired and back afterwards, stays; a copy of
    the reviewer Orca revives afterwards goes."""
    for host in ("local", WSL):
        reset_db()
        ticket_row("pk", status="complete")
        ticket_row("pk-review", agent="codex:pk-review")
        st, terms, worktree = pair("pk", host=host)
        visitor = term("term-visitor", 100, ident="codex", worktree=worktree)
        world(on(host, *terms.values(), visitor), git=git, gh=merged)
        tick(st)
        ticket_row("pk-review", status="complete", agent="codex:pk-review")
        world(on(host, terms["pk-review"]), git=git, gh=merged)
        tick(st, NOW + 60)
        assert st["workers"] == {}, (host, st["workers"])
        copy = term("term-pk-copy", 100, ident="codex", worktree=worktree)
        world(on(host, visitor, copy))
        tick(st, NOW + 120)
        assert worker_closes() == [("term-pk-copy", host)], (host, closes())


@scenario
def an_expired_entry_leaves_its_exclusions_with_the_newer_one():
    """The coder retires on day 0 with a visitor listed and the reviewer on day 6 with it
    gone: once the coder's entry expires the visitor still stays, and a revived copy goes."""
    for host in ("local", WSL):
        reset_db()
        ticket_row("pe", status="complete")
        ticket_row("pe-review", agent="codex:pe-review")
        st, terms, worktree = pair("pe", host=host)
        visitor = term("term-visitor", 100, ident="codex", worktree=worktree)
        world(on(host, *terms.values(), visitor), git=git, gh=merged)
        tick(st)
        ticket_row("pe-review", status="complete", agent="codex:pe-review")
        world(on(host, terms["pe-review"]), git=git, gh=merged)
        tick(st, NOW + 6 * 86400)
        assert st["workers"] == {}, (host, st["workers"])
        copy = term("term-pe-copy", 100, ident="codex", worktree=worktree)
        world(on(host, visitor, copy))
        tick(st, NOW + 7 * 86400 + 60)
        assert [e["ticket"] for e in st.get("retired", [])] == ["pe-review"], (host, st.get("retired"))
        assert worker_closes() == [("term-pe-copy", host)], (host, closes())


@scenario
def a_pane_seen_in_another_worktree_stays_past_that_entrys_expiry():
    """A visitor seen when ma retired on day 0, gone when mb retired elsewhere on day 6, is
    listed in mb's worktree under its pty: it stays before ma's entry expires and after,
    while mb's revived copies go."""
    for host in ("local", WSL):
        reset_db()
        ticket_row("ma", status="complete")
        ticket_row("mb")
        st = fresh_state(
            ma=rec("ma", "c3", host=host, owns_worktree=False), mb=rec("mb", "c1", host=host)
        )
        visitor = term("term-visitor", 100, worktree=wt("ma"))
        world(on(host, term("term-ma", 100), term("term-mb", 100), visitor))
        tick(st)
        assert list(st["workers"]) == ["mb"], (host, st["workers"])
        ticket_row("mb", status="complete")
        world(on(host, term("term-mb", 100)), git=git, gh=merged)
        tick(st, NOW + 6 * 86400)
        assert st["workers"] == {}, (host, st["workers"])
        moved = term("term-visitor-moved", 100, worktree=wt("mb"), pty="pty-term-visitor")
        for at, copy in ((6 * 86400 + 60, "term-mb-copy"), (7 * 86400 + 60, "term-mb-after")):
            world(on(host, moved, term(copy, 100, worktree=wt("mb"))))
            tick(st, NOW + at)
            assert worker_closes() == [(copy, host)], (host, at, closes())
        assert [e["ticket"] for e in st.get("retired", [])] == ["mb"], (host, st.get("retired"))


@scenario
def a_gone_terminal_retires_the_branch_its_cleanup_proved():
    """An owned record whose terminal is already gone, with no branch or a stale one,
    retires the branch cleanup's check proved before removing the merged worktree, so a
    revived copy on that branch goes next tick."""
    for host in ("local", WSL):
        for recorded in ({}, {"branch": "feat/old"}):
            reset_db()
            ticket_row("gb", status="complete")
            st = fresh_state(gb=rec("gb", "c3", host=host, **recorded))
            world([], git=git, gh=merged)
            tick(st)
            assert removed_with()[1] == [f"path:{wt('gb')}"], (host, recorded, removed_with())
            assert st["workers"] == {}, (host, recorded, st["workers"])
            world(on(host, term("term-gb-copy", 100, worktree=wt("gb"))))
            tick(st, NOW + 60)
            assert worker_closes() == [("term-gb-copy", host)], (host, recorded, closes())


@scenario
def revived_check_leaves_shells_and_unrelated_agents_alone():
    """Next to a revived pane it closes, a shell in that worktree, an agent in another
    worktree, an agent there on another branch or of another kind, and a pane a live
    record holds there all stay."""
    st = finished({"rv": "local"})
    ticket_row("rv-review", agent="codex:rv-review")
    st["workers"]["rv-review"] = rec(
        "rv-review", "x1", agent="codex", worktree=wt("rv"), owns_worktree=False
    )
    world(local(
        term("term-rv-copy", 100, worktree=wt("rv")),
        term("term-shell", 100, ident=None, worktree=wt("rv")),
        term("term-other-wt", 100, worktree=wt("unrelated")),
        term("term-other-branch", 100, worktree=wt("rv"), branch="refs/heads/feat/b"),
        term("term-other-kind", 100, ident="cursor", worktree=wt("rv")),
        term("term-rv-review", 100, ident="codex", worktree=wt("rv")),
    ))
    tick(st, NOW + 60)
    assert closes() == [("term-rv-copy", "local")], closes()


@scenario
def revived_pane_of_a_reopened_ticket_stays():
    """A retired ticket that was reopened keeps its revived pane; a finished one beside it
    loses its own."""
    st = finished({"rv": WSL, "ro": WSL})
    ticket_row("ro", status="active", agent="claude:ro")
    world(on(
        WSL,
        term("term-rv-copy", 100, worktree=wt("rv")),
        term("term-ro-copy", 100, worktree=wt("ro")),
    ))
    tick(st, NOW + 60)
    assert closes() == [("term-rv-copy", WSL)], closes()


@scenario
def revived_check_skips_a_host_that_cannot_list():
    """A host whose terminal list fails is skipped; another host's revived pane still goes."""
    st = finished({"rw": WSL, "rl": "local"})
    world(local(term("term-rl-copy", 100, worktree=wt("rl"))))
    data = fixture()
    data["terminals"][WSL] = "down"
    set_fixture(**data)
    tick(st, NOW + 60)
    assert closes() == [("term-rl-copy", "local")], closes()


@scenario
def retired_entries_expire_after_seven_days():
    """Six days after its record dropped a revived pane is still closed; past seven days the
    retired entry is gone and the pane stays."""
    st = finished({"rv": "local"})
    world(local(term("term-rv-copy", 100, worktree=wt("rv"))))
    tick(st, NOW + 6 * 86400)
    assert closes() == [("term-rv-copy", "local")], closes()
    world(local(term("term-rv-copy", 100, worktree=wt("rv"))))
    tick(st, NOW + 7 * 86400 + 60)
    assert closes() == [] and st.get("retired") == [], (closes(), st.get("retired"))


@scenario
def remote_ci():
    """A remote CI wait runs gh through a probe terminal on that host."""
    reset_db()
    ticket_row("rc")
    update("rc", "blocker", "Waiting on: ci")
    st = fresh_state(rc=rec("rc", "c3", host="Juzzark-WSL"))
    row, screen = term("term-rc", 100)
    world([("Juzzark-WSL", "term-rc", row, screen)], gh={CHECKS: [0, "pass\n"]})
    tick(st)
    assert sends() == [
        (
            "term-rc",
            woke("rc", "dev-auto: CI finished with 1 pass. Continue ticket rc."),
            True,
        )
    ]
    sent = calls("orca", ["terminal", "send"])[0]["args"]
    assert sent[-2:] == ["--environment", "Juzzark-WSL"]
    probes = [
        r
        for r in calls("orca", ["terminal", "create"])
        if "dev-auto probe" in r["args"]
    ]
    assert len(probes) == 1 and probes[0]["args"][-2:] == [
        "--environment",
        "Juzzark-WSL",
    ]
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == [
        "term-Juzzark-WSL-1"
    ]


@scenario
def remote_process():
    """A remote process wait is checked on the worker's host, through a probe terminal."""
    reset_db()
    ticket_row("rp")
    update("rp", "blocker", "Waiting on: process 4242")
    st = fresh_state(rp=rec("rp", "c3", host="Juzzark-WSL"))
    row, screen = term("term-rp", 100)
    terms = [("Juzzark-WSL", "term-rp", row, screen)]
    world(terms, pids=["4242"])
    tick(st)
    assert sends() == [], sends()
    probes = [
        r
        for r in calls("orca", ["terminal", "create"])
        if "dev-auto probe" in r["args"]
    ]
    assert len(probes) == 1 and probes[0]["args"][-2:] == ["--environment", "Juzzark-WSL"]
    assert "ps -p 4242 -o pid=" in opt(probes[0], "--command"), probes
    world(terms, pids=[])
    tick(st)
    assert sends() == [
        ("term-rp", woke("rp", "dev-auto: process 4242 has exited. Continue ticket rp."), True)
    ], sends()
    sent = calls("orca", ["terminal", "send"])[0]["args"]
    assert sent[-2:] == ["--environment", "Juzzark-WSL"]


@scenario
def remote_process_unread_probe_is_one_stuck():
    """da-4-12: a remote process wait whose every probe fails, the worker still listed,
    is the untrackable-wait STUCK once; later ticks probe again, raise no second STUCK,
    and wake the worker once a probe reads the process gone."""
    reset_db()
    ticket_row("rx")
    note = update("rx", "blocker", "Waiting on: process 4242")
    st = fresh_state(rx=rec("rx", "c3", host="Juzzark-WSL"))
    row, screen = term("term-rx", 100)
    terms = [("Juzzark-WSL", "term-rx", row, screen)]
    stuck = f"STUCK rx: dev-auto cannot track its Waiting on note, update {note} on the ticket"
    for later in (0, 60):
        world(terms, pids=["4242"], probe_fails=True)
        tick(st, NOW + later)
        assert sends() == ([("term-orch", stuck, True)] if later == 0 else []), (later, sends())
    world(terms, pids=[])
    tick(st, NOW + 120)
    assert sends() == [
        ("term-rx", woke("rx", "dev-auto: process 4242 has exited. Continue ticket rx."), True)
    ], sends()


@scenario
def probe_terminal_closes_with_its_tab():
    """A probe terminal on a remote host is closed with its tab: nothing of it is left to
    restore."""
    reset_db()
    ticket_row("rq")
    update("rq", "blocker", "Waiting on: process 4242")
    st = fresh_state(rq=rec("rq", "c3", host="Juzzark-WSL"))
    row, screen = term("term-rq", 100)
    world([("Juzzark-WSL", "term-rq", row, screen)], pids=["4242"])
    tick(st)
    assert tab_closes() == [("term-Juzzark-WSL-1", True)], tab_closes()
    assert resumable("Juzzark-WSL") == []


@scenario
def cleanup_merged():
    """Closed and merged: terminal, worktree and branch go; dirty or unmerged: STUCK."""
    reset_db()
    ticket_row("k1", status="complete")
    st = fresh_state(k1=rec("k1", "c1"))
    st["sent"]["k1|nudge|1"] = 1
    world(local(term("term-k1", 100)), git=git, gh=merged)
    tick(st)
    assert "k1" not in st["workers"] and st["sent"] == {}
    assert [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])] == [
        "term-k1"
    ]
    (removed,) = calls("orca", ["worktree", "rm"])
    assert opt(removed, "--worktree") == f"path:{tmp / 'wt' / 'k1'}"
    assert "--force" not in removed["args"], "a branch worktree is never forced"
    (deleted,) = [r for r in calls("git") if r["args"][0] == "update-ref"]
    assert deleted["args"] == ["update-ref", "-d", "refs/heads/feat/a", "c0ffee1"]
    assert deleted["cwd"] == str(tmp / "repo")
    for change, verdict in (
        ({STATUS: [0, "?? new.ts\n"]}, "dirty"),
        ({}, "unmerged"),
    ):
        st = fresh_state(k1=rec("k1", "c1"))
        world(local(term("term-k1", 100)), git={**git, **change}, gh=no_pr)
        tick(st)
        assert calls("orca", ["worktree", "rm"]) == [] and "k1" in st["workers"]
        assert sends() == [
            (
                "term-orch",
                f"STUCK k1: ticket is complete but its worktree is {verdict}. Not removed",
                True,
            )
        ], sends()
        assert notes()[-1].endswith(f"\n{tmp / 'wt' / 'k1'} on local, branch feat/a"), (
            notes()
        )
        world(local(term("term-k1", 100)), git={**git, **change}, gh=no_pr)
        tick(st)
        assert sends() == []
    st = fresh_state(k1=rec("k1", "c1"))
    world(local(term("term-k1", 100)), git=git, gh=opened)
    tick(st)
    assert (
        sends() == []
        and calls("orca", ["worktree", "rm"]) == []
        and "k1" in st["workers"]
    ), "merge pending"
    ticket_row("k2", status="closed")
    ticket_row("k3")
    update("k3", "blocker", "Waiting on: review")
    st = fresh_state(
        k2=rec("k2", "c1"),
        k3=rec("k3", "x1", agent="codex", worktree=str(tmp / "wt" / "k2")),
    )
    world(
        local(
            term("term-k2", 100),
            term("term-k3", 100, ident="codex", worktree=str(tmp / "wt" / "k2")),
        )
    )
    tick(st)
    assert (
        "k2" not in st["workers"]
        and calls("orca", ["worktree", "rm"]) == []
        and calls("git") == []
    )


def removed_with():
    """This tick's terminal closes, worktree removals and branch deletions."""
    closes = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    removed = [opt(r, "--worktree") for r in calls("orca", ["worktree", "rm"])]
    deleted = [r["args"] for r in calls("git") if r["args"][0] == "update-ref"]
    return closes, removed, deleted


@scenario
def active_ticket_keeps_its_merged_worktree():
    """Live 2026-11-14: onboarding-served-switch-08's worktree went between its feature
    merge and finish_work. An active ticket whose task branch is already merged keeps its
    worktree, branch and terminal through every tick; once complete they go as today."""
    reset_db()
    ticket_row("km")
    st = fresh_state(km=rec("km", "c1"))
    for _ in range(3):
        world(local(term("term-km", 100)), git=git, gh=merged)
        tick(st)
        assert removed_with() == ([], [], []), removed_with()
        assert "km" in st["workers"]
    ticket_row("km", status="complete")
    world(local(term("term-km", 100)), git=git, gh=merged)
    tick(st)
    assert removed_with() == (
        ["term-km"],
        [f"path:{tmp / 'wt' / 'km'}"],
        [["update-ref", "-d", "refs/heads/feat/a", "c0ffee1"]],
    ), removed_with()
    assert "km" not in st["workers"]


@scenario
def shared_worktree_kept_until_its_review_ticket_is_done():
    """A coder ticket complete while its review ticket in the same worktree is active:
    only the coder's terminal closes, and the worktree and branch stay through every tick
    until the review ticket is done too."""
    reset_db()
    ticket_row("kc", status="complete")
    ticket_row("kc-review", agent="codex:kc-review")
    wt = str(tmp / "wt" / "kc")
    review = rec("kc-review", "x1", agent="codex", worktree=wt)
    st = fresh_state(kc=rec("kc", "c1"), **{"kc-review": review})
    reviewer = term("term-kc-review", 100, ident="codex", worktree=wt)
    world(local(term("term-kc", 100), reviewer), git=git, gh=merged)
    tick(st)
    assert removed_with() == (["term-kc"], [], []), removed_with()
    assert "kc" not in st["workers"] and "kc-review" in st["workers"]
    for _ in range(2):
        world(local(reviewer), git=git, gh=merged)
        tick(st)
        assert removed_with() == ([], [], []), removed_with()
    ticket_row("kc-review", status="complete", agent="codex:kc-review")
    world(local(reviewer), git=git, gh=merged)
    tick(st)
    closes, removed, deleted = removed_with()
    assert closes == ["term-kc-review"] and removed == [f"path:{wt}"], (closes, removed)
    assert deleted == [["update-ref", "-d", "refs/heads/feat/a", "c0ffee1"]], deleted
    assert st["workers"] == {}, st["workers"]


def pair(tid, owns=None, host="local"):
    """A coder record and its review record, launched not owning the worktree as every
    review record was until 2026-11-14, sharing tmp/wt/<tid>; the coder owns it unless
    `owns` says otherwise."""
    wt = str(tmp / "wt" / tid)
    owned = {} if owns is None else {"owns_worktree": owns}
    coder = rec(tid, "c1", worktree=wt, host=host, **owned)
    review = rec(f"{tid}-review", "x1", agent="codex", worktree=wt, host=host, owns_worktree=False)
    terms = {
        tid: term(f"term-{tid}", 100, worktree=wt),
        f"{tid}-review": term(f"term-{tid}-review", 100, ident="codex", worktree=wt),
    }
    return fresh_state(**{tid: coder, f"{tid}-review": review}), terms, wt


@scenario
def last_record_out_of_a_merged_pair_worktree_removes_it():
    """Live 2026-11-14: runs-command-state-02's merged worktree stayed after both its
    tickets were done. Whichever of the coder and review records leaves last removes the
    shared, merged worktree and its branch: the record that keeps the worktree takes its
    ownership."""
    for first, last in (("pc", "pc-review"), ("pc-review", "pc")):
        reset_db()
        agents = {"pc": "claude:pc", "pc-review": "codex:pc-review"}
        for tid, agent in agents.items():
            ticket_row(tid, agent=agent)
        st, terms, wt = pair("pc")
        ticket_row(first, status="complete", agent=agents[first])
        world(local(*terms.values()), git=git, gh=merged)
        tick(st)
        assert removed_with() == ([f"term-{first}"], [], []), (first, removed_with())
        assert list(st["workers"]) == [last], (first, st["workers"])
        ticket_row(last, status="complete", agent=agents[last])
        world(local(terms[last]), git=git, gh=merged)
        tick(st)
        closes, removed, deleted = removed_with()
        assert closes == [f"term-{last}"] and removed == [f"path:{wt}"], (first, closes, removed)
        assert deleted == [["update-ref", "-d", "refs/heads/feat/a", "c0ffee1"]], deleted
        assert st["workers"] == {} and sends() == [], (first, st["workers"], sends())


@scenario
def unmerged_pair_worktree_stays_with_its_cleanup_stuck():
    """A coder done before its reviewer on an unmerged branch: the reviewer's finish
    keeps the worktree and branch with today's one STUCK, and its record stays."""
    reset_db()
    ticket_row("pu", status="complete")
    ticket_row("pu-review", agent="codex:pu-review")
    st, terms, _ = pair("pu")
    world(local(*terms.values()), git=git, gh=no_pr)
    tick(st)
    assert removed_with() == (["term-pu"], [], []), removed_with()
    ticket_row("pu-review", status="complete", agent="codex:pu-review")
    world(local(terms["pu-review"]), git=git, gh=no_pr)
    tick(st)
    assert removed_with() == (["term-pu-review"], [], []), removed_with()
    assert list(st["workers"]) == ["pu-review"], st["workers"]
    text = "STUCK pu-review: ticket is complete but its worktree is unmerged. Not removed"
    assert sends() == [("term-orch", text, True)], sends()


def uncleared():
    """Typed lines but the CLEARED REVIEW events a PUSH also raises."""
    return [s for s in sends() if not s[1].startswith("CLEARED")]


def settled_lines(tid):
    log = tmp / "delivered.log"
    rows = log.read_text().splitlines() if log.exists() else []
    return [r.split("\t")[2] for r in rows if r.split("\t")[1] == tid and "SETTLED" in r]


WAITS = (
    "SETTLED {}: ticket is complete; its worktree waits for its cleared PUSH c0ffee1 to "
    "merge, rechecked every 15 minutes, and is removed then"
)


ON = {"merge_wait_hours": 6}


@scenario
def cleared_unmerged_worktree_waits_for_its_merge():
    """Live (delivered.log 2026-11-14 05:59, 06:04, 08:17): a finished ticket whose head
    a reviewer cleared with PUSH read as unmerged until the orchestrator merged its pull
    request, and each was a STUCK. With merge_wait_hours set the tick keeps the worktree
    with one delivered.log line and no STUCK, takes no step twice, and removes it once
    the merged check passes."""
    reset_db()
    ticket_row("cw", status="complete")
    update("cw", "finding", "REVIEW VERDICT: PUSH c0ffee1", agent="codex:cw-review")
    with settings(**ON):
        st = finished_cleanup("cw", True, gh=no_pr)
        assert uncleared() == [] and calls("orca", ["worktree", "rm"]) == [], uncleared()
        assert "cw" in st["workers"] and settled_lines("cw") == [WAITS.format("cw")]
        finished_cleanup("cw", False, gh=no_pr, st=st, now=NOW + 960)
        assert uncleared() == [] and settled_lines("cw") == [WAITS.format("cw")], uncleared()
        finished_cleanup("cw", False, st=st, now=NOW + 1920)
    assert len(calls("orca", ["worktree", "rm"])) == 1 and "cw" not in st["workers"]
    assert uncleared() == [], uncleared()


@scenario
def cleared_unmerged_worktree_is_todays_stuck_while_off():
    """merge_wait_hours starts at 0: the same cleared, unmerged head is today's STUCK."""
    reset_db()
    ticket_row("co", status="complete")
    update("co", "finding", "REVIEW VERDICT: PUSH c0ffee1", agent="codex:co-review")
    assert cfg.get("merge_wait_hours") == 0
    finished_cleanup("co", True, gh=no_pr)
    text = "STUCK co: ticket is complete but its worktree is unmerged. Not removed"
    assert uncleared() == [("term-orch", text, True)], uncleared()
    assert settled_lines("co") == []


@scenario
def cleared_unmerged_worktree_stuck_once_after_its_wait():
    """A cleared head still unmerged after the wait is one STUCK saying what the tick
    tried, also when its pull request is open by then; later ticks raise nothing more
    and the worktree stays."""
    text = (
        "STUCK {}: ticket is complete but its worktree is still unmerged 6 hours after "
        "its cleared PUSH c0ffee1, rechecked every 15 minutes. Not removed"
    )
    for tid, late in (("cx", no_pr), ("cp", opened)):
        reset_db()
        ticket_row(tid, status="complete")
        update(tid, "finding", "REVIEW VERDICT: PUSH c0ffee1", agent=f"codex:{tid}-review")
        with settings(**ON):
            st = finished_cleanup(tid, True, gh=no_pr)
            finished_cleanup(tid, False, gh=late, st=st, now=NOW + 6 * 3600 - 960)
            assert uncleared() == [], (tid, uncleared())
            finished_cleanup(tid, False, gh=late, st=st, now=NOW + 6 * 3600 + 60)
            assert uncleared() == [("term-orch", text.format(tid), True)], uncleared()
            finished_cleanup(tid, False, gh=late, st=st, now=NOW + 8 * 3600)
        assert uncleared() == [] and calls("orca", ["worktree", "rm"]) == [], uncleared()
        assert tid in st["workers"] and settled_lines(tid) == [WAITS.format(tid)]


@scenario
def open_pr_starts_no_merge_wait():
    """An open pull request with no wait begun stays today's silent recheck."""
    reset_db()
    ticket_row("cn", status="complete")
    update("cn", "finding", "REVIEW VERDICT: PUSH c0ffee1", agent="codex:cn-review")
    with settings(**ON):
        st = finished_cleanup("cn", True, gh=opened)
        finished_cleanup("cn", False, gh=opened, st=st, now=NOW + 8 * 3600)
    assert uncleared() == [] and settled_lines("cn") == [], uncleared()
    assert "merge_wait" not in st["workers"]["cn"]


@scenario
def review_ticket_waits_for_its_coders_cleared_merge():
    """Live (dev-auto-fixes-44-review, 2026-11-14 08:17): the reviewer finished after the
    coder, on a head its own PUSH on the coder ticket cleared. No STUCK; the worktree
    stays for the merge."""
    reset_db()
    ticket_row("pw", status="complete")
    ticket_row("pw-review", status="complete", agent="codex:pw-review")
    update("pw", "finding", "REVIEW VERDICT: PUSH c0ffee1", agent="codex:pw-review")
    st, terms, _ = pair("pw")
    with settings(**ON):
        world(local(*terms.values()), git=git, gh=no_pr)
        tick(st)
    assert uncleared() == [] and calls("orca", ["worktree", "rm"]) == [], uncleared()
    assert list(st["workers"]) == ["pw-review"], st["workers"]
    assert settled_lines("pw-review") == [WAITS.format("pw-review")]


@scenario
def reviewer_cleanup_checks_merged_pr_after_feature_ref_disappears():
    """The PR was opened from the feature branch, not the reviewer's task branch."""
    main = real_main("reviewer-merged-pr")
    linked = main.parent / "review"
    branch = "task/feat/reviewed"
    feature = "fix/shipped-feature"
    git_at(main, "worktree", "add", "-b", branch, str(linked))
    git_at(linked, "commit", "--allow-empty", "-m", "cleared change")
    cleared = subprocess.check_output([GIT, "-C", str(linked), "rev-parse", "HEAD"], text=True).strip()
    git_at(linked, "commit", "--allow-empty", "-m", "published task tip")
    published = subprocess.check_output([GIT, "-C", str(linked), "rev-parse", "HEAD"], text=True).strip()
    git_at(main, "checkout", "-b", feature)
    git_at(main, "merge", "--no-ff", "--no-edit", branch)
    git_at(main, "checkout", "-b", "development", "staging")
    git_at(main, "merge", "--no-ff", "--no-edit", feature)
    git_at(main, "push", "origin", "development")
    git_at(main, "branch", "-d", feature)
    git_at(linked, "reset", "--hard", cleared)
    base_sha = subprocess.check_output([GIT, "-C", str(main), "rev-parse", "staging"], text=True).strip()
    git_at(main, "tag", base_sha[:12], cleared)
    merge = subprocess.check_output([GIT, "-C", str(main), "rev-parse", "HEAD"], text=True).strip()
    query = (
        f"pr list --repo github.com/o/r --head {feature} --base development --state merged"
        " --json mergeCommit,isCrossRepository"
        " --jq .[] | select(.isCrossRepository == false) | .mergeCommit.oid // empty"
    )
    unscoped_query = (
        f"pr list --repo github.com/o/r --head {feature} --state merged"
        " --json mergeCommit --jq .[].mergeCommit.oid // empty"
    )

    for tid, why in (
        ("mr-proof", ""),
        ("mr-legacy", "its launch did not record"),
        ("mr-other-base", "is still unmerged 6 hours after"),
        ("mr-aliased", "its worktree is unmerged"),
        ("mr-partial", ""),
        ("mr-unmerged", "is still unmerged 6 hours after"),
    ):
        if tid == "mr-partial":
            git_at(main, "branch", "feature/feat", cleared)
            git_at(main, "push", "origin", f"{published}:refs/heads/{branch}")
        if tid == "mr-unmerged":
            git_at(linked, "commit", "--allow-empty", "-m", "another cleared change")
        head = subprocess.check_output([GIT, "-C", str(linked), "rev-parse", "HEAD"], text=True).strip()
        reset_db()
        review = f"{tid}-review"
        reviewer = f"codex:{review}"
        ticket_row(
            tid, status="complete", notes=f"Branch: `{branch}` · Feature branch: `{feature}`"
        )
        ticket_row(review, status="complete", agent=reviewer)
        clearance = base_sha[:12] if tid == "mr-aliased" else head
        update(tid, "finding", f"REVIEW VERDICT: PUSH {clearance}", agent=reviewer)
        worker = rec(
            review, "x1", agent="codex", worktree=str(linked), closed=True,
            owns_worktree=True, owned_branch=None if tid == "mr-legacy" else branch,
            owned_common=str(main / ".git"), merge_wait={"head": head, "at": NOW - 7 * 3600},
        )
        st = fresh_state(**{review: worker})
        delete = f"update-ref -d refs/heads/{branch} {head}"
        world(
            [], git_bin=GIT,
            git={"remote get-url origin": git["remote get-url origin"], delete: [0, ""]},
            gh={
                unscoped_query: [0, f"{merge}\n"],
                query: [0, "" if tid == "mr-other-base" else f"{merge}\n"],
            },
        )
        with settings(repos={"o/r": {"base": "development"}}, **ON):
            tick(st)
        if not why:
            assert removed_with() == ([], [f"path:{linked}"], [delete.split()]), removed_with()
            assert review not in st["workers"] and uncleared() == [], uncleared()
        else:
            assert removed_with() == ([], [], []), removed_with()
            assert review in st["workers"]
            assert any(why in msg for _, msg, _ in uncleared()), uncleared()
        if tid == "mr-aliased":
            assert not any(call["args"][:2] == ["pr", "list"] for call in calls("gh"))


def supersede(update_id, by):
    con = sqlite3.connect(db)
    con.execute("UPDATE task_updates SET superseded_by = ? WHERE id = ?", (by, update_id))
    con.commit()
    con.close()


@scenario
def unmerged_head_no_push_cleared_keeps_its_stuck():
    """A head no PUSH cleared, a PUSH by the ticket's own owner, a PUSH a later FIX of the
    same head undid, a superseded PUSH, and a dirty worktree with a cleared head each keep
    today's one STUCK and settle nothing, with merge_wait_hours set."""
    fix = "REVIEW VERDICT: FIX c0ffee1"
    for tid, verdicts, change, why in (
        ("cy", [("REVIEW VERDICT: PUSH 1a2b3c4", "codex:cy-review")], {}, "unmerged"),
        ("cz", [("REVIEW VERDICT: PUSH c0ffee1", "claude:cz")], {}, "unmerged"),
        ("cf", [("REVIEW VERDICT: PUSH c0ffee1", "codex:cf-review"), (fix, "codex:cf-review")], {}, "unmerged"),
        ("cs", [("REVIEW VERDICT: PUSH c0ffee1", "codex:cs-review")], {}, "unmerged"),
        ("cd", [("REVIEW VERDICT: PUSH c0ffee1", "codex:cd-review")], {STATUS: [0, " M a.ts\n"]}, "dirty"),
    ):
        reset_db()
        ticket_row(tid, status="complete")
        ids = [update(tid, "finding", text, agent=agent) for text, agent in verdicts]
        if tid == "cs":
            supersede(ids[0], update(tid, "decision", "that PUSH no longer holds", agent="claude:orchestrator"))
        with settings(**ON):
            finished_cleanup(tid, True, change=change, gh=no_pr)
        text = f"STUCK {tid}: ticket is complete but its worktree is {why}. Not removed"
        assert uncleared() == [("term-orch", text, True)], (tid, uncleared())
        assert settled_lines(tid) == [], tid


@scenario
def borrowed_pair_worktree_is_never_removed():
    """A coder and reviewer both launched into a worktree on another ticket's branch: the
    last of them out closes its terminal and drops its record, and the merged worktree
    and its branch stay."""
    reset_db()
    ticket_row("pb", status="complete")
    ticket_row("pb-review", agent="codex:pb-review")
    st, terms, _ = pair("pb", owns=False)
    world(local(*terms.values()), git=git, gh=merged)
    tick(st)
    assert removed_with() == (["term-pb"], [], []), removed_with()
    ticket_row("pb-review", status="complete", agent="codex:pb-review")
    world(local(terms["pb-review"]), git=git, gh=merged)
    tick(st)
    assert removed_with() == (["term-pb-review"], [], []), removed_with()
    assert st["workers"] == {} and sends() == [], (st["workers"], sends())


@scenario
def missing_ticket():
    reset_db()
    st = fresh_state(gone=rec("gone", "c1"))
    world(local(term("term-gone", 100)))
    tick(st)
    tick(st)
    assert sends() == [
        ("term-orch", "STUCK gone: the ticket is not in the ledger", True)
    ]


@scenario
def orchestrator_relay():
    """Relay once; the successor at the limit stops everything; shells get nothing."""
    reset_db()
    ticket_row("o1")
    update("o1", "blocker", "Waiting on: review")
    ticket_row("o2")
    update("o2", "blocker", "Waiting on: review")
    claude_usage("c3", 96, NOW + 900)
    st = fresh_state(o1=rec("o1", "c1"), o2=rec("o2", "c2"))
    st["orchestrator"]["account"] = "c3"
    workers = [term("term-o1", 100), term("term-o2", 100)]
    world(local(*workers))
    tick(st)
    ((who, text, _),) = sends(events=False)
    assert who == "term-orch" and text.startswith(
        "RELAY: orchestrator account max is at 96%"
    )
    assert text in [l.split(" ", 2)[2] for l in event_lines()], "also in the file"
    assert st["relay"] == {"used": True, "by": "term-orch", "reset": NOW + 900}
    world(local(*workers))
    tick(st)
    assert sends() == []
    st["orchestrator"] = {"terminal": "term-orch2", "host": "local", "account": "c3"}
    world(local(workers[0], term("term-o2", 100, ident=None)), orch="term-orch2")
    tick(st)
    typed = sends(events=False)
    assert [s[0] for s in typed][:2] == ["term-orch2", "term-o1"] and st["stopped"], typed
    assert typed[0][1].startswith("STOPPED:") and typed[1][1].startswith("dev-auto: stop.")
    world(local(*workers), orch="term-orch2")
    tick(st)
    assert sends() == [("term-o2", da.STOP, True)], "a stop waits for the agent"
    assert created() == [], "stopped: nothing is relaunched"
    clear_usage()


@scenario
def unshown_relay_keeps_the_pointer_until_its_screen_shows_it():
    """da-2-8: a RELAY Orca accepts while the orchestrator's screen keeps its old frame is
    not delivered: the pointer and the unused relay stay, nothing goes to events.md, and
    it is typed once more on the next tick, then no more. Once the screen shows it, the
    relay is recorded and the pointer cleared, with nothing typed again."""
    reset_db()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    ticket_row("ou")
    update("ou", "blocker", "Waiting on: review")
    claude_usage("c3", 96, NOW + 900)
    st = fresh_state(ou=rec("ou", "c1"))
    st["orchestrator"]["account"] = "c3"
    workers = [term("term-ou", 100)]
    relays = []
    for later in (0, 60, 120):
        world(local(*workers))
        hold("term-orch")
        tick(st, NOW + later)
        relays.append([s[1] for s in sends(events=False) if s[1].startswith("RELAY")])
        assert st["orchestrator"] and st["orchestrator"]["terminal"] == "term-orch", (later, st["orchestrator"])
        assert st["relay"] == {"used": False}, (later, st["relay"])
        assert not [l for l in event_lines() if "RELAY" in l], event_lines()
    assert [len(r) for r in relays] == [1, 1, 0], relays
    world(local(*workers), orch_tail=["❯", relays[0][0]])
    tick(st, NOW + 180)
    assert [s for s in sends(events=False) if s[1].startswith("RELAY")] == [], sends(events=False)
    assert st["orchestrator"] is None and st["relay"]["used"], (st["orchestrator"], st["relay"])
    assert [l for l in event_lines() if l.endswith(relays[0][0])], event_lines()
    clear_usage()


def relayed(tid):
    """A tick that relays off account max at 96% in the window resetting at NOW + 900."""
    reset_db()
    ticket_row(tid)
    update(tid, "blocker", "Waiting on: review")
    claude_usage("c3", 96, NOW + 900)
    st = fresh_state(**{tid: rec(tid, "c1")})
    st["orchestrator"]["account"] = "c3"
    workers = [term(f"term-{tid}", 100)]
    world(local(*workers))
    tick(st)
    assert [s[1].split(":")[0] for s in sends()] == ["RELAY"], sends()
    st["orchestrator"] = {"terminal": "term-orch2", "host": "local", "account": "c3"}
    return st, workers


@scenario
def relay_rearms_after_its_window_resets():
    """A relay used in an earlier usage window relays again on a 96% reading in a
    later one, and no worker is told to stop."""
    st, workers = relayed("o6")
    assert st["relay"].get("reset") == NOW + 900, st["relay"]
    later = NOW + 1000
    claude_usage("c3", 96, NOW + 20000, saved=later)
    world(local(*workers), orch="term-orch2")
    tick(st, later)
    told = [(s[0], s[1].split(":")[0]) for s in sends()]
    assert told == [("term-orch2", "RELAY")], sends()
    assert not st["stopped"], "no worker is told to stop"
    assert st["relay"] == {"used": True, "by": "term-orch2", "reset": NOW + 20000}
    clear_usage()


@scenario
def relay_record_without_reset_waits_a_week():
    """A relay recorded before resets were stored names no account or window, so a
    successor on another account reading low then 96% inside a week still stops; a week
    after it is first seen, the next 96% relays again."""
    reset_db()
    ticket_row("o8")
    ticket_row("o8-review", agent="codex:o8-review")
    update("o8", "blocker", "Waiting on: review. REVIEW READY 1a2b3c4")
    workers = [term("term-o8", 100)]

    def legacy():
        st = fresh_state(o8=rec("o8", "c1"))
        st["relay"] = {"used": True, "by": "term-previous-window"}
        st["orchestrator"]["account"] = "c2"
        return st

    try:
        st = legacy()
        claude_usage("c2", 37, NOW + 900, saved=NOW + 10)
        world(local(*workers))
        tick(st, NOW + 10)
        assert sends() == [] and st["relay"].get("reset") == NOW + 10 + 7 * 86400
        claude_usage("c2", 96, NOW + 900, saved=NOW + 60)
        world(local(*workers))
        tick(st, NOW + 60)
        assert st["stopped"], "a lower successor reading proves no reset"
        clear_usage()
        st = legacy()
        world(local(*workers))
        tick(st)
        week = NOW + 7 * 86400
        claude_usage("c2", 96, week + 900, saved=week, week_reset=week + 86400)
        world(local(*workers))
        tick(st, week)
        assert [s[1].split(":")[0] for s in sends()] == ["RELAY"], sends()
        assert not st["stopped"]
    finally:
        clear_usage()


@scenario
def relay_raised_without_a_reset_time_waits_a_week():
    """A relay raised by a reading with no reset time saves the week's bound, and a stored
    relay whose reset is null gets it once, as one without the key does: a successor at
    96% a week later relays again instead of stopping."""
    reset_db()
    ticket_row("o9")
    workers = [term("term-o9", 100)]
    week = NOW + 7 * 86400

    def successor(st, at):
        st["orchestrator"] = {"terminal": "term-orch2", "host": "local", "account": "c3"}
        claude_usage("c3", 96, at + 900, saved=at, week_reset=at + 86400)
        world(local(*workers), orch="term-orch2")
        tick(st, at)
        told = [s[1].split(":")[0] for s in sends(events=False) if s[0] == "term-orch2"]
        assert told == ["RELAY"], sends()
        assert not st["stopped"]

    try:
        st = fresh_state(o9=rec("o9", "c1"))
        st["orchestrator"]["account"] = "c2"
        claude_usage("c2", 96, None, saved=NOW, week_reset=None)
        world(local(*workers))
        tick(st)
        assert [s[1].split(":")[0] for s in sends(events=False)] == ["RELAY"], sends()
        assert st["relay"]["reset"] == week, st["relay"]
        successor(st, week)
        clear_usage()
        st = fresh_state(o9=rec("o9", "c1"))
        st["relay"] = {"used": True, "by": "term-previous-window", "reset": None}
        st["orchestrator"]["account"] = "c2"
        claude_usage("c2", 37, NOW + 900, saved=NOW + 10)
        world(local(*workers))
        tick(st, NOW + 10)
        assert st["relay"]["reset"] == NOW + 10 + 7 * 86400, st["relay"]
        successor(st, week + 10)
    finally:
        clear_usage()


@scenario
def relay_stops_on_a_second_reading_in_its_window():
    """Inside the window that raised the relay, the successor's 95% still stops."""
    st, workers = relayed("o7")
    claude_usage("c3", 95, NOW + 900, saved=NOW + 60)
    world(local(*workers), orch="term-orch2")
    tick(st, NOW + 60)
    typed = sends(events=False)
    assert st["stopped"] and [s[0] for s in typed][:2] == ["term-orch2", "term-o7"], typed
    assert typed[1][1].startswith("dev-auto: stop."), typed
    clear_usage()


@scenario
def stopped_reaches_a_busy_orchestrator_in_full():
    """da-2-10, da-docs-13: the successor crosses the relay threshold while its terminal
    shows no agent at its prompt. STOPPED goes to events.md at once, and its full text is
    typed on the first tick the orchestrator can take it, once, with no EVENTS wake
    repeating it."""
    st, workers = relayed("o9")
    # Events the relay tick appended wait for the successor.
    before, waiting = len(event_lines()), st.get("events_new", 0)
    claude_usage("c3", 95, NOW + 900, saved=NOW + 60)
    world(local(*workers), orch="term-orch2", orch_ident=None)
    tick(st, NOW + 60)
    assert st["stopped"] and [l for l in event_lines() if " STOPPED: " in l], event_lines()
    assert [s for s in sends(events=False) if s[0] == "term-orch2"] == [], sends(events=False)
    path = root / "supervisor" / "events.md"
    for later in (120, 180):
        world(local(*workers), orch="term-orch2")
        tick(st, NOW + later)
        told = [s[1] for s in sends(events=False) if s[0] == "term-orch2"]
        if later == 120:
            # Every waiting event and every one since but STOPPED, typed in full.
            others = waiting + len(event_lines()) - before - 1
            # Batch 1 was reserved by the wake held at 60, when no agent was at the prompt.
            wake = [f"EVENTS: {others} new in {path}, {len(event_lines())} open, batch 2"] if others else []
            assert len(told) == 1 + len(wake) and told[0].startswith("STOPPED:"), told
            assert told[1:] == wake, (told, wake)
        else:
            assert told == [], (later, told)
    clear_usage()



@scenario
def busy_orchestrator_is_told_stopped_after_its_turn():
    """da-docs-13: an orchestrator Orca names whose screen shows a running turn is not
    typed into, not even Ctrl-U; STOPPED is typed once its prompt is back."""
    st, workers = relayed("od")
    claude_usage("c3", 95, NOW + 900, saved=NOW + 60)
    for later, tail in ((60, ["✻ Working… (esc to interrupt)"]), (120, ["❯"])):
        world(local(*workers), orch="term-orch2", orch_tail=tail)
        tick(st, NOW + later)
        told = [s[1] for s in sends(include_clear=True, events=False) if s[0] == "term-orch2"]
        if later == 60:
            assert st["stopped"] and told == [], told
        else:
            assert [t for t in told if t.startswith("STOPPED:")], told
    clear_usage()


@scenario
def stopped_and_events_wakes_count_each_event_once():
    """da-logs-8: STOPPED's full text and an EVENTS wake both typed and not yet shown, then
    one more event: once the screen shows both, that newer event is still counted, and the
    next tick wakes the orchestrator for it."""
    st, workers = relayed("oc")
    da.emit(st, "x|before", "NEEDS YOU oc: one event before the stop")
    claude_usage("c3", 95, NOW + 900, saved=NOW + 60)
    world(local(*workers), orch="term-orch2")
    hold("term-orch2")
    tick(st, NOW + 60)
    told = [s[1] for s in sends(events=False) if s[0] == "term-orch2"]
    assert [t.split(":")[0] for t in told] == ["STOPPED", "EVENTS"], told
    da.emit(st, "x|after", "NEEDS YOU oc: one event after the stop")
    world(local(*workers), orch="term-orch2", orch_tail=told)
    tick(st, NOW + 120)
    assert st["events_new"] == 1, st["events_new"]
    world(local(*workers), orch="term-orch2")
    tick(st, NOW + 180)
    told = [s[1] for s in sends(events=False) if s[0] == "term-orch2"]
    assert len(told) == 1 and told[0].startswith("EVENTS: 1 new"), told
    clear_usage()


@scenario
def stopped_line_a_later_tick_appends_is_not_counted():
    """da-logs-8: STOPPED whose append a full disk cut short is appended by the next tick's
    outbox flush, and still no EVENTS wake counts it: its full send is its wake."""
    reset_db()
    st = fresh_state()
    st["stopped"] = int(NOW - 60)
    text = "STOPPED: the relayed orchestrator reached its usage limit."
    line = da.event_text(st, text)
    st["sent"]["stopped|1"] = int(NOW)
    st["outbox"] = {"stopped|1": {"line": line, "text": text, "note_on": None, "detail": ""}}
    world([])
    da.flush_outbox(st, da.read_tickets(root))
    assert line in event_lines() and st.get("events_new", 0) == 0, (event_lines(), st.get("events_new"))

@scenario
def ledger_notes():
    """Locked writes retry until they land; other errors fail at once."""
    set_fixture(locked=3)
    da.note("r1", "relaunched")
    assert fixture()["calls"] == 4
    ((call,),) = [[r for r in calls("concord-mcp")]]
    assert call["call"] == {
        "name": "update_work",
        "arguments": {
            "task_id": "r1",
            "kind": "finding",
            "content": "dev-auto: relaunched",
        },
    }
    assert {k: call["env"][k] for k in call["env"] if k.startswith("CONCORD_A")} == {
        "CONCORD_AGENT_ID": "script:dev-auto",
        "CONCORD_AGENT_KIND": "script",
    }
    set_fixture(locked=9)
    try:
        da.concord_call("update_work", {}, attempts=3)
        raise AssertionError("locked past the attempts must fail")
    except RuntimeError as error:
        assert "database is locked" in str(error) and fixture()["calls"] == 3
    set_fixture(tool_error="Task x is not claimed. Call start_work first.")
    try:
        da.concord_call("update_work", {})
        raise AssertionError("tool errors must fail")
    except RuntimeError:
        assert fixture()["calls"] == 1


@scenario
def launch_end_to_end():
    """Launch, then refusal while the ticket's worker is alive."""
    reset_db()
    ticket_row("t-9", status="proposed", title="served pages / analytics")
    set_fixture()
    (tmp / "wt" / "t-9").mkdir(parents=True, exist_ok=True)
    da.main(
        [
            "launch",
            "t-9",
            "--agent",
            "codex",
            "--model",
            "gpt-6.1-sol",
            "--effort",
            "medium",
            "--worktree",
            str(tmp / "wt" / "t-9"),
        ]
    )
    saved = json.loads(state_file.read_text())["workers"]["t-9"]
    assert (saved["account"], saved["agent_id"], saved["terminal"]) == (
        "x1",
        "codex:ticket-t-9",
        "term-local-1",
    )
    (new,) = created()
    assert (
        opt(new, "--title") == "analytics codex"
        and opt(new, "--worktree") == f"path:{tmp / 'wt' / 't-9'}"
    )
    row, screen = term("term-local-1", 5, ident="codex", worktree=str(tmp / "wt/t-9"))
    set_fixture(terminals={"local": [row]}, screens={"term-local-1": screen})
    try:
        da.main(
            ["launch", "t-9", "--agent", "codex", "--model", "m", "--worktree", "x"]
        )
        raise AssertionError("a live worker blocks a second launch")
    except SystemExit as stop:
        assert "already runs" in str(stop.code)
    da.main(["set-orchestrator", "term-o", "--account", "c3"])
    saved = json.loads(state_file.read_text())
    assert saved["orchestrator"] == {
        "terminal": "term-o",
        "host": "local",
        "account": "c3",
        "session": None,
    }
    state_file.unlink()


def launch_check_prompt(host, git_fixture):
    reset_db()
    state_file.unlink(missing_ok=True)
    wt = tmp / "wt" / "check-prompt"
    wt.mkdir(parents=True, exist_ok=True)
    ticket_row("check-prompt", status="proposed", title="work / check prompt")
    set_fixture(git=git_fixture)
    try:
        result = cli(
            "launch", "check-prompt", "--agent", "claude", "--model", OPUS,
            "--worktree", str(wt), "--host", host,
        )
        assert result.returncode == 0, result
        (made,) = created()
        return opt(made, "--command")
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def launch_checks_follow_the_worker_repository_on_both_hosts():
    """Tickets in one ledger get check instructions for the repository they work in."""
    for host, origin, ci_only in (
        ("local", HR_RELAY_ORIGIN, True),
        ("Juzzark-WSL", "git@github.com:julianthant/hr-relay.git", True),
        ("local", "https://github.com/julianthant/dot-agents.git", False),
    ):
        command = launch_check_prompt(host, {**git, "remote get-url origin": [0, f"{origin}\n"]})
        if ci_only:
            assert_ci_only_prompt(command)
        else:
            assert "Run all tests, lint, and typecheck only in GitHub Actions" not in command, command
            assert "Run no typecheck, lint, unit, architecture or whole-suite tests" in command, command
            assert "Run only the named integration test files the ticket needs, one file at a time" in command, command
            assert "~/.agents/skills/dev-auto/scripts/host-check-lock <command>" in command, command


@scenario
def launch_checks_stay_in_ci_when_the_wsl_origin_is_unreadable():
    command = launch_check_prompt("Juzzark-WSL", UNREAD_ORIGIN_GIT)
    assert calls("git", ["remote", "get-url"])
    assert_ci_only_prompt(command)


def tiered(tid, tier, status="active"):
    line = f"Coder: claude · Reviewer: codex · Tier: {tier}" if tier else "Coder: claude"
    ticket_row(tid, status=status, title=f"caps / {tid}", notes=f"Outcome: x\n{line}\n")


def cap_world(live):
    """Live coder workers of every kind, one per (ticket, tier), plus a review ticket,
    a finished ticket and a done record of each tier, which never count; a hard ticket
    the orchestrator runs itself has no worker and never counts either."""
    reset_db()
    kinds = [("codex", "x1", "m"), ("grok", "gk", "grok-4.7"), ("cursor", "cur", "g")]
    workers = {}
    for n, (tid, tier) in enumerate(live):
        tiered(tid, tier)
        agent, account_id, model = kinds[n % len(kinds)]
        workers[tid] = rec(tid, account_id, agent=agent, model=model)
    for tier in ("hard", "medium"):
        tiered(f"{tier}-r-review", tier)
        workers[f"{tier}-r-review"] = rec(f"{tier}-r-review", "x2", agent="codex", model="m")
        tiered(f"{tier}-f", tier, status="complete")
        workers[f"{tier}-f"] = rec(f"{tier}-f", "gk", agent="grok", model="grok-4.7")
        tiered(f"{tier}-d", tier)
        workers[f"{tier}-d"] = rec(f"{tier}-d", "cur", agent="cursor", model="g", done=True)
    tiered("orch-run", "hard")
    state_file.write_text(json.dumps({"workers": workers}))
    set_fixture()


def cap_launch(tid, tier, path, *extra):
    if tier is not False:
        tiered(tid, tier, status="proposed")
    wt = tmp / "wt" / tid
    wt.mkdir(parents=True, exist_ok=True)
    argv = ("launch", tid, "--agent", "claude", "--model", SONNET, "--worktree", str(wt))
    return cli(*argv, *extra, DEV_AUTO_ACCOUNTS=str(path))


@scenario
def launch_cap_refuses_a_third_hard_ticket():
    """With max_hard_tickets 2, a third live hard coder ticket is refused before any
    terminal is created, naming the two counted."""
    _, path = rules_config(**RULES)
    cap_world([("h1", "hard"), ("h2", "hard"), ("m1", "medium")])
    try:
        done = cap_launch("h3", "hard", path)
        assert done.returncode == 1, done
        assert "h1" in done.stderr and "h2" in done.stderr, done.stderr
        for other in ("m1", "-review", "hard-f", "hard-d", "orch-run"):
            assert other not in done.stderr, (other, done.stderr)
        assert created() == [], "refused before a terminal"
        assert "h3" not in json.loads(state_file.read_text())["workers"]
        assert cap_launch("m2", "medium", path).returncode == 0, "medium has room"
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def launch_cap_counts_no_lost_or_stopped_record():
    """An open hard ticket whose record is lost after a host restart, or stopped awaiting
    a restart proposal, has no live worker, so it counts toward no tier cap: with
    max_hard_tickets 2, a third hard ticket launches."""
    _, path = rules_config(**RULES)
    cap_world([("h1", "hard"), ("h2", "hard")])
    try:
        data = json.loads(state_file.read_text())
        data["workers"]["h1"]["lost"] = "STUCK local: Orca restarted and lost 1 workers: h1"
        data["workers"]["h2"]["stopped"] = int(NOW)
        state_file.write_text(json.dumps(data))
        done = cap_launch("h3", "hard", path)
        assert done.returncode == 0, done
        assert "h3" in json.loads(state_file.read_text())["workers"]
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def launch_cap_counts_medium_ui_and_easy_together():
    """With max_medium_tickets 3, medium, ui and easy share the cap: a fourth of them is
    refused naming the three; a hard ticket still has room."""
    _, path = rules_config(**RULES)
    cap_world([("m1", "medium"), ("u1", "ui"), ("e1", "easy"), ("h1", "hard")])
    try:
        for tid, tier in (("m4", "medium"), ("u4", "ui"), ("e4", "easy")):
            done = cap_launch(tid, tier, path)
            assert done.returncode == 1, done
            for counted in ("m1", "u1", "e1"):
                assert counted in done.stderr, (counted, done.stderr)
            assert "h1" not in done.stderr, done.stderr
        assert created() == [], "refused before a terminal"
        assert cap_launch("h2", "hard", path).returncode == 0, "hard has room"
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def launch_cap_over_cap_flag_launches_and_is_recorded():
    """--over-cap launches past the cap, on the operator's word, and one finding on the
    ticket says so; a launch within the cap records nothing about it."""
    _, path = rules_config(**RULES)
    cap_world([("h1", "hard"), ("h2", "hard")])
    try:
        done = cap_launch("h3", "hard", path, "--over-cap")
        assert done.returncode == 0, done
        assert "h3" in json.loads(state_file.read_text())["workers"]
        said = [n for n in notes() if "over the cap" in n]
        assert len(said) == 1 and "operator's word" in said[0], notes()
        assert "h1" in said[0] and "h2" in said[0], said
        set_fixture()
        assert cap_launch("m1", "medium", path, "--over-cap").returncode == 0
        assert not [n for n in notes() if "over the cap" in n], notes()
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def launch_cap_needs_a_tier_line():
    """With a cap set, a coder ticket without a Tier line is refused with that reason;
    with the caps unset, launch reads no Tier and applies no cap."""
    _, path = rules_config(**RULES)
    cap_world([])
    try:
        done = cap_launch("nt", None, path)
        assert done.returncode == 1 and "no Tier line" in done.stderr, done
        assert created() == []
        cap_world([("h1", "hard"), ("h2", "hard")])
        assert cap_launch("h3", "hard", accounts).returncode == 0, "caps unset"
        assert cap_launch("nt", None, accounts).returncode == 0, "caps unset, no Tier"
    finally:
        state_file.unlink(missing_ok=True)


def relaunch_ro(
    owner, old_host, old_id, old_agent="codex", places=None, others=None, **fixture_extra
):
    """Launch ticket ro's Codex worker on Juzzark-WSL over an exited one on old_host.
    An owner of None leaves the ticket unowned; an old_id of None stores no record, as
    after cleanup dropped it. places maps agent ids to the worktree each registered with
    Concord; others are other tickets' records. Returns the saved record and the launch's exit message,
    None when it ran through."""
    wt = tmp / "wt" / "ro"
    wt.mkdir(parents=True, exist_ok=True)
    reset_db()
    ticket_row("ro", agent=owner)
    if owner is None:
        con = sqlite3.connect(db)
        con.execute("UPDATE tasks SET agent_id = NULL WHERE task_id = 'ro'")
        con.commit()
        con.close()
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO agents VALUES (?, ?, ?)", [(a, w, w) for a, w in (places or {}).items()])
    con.commit()
    con.close()
    workers = dict(others or {})
    if old_id:
        workers["ro"] = rec(
            "ro", "x1", agent=old_agent, host=old_host, agent_id=old_id, worktree=str(wt)
        )
    state_file.write_text(json.dumps(fresh_state(**workers)))
    set_fixture(**{"terminals": {old_host: []}, **fixture_extra})
    stopped = None
    try:
        da.main(
            ["launch", "ro", "--agent", "codex", "--model", "m", "--worktree", str(wt)]
            + ["--host", "Juzzark-WSL"]
        )
    except SystemExit as stop:
        stopped = stop.code
    saved = json.loads(state_file.read_text())["workers"].get("ro")
    state_file.unlink()
    return saved, stopped


def released_once(old_id):
    """The one release of ro, sent as old_id at the store's version, before any terminal."""
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    releases = [
        (i, r)
        for i, r in enumerate(rows)
        if r["cmd"] == "concord-mcp" and r["call"]["name"] == "transfer_work"
    ]
    assert len(releases) == 1, releases
    freed, release = releases[0]
    given = release["call"]["arguments"]
    assert given["action"] == "release" and given["expected_version"] == 1, given
    assert release["env"]["CONCORD_AGENT_ID"] == old_id, "it acts as the old id"
    started = [i for i, r in enumerate(rows) if r in created()]
    assert started and freed < started[0], "released before the worker starts"


@scenario
def relaunch_on_another_host_frees_the_old_owner():
    """Live: a reviewer moved from Mac2 to Juzzark-WSL kept codex:mac2-… as owner, so the
    new worker could not claim its ticket. A launch under a new id releases the old id's
    claim as that id before the worker starts; a ticket an unrelated agent owns stays."""
    saved, stopped = relaunch_ro("codex:mac2-feat-ro", "Mac2", "codex:mac2-feat-ro")
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    releases = [
        (i, r)
        for i, r in enumerate(rows)
        if r["cmd"] == "concord-mcp" and r["call"]["name"] == "transfer_work"
    ]
    assert len(releases) == 1, "the old id's claim is released"
    ((freed, release),) = releases
    assert release["call"]["arguments"]["action"] == "release", release
    assert release["env"]["CONCORD_AGENT_ID"] == "codex:mac2-feat-ro", "it acts as the old id"
    assert "agent_id" not in release["call"]["arguments"], "Concord ignores that argument"
    assert owner_of("ro") is None, "the new worker's start_work finds the ticket unowned"
    started = [i for i, r in enumerate(rows) if r in created()]
    assert started and freed < started[0], "released before the worker starts"
    saved, stopped = relaunch_ro("claude:someone-else", "Mac2", "codex:mac2-feat-ro")
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    assert ledger_calls("transfer_work") == [], "an unrelated owner is never released"
    assert owner_of("ro") == "claude:someone-else"


@scenario
def relaunch_as_another_agent_frees_the_old_owner():
    """Live: three reviewers moved from Codex to Claude could not be relaunched, as
    inspect_work answers with text and the launch parsed it as JSON. A launch that
    changes the agent on the same host releases the old id and starts the new worker."""
    saved, stopped = relaunch_ro(
        "claude:wsl-feat-ro", "Juzzark-WSL", "claude:wsl-feat-ro", "claude"
    )
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    released_once("claude:wsl-feat-ro")
    assert owner_of("ro") is None, "the new worker's start_work finds the ticket unowned"


@scenario
def relaunch_of_an_unowned_ticket_releases_nothing():
    """A ticket nobody owns is launched under the new id with no release."""
    saved, stopped = relaunch_ro(None, "Mac2", "codex:mac2-feat-ro")
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    assert ledger_calls("transfer_work") == [] and len(created()) == 1
    assert owner_of("ro") is None


@scenario
def refused_release_stops_the_relaunch():
    """A release Concord refuses stops the launch, naming the ticket and the old owner;
    no terminal is opened and the old record stays."""
    saved, stopped = relaunch_ro(
        "codex:mac2-feat-ro", "Mac2", "codex:mac2-feat-ro", release_refused="stale version"
    )
    assert stopped == (
        "STUCK ro codex:wsl-ticket-ro: the earlier owner codex:mac2-feat-ro's claim was not"
        " released: transfer_work: stale version"
    ), stopped
    assert stuck_notes() == [f"dev-auto: {stopped}"], notes()
    assert len(ledger_calls("transfer_work")) == 1 and created() == []
    assert owner_of("ro") == "codex:mac2-feat-ro"
    assert saved["agent_id"] == "codex:mac2-feat-ro" and saved["host"] == "Mac2", saved


@scenario
def relaunch_on_the_same_host_keeps_the_id():
    """Same agent, same host: the new worker keeps the old id, so it stays the owner and
    nothing is released."""
    saved, stopped = relaunch_ro("codex:wsl-ro-legacy", "Juzzark-WSL", "codex:wsl-ro-legacy")
    assert stopped is None and saved["agent_id"] == "codex:wsl-ro-legacy", (stopped, saved)
    assert ledger_calls("transfer_work") == [] and owner_of("ro") == "codex:wsl-ro-legacy"


def stuck_notes():
    return [n for n in notes() if n.startswith("dev-auto: STUCK")]


@scenario
def reopened_ticket_relaunched_on_another_kind_frees_its_earlier_owner():
    """Live: dependencies-refresh-03-review completed under a Cursor reviewer, cleanup
    dropped its record, and after the reopen the Codex reviewer's claim was refused until
    the Cursor id was released by hand. The ledger's owner, when it is one of the ticket's
    own ids, is released as that id whatever the stored record says."""
    places = {"cursor:wsl-feat-ro": str(tmp / "wt" / "ro")}
    # A plain shell and the coder's own Cursor, held by its record, hold no claim.
    shell = term("term-sh", 50, ["$ "], ident=None, worktree=str(tmp / "wt" / "ro"))
    shell[0]["lastOutputAt"] = int((time.time() - 120) * 1000)
    coder = term("term-co", 5, ident="cursor", worktree=str(tmp / "wt" / "ro"))
    saved, stopped = relaunch_ro(
        "cursor:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places=places,
        terminals={"Juzzark-WSL": [shell[0], coder[0]]},
        screens={"term-sh": shell[1], "term-co": coder[1]},
        others={
            "co": rec(
                "co",
                "x2",
                agent="cursor",
                host="Juzzark-WSL",
                agent_id="cursor:wsl-feat-co",
                worktree=str(tmp / "wt" / "ro"),
            )
        },
    )
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    released_once("cursor:wsl-feat-ro")
    assert owner_of("ro") is None, "the new worker's start_work finds the ticket unowned"
    saved, stopped = relaunch_ro(
        "cursor:wsl-feat-ro", "Juzzark-WSL", "claude:wsl-feat-ro", "claude", places=places
    )
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    released_once("cursor:wsl-feat-ro")
    assert owner_of("ro") is None
    saved, stopped = relaunch_ro(None, "Juzzark-WSL", None)
    assert stopped is None and saved["agent_id"] == "codex:wsl-ticket-ro", (stopped, saved)
    assert ledger_calls("transfer_work") == [] and len(created()) == 1
    saved, stopped = relaunch_ro("claude:someone-else", "Juzzark-WSL", None)
    assert stopped is None and ledger_calls("transfer_work") == [], "not one of ro's ids"
    assert owner_of("ro") == "claude:someone-else"


def kept(launched, owner, tail):
    """One STUCK naming both ids, no release, nothing started, the owner's claim kept."""
    saved, stopped = launched
    expected = f"STUCK ro codex:wsl-ticket-ro: the earlier owner {owner} {tail}; its claim is kept"
    assert stopped == expected, stopped
    assert stuck_notes() == [f"dev-auto: {expected}"], notes()
    assert ledger_calls("transfer_work") == [] and created() == [] and saved is None
    assert owner_of("ro") == owner


@scenario
def relaunch_over_a_live_earlier_owner_is_stuck():
    """An earlier owner whose agent may still run where it registered, in the ticket's
    worktree or on another host, keeps its claim: one STUCK names both ids and nothing
    starts. So does one whose place the ledger does not record."""
    ro = str(tmp / "wt" / "ro")
    row, screen = term("term-cur", 5, ident="cursor", worktree=ro)
    launched = relaunch_ro(
        "cursor:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places={"cursor:wsl-feat-ro": ro},
        terminals={"Juzzark-WSL": [row]},
    )
    kept(launched, "cursor:wsl-feat-ro", "may still run in term-cur")
    row, _ = term("term-cur", 5, ident="cursor", worktree="/mac2/wt/ro")
    launched = relaunch_ro(
        "cursor:mac2-feat-ro",
        "Juzzark-WSL",
        None,
        places={"cursor:mac2-feat-ro": "/mac2/wt/ro"},
        terminals={"Mac2": [row]},
    )
    kept(launched, "cursor:mac2-feat-ro", "may still run in term-cur")
    row, _ = term("term-cur", 5, ident="cursor", worktree="/wsl/wt/other")
    launched = relaunch_ro(
        "cursor:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places={"cursor:wsl-feat-ro": "/wsl/wt/other"},
        terminals={"Juzzark-WSL": [row]},
    )
    kept(launched, "cursor:wsl-feat-ro", "may still run in term-cur")
    # Orca's identity expires while an agent runs; its screen is no bare shell.
    row, screen = term(
        "term-cl", 5, ["bypass permissions on"], ident=None, worktree=ro
    )
    launched = relaunch_ro(
        "claude:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places={"claude:wsl-feat-ro": ro},
        terminals={"Juzzark-WSL": [row]},
        screens={"term-cl": screen},
    )
    kept(launched, "claude:wsl-feat-ro", "may still run in term-cl")
    # A shell prompt proves nothing until its last output is a minute old: a just-created
    # terminal shows one before its agent starts, and one with no output time is unproven.
    row, screen = term("term-new", 5, ["$ "], ident=None, worktree=ro)
    row["lastOutputAt"] = int((time.time() - 5) * 1000)
    unknown = dict(row, handle="term-old")
    del unknown["lastOutputAt"]
    launched = relaunch_ro(
        "claude:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places={"claude:wsl-feat-ro": ro},
        terminals={"Juzzark-WSL": [row, unknown]},
        screens={"term-new": screen, "term-old": screen},
    )
    kept(launched, "claude:wsl-feat-ro", "may still run in term-new, term-old")
    launched = relaunch_ro("cursor:wsl-feat-ro", "Juzzark-WSL", None)
    kept(launched, "cursor:wsl-feat-ro", "has no worktree in the ledger")
    launched = relaunch_ro(
        "cursor:mac2-feat-ro",
        "Juzzark-WSL",
        None,
        places={"cursor:mac2-feat-ro": "/mac2/wt/ro"},
        terminals={"Mac2": "down"},
    )
    kept(launched, "cursor:mac2-feat-ro", "could not be checked: Mac2's terminal list failed")


@scenario
def refused_release_of_a_reopened_ticket_is_stuck():
    """A release the ledger refuses raises one STUCK with the ledger's message and
    starts no worker."""
    saved, stopped = relaunch_ro(
        "cursor:wsl-feat-ro",
        "Juzzark-WSL",
        None,
        places={"cursor:wsl-feat-ro": str(tmp / "wt" / "ro")},
        release_refused="stale version",
    )
    expected = (
        "STUCK ro codex:wsl-ticket-ro: the earlier owner cursor:wsl-feat-ro's claim was not"
        " released: transfer_work: stale version"
    )
    assert stopped == expected, stopped
    assert stuck_notes() == [f"dev-auto: {expected}"], notes()
    assert len(ledger_calls("transfer_work")) == 1 and created() == [] and saved is None
    assert owner_of("ro") == "cursor:wsl-feat-ro"


@scenario
def features():
    """features against a copy of the trial store."""
    trial = tmp / "trial"
    (trial / ".concord").mkdir(parents=True)
    con = sqlite3.connect(trial / ".concord" / "concord.db")
    con.executescript(trial_sql)
    con.commit()
    con.close()
    repo = tmp / "feature-repo"
    (repo / ".work" / "specs").mkdir(parents=True)
    spec = repo / ".work" / "specs" / "trial-feature.md"
    spec.write_text("# spec\n")
    expected = (
        "trial-feature: 1 done, 4 remaining\n"
        f"  spec: {spec}\n"
        "  done: claude-lane-review\n"
        "  trial-lane [review_ready] script:mac-trial-lane\n"
        "    Brief: protocol trial task created by rpc.py.\n"
        "  claude-lane [review_ready] claude:trial-claude-lane"
        " {Waiting on: Codex reviewer verdict for claude-lane.}\n"
        "  wsl-lane [active] script:wsl-lane\n"
        "    Brief: protocol trial task created by rpc.py.\n"
        "  queued-lane [proposed] (unassigned)\n"
        "    BRIEF: Build the queued lane.\n"
        "probe: 1 done, 0 remaining\n"
        "  done: probe-overlap"
    )
    shown = da.features_text(da.read_tickets(trial), repo)
    assert shown == expected, shown
    done = cli("features", "--repo", str(repo), CONCORD_REPO_ROOT=str(trial))
    assert done.stdout == expected + "\n", done
    done = subprocess.run(
        [sys.executable, str(HERE / "dev_auto.py"), "features"],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo,
        env=dict(os.environ, CONCORD_REPO_ROOT=str(trial)),
    )
    assert done.stdout == expected + "\n", "the repo defaults to the current folder"


@scenario
def ledger_init_and_gate():
    """ledger-init and the SSH gate it installs."""
    home = tmp / "home"
    home.mkdir()
    set_fixture()
    done = cli("ledger-init", "demo", HOME=str(home))
    ledger_dir = home / ".concord-ledger" / "demo"
    assert done.returncode == 0, done
    assert f"export CONCORD_REPO_ROOT={ledger_dir}" in done.stdout, done
    (setup,) = calls("concord")
    assert setup["args"] == [
        "setup",
        "--no-global",
        "--no-mcp",
        "--no-adapters",
    ] and setup["cwd"] == str(ledger_dir)
    assert (
        setup["env"]["CONCORD_REPO_ROOT"] == str(ledger_dir)
        and setup["env"]["CONCORD_TELEMETRY_DISABLED"] == "1"
    )
    assert (ledger_dir / "supervisor").is_dir()
    gate = ledger_dir / "concord-ssh-gate.sh"
    assert (
        os.access(gate, os.X_OK)
        and gate.read_text() == (HERE / "concord-ssh-gate.sh").read_text()
    )
    bad = cli("ledger-init", "../x", HOME=str(home))
    assert bad.returncode != 0 and not (home / "x").exists()

    gate_home = tmp / "gate-home"
    (gate_home / ".local" / "bin").mkdir(parents=True)
    probe = gate_home / ".local" / "bin" / "concord-mcp"
    probe.write_text(
        '#!/bin/sh\necho "$CONCORD_REPO_ROOT|$CONCORD_AGENT_ID|$CONCORD_AGENT_KIND|'
        '$CONCORD_TELEMETRY_DISABLED|$CONCORD_NO_UPDATE_CHECK|${DA_LOG:-clean}"\n'
    )
    probe.chmod(0o755)

    def gate_run(command, prefix="wsl"):
        env = dict(os.environ, HOME=str(gate_home))
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        return subprocess.run(
            [str(gate), prefix], check=False, capture_output=True, text=True, env=env
        )

    for agent in (
        "codex:wsl-lane",
        "claude:mac2-a.b_c",
        "cursor:wsl-x",
        "script:wsl-selfcheck",
    ):
        prefix = "mac2" if agent.startswith("claude:mac2-") else "wsl"
        done = gate_run(f"concord-mcp {agent}", prefix)
        assert done.returncode == 0, (agent, done)
        assert (
            done.stdout == f"{ledger_dir}|{agent}|{agent.split(':')[0]}|1|1|clean\n"
        ), done.stdout
    for command in (
        None,
        "",
        "bash",
        "concord-mcp",
        "concord-mcp codex:lane",
        "concord-mcp codex:wsl-",
        "concord-mcp codex:mac2-lane",
        "concord-mcp bash:wsl-x",
        "concord-mcp codex:wsl-a:b",
        "concord-mcp Codex:wsl-x",
        "concord-mcp codex:wsl-x; touch pwned",
        "concord-mcp codex:wsl-$(id)",
        "concord-mcp codex:wsl-" + "a" * 60,
        "sh -c concord-mcp codex:wsl-x",
    ):
        done = gate_run(command)
        assert done.returncode == 64 and done.stdout == "", (command, done)
    assert not (tmp / "pwned").exists()


@scenario
def ledger_init_records_its_checkout():
    """da-4-5: `ledger-init <repo> --checkout <path>` records the repo's main checkout,
    where evidence is cleaned, with its root commit and origin's URL, the repo's identity.
    A linked worktree, a checkout without .work, or one without origin is refused and
    records nothing."""
    home = tmp / "home-checkout"
    home.mkdir()
    main = real_main("ledger-init-checkout")
    (main / ".work").mkdir()
    linked = tmp / "ledger-init-checkout" / "linked"
    git_at(main, "worktree", "add", "-q", "-b", "task/x", str(linked))
    bare = real_main("ledger-init-no-work")
    no_origin = real_main("ledger-init-no-origin")
    (no_origin / ".work").mkdir()
    git_at(no_origin, "remote", "remove", "origin")
    record = home / ".concord-ledger" / "demo" / "supervisor" / "checkout"
    for path in (linked, bare, no_origin):
        set_fixture(git_bin=GIT)
        done = cli("ledger-init", "demo", "--checkout", str(path), HOME=str(home))
        assert done.returncode != 0 and not record.exists(), (path, done)
    set_fixture(git_bin=GIT)
    done = cli("ledger-init", "demo", "--checkout", str(main), HOME=str(home))
    assert done.returncode == 0, done
    first = subprocess.run(
        [GIT, "-C", str(main), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    url = subprocess.run(
        [GIT, "-C", str(main), "config", "--get", "remote.origin.url"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert record.read_text() == f"{os.path.realpath(main)}\n{first}\n{url}\n", record.read_text()


@scenario
def ledger_init_provisions_and_enforces_the_configured_host_prefix():
    home = tmp / "third-host-gate"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "concord-mcp"
    stub.write_text('#!/bin/sh\necho "$CONCORD_AGENT_ID"\n')
    stub.chmod(0o755)
    config = memory_accounts(tail="""
[hosts.Third]
prefix = "far-host"
address = "100.64.0.92"
ledger_ssh = ["ssh", "me@mac"]
""")
    set_fixture()
    done = cli("ledger-init", "third", HOME=str(home), DEV_AUTO_ACCOUNTS=str(config))
    gate = home / ".concord-ledger" / "third" / "concord-ssh-gate.sh"
    assert done.returncode == 0, done
    assert f'from="100.64.0.92",restrict,command="{gate} far-host"' in done.stdout, done.stdout
    assert "'concord-mcp script:far-host-selfcheck'" in done.stdout, done.stdout
    for agent, code in (("script:far-host-selfcheck", 0), ("codex:wsl-lane", 64),
                        ("codex:far-host-", 64)):
        called = subprocess.run(
            [str(gate), "far-host"], capture_output=True, text=True, timeout=5,
            env={**os.environ, "HOME": str(home), "SSH_ORIGINAL_COMMAND": f"concord-mcp {agent}"},
        )
        assert (called.returncode, called.stdout) == (code, agent + "\n" if code == 0 else ""), called
    config.write_text(config.read_text().replace('"far-host"', '"far host"'))
    invalid = cli("ledger-init", "bad-prefix", HOME=str(home), DEV_AUTO_ACCOUNTS=str(config))
    assert invalid.returncode and "invalid prefix" in invalid.stderr, invalid
    assert not (home / ".concord-ledger" / "bad-prefix").exists()
    config.write_text(config.read_text().replace('"far host"', '"wsl-extra"'))
    overlap = cli("ledger-init", "overlap", HOME=str(home), DEV_AUTO_ACCOUNTS=str(config))
    assert overlap.returncode and "overlapping prefix" in overlap.stderr, overlap
    assert not (home / ".concord-ledger" / "overlap").exists()


@scenario
def trial_ledger_init_from_address():
    """Live: ledger-init printed the authorized_keys line without from=. Each host now
    gets a line limited to its address and the one-time host-key step. The accounts
    are the suite fixture, not the live file the operator edits."""
    home = tmp / "home-from"
    home.mkdir()
    set_fixture()
    done = cli("ledger-init", "demo", HOME=str(home))
    gate = home / ".concord-ledger" / "demo" / "concord-ssh-gate.sh"
    assert done.returncode == 0, done
    lines = done.stdout.splitlines()
    entry = f'from="100.64.0.9",restrict,command="{gate} wsl" <public key>'
    once = "ssh -n -o StrictHostKeyChecking=accept-new -i '~/.ssh/k' me@mac"
    assert lines[1:] == [
        f"Juzzark-WSL: authorized_keys entry for its ledger key: {entry}",
        f"Juzzark-WSL: run once there to trust this Mac's host key: {once} "
        + "'concord-mcp script:wsl-selfcheck'",
        "[hosts.Mac2] has no address: add it, then run ledger-init again",
    ], lines

@scenario
def scenarios_leave_the_live_accounts_file_unread():
    """The skill folder's accounts.toml is the file the operator edits. A scenario
    loads the suite's fixture copy, so none of them names that live path."""
    import inspect

    live = 'HERE.parent / "accounts.toml"'
    readers = [
        fn.__name__
        for fn in SCENARIOS
        if fn.__name__ != "scenarios_leave_the_live_accounts_file_unread"
        and live in inspect.getsource(fn)
    ]
    assert readers == [], readers


@scenario
def trial2_checkin():
    """Every checkin_minutes the orchestrator gets one fixed CHECK-IN line and checkin.txt
    lists open tickets, then those quiet, past their FIX rounds or running long, once per
    interval. It goes to the events file even without an agent in front; only its EVENTS
    wake waits for one."""
    from datetime import UTC

    def iso(epoch):
        return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")

    reset_db()
    for tid in ("ca", "cb", "cc", "cd"):
        ticket_row(tid)
    ticket_row("ce", status="complete")
    ticket_row("cf", status="proposed")
    for _ in range(4):
        update("cb", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:r")
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET updated_at = ?", (iso(NOW - 50 * 60),))
    con.execute("UPDATE tasks SET agent_id = NULL WHERE task_id = 'cf'")
    con.execute("UPDATE task_updates SET created_at = ?", (iso(NOW - 60),))
    con.execute(
        "UPDATE tasks SET updated_at = ? WHERE task_id = 'ca'", (iso(NOW - 600),)
    )
    con.commit()
    con.close()
    every = {**cfg, "checkin_minutes": 30}
    workers = {
        "ca": rec("ca", "c1"),
        "cb": rec("cb", "c2"),
        "cc": rec("cc", "c3", launched_at=NOW - 300 * 60),
    }
    terms = local(*(term(f"term-{t}", 5) for t in workers))
    path = root / "supervisor" / "checkin.txt"
    line = f"CHECK-IN: 3 flagged, see {path}"

    def told():
        return [s for s in sends() if s[0] == "term-orch"]

    def run(st, now):
        da.tick(every, root, st, da.read_tickets(root), now)

    st = fresh_state(**workers)
    world(terms)
    run(st, NOW)
    # cb's unread FIX verdicts also wake cb itself.
    assert told() == [("term-orch", line, True)], sends()
    text = path.read_text()
    assert (
        "ca owner claude:ca state active quiet 10m fix rounds 0 launched 120m ago"
        in text
    )
    assert "cf owner - state proposed quiet 50m fix rounds 0 not launched" in text
    assert "ce " not in text, text
    flagged = text.split("flagged (3):\n")[1].splitlines()
    assert flagged == [
        "cb: 4 FIX rounds",
        "cc: no ledger update for 50m, running 300m",
        "cd: no ledger update for 50m",
    ], text
    # cb answers its wake, so the interval holds no unanswered-wake STUCK.
    update("cb", "progress", "reading the verdicts")
    world(terms)
    run(st, NOW + 29 * 60)
    assert told() == [], "once per interval"
    world(terms, orch_ident=None)
    run(st, NOW + 31 * 60)
    assert [s[1][:9] for s in told()] == ["CHECK-IN:"], told()
    typed = [s for s in sends(events=False) if s[0] == "term-orch"]
    assert st["checkin_at"] == NOW + 31 * 60 and typed == [], "no agent in front"
    st["workers"].clear()
    reset_db()
    world([])
    run(st, NOW + 62 * 60)
    assert sends() == [("term-orch", f"CHECK-IN: 0 flagged, see {path}", True)]
    log = (st.path.parent / "delivered.log").read_text().splitlines()
    checkins = [row.split("\t")[2] for row in log if "CHECK-IN" in row]
    assert checkins[0] == line and checkins[-1] == f"CHECK-IN: 0 flagged, see {path}", log
    world([])
    da.tick(cfg, root, fresh_state(), da.read_tickets(root), NOW)
    assert sends() == [], "checkin_minutes = 0 turns it off"


@scenario
def trial2_waiting_worker_ends_its_turn():
    """Live: /goal workers kept taking turns while they waited and posted Waiting on notes
    again and again. The goal is met by one new Waiting on note, which ends the turn, and
    each wake sets the goal again; a stop and the orchestrator's events set none."""
    set_fixture(git=git)
    ticket = {"task_id": "gw", "title": "f / gw"}
    for agent in ("claude", "codex", "cursor"):
        text = da.brief(rec("gw", "c1", agent=agent), ticket)
        # Codex's goal is met by the open note instead: codex_goal_never_asks_for_a_repeated_wait_note.
        wait = "until it holds a Waiting on" if agent == "codex" else "or record a new Waiting on"
        assert wait in text, text
        assert "end your turn and stay quiet until dev-auto wakes you" in text, text
        assert "Run no typecheck, lint, unit, architecture or whole-suite tests" in text, text
        assert "named integration test files the ticket needs, one file at a time" in text
        assert "~/.agents/skills/dev-auto/scripts/host-check-lock <command>" in text, text
        assert text.startswith("/goal ") == (agent != "cursor"), text
    reset_db()
    ticket_row("gw")
    update("gw", "blocker", "Waiting on: review")
    update("gw", "finding", "REVIEW VERDICT: FIX 1a2b3c4 two findings", "codex:gw-r")
    st = fresh_state(gw=rec("gw", "c1"))
    world(local(term("term-gw", 100)))
    tick(st)
    ((handle, typed, _),) = sends()
    assert handle == "term-gw" and typed.startswith("/goal Finish Concord ticket gw as")
    assert typed.endswith(
        "dev-auto: a review verdict was posted on ticket gw. Read it there and continue."
    )
    assert da.SAFE.fullmatch(typed) and notes()[-1].startswith("dev-auto: a review")
    ticket_row("gl")
    st = fresh_state(gl=rec("gl", "c1"))
    world(local(term("term-gl", 120, ["You've hit your limit · resets 5pm"])))
    tick(st)
    (new,) = created()
    assert shlex.split(opt(new, "--command"))[-1].startswith(
        "/goal Finish Concord ticket gl"
    )


@scenario
def trial2_claude_launch_takes_no_codex():
    """Every pair is two terminals: a Claude launch picks, holds and exports no Codex
    account, so it starts with every Codex slot taken; a record saved with one by an
    earlier version holds no Codex slot."""
    reset_db()
    ticket_row("nc", status="proposed", title="f / nc")
    wt = str(tmp / "wt" / "nc")
    Path(wt).mkdir(parents=True, exist_ok=True)
    full = {
        "a": rec("a", "x1", agent="codex"),
        "b": rec("b", "x1", agent="codex"),
        "c": rec("c", "x2", agent="codex"),
    }
    state_file.write_text(json.dumps({"workers": full}))
    set_fixture()
    done = cli("launch", "nc", "--agent", "claude", "--model", OPUS, "--worktree", wt)
    assert done.returncode == 0, done
    saved = json.loads(state_file.read_text())["workers"]["nc"]
    assert "codex_account" not in saved and "codex_home" not in saved, saved
    command = opt(created()[0], "--command")
    assert "CODEX_HOME" not in command and "command -v codex" not in command, command
    old = rec("o", "c1", codex_account="x2", codex_home=str(homes["x2"]))
    assert pick({"o": old}, agent="codex", model="m") == "x1"
    assert (
        pick({"o": old, "a": full["a"], "b": full["b"]}, agent="codex", model="m")
        == "x2"
    )
    state_file.unlink()


@scenario
def same_lane_features_get_their_own_ids():
    """Same-lane tickets and long ticket IDs get distinct identities. Same-kind,
    same-host relaunch keeps a legacy identity already recorded for the worker."""
    reset_db()
    set_fixture()
    claude_usage("c1", 1, NOW + 600)
    ids = []
    long = "accounts-lifecycle-state-machine-worker-session-authentication-"
    features = (
        ("ld1", "accounts-lifecycle-state"),
        ("ld2", "runs status"),
        (long + "a-design", long + "a"),
        (long + "b-design", long + "b"),
    )
    for tid, feature in features:
        ticket_row(tid, status="proposed", title=f"{feature} / design")
        wt = str(tmp / "wt" / tid)
        Path(wt).mkdir(parents=True, exist_ok=True)
        done = cli("launch", tid, "--agent", "claude", "--model", OPUS, "--worktree", wt)
        assert done.returncode == 0, done
        ids.append(json.loads(state_file.read_text())["workers"][tid]["agent_id"])
    assert ids[:2] == [
        "claude:ticket-ld1",
        "claude:ticket-ld2",
    ]
    # Over 64 characters, two features that differ only near the end stay apart.
    assert ids[2] != ids[3], ids
    for got in ids[2:]:
        assert len(got) == 64 and re.fullmatch(r"claude:[a-z0-9._-]+", got), got
        assert got.startswith("claude:ticket-accounts-lifecycle-state-machine-"), got
    remote = da.agent_id(cfg, "codex", "Juzzark-WSL", long + "a-design")
    assert len(remote) == 64 and remote.startswith("codex:wsl-accounts-"), remote
    assert remote == da.agent_id(cfg, "codex", "Juzzark-WSL", long + "a-design")
    saved = json.loads(state_file.read_text())
    saved["workers"]["ld1"]["agent_id"] = "claude:design"
    state_file.write_text(json.dumps(saved))
    set_fixture(terminals={"local": []})
    wt = str(tmp / "wt" / "ld1")
    for agent, model, want in (("claude", OPUS, "claude:design"),
                               ("codex", "m", "codex:ticket-ld1")):
        done = cli("launch", "ld1", "--agent", agent, "--model", model, "--worktree", wt)
        assert done.returncode == 0, done
        got = json.loads(state_file.read_text())["workers"]["ld1"]["agent_id"]
        assert got == want, (agent, got)
    state_file.unlink()
    clear_usage()


@scenario
def distinct_ticket_ids_survive_colliding_title_slugs():
    reset_db()
    set_fixture()
    try:
        for tid, title in (("id-41", "a-b / c"), ("id-42", "a / b-c")):
            ticket_row(tid, status="proposed", title=title)
            worktree = tmp / "wt" / tid
            worktree.mkdir(parents=True, exist_ok=True)
            done = cli("launch", tid, "--agent", "codex", "--model", "m", "--worktree", str(worktree))
            assert done.returncode == 0, done
        saved = json.loads(state_file.read_text())["workers"]
        assert [saved[tid]["agent_id"] for tid in ("id-41", "id-42")] == [
            "codex:ticket-id-41", "codex:ticket-id-42"
        ], saved
    finally:
        state_file.unlink(missing_ok=True)


@scenario
def trial2_closed_terminal_is_not_other():
    """Live: the coder's cleanup closed its terminal, then the reviewer's cleanup in the
    same tick named that terminal as another in the worktree. A terminal dev-auto did not
    launch still blocks."""
    reset_db()
    ticket_row("s1", status="complete")
    ticket_row("s1-review", status="complete")
    pair = str(tmp / "wt" / "s1")
    for extra in ((), (term("term-op", 100, ident=None, worktree=pair),)):
        st = fresh_state(
            s1=rec("s1", "c1"),
            **{"s1-review": rec("s1-review", "x1", agent="codex", worktree=pair)},
        )
        reviewer = term("term-s1-review", 100, ident="codex", worktree=pair)
        world(local(term("term-s1", 100), reviewer, *extra), git=git, gh=merged)
        tick(st)
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        if not extra:
            assert sends() == [] and st["workers"] == {}, sends()
            assert closed == ["term-s1", "term-s1-review"], closed
            assert len(calls("orca", ["worktree", "rm"])) == 1
            continue
        busy = (
            "STUCK s1-review: ticket is complete but its worktree holds other "
            "terminals term-op. Not removed"
        )
        assert sends() == [("term-orch", busy, True)], sends()
        assert closed == ["term-s1", "term-s1-review"], closed
        assert calls("orca", ["worktree", "rm"]) == []


@scenario
def trial2_delivered_events_outlive_drop():
    """Dropping a ticket forgets its launch-scoped sent keys but not the delivered-events
    log, which never stops a fresh launch's event from being delivered again."""
    reset_db()
    log = tmp / "delivered.log"
    log.unlink(missing_ok=True)
    st = fresh_state(d1=rec("d1", "c1"))
    world([])
    assert da.emit(st, "d1|lost|7", "STUCK d1: terminal gone")
    da.drop(st, "d1")
    assert st["sent"] == {}
    world([])
    assert da.emit(st, "d1|lost|7", "STUCK d1: terminal gone")
    assert len(sends()) == 1
    lines = [line.split("\t") for line in log.read_text().splitlines()]
    assert [line[1:] for line in lines] == [
        ["d1", "STUCK d1: terminal gone"]
    ] * 2
    st["workers"]["d1"] = rec("d1", "c1")
    world([], send_fails=["term-d1"])
    assert not da.emit(st, "d1|nudge|1", "x", worker=st["workers"]["d1"])
    assert len(log.read_text().splitlines()) == 2


@scenario
def dropped_record_keeps_its_ticket_and_pr_alerts():
    """da-logs-6: dropping a record keeps the sent keys of alerts tied to a ticket update or
    a head, so a reopened ticket's same operator wait, the same CLEARED REVIEW or the same
    PR RED head appends nothing again; a launch-scoped key is forgotten."""
    reset_db()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    st = fresh_state(dz=rec("dz", "c1"))
    alerts = (
        ("dz|needs|10010", "NEEDS YOU dz: its worker waits on the operator, update 10010 on the ticket"),
        ("dz|cleared|7", "CLEARED REVIEW dz: PUSH abc1234"),
        ("dz|pr-red|657f2cb|test", "PR RED dz: #16 at 657f2cb, test failed"),
    )
    launch = ("dz|lost|1", "STUCK dz: terminal gone")
    world([])
    for key, text in (*alerts, launch):
        assert da.emit(st, key, text)
    da.drop(st, "dz")
    world([])
    for key, text in alerts:
        assert not da.emit(st, key, text), key
    assert da.emit(st, *launch)
    texts = [l.split(" ", 2)[2] for l in event_lines()]
    assert texts == [t for _, t in alerts] + [launch[1], f"{launch[1]} (2)"], texts


@scenario
def wake_on_later_ticket_update():
    """Waiting on ticket:<id> wakes once a tick, for the newest later update another agent
    posts on either ticket, and still wakes when that ticket is done. The waiting worker
    and script:dev-auto do not count."""
    reset_db()
    ticket_row("dep")
    ticket_row("w1")
    update("dep", "progress", "already posted", agent="claude:dep")
    update("w1", "blocker", "Waiting on: ticket:dep")
    st = fresh_state(w1=rec("w1", "c1"))
    screen = local(term("term-w1", 100))
    world(screen)
    tick(st)
    assert sends() == [], "an update from before the wait is not a reply"
    update("dep", "progress", "schema is ready", agent="claude:dep")
    update("w1", "progress", "preview for you", agent="claude:dep")
    update("w1", "finding", "dev-auto: held", agent="script:dev-auto")
    update("dep", "progress", "my own note", agent="claude:w1")
    world(screen)
    tick(st)
    on_w1 = "dev-auto: a new update was posted on ticket w1. Continue ticket w1."
    assert sends() == [("term-w1", woke("w1", on_w1), True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], "each update wakes once"
    ticket_row("dep", status="closed")
    world(screen)
    tick(st)
    done = "dev-auto: ticket dep is closed. Continue ticket w1."
    assert sends() == [("term-w1", woke("w1", done), True)], sends()


@scenario
def one_wake_for_a_burst_of_updates():
    """Live (delivered.log 281-289): nine coder updates in 44 seconds typed nine wakes into
    the reviewer waiting on the coder's ticket. A tick types one, for the newest update;
    a later update wakes once more. The updates name the review ticket: other coder
    progress wakes no reviewer (coder_progress_wakes_no_reviewer)."""
    reset_db()
    ticket_row("bu")
    ticket_row("bu-review", agent="codex:bu-review")
    waiting = update("bu-review", "blocker", "Waiting on: ticket:bu", agent="codex:bu-review")
    reviewer = rec("bu-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"bu-review": reviewer})
    screen = local(term("term-bu-review", 100, ident="codex"))
    for n in range(9):
        update("bu", "progress", f"bu-review: step {n} done")
    world(screen)
    tick(st)
    text = "dev-auto: a new update was posted on ticket bu. Continue ticket bu-review."
    wake = ("term-bu-review", woke("bu-review", text, "codex"), True)
    assert sends() == [wake], sends()
    world(screen)
    tick(st)
    assert sends() == [], "the burst is delivered once"
    newest = update("bu", "progress", "bu-review: step 9 done")
    world(screen)
    tick(st)
    # A repeat of a typed text carries its event.
    again = ("term-bu-review", woke("bu-review", f"{text} Event wake {waiting} bu {newest}.", "codex"), True)
    assert sends() == [again], sends()


@scenario
def review_ready_and_wait_note_wake_once():
    """R2: REVIEW READY and the coder's wait-for-review note in one tick typed three wakes
    into the reviewer waiting on the coder's ticket. It gets one, the REVIEW READY one,
    and the two updates do not wake it on a later tick."""
    reset_db()
    ticket_row("rw")
    ticket_row("rw-review", agent="codex:rw-review")
    update("rw-review", "blocker", "Waiting on: ticket:rw", agent="codex:rw-review")
    reviewer = rec("rw-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"rw-review": reviewer})
    screen = local(term("term-rw-review", 100, ident="codex"))
    update("rw", "progress", "REVIEW READY 1a2b3c4")
    update("rw", "blocker", "Waiting on: review of 1a2b3c4")
    world(screen)
    tick(st)
    text = (
        "dev-auto: REVIEW READY was posted on ticket rw. Review it as your ticket says."
    )
    assert sends() == [("term-rw-review", woke("rw-review", text, "codex"), True)], (
        sends()
    )
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def coder_progress_wakes_no_reviewer():
    """Live (delivered.log 11-11..11-14): 412 of 435 new-update wakes went to reviewers
    waiting on their coder's ticket. A reviewer waiting on its coder is woken only by a
    review request, an update that names its review ticket, or the coder ticket
    completing, each once."""
    reset_db()
    ticket_row("cw")
    ticket_row("cw-review", agent="codex:cw-review")
    update("cw-review", "blocker", "Waiting on: ticket:cw", agent="codex:cw-review")
    reviewer = rec("cw-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"cw-review": reviewer})
    screen = local(term("term-cw-review", 100, ident="codex"))
    update("cw", "progress", "schema migrated, tests next")
    update("cw", "finding", "baseline recorded", agent="claude:orchestrator")
    world(screen)
    tick(st)
    assert sends() == [], "coder and orchestrator notes are not for the reviewer"
    update("cw", "progress", "REVIEW READY 1a2b3c4")
    world(screen)
    tick(st)
    ready = "dev-auto: REVIEW READY was posted on ticket cw. Review it as your ticket says."
    assert sends() == [("term-cw-review", woke("cw-review", ready, "codex"), True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("cw", "progress", "cw-review: the probe log is at /tmp/probe.log")
    world(screen)
    tick(st)
    named = "dev-auto: a new update was posted on ticket cw. Continue ticket cw-review."
    assert sends() == [("term-cw-review", woke("cw-review", named, "codex"), True)], sends()
    ticket_row("cw", status="complete")
    update("cw", "progress", "PR title and body recorded")
    world(screen)
    tick(st)
    done = "dev-auto: ticket cw is complete. Continue ticket cw-review."
    assert sends() == [("term-cw-review", woke("cw-review", done, "codex"), True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def verdict_and_reply_wake_once():
    """A verdict on a worker's own ticket and a reply on the ticket it waits on, in one
    tick, give it one wake."""
    reset_db()
    ticket_row("dep")
    ticket_row("vr")
    update("vr", "blocker", "Waiting on: ticket:dep")
    st = fresh_state(vr=rec("vr", "c1"))
    screen = local(term("term-vr", 100))
    update(
        "vr", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "codex:vr-review"
    )
    update("dep", "progress", "schema is ready", agent="claude:dep")
    world(screen)
    tick(st)
    text = "dev-auto: a new update was posted on ticket dep. Continue ticket vr."
    assert sends() == [("term-vr", woke("vr", text), True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], sends()


@scenario
def own_wait_note_is_no_reply():
    """A Waiting on note the coder writes on its own ticket wakes nobody waiting on that
    ticket; its next real update addressed to the reviewer does."""
    reset_db()
    ticket_row("ow2")
    ticket_row("ow2-review", agent="codex:ow2-review")
    update("ow2-review", "blocker", "Waiting on: ticket:ow2", agent="codex:ow2-review")
    reviewer = rec("ow2-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"ow2-review": reviewer})
    screen = local(term("term-ow2-review", 100, ident="codex"))
    update("ow2", "blocker", "Waiting on: operator to pick the layout")
    world(screen)
    tick(st)
    assert sends() == [], sends()
    update("ow2", "progress", "layout picked, building it; ow2-review, recheck the grid")
    world(screen)
    tick(st)
    text = (
        "dev-auto: a new update was posted on ticket ow2. Continue ticket ow2-review."
    )
    assert sends() == [("term-ow2-review", woke("ow2-review", text, "codex"), True)], (
        sends()
    )


@scenario
def wake_both_directions_when_the_blocker_is_older():
    """Each side waits on the other. Neither's own Waiting on note wakes the other. A
    later post on a waiter's own ticket wakes that waiter once, and the opposite post
    wakes the other. Neither waiter posts again, so its blocker stays older than the
    reply."""
    reset_db()
    ticket_row("p02")
    ticket_row("p03")
    update("p03", "blocker", "Waiting on: ticket:p02")
    update("p02", "blocker", "Waiting on: ticket:p03")
    st = fresh_state(p02=rec("p02", "c1"), p03=rec("p03", "c2"))
    screen = local(term("term-p02", 100), term("term-p03", 100))
    world(screen)
    tick(st)
    assert sends() == [], "p02's blocker is no reply for p03"
    update("p03", "progress", "preview round", agent="claude:p02")
    update("p02", "progress", "comparer reply", agent="claude:p03")
    world(screen)
    tick(st)
    on_03 = "dev-auto: a new update was posted on ticket p03. Continue ticket p03."
    on_02 = "dev-auto: a new update was posted on ticket p02. Continue ticket p02."
    assert sorted(sends()) == sorted(
        [
            ("term-p02", woke("p02", on_02), True),
            ("term-p03", woke("p03", on_03), True),
        ]
    ), sends()
    world(screen)
    tick(st)
    assert sends() == [], "each update wakes once"


@scenario
def review_ready_newer_than_verdict_for_same_sha():
    """A REVIEW READY posted after the latest verdict for that same SHA wakes the reviewer."""
    reset_db()
    ticket_row("pv2")
    ticket_row("pv2-review", agent="codex:pv2-review")
    reviewer = rec("pv2-review", "x1", agent="codex", model="gpt-6.1-sol")
    screen = local(term("term-pv2-review", 5, ident="codex"))
    sha = "e632a0c1a"
    update("pv2", "finding", f"REVIEW VERDICT: FIX {sha} one finding", "codex:pv2-review")
    update("pv2", "progress", f"REVIEW READY {sha}")
    st = fresh_state(**{"pv2-review": reviewer})
    world(screen)
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket pv2. Review it as your ticket says."
    )
    assert sends() == [("term-pv2-review", woke("pv2-review", wake, "codex"), True)], (
        sends()
    )
    world(screen)
    tick(st)
    assert sends() == [], "that READY is delivered once"


@scenario
def review_ready_answered_by_a_withdrawn_verdict_still_wakes():
    """A verdict on the requested SHA that its reviewer then supersedes answers nothing:
    the request still wakes the reviewer, the coder's wait is open and the reviewer has
    work left."""
    reset_db()
    ticket_row("pw3")
    ticket_row("pw3-review", agent="codex:pw3-review")
    sha = "e632a0c1a"
    wait = update("pw3", "blocker", f"Waiting on: review of {sha}\nREVIEW READY {sha}")
    verdict = update("pw3", "finding", f"REVIEW VERDICT: PUSH {sha}", "codex:pw3-review")
    supersede(verdict, update("pw3", "decision", "verdict withdrawn", "codex:pw3-review"))
    st = fresh_state(**{"pw3-review": rec("pw3-review", "x1", agent="codex", model="gpt-6.1-sol")})
    tickets = da.read_tickets(root)
    coder = tickets["", "pw3"]
    blocker = next(u for u in coder["updates"] if u["id"] == wait)
    assert not da.wait_answered(st, "pw3", coder, tickets, blocker)
    assert not da.reviewed_all(coder) and da.open_request(coder)
    assert da.review_wait_gap({}, "pw3", coder, tickets, blocker) is None
    world(local(term("term-pw3-review", 5, ident="codex")))
    tick(st)
    wake = "dev-auto: REVIEW READY was posted on ticket pw3. Review it as your ticket says."
    mine = [s for s in sends() if s[0] == "term-pw3-review"]
    assert mine == [("term-pw3-review", woke("pw3-review", wake, "codex"), True)], sends()


@scenario
def review_wait_for_a_sha_is_answered_only_by_its_verdict():
    """A wait naming SHA A is not answered by a verdict on B or one naming no SHA, nor by
    A's verdict once superseded; a live verdict on A answers it."""
    reset_db()
    ticket_row("wa")
    ticket_row("wa-review", agent="codex:wa-review")
    wait = update("wa", "blocker", "Waiting on: review of 1a2b3c4\nREVIEW READY 1a2b3c4")
    st = fresh_state()
    say = lambda text: update("wa", "finding", text, "codex:wa-review")  # noqa: E731

    def answered():
        tickets = da.read_tickets(root)
        coder = tickets["", "wa"]
        blocker = next(u for u in coder["updates"] if u["id"] == wait)
        yes = da.wait_answered(st, "wa", coder, tickets, blocker)
        gap = da.review_wait_gap({}, "wa", coder, tickets, blocker)
        return yes, gap is None

    bad = []
    say("REVIEW VERDICT: FIX 5e6f7a8 another commit")
    if answered()[0]:
        bad.append("a verdict on B")
    say("REVIEW VERDICT: FIX one finding")
    if answered()[0]:
        bad.append("a verdict naming no SHA")
    supersede(say("REVIEW VERDICT: PUSH 1a2b3c4"), say("withdrawn"))
    if answered()[0]:
        bad.append("A's superseded verdict")
    assert answered()[1], "the open request on A can still end the wait"
    say("REVIEW VERDICT: PUSH 1a2b3c4")
    if answered() != (True, True):
        bad.append("A's live verdict did not answer it")
    assert not bad, bad


@scenario
def review_ready_withdrawn_by_the_coder_wakes_nobody():
    """A REVIEW READY the coder supersedes before the next tick is no request."""
    reset_db()
    ticket_row("pw4")
    ticket_row("pw4-review", agent="codex:pw4-review")
    ready = update("pw4", "progress", "REVIEW READY 1a2b3c4")
    supersede(ready, update("pw4", "decision", "not ready: a check is red"))
    st = fresh_state(**{"pw4-review": rec("pw4-review", "x1", agent="codex", model="gpt-6.1-sol")})
    assert da.open_request(da.read_tickets(root)["", "pw4"]) is None
    world(local(term("term-pw4-review", 5, ident="codex")))
    tick(st)
    assert sends() == [], sends()


@scenario
def review_ready_line_after_wait_line_wakes_once():
    """Live (dev-auto-fixes-02 update 1856): a coder posted "Waiting on: review" with
    "REVIEW READY <sha>" on the next line, and its reviewer was woken by hand 40 minutes
    later. A REVIEW READY line anywhere in the coder's update wakes the reviewer once, and
    the wake names that line's SHA."""
    reset_db()
    ticket_row("wr")
    ticket_row("wr-review", agent="codex:wr-review")
    reviewer = rec("wr-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"wr-review": reviewer})
    screen = local(term("term-wr-review", 5, ident="codex"))
    sha = "9c0ffee1"
    update("wr", "blocker", f"Waiting on: review of {sha}\nREVIEW READY {sha}")
    world(screen)
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket wr. Review it as your ticket says."
    )
    assert sends() == [("term-wr-review", woke("wr-review", wake, "codex"), True)], (
        sends()
    )
    assert any(n.startswith(wake) and sha in n for n in notes()), notes()
    world(screen)
    tick(st)
    assert sends() == [], "that READY is delivered once"


@scenario
def review_ready_first_line_wakes_once():
    """A coder update whose first line starts with REVIEW READY wakes the reviewer once."""
    reset_db()
    ticket_row("fr")
    ticket_row("fr-review", agent="codex:fr-review")
    reviewer = rec("fr-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"fr-review": reviewer})
    screen = local(term("term-fr-review", 5, ident="codex"))
    update("fr", "progress", "REVIEW READY 1a2b3c4\nSuite and ruff pass.")
    world(screen)
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket fr. Review it as your ticket says."
    )
    assert sends() == [("term-fr-review", woke("fr-review", wake, "codex"), True)], (
        sends()
    )
    world(screen)
    tick(st)
    assert sends() == [], "that READY is delivered once"


@scenario
def review_ready_quoted_or_not_by_coder_wakes_nobody():
    """REVIEW READY quoted mid-sentence by the coder, or posted by anyone but the coder,
    wakes no reviewer."""
    reset_db()
    ticket_row("qr")
    ticket_row("qr-review", agent="codex:qr-review")
    reviewer = rec("qr-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"qr-review": reviewer})
    update("qr", "progress", "Running the suite; then REVIEW READY 1a2b3c4.")
    update("qr", "progress", "Suite passes.\nNext I post REVIEW READY 1a2b3c4.")
    update("qr", "finding", "REVIEW READY 1a2b3c4", agent="claude:orchestrator")
    update("qr", "finding", "Waiting on: review\nREVIEW READY 1a2b3c4", "codex:qr-review")
    world(local(term("term-qr-review", 5, ident="codex")))
    tick(st)
    assert sends() == [], sends()


@scenario
def review_ready_without_its_exact_words_and_a_sha_wakes_nobody():
    """A line starting REVIEW READY with no SHA after it, or a lookalike such as REVIEW
    READYING, is no review request and wakes no reviewer."""
    reset_db()
    ticket_row("mr")
    ticket_row("mr-review", agent="codex:mr-review")
    st = fresh_state(**{"mr-review": rec("mr-review", "x1", agent="codex", model="gpt-6.1-sol")})
    lines = ("REVIEW READY", "REVIEW READY once the checks finish", "REVIEW READYING 1a2b3c4")
    for line in lines:
        assert da.review_request(line) is None, line
        update("mr", "progress", line)
    world(local(term("term-mr-review", 5, ident="codex")))
    tick(st)
    assert sends() == [], sends()


# run-dialog-selection-01 update 5418, the coder's only review request; nobody was woken.
WAIT_5418 = (
    "Waiting on: review — a71ff2252 on fix/run-dialog-selection is REVIEW READY (checks "
    "green; red/green recorded in finish_work). After REVIEW VERDICT: PUSH, record PR "
    "title and body (pr skill)."
)


@scenario
def review_ready_in_wait_note_wakes_once():
    """Live (update 5418): a review-wait line that says REVIEW READY and names one SHA
    wakes the reviewer for that SHA once."""
    reset_db()
    ticket_row("wn")
    ticket_row("wn-review", agent="codex:wn-review")
    reviewer = rec("wn-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"wn-review": reviewer})
    screen = local(term("term-wn-review", 5, ident="codex"))
    update("wn", "blocker", WAIT_5418)
    world(screen)
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket wn. Review it as your ticket says."
    )
    assert sends() == [("term-wn-review", woke("wn-review", wake, "codex"), True)], (
        sends()
    )
    assert any(n.startswith(wake) and "a71ff2252" in n for n in notes()), notes()
    world(screen)
    tick(st)
    assert sends() == [], "that request is delivered once"


@scenario
def review_wait_naming_one_sha_is_a_request():
    """A coder's review wait naming exactly one full or short SHA is a review request
    without the words REVIEW READY: it wakes the reviewer once."""
    reset_db()
    ticket_row("wy")
    ticket_row("wy-review", agent="codex:wy-review")
    reviewer = rec("wy-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"wy-review": reviewer})
    screen = local(term("term-wy-review", 5, ident="codex"))
    update("wy", "blocker", "Waiting on: review of 9c1d2e3")
    world(screen)
    tick(st)
    wake = "dev-auto: REVIEW READY was posted on ticket wy. Review it as your ticket says."
    assert sends() == [("term-wy-review", woke("wy-review", wake, "codex"), True)], sends()
    world(screen)
    tick(st)
    assert sends() == [], "once"
    assert da.review_request("Waiting on: review of " + "ab" * 20) == "ab" * 20


@scenario
def review_request_with_no_reviewer_running_is_one_stuck():
    """A coder's review request while its review ticket is open and no record runs its
    reviewer is one STUCK per request; a live reviewer record raises none."""
    reset_db()
    ticket_row("nr")
    ticket_row("nr-review", status="proposed", agent=None)
    update("nr", "progress", "REVIEW READY 4d5e6f7")
    st = fresh_state(nr=rec("nr", "c1"))
    screen = local(term("term-nr", 5))
    world(screen)
    tick(st)
    stuck = [t for _w, t, _e in sends() if "no reviewer" in t]
    assert stuck == ["STUCK nr: review requested, no reviewer running"], sends()
    world(screen)
    tick(st)
    assert [t for _w, t, _e in sends() if "no reviewer" in t] == [], "once per request"
    update("nr", "progress", "REVIEW READY 8a9b0c1")
    st["workers"]["nr-review"] = rec("nr-review", "x1", agent="codex", model="gpt-6.1-sol")
    world(local(term("term-nr", 5), term("term-nr-review", 5, ident="codex")))
    tick(st)
    assert [t for _w, t, _e in sends() if "no reviewer" in t] == [], sends()
    st["workers"]["nr-review"]["lost"] = "STUCK nr-review: gone"
    world(screen)
    tick(st)
    assert [t for _w, t, _e in sends() if "no reviewer" in t] == [
        "STUCK nr: review requested, no reviewer running"
    ], sends()
    ticket_row("nr-review", status="active", agent="claude:orchestrator")
    update("nr", "progress", "REVIEW READY 2b3c4d5")
    world(screen)
    tick(st)
    assert [t for _w, t, _e in sends() if "no reviewer" in t] == [], "claimed outside dev-auto"


@scenario
def review_ready_then_its_wait_with_no_reviewer_is_one_stuck():
    """REVIEW READY <sha>, then a review wait naming that SHA, with the review ticket
    open and no reviewer running: one STUCK for the one request."""
    reset_db()
    ticket_row("ns")
    ticket_row("ns-review", status="proposed", agent=None)
    update("ns", "progress", "REVIEW READY 4d5e6f7")
    st = fresh_state(ns=rec("ns", "c1"))
    world(local(term("term-ns", 5)))
    tick(st)
    stuck = [t for _w, t, _e in sends() if "no reviewer" in t]
    assert stuck == ["STUCK ns: review requested, no reviewer running"], sends()
    update("ns", "blocker", "Waiting on: review of 4d5e6f7")
    world(local(term("term-ns", 5)))
    tick(st)
    assert [t for _w, t, _e in sends() if "no reviewer" in t] == [], "the same SHA is one request"


@scenario
def idle_at_its_prompt_nudged_after_idle_minutes():
    """A worker at its own prompt, no turn running and no wait note, is nudged after
    idle_minutes and STUCK idle_minutes after that; one in a turn waits silent_minutes."""
    reset_db()
    ticket_row("ip")
    st = fresh_state(ip=rec("ip", "c1"))
    prompt = ["⏺ Done with step 2.", "> ", "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]
    world(local(term("term-ip", 9 * 60, prompt)))
    tick(st)
    assert sends() == []
    world(local(term("term-ip", 11 * 60, prompt)))
    tick(st)
    nudge = "dev-auto: no output for 10 minutes while ticket ip is open. Continue, or record a Waiting on: blocker note on it."
    assert sends() == [("term-ip", woke("ip", nudge), True)], sends()
    world(local(term("term-ip", 21 * 60 + 60, prompt)))
    tick(st, NOW + 660)
    ((who, text, _),) = sends()
    assert who == "term-orch" and "silent 10 minutes after a nudge" in text, text
    reset_db()
    ticket_row("it")
    st = fresh_state(it=rec("it", "c1"))
    busy = ["✻ Thinking… (12m · esc to interrupt)", "  ⏵⏵ bypass permissions on"]
    world(local(term("term-it", 20 * 60, busy)))
    tick(st)
    assert sends() == [], "a running turn keeps silent_minutes"


@scenario
def review_ready_in_wait_note_needs_one_sha_review_and_coder():
    """The 5418 text answered by a verdict on its SHA, then a review-wait line with no
    SHA or two SHAs, the words in a wait on ci, a ticket or a process, and the 5418 text
    by anyone but the coder wake no reviewer."""
    reset_db()
    ticket_row("wx")
    ticket_row("wx-review", agent="codex:wx-review")
    reviewer = rec("wx-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"wx-review": reviewer})
    update("wx", "blocker", WAIT_5418)
    update("wx", "finding", "REVIEW VERDICT: PUSH a71ff2252", "codex:wx-review")
    update("wx", "blocker", "Waiting on: review — the branch is REVIEW READY.")
    update("wx", "blocker", "Waiting on: review — 1a2b3c4 or 5d6e7f8 is REVIEW READY.")
    update("wx", "blocker", "Waiting on: review — a71ff2252 or a71ff22aa is REVIEW READY.")
    update("wx", "blocker", "Waiting on: ci — a71ff2252 is REVIEW READY once green.")
    update("wx", "blocker", "Waiting on: ticket:dep — then a71ff2252 is REVIEW READY.")
    update("wx", "blocker", "Waiting on: process 4242, then a71ff2252 is REVIEW READY.")
    update("wx", "finding", WAIT_5418, agent="claude:orchestrator")
    update("wx", "finding", WAIT_5418, agent="codex:wx-review")
    world(local(term("term-wx-review", 5, ident="codex")))
    tick(st)
    typed = sends()
    assert [s for s in typed if s[0] == "term-wx-review"] == [], typed


@scenario
def review_ready_bare_and_wait_note_one_wake():
    """A bare REVIEW READY <sha> line and then a review-wait note naming the same SHA as
    REVIEW READY, on separate ticks, give the reviewer one wake."""
    reset_db()
    ticket_row("bw")
    ticket_row("bw-review", agent="codex:bw-review")
    reviewer = rec("bw-review", "x1", agent="codex", model="gpt-6.1-sol")
    st = fresh_state(**{"bw-review": reviewer})
    screen = local(term("term-bw-review", 5, ident="codex"))
    update("bw", "progress", "REVIEW READY a71ff2252")
    world(screen)
    tick(st)
    wake = (
        "dev-auto: REVIEW READY was posted on ticket bw. Review it as your ticket says."
    )
    assert sends() == [("term-bw-review", woke("bw-review", wake, "codex"), True)], (
        sends()
    )
    update("bw", "blocker", WAIT_5418)
    world(screen)
    tick(st)
    assert sends() == [], "the same SHA is one request"


@scenario
def review_request_run_id_yields_the_lettered_sha():
    """A review-wait line with one SHA and a digit-only CI run id yields that SHA.

    The same holds when that line also names a PR number and a date. A short form
    and a long form of one SHA still yield the long one.
    """
    run_id = "Waiting on: review. REVIEW READY 1a2b3c4d; CI run 19283746551 green"
    assert da.review_request(run_id) == "1a2b3c4d", da.review_request(run_id)
    with_pr_and_date = (
        "Waiting on: review. REVIEW READY 1a2b3c4d; CI run 19283746551; "
        "PR 1234567; date 20261114"
    )
    assert da.review_request(with_pr_and_date) == "1a2b3c4d", da.review_request(
        with_pr_and_date
    )
    short_long = "Waiting on: review. REVIEW READY 1a2b3c4 (1a2b3c4d0e1f2)"
    assert da.review_request(short_long) == "1a2b3c4d0e1f2", da.review_request(
        short_long
    )


@scenario
def review_request_two_shas_or_digits_alone_yield_none():
    """Two real SHAs yield none. An 8-digit date or a PR number alone yields none."""
    two = "Waiting on: review. REVIEW READY 1a2b3c4d or 5e6f7a8b"
    assert da.review_request(two) is None, da.review_request(two)
    date = "Waiting on: review. REVIEW READY on 20261114."
    assert da.review_request(date) is None, da.review_request(date)
    pr = "Waiting on: review. REVIEW READY for PR 1234567."
    assert da.review_request(pr) is None, da.review_request(pr)


@scenario
def claude_first_run_is_seeded():
    """A fresh Claude launch writes the settings and claude.json keys that suppress the
    theme picker, security notes, trust dialog, bypass-permissions accept, and the
    fullscreen-renderer offer. A theme the account already chose is left as it is."""
    reset_db()
    ticket_row("fr", status="proposed", title="feat / fr")
    ticket_row("fr2", status="proposed", title="feat / fr2")
    wt = str(tmp / "wt" / "fr")
    wt2 = str(tmp / "wt" / "fr2")
    Path(wt).mkdir(parents=True, exist_ok=True)
    Path(wt2).mkdir(parents=True, exist_ok=True)
    (homes["c2"] / "settings.json").write_text(
        json.dumps({"theme": "light", "extra": 1})
    )
    (homes["c2"] / ".claude.json").write_text(
        json.dumps(
            {"numStartups": 3, "projects": {wt2: {"keep": True, "hasTrustDialogAccepted": False}}}
        )
    )
    state_file.unlink(missing_ok=True)
    set_fixture()
    claude_usage("c1", 1, NOW + 600)
    da.main(["set-orchestrator", "term-orch"])
    da.main(["launch", "fr", "--agent", "claude", "--model", OPUS, "--worktree", wt])
    settings = json.loads((homes["c1"] / "settings.json").read_text())
    dot = json.loads((homes["c1"] / ".claude.json").read_text())
    assert settings["theme"] == "dark", settings
    assert settings["skipDangerousModePermissionPrompt"] is True, settings
    assert settings["tui"] == "default", settings
    assert dot["hasCompletedOnboarding"] is True, dot
    assert dot["projects"][wt]["hasTrustDialogAccepted"] is True, dot
    da.main(["launch", "fr2", "--agent", "claude", "--model", OPUS, "--worktree", wt2])
    kept = json.loads((homes["c2"] / "settings.json").read_text())
    dot2 = json.loads((homes["c2"] / ".claude.json").read_text())
    assert kept["theme"] == "light" and kept["extra"] == 1, kept
    assert kept["skipDangerousModePermissionPrompt"] is True and kept["tui"] == "default"
    assert dot2["numStartups"] == 3, dot2
    assert dot2["projects"][wt2]["keep"] is True, dot2
    assert dot2["projects"][wt2]["hasTrustDialogAccepted"] is True, dot2
    assert dot2["hasCompletedOnboarding"] is True
    state_file.unlink()
    clear_usage()


@scenario
def claude_seed_leaves_an_unreadable_profile_alone():
    """da-2-3: a profile file read while Claude rewrites it is partial JSON. The seed
    leaves it byte for byte instead of replacing it with only the seed keys; a valid one
    still gets them, and no temporary file is left beside either."""
    home = tmp / "homes" / "seed"
    home.mkdir(parents=True, exist_ok=True)
    wt = tmp / "wt" / "seed"
    wt.mkdir(parents=True, exist_ok=True)
    partial = {"settings.json": '{"theme": "light", "extra"', ".claude.json": '{"projects": {'}
    for name, text in partial.items():
        (home / name).write_text(text)
    da.seed_claude_launch({"home": str(home)}, str(wt), "local")
    for name, text in partial.items():
        assert (home / name).read_text() == text, (name, (home / name).read_text())
    (home / "settings.json").write_text(json.dumps({"extra": 1}))
    (home / ".claude.json").unlink()
    da.seed_claude_launch({"home": str(home)}, str(wt), "local")
    settings = json.loads((home / "settings.json").read_text())
    dot = json.loads((home / ".claude.json").read_text())
    assert settings["extra"] == 1 and settings["skipDangerousModePermissionPrompt"] is True, settings
    assert dot["projects"][str(wt)]["hasTrustDialogAccepted"] is True, dot
    assert sorted(p.name for p in home.iterdir()) == [".claude.json", "settings.json"], list(home.iterdir())
    shutil.rmtree(home)


@scenario
def remote_seed_leaves_a_file_another_session_created():
    """da-2-3: a remote seed writes only missing profile files. One a Claude session creates
    after the seed found it missing, before the seed publishes, keeps that session's bytes,
    and no temporary file is left beside it."""
    home = tmp / "homes" / "race"
    home.mkdir(parents=True, exist_ok=True)
    hook = tmp / "race-hook"
    hook.mkdir(exist_ok=True)
    # Every way a file can be published first finds the other session's file there.
    (hook / "sitecustomize.py").write_text(
        "import os\n"
        "def racing(real):\n"
        "    def call(src, dst, *a, **k):\n"
        "        if not os.path.exists(dst):\n"
        "            with open(dst, 'w') as out:\n"
        "                out.write('{\"winner\": 1}')\n"
        "        return real(src, dst, *a, **k)\n"
        "    return call\n"
        "os.replace = racing(os.replace)\n"
        "os.rename = racing(os.rename)\n"
        "os.link = racing(os.link)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(hook)}
    argv = ["sh", "-c", da.CLAUDE_SEED, "seed", str(home / "settings.json"), str(home / ".claude.json")]
    done = subprocess.run([*argv, str(tmp / "wt" / "race"), "1"], env=env, capture_output=True, text=True)
    for name in ("settings.json", ".claude.json"):
        assert (home / name).read_text() == '{"winner": 1}', (name, (home / name).read_text(), done.stderr)
    assert sorted(p.name for p in home.iterdir()) == [".claude.json", "settings.json"], list(home.iterdir())
    shutil.rmtree(home)


@scenario
def claude_seed_leaves_a_profile_in_use_alone():
    """da-2-3: a launch on an account another live record already runs on leaves that
    profile's files as they are, since that session may be writing them; the seed's
    snapshot never replaces what it wrote meanwhile."""
    reset_db()
    ticket_row("pu", status="proposed", title="feat / pu")
    wt = str(tmp / "wt" / "pu")
    Path(wt).mkdir(parents=True, exist_ok=True)
    texts = {"settings.json": json.dumps({"extra": 1}), ".claude.json": json.dumps({"projects": {}})}
    for name, text in texts.items():
        (homes["c1"] / name).write_text(text)
    claude_usage("c1", 1, NOW + 600)
    state_file.write_text(json.dumps({"workers": {"pa": rec("pa", "c1", model=SONNET)}}))
    world(local(term("term-pa", 5, ["❯ "])))
    da.main(["launch", "pu", "--agent", "claude", "--model", SONNET, "--worktree", wt])
    assert json.loads(state_file.read_text())["workers"]["pu"]["account"] == "c1"
    for name, text in texts.items():
        assert (homes["c1"] / name).read_text() == text, (name, (homes["c1"] / name).read_text())
    # A finished ticket whose close Orca refused, and a lost record: either Claude may
    # still run, so its profile is left too.
    state_file.write_text(json.dumps({"workers": {"pa": rec("pa", "c1", model=SONNET, done=True)}}))
    world(local(term("term-pa", 5, ["❯ "])))
    ticket_row("pu", status="proposed", title="feat / pu")
    da.main(["launch", "pu", "--agent", "claude", "--model", SONNET, "--worktree", wt])
    assert json.loads(state_file.read_text())["workers"]["pu"]["account"] == "c1"
    for name, text in texts.items():
        assert (homes["c1"] / name).read_text() == text, (name, (homes["c1"] / name).read_text())
    state_file.write_text(json.dumps({"workers": {"pa": rec("pa", "c1", model=SONNET, lost="STUCK pa: gone")}}))
    world(local())
    ticket_row("pu", status="proposed", title="feat / pu")
    da.main(["launch", "pu", "--agent", "claude", "--model", SONNET, "--worktree", wt])
    assert json.loads(state_file.read_text())["workers"]["pu"]["account"] == "c1"
    for name, text in texts.items():
        assert (homes["c1"] / name).read_text() == text, (name, (homes["c1"] / name).read_text())
    state_file.unlink()

@scenario
def claude_first_run_screens_are_answered():
    """When a named first-run screen is already up, launch answers it. Login is not."""
    reset_db()
    screens = {
        "theme": (
            [
                "Choose the text style that looks best with your terminal",
                "To change this later, run /theme",
                "❯ 1. Dark mode",
                "  2. Light mode",
            ],
            [("", True)],
        ),
        "security": (
            [
                "Security notes:",
                "Claude can read and edit files in this folder.",
                "Press Enter to continue",
            ],
            [("", True)],
        ),
        "trust": (CLAUDE_TRUST, [("\x1b[B", False), ("", True)]),
        "bypass": (
            [
                "WARNING: Claude Code running in Bypass Permissions mode",
                "❯ No, exit",
                "  Yes, I accept",
            ],
            [("\x1b[B", False), ("", True)],
        ),
        "fullscreen": (
            ["Try the new fullscreen renderer?", "❯ Yes", "  Not now"],
            [("\x1b[B", False), ("", True)],
        ),
    }
    for name, (lines, keys) in screens.items():
        tid = f"sc{name[:2]}"
        ticket_row(tid)
        st = fresh_state(**{tid: rec(tid, "c1")})
        world(local(term(f"term-{tid}", 31 * 60, lines)))
        tick(st)
        assert sends() == [(f"term-{tid}", text, enter) for text, enter in keys], (
            name,
            sends(),
        )
        world(local(term(f"term-{tid}", 31 * 60, lines)))
        tick(st)
        assert sends() == [], name
    ticket_row("login")
    st = fresh_state(login=rec("login", "c1"))
    login_screen = [
        "Select login method:",
        "❯ Claude account with subscription",
        "  Anthropic Console account",
        "  Press Enter to log in",
    ]
    world(local(term("term-login", 5, login_screen)))
    tick(st)
    assert [s for s in sends() if s[0] == "term-login"] == [], sends()


@scenario
def cleanup_closes_agentless_plain_shells():
    """Orca's Setup and Terminal 1 shells have no agent: cleanup closes them with the
    worktree. An agent in that worktree still refuses. A terminal in another folder
    is not closed."""
    reset_db()
    ticket_row("cu", status="complete")
    wt = str(tmp / "wt" / "cu")
    prompt = ["user@host:~/cu$"]
    setup = term("Setup", 30, prompt, ident=None, worktree=wt)
    terminal_1 = term("Terminal 1", 30, prompt, ident=None, worktree=wt)
    elsewhere = term(
        "term-else", 30, prompt, ident=None, worktree=str(tmp / "wt" / "other")
    )
    st = fresh_state(cu=rec("cu", "c1", worktree=wt))
    world(
        local(term("term-cu", 100, worktree=wt), setup, terminal_1, elsewhere),
        git=git,
        gh=merged,
    )
    tick(st)
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-cu", "Setup", "Terminal 1"], closed
    assert len(calls("orca", ["worktree", "rm"])) == 1 and sends() == []
    assert "cu" not in st["workers"]
    ticket_row("cu", status="complete")
    agent = term("term-agent", 30, ["still working"], ident="codex", worktree=wt)
    st = fresh_state(cu=rec("cu", "c1", worktree=wt))
    world(
        local(term("term-cu", 100, worktree=wt), setup, agent, elsewhere),
        git=git,
        gh=merged,
    )
    tick(st)
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-cu"], "only its own terminal"
    assert calls("orca", ["worktree", "rm"]) == [] and "cu" in st["workers"]
    assert "term-agent" in sends()[0][1] and "term-else" not in sends()[0][1], sends()


@scenario
def borrowed_reference_worktree_is_released():
    """A launch into a worktree whose branch is not the ticket's records that the ticket
    does not own it. When the ticket is done, cleanup closes only that ticket's terminal
    and drops its record. The worktree, its branch and every other terminal stay,
    including one that would otherwise block removal."""
    reset_db()
    state_file.unlink(missing_ok=True)
    tid = "dashboard-served-pages-2-03"
    title = "dashboard-served-pages-2 / 03"
    ticket_row(tid, status="proposed", title=title)
    wt = tmp / "wt" / "dashboard-mock"
    wt.mkdir(parents=True, exist_ok=True)
    reference = term(
        "term-19791c37", 100, ["reference session"], ident="claude", worktree=str(wt)
    )
    world(
        local(reference),
        git={"rev-parse --abbrev-ref HEAD": [0, "design/dashboard-mock\n"]},
    )
    da.main(["set-orchestrator", "term-orch"])
    da.main(
        ["launch", tid, "--agent", "claude", "--model", SONNET, "--worktree", str(wt)]
    )
    saved = json.loads(state_file.read_text())["workers"][tid]
    assert saved.get("owns_worktree") is False, saved
    assert saved["agent_id"] == owner_of(tid) == f"claude:{tid}", saved
    assert not ledger_calls("transfer_work"), "the same-kind, same-host launch keeps the claim"
    ticket_row(tid, status="complete", title=title)
    handle = saved["terminal"]
    world(
        local(term(handle, 100, ident="claude", worktree=str(wt)), reference),
        git={"rev-parse --abbrev-ref HEAD": [0, "design/dashboard-mock\n"]},
    )
    da.main(["tick"])
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == [handle], closed
    assert calls("orca", ["worktree", "rm"]) == []
    assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []
    assert tid not in json.loads(state_file.read_text())["workers"]
    assert sends() == [], sends()

    # Its branch could not be read at launch: not owned, so a merge removes nothing.
    tid = "unknown"
    ticket_row(tid, status="proposed", title="feat / unknown")
    wt = tmp / "wt" / "borrowed-existing"
    wt.mkdir(parents=True, exist_ok=True)
    world(local(), git={"rev-parse --abbrev-ref HEAD": [1, ""]})
    da.main(
        ["launch", tid, "--agent", "claude", "--model", SONNET, "--worktree", str(wt)]
    )
    saved = json.loads(state_file.read_text())["workers"][tid]
    assert saved.get("owns_worktree") is False, saved
    ticket_row(tid, status="complete", title="feat / unknown")
    world(local(term(saved["terminal"], 100, worktree=str(wt))), git=git, gh=merged)
    da.main(["tick"])
    assert calls("orca", ["worktree", "rm"]) == []
    assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []
    assert tid not in json.loads(state_file.read_text())["workers"]


@scenario
def refused_close_keeps_the_record():
    """A finished ticket whose terminal close is refused keeps its record and says so
    once; the close is tried again next tick, borrowed, shared or owned."""
    reset_db()
    pair = str(tmp / "wt" / "rq")
    for tid, extra in (
        ("rb", {"owns_worktree": False}),
        ("rq", {"worktree": pair}),
        ("ro", {}),
    ):
        ticket_row(tid, status="complete")
        workers = {tid: rec(tid, "c1", **extra)}
        if tid == "rq":
            ticket_row("rq-review")
            workers["rq-review"] = rec("rq-review", "x1", agent="codex", worktree=pair)
        st = fresh_state(**workers)
        others = [term("term-rq-review", 5, ident="codex", worktree=pair)] * (tid == "rq")
        mine = term(f"term-{tid}", 100, worktree=extra.get("worktree"))
        refused = {"git": git, "gh": merged, "close_fails": [f"term-{tid}"]}
        said = []
        for _ in range(2):
            world(local(mine, *others), **refused)
            tick(st)
            assert tid in st["workers"] and not st["workers"][tid].get("closed")
            assert calls("orca", ["worktree", "rm"]) == []
            assert all(" ".join(r["args"]) != STATUS for r in calls("git")), tid
            said.append(sum("closing its terminal" in s[1] for s in sends()))
        assert said == [1, 0], (tid, said)
        world(local(mine, *others), git=git, gh=merged)
        tick(st)
        closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
        assert closed[0] == f"term-{tid}", closed
        assert tid not in st["workers"], tid


@scenario
def owned_worktree_still_removed_when_merged():
    """A launch on task/<feature>/<lane>, or on the branch its brief's Branch line names,
    records that the ticket owns the worktree. Cleanup then keeps today's rules: once
    merged, the terminal, a plain shell, the worktree and the branch all go."""
    reset_db()
    state_file.unlink(missing_ok=True)
    named = "Branch: `fix/dev-auto-owned` · Feature branch: none"
    ticket_row("named", status="proposed", title="fixes / named", notes=named)
    wt = tmp / "wt" / "named"
    wt.mkdir(parents=True, exist_ok=True)
    world(local(), git={"rev-parse --abbrev-ref HEAD": [0, "fix/dev-auto-owned\n"], "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"]})
    da.main(["set-orchestrator", "term-orch"])
    da.main(
        ["launch", "named", "--agent", "codex", "--model", "m", "--worktree", str(wt)]
    )
    saved = json.loads(state_file.read_text())["workers"]["named"]
    assert saved.get("owns_worktree") is True, saved
    tid = "owned"
    title = "feat / owned"
    ticket_row(tid, status="proposed", title=title)
    wt = tmp / "wt" / tid
    wt.mkdir(parents=True, exist_ok=True)
    branch = "task/feat/owned"
    world(local(), git={"rev-parse --abbrev-ref HEAD": [0, f"{branch}\n"], "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"]})
    da.main(
        ["launch", tid, "--agent", "claude", "--model", OPUS, "--worktree", str(wt)]
    )
    saved = json.loads(state_file.read_text())["workers"][tid]
    assert saved.get("owns_worktree") is True, saved
    state = json.loads(state_file.read_text())
    del state["workers"]["named"]
    state_file.write_text(json.dumps(state))
    ticket_row(tid, status="closed", title=title)
    prompt = ["user@host:~/owned$"]
    shell = term("Setup", 30, prompt, ident=None, worktree=str(wt))
    tip = "f00d"
    fixture_git = {
        **git,
        "rev-parse --abbrev-ref HEAD": [0, f"{branch}\n"],
        f"ls-remote --heads origin refs/heads/{branch}": [0, ""],
        "ls-remote --heads origin refs/heads/feature/feat": [
            0,
            f"{tip}\trefs/heads/feature/feat\n",
        ],
        "rev-parse -q --verify refs/heads/feature/feat": [0, f"{tip}\n"],
        f"merge-base --is-ancestor HEAD {tip}": [0, ""],
        f"update-ref -d refs/heads/{branch} c0ffee1": [0, ""],
    }
    handle = saved["terminal"]
    world(local(term(handle, 100, worktree=str(wt)), shell), git=fixture_git, gh={})
    da.main(["tick"])
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == [handle, "Setup"], closed
    assert len(calls("orca", ["worktree", "rm"])) == 1
    (deleted,) = [r for r in calls("git") if r["args"][0] == "update-ref"]
    assert deleted["args"] == ["update-ref", "-d", f"refs/heads/{branch}", "c0ffee1"]
    assert tid not in json.loads(state_file.read_text())["workers"]
    assert sends() == [], sends()


@scenario
def task_worktree_keeps_the_branch_it_checked_out():
    """A launch on task/feat/ob owns that branch only. Its worker then checks out another
    branch there for a reviewed merge; with that branch merged and the tree clean, cleanup
    removes neither the worktree nor the branch it now shows."""
    reset_db()
    state_file.unlink(missing_ok=True)
    ticket_row("ob", status="proposed", title="feat / ob")
    wt = tmp / "wt" / "ob"
    wt.mkdir(parents=True, exist_ok=True)
    world(local(), git={"rev-parse --abbrev-ref HEAD": [0, "task/feat/ob\n"], "rev-parse --path-format=absolute --git-common-dir": [0, f"{tmp / 'repo'}/.git\n"]})
    da.main(["set-orchestrator", "term-orch"])
    da.main(["launch", "ob", "--agent", "claude", "--model", OPUS, "--worktree", str(wt)])
    saved = json.loads(state_file.read_text())["workers"]["ob"]
    assert saved.get("owned_branch") == "task/feat/ob", saved
    assert saved.get("owned_common") == f"{tmp / 'repo'}/.git", saved
    ticket_row("ob", status="closed", title="feat / ob")
    world(local(term(saved["terminal"], 100, worktree=str(wt))), git=git, gh=merged)
    da.main(["tick"])
    assert calls("orca", ["worktree", "rm"]) == []
    assert [r for r in calls("git") if r["args"][0] == "update-ref"] == []
    text = (
        "STUCK ob: ticket is closed but its worktree has feat/a checked out, not its own "
        "task/feat/ob. Not removed"
    )
    assert text in [s[1] for s in sends()], sends()
    assert "ob" in json.loads(state_file.read_text())["workers"]
    state_file.unlink()


@scenario
def review_launch_owns_its_coders_worktree():
    """Live 2026-11-14: all 16 review records said owns_worktree False. A review ticket
    launched on its coder ticket's task/<feature>/<lane>, or on the branch the coder's
    Branch line names, owns the worktree; on any other branch it does not."""
    reset_db()
    line = "Reviewer: codex (gpt-6.1-sol, effort xhigh)"
    notes = f"{CODER_LINE} · {line} · Tier: medium\nBranch: `fix/dev-auto-ro` · Feature branch: none"
    ticket_row("ro", title="fixes / ro", notes=notes)
    ticket_row("ro-review", status="proposed", title="fixes / ro-review")
    wt = tmp / "wt" / "ro"
    wt.mkdir(parents=True, exist_ok=True)
    for branch, owns in (
        ("fix/dev-auto-ro", True),
        ("task/fixes/ro", True),
        ("design/mock", False),
    ):
        state_file.unlink(missing_ok=True)
        world(local(), git={"rev-parse --abbrev-ref HEAD": [0, f"{branch}\n"]})
        da.main(["set-orchestrator", "term-orch"])
        da.main(
            ["launch", "ro-review", "--agent", "codex", "--model", "gpt-6.1-sol", "--worktree", str(wt)]
        )
        saved = json.loads(state_file.read_text())["workers"]["ro-review"]
        assert saved.get("owns_worktree") is owns, (branch, saved)
    state_file.unlink()


@scenario
def detached_launch_owns_nothing():
    """A brief whose Branch field names `HEAD` makes no detached checkout its launch's: the
    launch owns no worktree and records no branch."""
    reset_db()
    state_file.unlink(missing_ok=True)
    ticket_row("hd", status="proposed", title="fixes / hd", notes="Branch: `HEAD`")
    wt = tmp / "wt" / "hd"
    wt.mkdir(parents=True, exist_ok=True)
    world(local(), git={"rev-parse --abbrev-ref HEAD": [0, "HEAD\n"]})
    da.main(["set-orchestrator", "term-orch"])
    da.main(["launch", "hd", "--agent", "codex", "--model", "m", "--worktree", str(wt)])
    saved = json.loads(state_file.read_text())["workers"]["hd"]
    assert saved.get("owns_worktree") is False and not saved.get("owned_branch"), saved
    state_file.unlink()


@scenario
def compact_branch_field_owns_its_worktree():
    """A standalone brief's `Feature branch: none · Branch: `fix/ga` from `main``, the form
    fill creates fix/ga from: a launch on fix/ga owns the worktree and records the branch."""
    reset_db()
    state_file.unlink(missing_ok=True)
    notes = "Feature branch: none · Branch: `fix/ga` from `main`"
    ticket_row("ga", status="proposed", title="fixes / ga", notes=notes)
    assert da.fill_branches(da.read_tickets(root)[("", "ga")]) == ("fix/ga", "main")
    wt = tmp / "wt" / "ga"
    wt.mkdir(parents=True, exist_ok=True)
    world(local(), git={"rev-parse --abbrev-ref HEAD": [0, "fix/ga\n"]})
    da.main(["set-orchestrator", "term-orch"])
    da.main(["launch", "ga", "--agent", "codex", "--model", "m", "--worktree", str(wt)])
    saved = json.loads(state_file.read_text())["workers"]["ga"]
    assert saved.get("owns_worktree") is True and saved.get("owned_branch") == "fix/ga", saved
    state_file.unlink()
    # An example in the brief's prose is no declaration: the real field below it is.
    prose = "- da-2-14: a brief such as “Feature branch: none · Branch: `fix/ex` from `main`”.\n"
    for real, made in (
        ("Feature branch: none · Branch: `fix/real` from `main`", ("fix/real", "main")),
        ("Branch: `fix/real` · Feature branch: none", None),
    ):
        ticket_row("gp", status="proposed", title="fixes / gp", notes=prose + real)
        tickets = da.read_tickets(root)
        ticket = tickets[("", "gp")]
        assert da.fill_branches(ticket) == made, real
        owned = da.own_branches(ticket, tickets)
        assert "fix/real" in owned and "fix/ex" not in owned, owned


@scenario
def older_saved_at_does_not_win():
    """The statusline writer keeps the newer saved_at, and a reading whose window has
    already reset does not beat a live one. slot then uses whatever that left on disk."""
    home = homes["c1"]
    target = home / "rate-limits.json"
    script = Path.home() / ".claude" / "statusline-with-usage.sh"
    clock = time.time()

    def plant(saved_at, weekly, reset):
        target.write_text(
            json.dumps(
                {
                    "saved_at": saved_at,
                    "rate_limits": {
                        "five_hour": {"used_percentage": 1, "resets_at": clock + 3600},
                        "seven_day": {"used_percentage": weekly, "resets_at": reset},
                    },
                }
            )
        )

    def run_script(weekly, reset):
        payload = json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 1, "resets_at": clock + 3600},
                    "seven_day": {"used_percentage": weekly, "resets_at": reset},
                }
            }
        )
        return subprocess.run(
            ["bash", str(script)],
            input=payload,
            text=True,
            capture_output=True,
            timeout=20,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(home)},
            check=False,
        )

    def seen():
        return da.peak(cfg, da.account(cfg, "c1", "local"), clock)[0]

    set_fixture()
    try:
        plant(int(clock + 100000), 96, clock + 9000)
        run_script(100, clock - 10)
        got = json.loads(target.read_text())
        assert got["saved_at"] == int(clock + 100000), got
        assert got["rate_limits"]["seven_day"]["used_percentage"] == 96, got
        assert seen() == 96, "the live 96% reading is still the one read"
        plant(int(clock + 100000), 100, clock - 10)
        run_script(96, clock + 9000)
        got = json.loads(target.read_text())
        assert got["rate_limits"]["seven_day"]["used_percentage"] == 96, got
        assert got["saved_at"] < clock + 100000, got
        assert seen() == 96, "a reset window is not the newer reading"
        plant(1, 1, clock + 9000)
        run_script(96, clock + 9000)
        got = json.loads(target.read_text())
        assert got["saved_at"] > 1, got
        assert got["rate_limits"]["seven_day"]["used_percentage"] == 96, got
        assert seen() == 96
    finally:
        clear_usage()
        shutil.rmtree(home / "context", ignore_errors=True)


@scenario
def named_account_does_not_fall_through():
    """--account on slot and launch uses that id or label, and still refuses it when
    the row is disabled, full, or over its usage rule."""
    state_file.unlink(missing_ok=True)
    set_fixture()
    done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "pro2")
    assert done.returncode == 0 and done.stdout.startswith("c2\tpro2\t"), done
    done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "c2")
    assert done.returncode == 0 and done.stdout.startswith("c2\tpro2\t"), done
    done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "off")
    assert done.returncode != 0 and done.stdout == "no capacity\n", done
    done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "c4")
    assert done.returncode != 0 and done.stdout == "no capacity\n", done
    state_file.write_text(json.dumps(fresh_state(other=rec("other", "c1"))))
    done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "pro1")
    assert done.returncode != 0 and done.stdout == "no capacity\n", done
    assert pick({"other": rec("other", "c1")}, account="pro1") is None
    assert pick({}, account="pro2") == "pro2"
    assert pick({}, account="off") is None
    try:
        claude_usage("c1", 96, NOW + 3600)
        assert pick({}, account="pro1") is None
        done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "pro1")
        assert done.returncode != 0 and done.stdout == "no capacity\n", done
        done = cli("slot", "--agent", "claude", "--model", OPUS, "--account", "pro2")
        assert done.returncode == 0 and done.stdout.startswith("c2\tpro2\t"), done
    finally:
        clear_usage()
    reset_db()
    ticket_row("ac")
    wt = str(tmp / "wt" / "ac")
    Path(wt).mkdir(parents=True, exist_ok=True)
    set_fixture()
    state_file.unlink(missing_ok=True)
    try:
        da.main(
            [
                "launch",
                "ac",
                "--agent",
                "claude",
                "--model",
                OPUS,
                "--effort",
                "high",
                "--worktree",
                wt,
                "--account",
                "pro2",
            ]
        )
    except SystemExit as stop:
        raise AssertionError(f"launch --account pro2 failed: {stop.code}") from stop
    saved = json.loads(state_file.read_text())["workers"]["ac"]
    assert saved["account"] == "c2", saved
    assert not [
        r for r in calls("orca", ["terminal", "create"]) if "dev-auto probe" in r["args"]
    ]
    ticket_row("ad")
    claude_usage("c1", 96, NOW + 3600)
    set_fixture()
    try:
        try:
            da.main(
                [
                    "launch",
                    "ad",
                    "--agent",
                    "claude",
                    "--model",
                    OPUS,
                    "--worktree",
                    wt,
                    "--account",
                    "pro1",
                ]
            )
        except SystemExit as stop:
            assert stop.code not in (0, None), stop.code
        else:
            raise AssertionError("an over-usage account must not launch another row")
        assert "ad" not in json.loads(state_file.read_text()).get("workers", {})
    finally:
        clear_usage()
        state_file.unlink(missing_ok=True)


def selfcheck_calls():
    return [
        row
        for row in calls("ssh")
        if any(arg.startswith("concord-mcp script:") and arg.endswith("-selfcheck") for arg in row["args"])
    ]


@scenario
def remote_preflight_names_the_gap_and_does_not_launch():
    """A remote launch runs the ledger-init SSH self-check with closed stdin, then
    requires each Skills: name to have a SKILL.md on that host. A missing key, a failed
    connect, or a missing skill is one STUCK and no launch. A positional concord-mcp
    that exits 0 is not that check."""
    # "duo" in both folders' paths: only the missing-name segment may lack it.
    skills = tmp / "skills-host-duo"
    duo_dir = skills / "duo"
    duo_dir.mkdir(parents=True)
    (duo_dir / "SKILL.md").write_text("# duo\n")
    wt = tmp / "wt" / "rp-duo"
    wt.mkdir(parents=True, exist_ok=True)
    argv = [
        "launch",
        "rp",
        "--agent",
        "claude",
        "--model",
        OPUS,
        "--worktree",
        str(wt),
        "--host",
        "Juzzark-WSL",
    ]
    original = accounts.read_text()

    def stuck_launch(fixture_extra):
        reset_db()
        ticket_row("rp", notes="Brief.\nSkills: duo, missing-skill\n")
        set_fixture(**fixture_extra)
        state_file.unlink(missing_ok=True)
        os.environ["DEV_AUTO_SKILLS_DIR"] = str(skills)
        try:
            try:
                da.main(argv)
            except SystemExit as stop:
                assert stop.code not in (0, None), stop.code
            else:
                raise AssertionError("preflight failure must not launch")
        finally:
            os.environ.pop("DEV_AUTO_SKILLS_DIR", None)
        assert created() == [], created()
        saved = json.loads(state_file.read_text()) if state_file.exists() else {}
        assert "rp" not in saved.get("workers", {})
        logged = notes()
        assert len(logged) == 1 and logged[0].count("STUCK") == 1, logged
        return logged[0]

    def missing(text):
        found = re.search(r"missing SKILL\.md: (.*?) \(looked in ", text)
        assert found, text
        assert str(skills) in text and str(wt) in text, text
        return [name.strip() for name in found.group(1).split(",")]

    def ssh_argv(text):
        assert "-n" in text and "StrictHostKeyChecking=accept-new" in text, text
        assert "concord-mcp script:wsl-selfcheck" in text, text
        checked = selfcheck_calls()
        assert checked and "-n" in checked[0]["args"], checked
        assert not any(
            arg.startswith("script:") and arg.endswith("-selfcheck")
            for row in calls("concord-mcp")
            for arg in row["args"]
        ), calls("concord-mcp")

    try:
        text = stuck_launch({"ssh_fail": True})
        ssh_argv(text)
        assert missing(text) == ["missing-skill"], text

        accounts.write_text(
            original.replace(
                'ledger_ssh = ["ssh", "-i", "~/.ssh/k", "me@mac"]',
                'ledger_ssh = ["ssh", "-i", "MISSING_LEDGER_KEY", "host"]',
            )
        )
        text = stuck_launch({})
        ssh_argv(text)
        assert "MISSING_LEDGER_KEY" in text, text
        accounts.write_text(original)

        text = stuck_launch({})
        assert missing(text) == ["missing-skill"], text
        assert "concord-mcp script:wsl-selfcheck" not in text, text
        assert selfcheck_calls(), "a passing self-check still runs over ssh -n"
    finally:
        accounts.write_text(original)

    (skills / "missing-skill").mkdir()
    (skills / "missing-skill" / "SKILL.md").write_text("# missing\n")
    reset_db()
    ticket_row("rp", notes="Skills: duo, missing-skill\n")
    set_fixture()
    state_file.unlink(missing_ok=True)
    os.environ["DEV_AUTO_SKILLS_DIR"] = str(skills)
    try:
        da.main(argv)
    finally:
        os.environ.pop("DEV_AUTO_SKILLS_DIR", None)
    saved = json.loads(state_file.read_text())["workers"]["rp"]
    assert saved["host"] == "Juzzark-WSL" and saved["account"] == "c3", saved
    assert created(), "a passed preflight still launches"
    assert not any("STUCK" in line for line in notes())
    passed = selfcheck_calls()
    assert passed and "-n" in passed[0]["args"], passed
    assert not any(
        arg.startswith("script:") and arg.endswith("-selfcheck")
        for row in calls("concord-mcp")
        for arg in row["args"]
    )
    state_file.unlink(missing_ok=True)


@scenario
def remote_preflight_reads_only_the_skills_line():
    """Only a line that starts with `Skills:` lists skills: a brief sentence that
    mentions Skills: in passing does not fail the remote preflight."""
    skills = tmp / "skills-line"
    (skills / "duo").mkdir(parents=True)
    (skills / "duo" / "SKILL.md").write_text("# duo\n")
    wt = tmp / "wt" / "rl"
    wt.mkdir(parents=True, exist_ok=True)
    brief = (
        "Brief: the planner names a ticket's Skills: line from /expose, never prose.\n"
        "Skills: duo\n"
    )
    assert da.skill_names(brief) == ["duo"], da.skill_names(brief)
    reset_db()
    ticket_row("rl", notes=brief)
    set_fixture()
    state_file.unlink(missing_ok=True)
    os.environ["DEV_AUTO_SKILLS_DIR"] = str(skills)
    try:
        da.main(
            [
                "launch",
                "rl",
                "--agent",
                "claude",
                "--model",
                OPUS,
                "--worktree",
                str(wt),
                "--host",
                "Juzzark-WSL",
            ]
        )
    finally:
        os.environ.pop("DEV_AUTO_SKILLS_DIR", None)
    assert created(), "the preflight passes and the worker launches"
    assert not any("STUCK" in line for line in notes()), notes()
    state_file.unlink(missing_ok=True)


@scenario
def memory_floor_launch_starts_nothing_on_a_host_short_of_memory():
    """launch reads the named Linux host's memory before anything starts. Under its
    floor, under its Windows side's floor, or unread because the reading failed or did
    not arrive in time, it opens no terminal, keeps no worker and prints the reading
    and the floor, or the failure. With room it launches as before."""
    wt = tmp / "wt" / "mf"
    wt.mkdir(parents=True, exist_ok=True)
    argv = ["launch", "mf", "--agent", "claude", "--model", OPUS, "--worktree", str(wt)]
    argv += ["--host", "Juzzark-WSL"]

    def launch(memory, uname=None, **env):
        reset_db()
        ticket_row("mf")
        state_file.unlink(missing_ok=True)
        set_fixture(memory=memory, uname=uname or {})
        return cli(*argv, **env)

    def refused(memory, uname=None, **env):
        done = launch(memory, uname, **env)
        assert done.returncode == 1 and created() == [], done
        saved = json.loads(state_file.read_text()) if state_file.exists() else {}
        assert "mf" not in saved.get("workers", {}), saved
        return done.stderr.strip()

    assert refused({"Juzzark-WSL": host_memory(7.9)}) == (
        "Juzzark-WSL: 7.9 GB memory available, under its floor of 8 GB; no worker starts there"
    )
    linux = {"-s": [0, "Linux\n"]}
    fallback = refused({"Juzzark-WSL": host_memory(3, source="free-memory")}, uname=linux)
    assert fallback.startswith("Juzzark-WSL: 3.0 GB memory available, under its floor"), fallback
    lost = "Could not connect to the remote Orca runtime."
    assert refused({"Juzzark-WSL": lost}) == (
        f"Juzzark-WSL: memory unread ({lost}); no worker starts there"
    )
    windows = {"Juzzark-WSL": host_memory(12), "Juzzark": host_memory(2, source="free-memory")}
    path = str(memory_accounts(wsl='windows = "Juzzark"\n'))
    assert "Windows side Juzzark has 2.0 GB memory free" in refused(windows, DEV_AUTO_ACCOUNTS=path)
    reset_db()
    ticket_row("mf")
    set_fixture(memory={"Juzzark-WSL": "hang"})
    timeout, da.MEMORY_TIMEOUT = da.MEMORY_TIMEOUT, 1
    try:
        da.main(argv)
    except SystemExit as stop:
        text = str(stop.code)
    else:
        raise AssertionError("an unread host must not launch")
    finally:
        da.MEMORY_TIMEOUT = timeout
    assert text.startswith("Juzzark-WSL: memory unread (") and "timed out" in text, text
    assert created() == [], created()
    done = launch({"Juzzark-WSL": host_memory(12)})
    assert done.returncode == 0 and created(), done
    state_file.unlink(missing_ok=True)


@scenario
def memory_floor_holds_back_a_relaunch():
    """A worker that exited is replaced through slot, so a host under its memory floor
    gets no replacement terminal; with room the worker is relaunched there. The host is
    read once, and once more when that reading refuses."""
    for gb, relaunched in ((6, False), (12, True)):
        reset_db()
        ticket_row("mr")
        st = fresh_state(mr=rec("mr", "c3", host="Juzzark-WSL"))
        row, screen = term("term-mr", 300, ["% "], ident=None)
        world([("Juzzark-WSL", "term-mr", row, screen)], memory={"Juzzark-WSL": host_memory(gb)})
        tick(st)
        assert bool(created()) == relaunched, (gb, created())
        assert len(memory_reads()) == (1 if relaunched else 2), memory_reads()


def launch_with_skill(tid, place, host="Juzzark-WSL"):
    """Launch a ticket whose Skills: line names hr-debug, with its SKILL.md only at
    `place`: "global", ".agents/skills", ".claude/skills" or None. Returns the exit
    code, the STUCK notes, the global folder and the worktree."""
    skills = tmp / "preflight" / tid / "skills"
    skills.mkdir(parents=True)
    wt = tmp / "preflight" / tid / "wt"
    wt.mkdir()
    if place:
        base = skills if place == "global" else wt / place
        (base / "hr-debug").mkdir(parents=True)
        (base / "hr-debug" / "SKILL.md").write_text("# hr-debug\n")
    reset_db()
    ticket_row(tid, notes="Brief.\nSkills: hr-debug\n")
    set_fixture()
    state_file.unlink(missing_ok=True)
    argv = ["launch", tid, "--agent", "claude", "--model", OPUS, "--worktree", str(wt)]
    if host != "local":
        argv += ["--host", host]
    os.environ["DEV_AUTO_SKILLS_DIR"] = str(skills)
    code = 0
    try:
        da.main(argv)
    except SystemExit as stop:
        code = stop.code
    finally:
        os.environ.pop("DEV_AUTO_SKILLS_DIR", None)
        state_file.unlink(missing_ok=True)
    stuck = [line for line in notes() if "STUCK" in line]
    return code, stuck, skills, wt


@scenario
def launch_preflight_finds_a_skill_in_the_worktree_agents_folder():
    """A Skills: name whose SKILL.md is only in the worktree's .agents/skills passes the
    remote launch self-check, and the worker launches."""
    code, stuck, _, _ = launch_with_skill("ra", ".agents/skills")
    assert code in (0, None) and not stuck, (code, stuck)
    assert created(), "a skill in .agents/skills launches"


@scenario
def launch_preflight_finds_a_skill_in_the_worktree_claude_folder():
    """A Skills: name whose SKILL.md is only in the worktree's .claude/skills passes the
    remote launch self-check, and the worker launches."""
    code, stuck, _, _ = launch_with_skill("rc", ".claude/skills")
    assert code in (0, None) and not stuck, (code, stuck)
    assert created(), "a skill in .claude/skills launches"


@scenario
def launch_preflight_finds_a_global_skill():
    """A Skills: name whose SKILL.md is in the host's global skills folder passes."""
    code, stuck, _, _ = launch_with_skill("rg", "global")
    assert code in (0, None) and not stuck, (code, stuck)
    assert created(), "a global skill launches"


@scenario
def launch_preflight_refuses_a_skill_found_nowhere():
    """A Skills: name with no SKILL.md in any place is one STUCK naming it, and nothing
    is launched."""
    code, stuck, _, _ = launch_with_skill("rn", None)
    assert code not in (0, None), code
    assert len(stuck) == 1 and "missing SKILL.md: hr-debug" in stuck[0], stuck
    assert created() == [], created()


@scenario
def launch_preflight_names_the_places_looked_in():
    """The refusal names the global folder and the worktree's two skill folders."""
    _, stuck, skills, wt = launch_with_skill("rw", None)
    for place in (skills, wt / ".agents" / "skills", wt / ".claude" / "skills"):
        assert len(stuck) == 1 and str(place) in stuck[0], (place, stuck)


@scenario
def local_launch_checks_skills_like_a_remote_one():
    """A local launch runs the same skill check: a repo skill in the worktree launches,
    a skill found nowhere is one STUCK and nothing is launched."""
    code, stuck, _, _ = launch_with_skill("la", ".agents/skills", host="local")
    assert code in (0, None) and not stuck, (code, stuck)
    assert created(), "a local repo skill launches"
    code, stuck, skills, _ = launch_with_skill("ln", None, host="local")
    assert code not in (0, None), code
    assert len(stuck) == 1 and "missing SKILL.md: hr-debug" in stuck[0], stuck
    assert str(skills) in stuck[0], stuck
    assert created() == [], created()


@scenario
def launch_into_a_missing_worktree_says_worktree_missing():
    """Live 2026-11-14, update 5993: a relaunch into a removed worktree said every skill
    was missing. A launch or relaunch whose worktree is gone, a folder here or Orca's
    record on a host, starts nothing and says "worktree missing" with the path and the
    ticket. An existing worktree with a skill missing keeps today's message."""
    for tid, host in (("mw", "local"), ("mh", "Juzzark-WSL")):
        for relaunch in (False, True):
            wt = tmp / "preflight" / f"{tid}-{relaunch}" / "gone"
            reset_db()
            ticket_row(tid, notes="Brief.\nSkills: duo\n")
            set_fixture(worktree_show="missing")
            state_file.unlink(missing_ok=True)
            if relaunch:
                old = rec(tid, "c1", host=host, worktree=str(wt), lost="STUCK lost")
                wt.rmdir()
                state_file.write_text(json.dumps({"workers": {tid: old}}))
            argv = ["launch", tid, "--agent", "claude", "--model", OPUS]
            argv += ["--worktree", str(wt), "--host", host]
            try:
                da.main(argv)
            except SystemExit as stop:
                assert stop.code not in (0, None), stop.code
            else:
                raise AssertionError("a missing worktree must not launch")
            stuck = [line for line in notes() if "STUCK" in line]
            assert len(stuck) == 1, stuck
            assert f"STUCK {tid}: worktree missing: {wt}" in stuck[0], stuck
            assert "missing SKILL.md" not in stuck[0], stuck
            assert created() == [], created()
            saved = json.loads(state_file.read_text()) if state_file.exists() else {}
            assert saved.get("workers", {}).get(tid, {}).get("worktree") in (
                None,
                str(wt),
            ), saved
            state_file.unlink(missing_ok=True)
    for tid, host in (("me", "local"), ("mf", "Juzzark-WSL")):
        _, stuck, _, _ = launch_with_skill(tid, None, host=host)
        assert len(stuck) == 1 and "missing SKILL.md: hr-debug" in stuck[0], stuck
        assert "worktree missing" not in stuck[0], stuck


@scenario
def claude_first_run_ignores_unrelated_text():
    """Quoted, stale, or unrelated text is not a first-run dialog, so nothing is typed."""
    negatives = {
        "bypass prose": ["The audit covers Bypass Permissions and other settings.", "❯"],
        "security prose": [
            "Security notes: all checks passed.",
            "Do you want to delete the workspace?",
            "❯ Yes",
            "  No",
        ],
        "quoted trust": [
            'The note quotes "Yes, I trust this folder" and "Quick safety check".',
            "❯ Send it",
        ],
        "stale security": [
            "Security notes:",
            "Claude can read and edit files in this folder.",
            "Press Enter to continue",
            "Do you want to delete the workspace?",
            "❯ Yes",
            "  No",
        ],
        "unrelated": ["Do you want to continue?", "❯ Yes", "  No"],
    }
    reset_db()
    ticket_row("neg")
    for name, lines in negatives.items():
        assert da.claude_first_run_keys(lines) is None, name
        st = fresh_state(neg=rec("neg", "c1"))
        world(local(term("term-neg", 5, lines)))
        tick(st)
        assert [row for row in sends() if row[0] == "term-neg"] == [], (name, sends())
    assert da.claude_first_run_keys(CLAUDE_TRUST)[0] == "trust"


@scenario
def claude_first_run_dialog_is_once_per_launch():
    """The same dialog at a later lastOutputAt is not answered again. A different dialog is."""
    reset_db()
    ticket_row("once")
    security = [
        "Security notes:",
        "Claude can read and edit files in this folder.",
        "Press Enter to continue",
    ]
    bypass = [
        "WARNING: Claude Code running in Bypass Permissions mode",
        "❯ No, exit",
        "  Yes, I accept",
    ]
    st = fresh_state(once=rec("once", "c1"))
    row, screen = term("term-once", NOW - 10, security)
    world(local((row, screen)))
    tick(st)
    assert sends() == [("term-once", "", True)], sends()
    row, screen = term("term-once", NOW - 11, security)
    world(local((row, screen)))
    tick(st)
    assert sends() == [], sends()
    row, screen = term("term-once", NOW - 12, bypass)
    world(local((row, screen)))
    tick(st)
    assert sends() == [("term-once", "\x1b[B", False), ("term-once", "", True)], sends()
    row, screen = term("term-once", NOW - 13, bypass)
    world(local((row, screen)))
    tick(st)
    assert sends() == [], sends()


@scenario
def claude_remote_seed_uses_the_host():
    """A remote Claude launch seeds that host, including an empty home's default profile.
    The supervisor does not write a profile on its own disk."""
    skills = tmp / "skills-remote"
    (skills / "duo").mkdir(parents=True)
    (skills / "duo" / "SKILL.md").write_text("# duo\n")
    host_home = tmp / "wsl-home"
    host_home.mkdir()
    remote_home = tmp / "remote-profile"
    wt = tmp / "wt" / "rs"
    wt.mkdir(parents=True)
    real = Path.home()
    watched = [real / ".claude.json", real / ".claude" / "settings.json", real / "settings.json"]
    before = {path: path.stat().st_mtime if path.exists() else None for path in watched}
    argv = [
        "launch",
        "rs",
        "--agent",
        "claude",
        "--model",
        OPUS,
        "--worktree",
        str(wt),
        "--host",
        "Juzzark-WSL",
    ]
    original = accounts.read_text()

    def launch():
        reset_db()
        ticket_row("rs", notes="Skills: duo\n")
        state_file.unlink(missing_ok=True)
        os.environ["DEV_AUTO_SKILLS_DIR"] = str(skills)
        try:
            da.main(argv)
        finally:
            os.environ.pop("DEV_AUTO_SKILLS_DIR", None)

    try:
        set_fixture(probe_env={"HOME": str(host_home)})
        launch()
        settings_path = host_home / ".claude" / "settings.json"
        state_path = host_home / ".claude.json"
        assert settings_path.is_file() and state_path.is_file(), (
            "empty remote home was not seeded on the host"
        )
        settings = json.loads(settings_path.read_text())
        state = json.loads(state_path.read_text())
        assert settings["skipDangerousModePermissionPrompt"] is True, settings
        assert settings["theme"] == "dark" and settings["tui"] == "default", settings
        assert state["hasCompletedOnboarding"] is True, state
        assert state["projects"][str(wt)]["hasTrustDialogAccepted"] is True, state
        assert not remote_home.exists()
        probes = [
            " ".join(row["args"])
            for row in calls("orca", ["terminal", "create"])
            if "dev-auto probe" in row["args"]
        ]
        assert any("$HOME/.claude" in line and "hasCompletedOnboarding" in line for line in probes), probes
        after = {path: path.stat().st_mtime if path.exists() else None for path in watched}
        assert after == before, after

        remote_home.mkdir()
        (remote_home / "settings.json").write_text(json.dumps({"theme": "light", "extra": 1}))
        (remote_home / ".claude.json").write_text(json.dumps({"numStartups": 4, "projects": {}}))
        accounts.write_text(
            original.replace(
                'label = "max@wsl"\nid = "c3"\nhost = "Juzzark-WSL"\nplan = "max"\n',
                'label = "max@wsl"\nid = "c3"\nhost = "Juzzark-WSL"\nplan = "max"\n'
                + f"home = {json.dumps(str(remote_home))}\n",
            )
        )
        set_fixture(probe_env={"HOME": str(host_home)})
        texts = {name: (remote_home / name).read_text() for name in ("settings.json", ".claude.json")}
        launch()
        # da-2-3: no scan sees a session on that host that may write them, so existing
        # profile files there are left byte for byte; only missing ones are seeded.
        for name, text in texts.items():
            assert (remote_home / name).read_text() == text, (name, (remote_home / name).read_text())
        probes = [
            " ".join(row["args"])
            for row in calls("orca", ["terminal", "create"])
            if "dev-auto probe" in row["args"]
        ]
        assert any(str(remote_home) in line and "settings.json" in line for line in probes), probes
        after = {path: path.stat().st_mtime if path.exists() else None for path in watched}
        assert after == before, after
        assert not (host_home / "settings.json").exists()
    finally:
        accounts.write_text(original)
        state_file.unlink(missing_ok=True)


@scenario
def interleaved_rate_writers_keep_the_newer():
    """Two writers of one profile cannot pass each other. The older snapshot does not
    replace a newer saved_at that landed while it waited."""
    home = homes["c1"]
    target = home / "rate-limits.json"
    hold = home / "rate-hold"
    script = Path.home() / ".claude" / "statusline-with-usage.sh"
    clock = time.time()
    target.write_text(
        json.dumps(
            {
                "saved_at": 80,
                "rate_limits": {
                    "five_hour": {"used_percentage": 1, "resets_at": clock + 3600},
                    "seven_day": {"used_percentage": 50, "resets_at": clock + 9000},
                },
            }
        )
    )

    def payload(weekly):
        return json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 1, "resets_at": clock + 3600},
                    "seven_day": {"used_percentage": weekly, "resets_at": clock + 9000},
                }
            }
        )

    def start(weekly, extra):
        return subprocess.Popen(
            ["bash", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(home), **extra},
        )

    first = second = None
    try:
        first = start(20, {"DEV_AUTO_RATE_HOLD": str(hold)})
        first.stdin.write(payload(20))
        first.stdin.close()
        deadline = time.time() + 8
        while not hold.exists():
            if first.poll() is not None:
                raise AssertionError(
                    f"writer finished without the lock hold ({first.returncode})"
                )
            if time.time() > deadline:
                raise AssertionError("writer never reached the hold inside the lock")
            time.sleep(0.05)
        time.sleep(1.2)
        second = start(96, {})
        second.stdin.write(payload(96))
        second.stdin.close()
        time.sleep(0.4)
        assert second.poll() is None, "the second writer must wait until the first leaves the lock"
        hold.unlink()
        assert first.wait(timeout=20) == 0, first.returncode
        assert second.wait(timeout=20) == 0, second.returncode
        got = json.loads(target.read_text())
        assert got["rate_limits"]["seven_day"]["used_percentage"] == 96, got
        assert got["saved_at"] > 80, got
        assert not (home / "rate-limits.json.tmp").exists()
    finally:
        hold.unlink(missing_ok=True)
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        clear_usage()
        shutil.rmtree(home / "context", ignore_errors=True)


@scenario
def live_agent_screen_is_not_stuck():
    """Orca lists no agent after a session retitles its terminal. The agent's own prompt
    box and footer are the session, so this is not an unknown screen."""
    screens = {
        "claude": [
            "Operator live run pause",
            "❯",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ],
        "codex": ["Ask Codex to do anything"],
        "grok": ["╭──────────────╮", "│ Ask Grok     │", "╰──────────────╯"],
    }
    reset_db()
    accounts_for = {"claude": ("c1", OPUS), "codex": ("x1", "gpt-6.1-sol"), "grok": ("gk", "grok-4.7")}
    for agent, lines in screens.items():
        tid = f"ui{agent[:2]}"
        ticket_row(tid)
        account, model = accounts_for[agent]
        st = fresh_state(
            **{tid: rec(tid, account, agent=agent, model=model, launched_at=NOW - 7200)}
        )
        world(local(term(f"term-{tid}", 120, lines, ident=None)))
        tick(st)
        assert sends() == [], (agent, sends())
        assert not any("STUCK" in line for line in notes()), (agent, notes())


@scenario
def wake_clears_a_stray_slash():
    """A wake clears the input before typing. A leftover slash is not part of the line,
    and the screen shows the text was taken as a prompt."""
    reset_db()
    ticket_row("sl")
    ticket_row("sl-review", agent="codex:sl-review")
    update("sl", "progress", "REVIEW READY 1111aaa")
    update("sl", "finding", "REVIEW VERDICT: FIX abcdef1 one finding", "codex:sl-r")
    update("sl", "blocker", "Waiting on: review")
    st = fresh_state(sl=rec("sl", "c1"))
    row, screen = term("term-sl", 5, ["❯ /"])
    world(local((row, screen)))
    data = json.loads(fixture_file.read_text())
    data["screens"]["term-sl"]["input"] = "/"
    fixture_file.write_text(json.dumps(data))
    tick(st)
    raw = sends(include_clear=True)
    assert raw[:1] == [("term-sl", "\x15", False)], raw
    assert raw[1][0] == "term-sl" and raw[1][2] is True, raw
    assert raw[1][1].startswith("/goal ") and not raw[1][1].startswith("//"), raw
    tail = fixture()["screens"]["term-sl"]["tail"]
    assert any(line.startswith("/goal ") for line in tail), tail
    assert not any(line.startswith("Unknown command:") for line in tail), tail
    assert sends() == [("term-sl", raw[1][1], True)], sends()


@scenario
def claude_first_run_matches_the_bottom_dialog():
    """The dialog at the bottom is the one on screen. A trust block left above a later
    prompt is not answered, the highlighted choice stays put, and an unrendered
    screen is left alone."""
    trust_yes = [
        " Accessing workspace:",
        " Quick safety check: Is this a project you created or one you trust? (Like your",
        "   No, exit",
        " ❯ Yes, I trust this folder",
        " Enter to confirm · Esc to cancel",
    ]
    negatives = {
        "stale trust": [*CLAUDE_TRUST, "Claude: Ready for your next request.", "❯"],
        "trust then allow": [
            *CLAUDE_TRUST,
            "Allow command: rm -rf workspace",
            "❯ Yes",
            "  No",
        ],
        "agent input": ["Claude: Ready for your next request.", "❯"],
        "allow command": ["Allow command: rm -rf workspace", "❯ Yes", "  No"],
    }
    for name, lines in negatives.items():
        assert da.claude_first_run_keys(lines) is None, name
    assert da.claude_first_run_keys(trust_yes) == ("trust", (("", True),))
    assert da.claude_first_run_keys(CLAUDE_TRUST) == (
        "trust",
        (("\x1b[B", False), ("", True)),
    )
    assert da.claude_first_run_keys(
        [
            "WARNING: Claude Code running in Bypass Permissions mode",
            "  No, exit",
            "❯ Yes, I accept",
        ]
    ) == ("bypass", (("", True),))
    assert da.claude_first_run_keys(
        ["Try the new fullscreen renderer?", "  Yes", "❯ Not now"]
    ) == ("fullscreen", (("", True),))

    reset_db()
    ticket_row("bt")

    def shown(lines, source=None):
        st = fresh_state(bt=rec("bt", "c1"))
        row, screen = term("term-bt", 5, lines)
        if source:
            screen = {**screen, "source": source}
        world(local((row, screen)))
        tick(st)
        return st, sends()

    _st, typed = shown(CLAUDE_TRUST, "screen-unavailable")
    assert typed == [], typed
    for name, lines in negatives.items():
        _st, typed = shown(lines)
        assert [item for item in typed if item[0] == "term-bt"] == [], (name, typed)
    st, typed = shown(trust_yes)
    assert typed == [("term-bt", "", True)], typed
    world(local(term("term-bt", 5, trust_yes)))
    tick(st)
    assert sends() == [], sends()


@scenario
def accepted_wake_is_not_retyped():
    """An accepted Enter stays sent: a later emit of the same key types nothing. A stale,
    wrapped, missing, or old Unknown command screen leaves it unconfirmed, so it is
    rechecked and typed once more (da-4-10), never into an unread screen."""
    reset_db()
    ticket_row("wk")
    text = "verdict posted on the ticket"
    frames = {
        "stale": {"status": "running", "tail": ["❯"], "hold": True},
        "wrapped": {
            "status": "running",
            "tail": ["verdict posted", "on the ticket", "❯"],
            "hold": True,
        },
        "unknown": {
            "status": "running",
            "tail": ["Unknown command: /old", "❯"],
            "hold": True,
        },
        # Read before the send, then never again: a screen unread before it is never typed into.
        "missing": {"status": "running", "tail": ["❯"], "reads_left": 1},
    }
    for name, frame in frames.items():
        st = fresh_state()
        worker = rec("wk", "c1")  # fresh per case: a typed text is marked on a repeat
        row, _screen = term("term-wk", 5, frame["tail"])
        world(local((row, frame)))
        assert da.emit(st, "synthetic|wake|123", text, worker=worker, rearm=False), name
        assert "synthetic|wake|123" in st["sent"], name
        assert da.emit(st, "synthetic|wake|123", text, worker=worker, rearm=False) is False
        enters = [item for item in sends() if item[2] and item[1] == text]
        times = 1 if name == "missing" else 2
        assert enters == [("term-wk", text, True)] * times, (name, sends())
        assert "synthetic|wake|123" in st["sent"], name


@scenario
def unconfirmed_wake_is_logged_unconfirmed():
    """delivered.log marks a wake the screen never showed, after its recheck and one more
    send, as missed (da-4-10: a raw one is rechecked too); a wake the screen shows is
    logged as sent."""
    reset_db()
    ticket_row("uc")
    worker = rec("uc", "c1")
    log = tmp / "delivered.log"
    log.unlink(missing_ok=True)
    for n, hold in ((1, True), (2, False)):
        row, screen = term("term-uc", 5, ["❯"])
        world(local((row, {**screen, "hold": hold})))
        text = f"dev-auto: wake {n} on ticket uc."
        assert da.emit(fresh_state(), f"uc|wake|{n}", text, worker=worker, rearm=False), n
    rows = [line.split("\t")[1:] for line in log.read_text().splitlines()]
    assert rows == [
        ["uc", "dev-auto: wake 1 on ticket uc.", "missed"],
        ["uc", "dev-auto: wake 2 on ticket uc."],
    ], rows


class FakeTime:
    """dev_auto's time module on a clock that only sleep() moves. Each sleep is recorded,
    then during() runs: what the worker does while the recheck waits."""

    def __init__(self, during=None):
        self.now, self.slept, self.during = time.time(), [], during

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(round(seconds))
        self.now += seconds
        if self.during:
            self.during()


@scenario
def stale_claude_wake_waits_for_a_fresh_session():
    reset_db()
    ticket_row("age")
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    worker = rec("age", "c1")
    st = fresh_state(age=worker)
    text = verdict_text("age")
    with recheck_wait() as clock:
        worker["claude_input"] = [worker["session"], clock.time() - 3600]
        world(local(term("term-age", 0)))
        def unavailable(*_args):
            raise OSError("events file unavailable")

        append = da.append_event
        da.append_event = unavailable
        try:
            assert not da.emit(st, "age|verdict|7", text, worker=worker)
        finally:
            da.append_event = append
        assert not any(s[0] == "term-age" for s in sends(include_clear=True))
        assert not path.exists()
        st = da.State(json.loads(st.path.read_text()), st.path)
        worker = st["workers"]["age"]
        da.flush_outbox(st, da.read_tickets(root))
        event = path.read_text()
        for value in ("age", "local", "term-age", worker["session"], "verdict|7", text):
            assert value in event, event
        assert "age|verdict|7" not in st["sent"]
        clock.now -= 1
        assert not da.emit(st, "age|verdict|7", text, worker=worker)
        assert path.read_text() == event
        assert not any(s[0] == "term-age" for s in sends(include_clear=True))


@scenario
def wake_age_requires_a_receipt_for_the_same_claude_session():
    reset_db()
    ticket_row("age")
    with recheck_wait() as clock:
        for index, receipt in enumerate((None, ["different-session", clock.time()])):
            worker = rec("age", "c1", claude_input=receipt)
            st = fresh_state(age=worker)
            world(local(term("term-age", 0)))
            assert not da.emit(st, f"age|verdict|{index}", verdict_text("age"), worker=worker)
            assert not any(s[0] == "term-age" for s in sends(include_clear=True))
        worker = rec("age", "x1", agent="codex", model="gpt-6.1-sol", claude_input=None)
        world(local(term("term-age", 0, ident="codex")))
        assert da.emit(fresh_state(age=worker), "age|verdict|4", verdict_text("age"), worker=worker)
        assert any(s[0] == "term-age" and s[2] for s in sends())


@scenario
def only_an_acknowledged_message_moves_the_claude_cutoff():
    reset_db()
    ticket_row("age")
    text = verdict_text("age")
    for unknown_receipt in (True, False):
        worker = rec("age", "c1")
        st = fresh_state(age=worker)
        row, screen = term("term-age", 0)
        with recheck_wait() as clock:
            original = clock.time() - 3599
            worker["claude_input"] = [worker["session"], original]
            world(local((row, {**screen, "unknown_receipt": unknown_receipt})))
            assert da.emit(st, "age|verdict|7", text, worker=worker)
            saved = json.loads(st.path.read_text())["workers"]["age"]["claude_input"]
            assert saved == [worker["session"], original if unknown_receipt else clock.time()]
            clock.now += 1
            world(local((row, screen)))
            assert da.emit(st, "age|verdict|8", text, worker=worker) is (not unknown_receipt)
            assert any(s[0] == "term-age" and s[2] for s in sends()) is (not unknown_receipt)


@scenario
def stale_claude_usage_limit_preserves_the_old_session_for_handoff():
    reset_db()
    ticket_row("age")
    worker = rec("age", "c1", claude_input=None)
    st = fresh_state(age=worker)
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    world(local(term("term-age", 120, ["You've hit your limit · resets 5pm"])))
    tick(st)
    assert not created() and not calls("orca", ["terminal", "close"])
    assert worker["terminal"] == "term-age" and worker["account"] == "c1"
    assert "after a usage limit" in path.read_text()


@scenario
def claude_resume_expiry_after_close_keeps_a_fresh_session_proposal():
    for automatic in (True, False):
        reset_db()
        ticket_row("age")
        path = root / "supervisor" / "events.md"
        path.unlink(missing_ok=True)
        worker = rec("age", "c1" if automatic else "c2")
        st = fresh_state(age=worker)
        tail = ["You've hit your limit · resets 5pm"] if automatic else ["❯"]
        world(local(term("term-age", 120, tail)))
        with recheck_wait() as clock:
            worker["claude_input"] = [worker["session"], clock.time() - 3599]
            real_close = da.close_terminal

            def slow_close(host, handle):
                result = real_close(host, handle)
                clock.now += 2
                return result

            da.close_terminal = slow_close
            try:
                if automatic:
                    tick(st)
                else:
                    state_file.write_text(json.dumps(st))
                    try:
                        da.main(["resume", "age"])
                    except SystemExit as error:
                        assert error.code == 1, error
                    st = da.State(json.loads(state_file.read_text()), st.path)
            finally:
                da.close_terminal = real_close
                if not automatic:
                    state_file.unlink(missing_ok=True)
            assert tab_closes() == [("term-age", True)] and not created(), (tab_closes(), created())
            assert not any(s[0] == "term-age" for s in sends(include_clear=True))
            event = path.read_text()
            assert "FRESH SESSION age" in event and "term-age" in event and worker["session"] in event, event
            st = da.State(json.loads(st.path.read_text()), st.path) if automatic else st
            world(local())
            tick(st, NOW + 10)
            saved = st["workers"]["age"]
            assert "lost" not in saved and saved["agent_id"] == worker["agent_id"], saved
            assert st["proposals"]["age"]["launch"]["worktree"] == worker["worktree"]
            assert not created()


@scenario
def interrupted_claude_cutoff_stop_finishes_with_usage_handoffs_disabled():
    reset_db()
    ticket_row("cutoff")
    ticket_row("ordinary")
    update("ordinary", "progress", "Handoff: next step", at=NOW - 300)
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    worker = rec("cutoff", "c1", claude_input=None, stopping={"why": "Claude input cutoff", "handoff": None})
    st = fresh_state(cutoff=worker, ordinary=rec("ordinary", "c2"))
    st.save()
    with settings(limit_handoff=False):
        for when in (NOW, NOW + 60):
            st = da.State(json.loads(st.path.read_text()), st.path)
            world(local(term("term-ordinary", 120)))
            tick(st, when)
            assert not created() and not tab_closes()
            assert not any(s[0] in ("term-cutoff", "term-ordinary") for s in sends(include_clear=True))
            recovered = st["workers"]["cutoff"]
            assert "stopped" in recovered and not {"stopping", "lost"} & recovered.keys(), recovered
            assert recovered["agent_id"] == worker["agent_id"] and recovered["session"] == worker["session"]
            assert list(st["proposals"]) == ["cutoff"], st["proposals"]
            proposal = st["proposals"]["cutoff"]
            assert proposal["launch"]["worktree"] == worker["worktree"] and proposal["created"] == NOW
            assert "stopped" not in st["workers"]["ordinary"]
            st.save()
    assert len([line for line in event_lines() if "PROPOSALS:" in line]) == 1


@scenario
def stale_claude_retry_keeps_unconfirmed_work_for_the_orchestrator():
    reset_db()
    ticket_row("age")
    worker = rec("age", "c1")
    st = fresh_state(age=worker)
    path = root / "supervisor" / "events.md"
    path.unlink(missing_ok=True)
    row, _ = term("term-age", 0)
    world(local((row, unshown())))
    text, key = verdict_text("age"), "age|verdict|7"
    with recheck_wait() as clock:
        worker["claude_input"] = [worker["session"], clock.time() - 3600]
        worker["unshown"] = [
            {"key": key, "text": text, "at": clock.time() - 60, "update": 0, "output": row["lastOutputAt"]}
        ]
        assert da.recheck(st, worker, worker["unshown"][0]) == "deferred"
        assert not any(s[0] == "term-age" for s in sends(include_clear=True))
        assert not worker.get("missed")
        assert text in path.read_text()


@contextmanager
def recheck_wait(during=None):
    clock = FakeTime(during)
    old = da.time, da.WAKE_RECHECK
    da.time, da.WAKE_RECHECK = clock, 10
    try:
        yield clock
    finally:
        da.time, da.WAKE_RECHECK = old


def unshown(tail=("❯",), shown_after=None, typed=""):
    """A screen that keeps its frame; after shown_after more reads it shows typed."""
    frame = {"status": "running", "tail": list(tail), "hold": True}
    if shown_after is not None:
        frame.update(reads_left=shown_after, then={"status": "running", "tail": [typed]})
    return frame


def delivered_rows():
    return [line.split("\t")[1:] for line in (tmp / "delivered.log").read_text().splitlines()]


def verdict_text(tid):
    return f"dev-auto: a review verdict was posted on ticket {tid}. Read it there and continue."


@scenario
def unshown_wake_shown_on_recheck_is_not_retyped():
    """A wake the screen shows at once is never rechecked or typed again. One it did not
    show is read again about 10 seconds later; shown then, it is not typed again and
    delivered.log says rechecked."""
    reset_db()
    ticket_row("rk")
    worker = rec("rk", "c1")
    text = verdict_text("rk")
    typed = woke("rk", text)
    (tmp / "delivered.log").unlink(missing_ok=True)
    row, _ = term("term-rk", 5)
    for n, frame, waits in (
        (1, {"status": "running", "tail": ["❯"]}, []),
        (2, unshown(shown_after=2, typed=typed), [10]),
    ):
        world(local((row, frame)))
        # Each its own worker: a record typed that text before would mark the next one.
        worker = rec("rk", "c1")
        with recheck_wait() as clock:
            assert da.emit(fresh_state(), f"rk|verdict|{n}", text, worker=worker), n
        enters = [s for s in sends() if s[2] and s[1] == typed]
        assert enters == [("term-rk", typed, True)], (n, sends())
        assert clock.slept == waits, (n, clock.slept)
        assert not {"unshown", "missed"} & set(worker["wake"]), worker["wake"]
    assert delivered_rows() == [["rk", text], ["rk", text, "rechecked"]], delivered_rows()


def hold(handle, **frame):
    """From now on that terminal keeps its frame: keys are accepted, nothing shows."""
    data = fixture()
    data["screens"][handle].update(hold=True, **frame)
    fixture_file.write_text(json.dumps(data))


@scenario
def earlier_wake_on_screen_never_confirms_a_later_one():
    """da-2-5: a second wake on the same ticket, with the same goal and text, onto a screen
    that still shows the first and keeps its frame, is not confirmed by that first copy:
    it is rechecked, typed once more, and missed. A wake the screen shows wrapped over
    several lines is shown at once and typed once."""
    reset_db()
    ticket_row("ew")
    worker = rec("ew", "c1")
    st = fresh_state(ew=worker)
    text = verdict_text("ew")
    typed = woke("ew", text)
    (tmp / "delivered.log").unlink(missing_ok=True)
    row, _ = term("term-ew", 5)
    world(local((row, {"status": "running", "tail": ["❯"]})))
    assert da.emit(st, "ew|verdict|1", text, worker=worker)
    hold("term-ew")
    with recheck_wait():
        assert da.emit(st, "ew|verdict|2", text, worker=worker)
    marked = woke("ew", f"{text} Event verdict 2.")
    assert [s[1] for s in sends() if s[2]] == [typed, marked, marked], sends()
    assert delivered_rows() == [["ew", text], ["ew", f"{text} Event verdict 2.", "missed"]], delivered_rows()
    reset_db()
    ticket_row("ex")
    worker = rec("ex", "c1")
    text = verdict_text("ex")
    typed = woke("ex", text)
    wrapped = ["❯ " + typed[:70], *("  " + typed[i : i + 70] for i in range(70, len(typed), 70))]
    (tmp / "delivered.log").unlink(missing_ok=True)
    row, _ = term("term-ex", 5)
    frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 1}
    world(local((row, {**frame, "then": {"status": "running", "tail": wrapped}})))
    with recheck_wait() as clock:
        assert da.emit(fresh_state(ex=worker), "ex|verdict|1", text, worker=worker)
    assert [s for s in sends() if s[2] and s[1] == typed] == [("term-ex", typed, True)], sends()
    assert clock.slept == [] and delivered_rows() == [["ex", text]], (clock.slept, delivered_rows())




@scenario
def repainted_old_wake_confirms_no_later_one():
    """da-2-5: a second wake with the same text, typed into a rendered frame without the
    first, whose next frame repaints the first wake: the repaint is no proof, since the
    second carries its own key; it is rechecked, typed once more and missed."""
    reset_db()
    ticket_row("rp")
    worker = rec("rp", "c1")
    st = fresh_state(rp=worker)
    text = verdict_text("rp")
    typed = woke("rp", text)
    world(local((term("term-rp", 5)[0], {"status": "running", "tail": ["❯"]})))
    assert da.emit(st, "rp|verdict|1", text, worker=worker)
    (tmp / "delivered.log").unlink(missing_ok=True)
    then = {"status": "running", "tail": [typed, "❯"]}
    frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 1, "then": then}
    world(local((term("term-rp", 5)[0], frame)))
    with recheck_wait():
        assert da.emit(st, "rp|verdict|2", text, worker=worker)
    marked = f"{text} Event verdict 2."
    assert [s[1] for s in sends() if s[2]] == [woke("rp", marked)] * 2, sends()
    assert delivered_rows() == [["rp", marked, "missed"]], delivered_rows()


@scenario
def worker_from_before_typed_texts_marks_its_wakes():
    """da-2-5: a record launched before typed_texts was kept may already show a wake with
    the same text, so every wake it gets carries its event: a repaint of the old echo
    proves nothing."""
    reset_db()
    ticket_row("wp")
    worker = rec("wp", "c1")
    del worker["typed_texts"]
    text = verdict_text("wp")
    (tmp / "delivered.log").unlink(missing_ok=True)
    then = {"status": "running", "tail": [woke("wp", text), "❯"]}
    frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 1, "then": then}
    world(local((term("term-wp", 5)[0], frame)))
    with recheck_wait():
        assert da.emit(fresh_state(wp=worker), "wp|verdict|3", text, worker=worker)
        marked = f"{text} Event verdict 3."
        assert [s[1] for s in sends() if s[2]] == [woke("wp", marked)] * 2, sends()
        assert delivered_rows() == [["wp", marked, "missed"]], delivered_rows()
        # Another text, never typed since the upgrade, may still be on screen from before it.
        other = nudge_of("wp")
        (tmp / "delivered.log").unlink(missing_ok=True)
        then = {"status": "running", "tail": [woke("wp", other), "❯"]}
        world(local((term("term-wp", 5)[0], {**frame, "then": then})))
        assert da.emit(fresh_state(wp=worker), "wp|nudge|4", other, worker=worker)
    marked = f"{other} Event nudge 4."
    assert [s[1] for s in sends() if s[2]] == [woke("wp", marked)] * 2, sends()

@scenario
def unknown_command_screen_confirms_no_wake():
    """da-2-5: a wake typed while an Unknown command line and an earlier copy of the same
    wake are on screen, whose next frame drops that line and keeps the old copy, is not
    confirmed by it: rechecked, typed once more and missed."""
    reset_db()
    ticket_row("uc")
    worker = rec("uc", "c1")
    text = verdict_text("uc")
    typed = woke("uc", text)
    (tmp / "delivered.log").unlink(missing_ok=True)
    row, _ = term("term-uc", 5)
    then = {"status": "running", "tail": [typed, "❯"]}
    frame = {"status": "running", "tail": ["Unknown command: /old", typed, "❯"], "hold": True}
    world(local((row, {**frame, "reads_left": 1, "then": then})))
    with recheck_wait():
        assert da.emit(fresh_state(uc=worker), "uc|verdict|2", text, worker=worker)
    assert len([s for s in sends() if s[2] and s[1] == typed]) == 2, sends()
    assert delivered_rows() == [["uc", text, "missed"]], delivered_rows()

@scenario
def unshown_wake_is_typed_once_more():
    """Not shown on the recheck, with no output and no update from the worker: the wake is
    typed once more, and shown then it is logged retried and raises nothing."""
    reset_db()
    ticket_row("rt")
    worker = rec("rt", "c1")
    st = fresh_state(rt=worker)
    text = verdict_text("rt")
    typed = woke("rt", text)
    (tmp / "delivered.log").unlink(missing_ok=True)
    row, _ = term("term-rt", 5)
    world(local((row, unshown(shown_after=4, typed=typed))))
    with recheck_wait() as clock:
        assert da.emit(st, "rt|verdict|7", text, worker=worker)
    enters = [s for s in sends() if s[2] and s[1] == typed]
    assert enters == [("term-rt", typed, True)] * 2, sends()
    assert clock.slept == [10], clock.slept
    assert delivered_rows() == [["rt", text, "retried"]], delivered_rows()
    tick(st, NOW + 400)
    assert orchestrator_stucks("rt") == [], sends()


@scenario
def stamped_wake_keeps_its_baseline_when_the_listing_fails_after_a_retry():
    """A wake stamped on an earlier tick and retried now keeps its output baseline when the
    terminal listing after the retry fails; a wake saved with no baseline takes the tick's
    own listing. Neither ends the tick."""
    reset_db()
    ticket_row("lb")
    worker = rec("lb", "c1")
    st = fresh_state(lb=worker)
    text = verdict_text("lb")
    typed = woke("lb", text)
    row, _ = term("term-lb", 5)
    world(local((row, unshown(shown_after=2, typed=typed))))
    base, key = row["lastOutputAt"], "lb|verdict|7"
    real_listing, real_type = da.listing, da.type_message
    retried = []

    def type_message(*args, **kwargs):
        sent = real_type(*args, **kwargs)
        retried.append(sent)
        return sent

    da.listing = lambda host: None if retried else real_listing(host)
    da.type_message = type_message
    try:
        with recheck_wait() as clock:
            at = clock.now - 60
            worker["wake"] = {"key": key, "output": base, "at": at, "update": 0}
            worker["unshown"] = [{"key": key, "text": text, "at": at, "update": 0, "output": base}]
            assert da.recheck(st, worker, worker["unshown"][0]) == "retried"
    finally:
        da.listing, da.type_message = real_listing, real_type
    assert worker["wake"]["output"] == base, worker["wake"]
    worker["wake"]["output"] = None
    world(local((row, {"status": "running", "tail": ["❯"]})))
    tick(st)
    assert "tick" in st, st.get("tick")


@scenario
def unshown_wake_missed_twice_is_one_stuck():
    """Typed twice and shown neither time: the orchestrator gets one STUCK naming the wake,
    delivered.log says missed, and later ticks type nothing and add no STUCK."""
    reset_db()
    ticket_row("mt")
    worker = rec("mt", "c1")
    st = fresh_state(mt=worker)
    text = verdict_text("mt")
    typed = woke("mt", text)
    (tmp / "delivered.log").unlink(missing_ok=True)
    world(local((term("term-mt", 5)[0], unshown())))
    with recheck_wait():
        assert da.emit(st, "mt|verdict|7", text, worker=worker)
    for later in (0, 200, 400, 900):
        tick(st, NOW + later)
    enters = [s for s in sends() if s[0] == "term-mt" and s[2]]
    assert enters == [("term-mt", typed, True)] * 2, sends()
    stucks = orchestrator_stucks("mt")
    assert len(stucks) == 1 and "its wake mt verdict 7" in stucks[0], stucks
    assert delivered_rows()[0] == ["mt", text, "missed"], delivered_rows()


@scenario
def worker_output_or_update_during_recheck_counts_as_shown():
    """Terminal output, or a ledger update from the worker, between the send and the
    recheck confirms an unshown wake: nothing is typed again."""
    reset_db()
    ticket_row("wo")
    text = verdict_text("wo")
    typed = woke("wo", text)

    def output():
        data = fixture()
        for row in data["terminals"]["local"]:
            if row["handle"] == "term-wo":
                row["lastOutputAt"] += 1000
        fixture_file.write_text(json.dumps(data))

    def posted():
        update("wo", "progress", "reading the verdict", "claude:wo")

    for name, during in (("output", output), ("update", posted)):
        worker = rec("wo", "c1")  # fresh per case: a typed text is marked on a repeat
        (tmp / "delivered.log").unlink(missing_ok=True)
        world(local((term("term-wo", 5)[0], unshown())))
        with recheck_wait(during) as clock:
            assert da.emit(fresh_state(), f"wo|verdict|{name}", text, worker=worker), name
        enters = [s for s in sends() if s[2] and s[1] == typed]
        assert enters == [("term-wo", typed, True)], (name, sends())
        assert clock.slept == [10], (name, clock.slept)
        assert delivered_rows() == [["wo", text, "rechecked"]], (name, delivered_rows())


@scenario
def update_from_another_projects_owner_is_no_answer():
    """Only the worker, or its own project's ticket owner, answers an unshown wake: an update
    by the owner of the same ticket id in another project leaves it to be typed again."""
    reset_db()
    ticket_row("pj", agent="claude:ours")
    ticket_row("pj", agent="claude:other", project="other")
    text = verdict_text("pj")
    typed = woke("pj", text)
    for who, project, outcome, enters in (
        ("claude:other", "other", "retried", 2),
        ("claude:ours", "", "rechecked", 1),
    ):
        worker = rec("pj", "c1")  # fresh per case: a typed text is marked on a repeat
        (tmp / "delivered.log").unlink(missing_ok=True)
        world(local((term("term-pj", 5)[0], unshown(shown_after=4, typed=typed))))
        with recheck_wait(lambda w=who, p=project: update("pj", "progress", "reading", w, project=p)):
            assert da.emit(fresh_state(), f"pj|verdict|{who}", text, worker=worker), who
        typed_in = [s for s in sends() if s[2] and s[1] == typed]
        assert len(typed_in) == enters, (who, sends())
        assert delivered_rows() == [["pj", text, outcome]], (who, delivered_rows())


@scenario
def retry_cut_short_is_not_typed_a_third_time():
    """The supervisor dies while it types a wake the second time: the next tick, a fresh
    process on the saved state, types it no more and raises one STUCK naming it."""
    reset_db()
    ticket_row("rc")
    worker = rec("rc", "c1")
    st = fresh_state(rc=worker)
    text = verdict_text("rc")
    typed = woke("rc", text)
    world(local((term("term-rc", 5)[0], unshown())))
    real_send, enters = da.send, []

    def dies_on_the_retry(host, terminal, line, enter=True):
        if enter and line == typed:
            enters.append(line)
            if len(enters) == 2:
                raise SystemExit("killed mid-send")
        return real_send(host, terminal, line, enter)

    da.send = dies_on_the_retry
    try:
        with recheck_wait():
            da.emit(st, "rc|verdict|7", text, worker=worker)
    except SystemExit:
        pass
    finally:
        da.send = real_send
    assert len(enters) == 2, enters
    log_file.write_text("")
    restarted = da.State(json.loads(st.path.read_text()), st.path)
    for later in (0, 200, 400):
        tick(restarted, NOW + later)
    assert [s for s in sends() if s[0] == "term-rc" and s[2]] == [], sends()
    stucks = orchestrator_stucks("rc")
    assert len(stucks) == 1 and "its wake rc verdict 7" in stucks[0], stucks


@scenario
def missed_wake_stuck_survives_a_newer_wake():
    """A wake missed twice keeps its STUCK while the orchestrator is held and a newer wake
    reaches the same worker: once the orchestrator is back it gets that one STUCK."""
    reset_db()
    (root / "supervisor" / "events.md").unlink(missing_ok=True)
    ticket_row("mn")
    worker = rec("mn", "c1")
    st = fresh_state(mn=worker)
    old, new = verdict_text("mn"), "dev-auto: a new update was posted on ticket mn. Read it there and continue."
    row = term("term-mn", 5)[0]
    world(local((row, unshown())), orch_ident=None)
    with recheck_wait():
        assert da.emit(st, "mn|verdict|7", old, worker=worker)
        tick(st, NOW)
        world(local((row, {"status": "running", "tail": ["❯"]})), orch_ident=None)
        assert da.emit(st, "mn|reply|8", new, worker=worker)
        world(local((row, {"status": "running", "tail": ["❯", woke("mn", new)]})))
        for later in (60, 120, 400):
            tick(st, NOW + later)
    stucks = [l for l in event_lines() if "its wake mn verdict 7" in l]
    assert len(stucks) == 1, event_lines()


def unread(handle):
    """From now on that terminal's screen cannot be read; it stays listed."""
    data = fixture()
    data["screens"][handle]["unread"] = True
    fixture_file.write_text(json.dumps(data))


@scenario
def deferred_recheck_runs_while_the_screen_is_unread():
    """A wake left to the next tick's recheck is still rechecked when that worker's screen
    can no longer be read. Nothing is typed into a screen that cannot be checked for a
    menu, so with no output and no update it is one STUCK naming the wake."""
    reset_db()
    ticket_row("ua")
    ticket_row("ub")
    a, b = rec("ua", "c1"), rec("ub", "c2")
    st = fresh_state(ua=a, ub=b)
    ta, tb = verdict_text("ua"), verdict_text("ub")
    world(
        local(
            (term("term-ua", 5)[0], unshown(shown_after=2, typed=woke("ua", ta))),
            (term("term-ub", 5)[0], unshown()),
        )
    )
    with recheck_wait() as clock:
        assert da.emit(st, "ua|verdict|1", ta, worker=a)
        assert da.emit(st, "ub|verdict|2", tb, worker=b)
        unread("term-ub")
        for later in (60, 200, 400, 900):
            clock.now += 60
            tick(st, NOW + later)
    assert [s for s in sends() if s[0] == "term-ub" and s[2]] == [("term-ub", woke("ub", tb), True)]
    stucks = orchestrator_stucks("ub")
    assert len(stucks) == 1 and "its wake ub verdict 2" in stucks[0], stucks
    assert "unshown" not in b and "missed" not in b, b


@scenario
def recheck_types_nothing_without_a_rendered_agent_screen():
    """da-2-6: an unshown wake whose recheck finds Orca's accumulated stream, a bare shell,
    a footer-only dialog or an exited pane, with no output and no update, is missed
    without being typed again."""
    frames = (
        ("sa", {"status": "running", "source": "screen-unavailable", "tail": ["❯", "old prompt"]}),
        ("sb", {"status": "running", "tail": ["julian@mac:~$"]}),
        ("sc", {"status": "running", "tail": ["Enter to confirm · Esc to cancel"]}),
        ("sd", {"status": "exited", "tail": ["❯"]}),
    )
    for tid, then in frames:
        reset_db()
        ticket_row(tid)
        worker = rec(tid, "c1")
        text = verdict_text(tid)
        typed = woke(tid, text)
        (tmp / "delivered.log").unlink(missing_ok=True)
        row, _ = term(f"term-{tid}", 5)
        frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 2, "then": then}
        world(local((row, frame)))
        with recheck_wait():
            assert da.emit(fresh_state(**{tid: worker}), f"{tid}|verdict|1", text, worker=worker)
        enters = [s for s in sends() if s[2] and s[1] == typed]
        assert enters == [(f"term-{tid}", typed, True)], (tid, sends())
        assert delivered_rows() == [[tid, text, "missed"]], (tid, delivered_rows())



@scenario
def recheck_reads_the_frame_it_types_into():
    """da-2-6: a recheck whose own read shows the prompt, but whose last read before the
    keys shows a bare shell or Orca's accumulated stream, types nothing, not even Ctrl-U."""
    frames = (
        ("ta", {"status": "running", "tail": ["julian@mac:~$"]}),
        ("tb", {"status": "running", "source": "screen-unavailable", "tail": ["❯"]}),
    )
    for tid, then in frames:
        reset_db()
        ticket_row(tid)
        worker = rec(tid, "c1")
        text = verdict_text(tid)
        (tmp / "delivered.log").unlink(missing_ok=True)
        row, _ = term(f"term-{tid}", 5)
        # Read before and after the send and by the recheck; the next read is "then".
        frame = {"status": "running", "tail": ["❯"], "hold": True, "reads_left": 3, "then": then}
        world(local((row, frame)))
        with recheck_wait():
            assert da.emit(fresh_state(**{tid: worker}), f"{tid}|verdict|1", text, worker=worker)
        clears = [s for s in sends(include_clear=True) if s[1] == "\x15"]
        assert clears == [(f"term-{tid}", "\x15", False)], (tid, sends(include_clear=True))
        assert delivered_rows() == [[tid, text, "missed"]], (tid, delivered_rows())


@scenario
def empty_or_unknown_prompt_is_never_typed_into():
    """da-2-6: a running terminal whose frame is empty, or shows a prompt dev-auto does not
    know as a shell's, shows no agent, nor does such a prompt below an exited agent's old
    input line: the wake is held, nothing typed, not even Ctrl-U."""
    for tid, tail in (("ea", []), ("eb", ["bash-3.2$"]), ("ec", ["❯ previous input", "bash-3.2$"])):
        reset_db()
        ticket_row(tid)
        worker = rec(tid, "c1")
        world(local((term(f"term-{tid}", 5)[0], {"status": "running", "tail": tail})))
        assert not da.emit(fresh_state(**{tid: worker}), f"{tid}|verdict|1", verdict_text(tid), worker=worker)
        assert sends(include_clear=True) == [], (tid, sends(include_clear=True))


@scenario
def starship_frame_holds_and_a_status_line_does_not():
    """da-2-6: a last frame that is Starship's prompt (its path line, then a bare glyph)
    is a shell, so the wake is held with nothing typed; an agent frame ending in its
    opus@max: 87% status line takes the wake."""
    cases = (
        ("sp", ["~/wt/uk on feat/a", "❯"], False),
        ("sq", ["⏺ Done with step 2.", "❯ ", "opus@max: 87%"], True),
    )
    for tid, tail, typed in cases:
        reset_db()
        ticket_row(tid)
        worker = rec(tid, "c1")
        world(local((term(f"term-{tid}", 5)[0], {"status": "running", "tail": tail})))
        sent = da.emit(fresh_state(**{tid: worker}), f"{tid}|verdict|1", verdict_text(tid), worker=worker)
        mine = [s for s in sends(include_clear=True) if s[0] == f"term-{tid}"]
        assert bool(sent) is typed and bool(mine) is typed, (tid, sent, mine)

@scenario
def missed_wake_stuck_raised_while_the_screen_is_unread():
    """A wake missed twice whose supervisor restarts before its STUCK, onto a screen that
    can no longer be read: the STUCK is still raised once."""
    reset_db()
    ticket_row("um")
    worker = rec("um", "c1")
    st = fresh_state(um=worker)
    world(local((term("term-um", 5)[0], unshown())))
    assert da.emit(st, "um|verdict|3", verdict_text("um"), worker=worker)
    assert worker.get("missed"), worker
    restarted = da.State(json.loads(st.path.read_text()), st.path)
    unread("term-um")
    for later in (60, 200, 400, 900):
        tick(restarted, NOW + later)
    stucks = orchestrator_stucks("um")
    assert len(stucks) == 1 and "its wake um verdict 3" in stucks[0], stucks
    assert "missed" not in restarted["workers"]["um"], restarted["workers"]["um"]


@scenario
def recheck_waits_once_per_tick():
    """Two unshown wakes in one tick: only the first waits for its recheck, so the state
    lock is held for one wait at most. The second is logged unconfirmed and rechecked by
    the next tick, a minute on, without a wait."""
    reset_db()
    ticket_row("ra")
    ticket_row("rb")
    a, b = rec("ra", "c1"), rec("rb", "c2")
    st = fresh_state(ra=a, rb=b)
    ta, tb = verdict_text("ra"), verdict_text("rb")
    typed_b = woke("rb", tb)
    (tmp / "delivered.log").unlink(missing_ok=True)
    world(
        local(
            (term("term-ra", 5)[0], unshown(shown_after=2, typed=woke("ra", ta))),
            (term("term-rb", 5)[0], unshown()),
        )
    )
    with recheck_wait() as clock:
        assert da.emit(st, "ra|verdict|1", ta, worker=a)
        assert da.emit(st, "rb|verdict|2", tb, worker=b)
        assert clock.slept == [10], clock.slept
        assert delivered_rows() == [["ra", ta, "rechecked"], ["rb", tb, "unconfirmed"]]
        data = fixture()
        data["screens"]["term-rb"] = {"status": "running", "tail": [typed_b]}
        fixture_file.write_text(json.dumps(data))
        clock.now += 60
        tick(st, NOW + 60)
        assert clock.slept == [10], clock.slept
    enters = [s for s in sends() if s[0] == "term-rb" and s[2]]
    assert enters == [("term-rb", typed_b, True)], sends()
    assert delivered_rows()[2:] == [["rb", tb, "rechecked"]], delivered_rows()



@scenario
def deferred_wake_retried_without_a_withdrawn_finish():
    """A non-verdict wake deferred past its tick, its one wait already spent, on a coder
    armed to finish: once that PUSH is withdrawn, the next tick's recheck retypes it with
    the plain goal, never the finish goal, and waits for nothing."""
    reset_db()
    ticket_row("rf", notes=STANDALONE)
    ready = update("rf", "progress", "REVIEW READY 1a2b3c4")
    push = update("rf", "finding", "REVIEW VERDICT: PUSH 1a2b3c4", "codex:rf-r")
    proof = {"update": push, "commit": "1a2b3c4".ljust(40, "0"), "request": ready}
    worker = rec("rf", "c1", finish=proof)
    st = fresh_state(rf=worker)
    text = "dev-auto: CI finished on ticket rf. Read it there and continue."
    world(local((term("term-rf", 5)[0], unshown())))
    with recheck_wait() as clock:
        st.waited = True
        assert da.emit(st, "rf|ci|7", text, worker=worker)
        (pending,) = st["workers"]["rf"]["unshown"]
        assert da.FINISH_DONE in pending["typed"], pending
        supersede(push, update("rf", "decision", "withdrawn", "codex:rf-r"))
        clock.now += 60
        tick(st, NOW + 60)
        assert clock.slept == [], clock.slept
    typed = [s[1] for s in sends() if s[0] == "term-rf" and s[2]]
    assert len(typed) == 2 and da.FINISH_DONE in typed[0], typed
    assert da.FINISH_DONE not in typed[1] and da.GOAL_DONE in typed[1], typed[1]
    assert "finish" not in st["workers"]["rf"], st["workers"]["rf"]

def pending_wake(tid, wait=True):
    """A Codex worker, waiting unless told otherwise, woken for a verdict its screen
    shows."""
    reset_db()
    ticket_row(tid, agent=f"codex:{tid}")
    ticket_row(f"{tid}-review", agent=f"codex:{tid}-review")
    update(tid, "progress", "REVIEW READY 1111aaa", f"codex:{tid}")
    verdict = update(tid, "finding", "REVIEW VERDICT: FIX abcdef1 one finding", f"codex:{tid}-r")
    if wait:
        update(tid, "blocker", "Waiting on: review", f"codex:{tid}")
    worker = rec(tid, "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
    st = fresh_state(**{tid: worker})
    row, screen = term(f"term-{tid}", 5, codex_screen(), ident="codex")
    world(local((row, screen)))
    tick(st)
    text = f"dev-auto: a review verdict was posted on ticket {tid}. Read it there and continue."
    assert (f"term-{tid}", woke(tid, text, "codex"), True) in sends(), sends()
    return st, f"its wake {tid} verdict {verdict}."


def orchestrator_stucks(tid):
    return [t for h, t, _ in sends() if h == "term-orch" and t.startswith(f"STUCK {tid}")]


@scenario
def waiting_worker_wake_with_no_turn_is_stuck_once():
    """Live (finding 1268): a waiting worker's wake started no turn. With no update from it
    and no terminal output about 3 minutes after the wake, the orchestrator gets one STUCK
    naming the ticket and the wake; later ticks add none and the wake is not retyped."""
    st, wake = pending_wake("pw")
    log_file.write_text("")
    tick(st, NOW + 60)
    assert orchestrator_stucks("pw") == [], sends()
    for later in (200, 400, 900):
        tick(st, NOW + later)
    assert not [s for s in sends() if s[0] == "term-pw"], sends()
    stucks = orchestrator_stucks("pw")
    assert len(stucks) == 1, stucks
    assert wake in stucks[0], stucks


@scenario
def wake_stuck_cut_short_is_delivered_with_its_note_later():
    """A wake's STUCK whose line a full disk refused is held: the wake is settled at once,
    and the first tick that can write appends the line once and notes it on the ticket."""
    st, wake = pending_wake("pn")
    log_file.write_text("")
    real = da.event_line

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    da.event_line = full
    try:
        for later in (60, 200, 400):
            tick(st, NOW + later)
    finally:
        da.event_line = real
    held = [e for e in st.get("outbox", {}).values() if e["text"].startswith("STUCK pn")]
    assert len(held) == 1 and "wake" not in st["workers"]["pn"], (held, st["workers"]["pn"].get("wake"))
    assert not [n for n in notes() if "STUCK pn" in n], notes()
    for later in (900, 1000):
        tick(st, NOW + later)
    stucks = [l for l in event_lines() if "STUCK pn" in l]
    assert len(stucks) == 1 and wake in stucks[0], stucks
    assert len([n for n in notes() if "STUCK pn" in n and wake in n]) == 1, notes()
    assert not st.get("outbox"), st["outbox"]


@scenario
def working_worker_verdict_wake_with_no_turn_is_stuck_once():
    """A verdict wake to a worker with no Waiting on note is covered too: one STUCK naming
    it about 3 minutes later. The silence nudge stays silence's to report."""
    st, wake = pending_wake("pk", wait=False)
    log_file.write_text("")
    for later in (60, 200, 400):
        tick(st, NOW + later)
    stucks = orchestrator_stucks("pk")
    assert len(stucks) == 1 and wake in stucks[0], stucks


@scenario
def unshown_nudge_waits_for_silence_to_be_stuck():
    """da-logs-7: a nudge the screen never shows is typed twice and logged missed, but
    raises no STUCK of its own; silence's STUCK comes once, a full interval after it."""
    reset_db()
    ticket_row("nz")
    st = fresh_state(nz=rec("nz", "c1"))
    (tmp / "delivered.log").unlink(missing_ok=True)
    quiet = cfg["silent_minutes"] * 60 + 60
    for later in (0, 60, 300, cfg["silent_minutes"] * 60 + 60):
        world(local((term("term-nz", quiet)[0], unshown())))
        tick(st, NOW + later)
        stucks = orchestrator_stucks("nz")
        if later < cfg["silent_minutes"] * 60:
            assert stucks == [], (later, stucks)
    assert len(stucks) == 1 and "after a nudge" in stucks[0], stucks
    nudges = [r for r in delivered_rows() if r[0] == "nz" and r[1].startswith("dev-auto: no output")]
    assert [r[2:] for r in nudges] == [["missed"]], delivered_rows()


@scenario
def wake_answered_by_an_update_is_not_stuck():
    """The same wake followed by an update from that worker raises no STUCK."""
    st, _wake = pending_wake("pu")
    update("pu", "progress", "reading the verdict", "codex:pu")
    for later in (200, 400):
        tick(st, NOW + later)
        assert orchestrator_stucks("pu") == [], sends()


@scenario
def wake_answered_by_terminal_output_is_not_stuck():
    """The same wake followed by new terminal output raises no STUCK."""
    st, _wake = pending_wake("po")
    row, screen = term("term-po", -100, codex_screen(), ident="codex")
    for later in (200, 400):
        world(local((row, {**screen, "hold": True})))
        tick(st, NOW + later)
        assert orchestrator_stucks("po") == [], sends()


@scenario
def wake_answered_by_early_output_is_not_stuck():
    """Output seconds after the wake, from a turn that started at once, raises no STUCK."""
    st, _wake = pending_wake("pe")
    row, screen = term("term-pe", -10, codex_screen(), ident="codex")
    screen["tail"] = [*screen["tail"], "• Reading the review verdict."]
    for later in (200, 400):
        world(local((row, {**screen, "hold": True})))
        tick(st, NOW + later)
        assert orchestrator_stucks("pe") == [], sends()


@scenario
def wake_does_not_esc_a_running_turn():
    """A wake during a rendered running turn types the text and sends no Esc, on every
    agent. One whose last read before the keys fails or is Orca's accumulated stream is
    held, with nothing typed, and stays pending for a later tick (da-2-6)."""
    stale = {
        "status": "running",
        "source": "screen-unavailable",
        "tail": ["Ask Codex to do anything", "› old prompt"],
    }
    missed = (
        ("nx", ["esc to interrupt"], None),
        ("nw", ["Working"], None),
        ("nb", ["Working", "esc to interrupt"], None),
        ("ns", ["Working", "esc to interrupt"], stale),
    )
    for tid, lines, then in missed:
        reset_db()
        ticket_row(tid, agent=f"codex:{tid}")
        update(
            tid,
            "finding",
            "REVIEW VERDICT: FIX abcdef1 one finding",
            f"codex:{tid}-r",
        )
        update(tid, "blocker", "Waiting on: review", f"codex:{tid}")
        st = fresh_state(**{tid: rec(tid, "x1", agent="codex", model="gpt-6.1-sol")})
        row, screen = term(f"term-{tid}", 5, lines, ident="codex")
        screen["reads_left"] = 1
        if then:
            screen["then"] = then
        world(local((row, screen)))
        tick(st)
        raw = sends(include_clear=True)
        assert not [s for s in raw if s[0] == f"term-{tid}"], (tid, raw)
        assert not [k for k in st["sent"] if k.startswith(f"{tid}|verdict|")], (tid, st["sent"])
    cases = (
        ("cx", "codex", "x1", "gpt-6.1-sol", ["esc to interrupt"]),
        ("cw", "codex", "x1", "gpt-6.1-sol", ["Working", "›"]),
        ("cl", "claude", "c1", OPUS, ["✶", "esc to interrupt"]),
        ("gk", "grok", "gk", "grok-4.7", ["[stop]", "Ask Grok"]),
        ("cs", "claude", "c1", OPUS, ["esc to interrupt"]),
    )
    for tid, agent, account, model, lines in cases:
        reset_db()
        ticket_row(tid, agent=f"{agent}:{tid}")
        update(
            tid,
            "finding",
            "REVIEW VERDICT: FIX abcdef1 one finding",
            f"codex:{tid}-r",
        )
        update(tid, "blocker", "Waiting on: review", f"{agent}:{tid}")
        st = fresh_state(**{tid: rec(tid, account, agent=agent, model=model)})
        row, screen = term(f"term-{tid}", 5, lines, ident=agent)
        world(local((row, screen)))
        tick(st)
        raw = sends(include_clear=True)
        text = (
            f"dev-auto: a review verdict was posted on ticket {tid}. "
            "Read it there and continue."
        )
        expect = woke(tid, text, agent)
        assert (f"term-{tid}", expect, True) in raw, (tid, raw)
        saw_esc = any(item[1] == "\x1b" for item in raw)
        assert saw_esc is False, (tid, raw)
        print(f"{tid} esc={saw_esc} enter={expect!r}")


@scenario
def wake_resumes_a_stalled_or_paused_codex_goal():
    """A wake to a Codex screen whose goal is stalled or paused sends /goal resume
    first, then the wake text."""
    interrupted = ["■ Conversation interrupted - use /feedback if something went wrong"]
    for goal, above in (
        ("Goal stalled (/goal resume)", ()),
        ("Goal paused (/goal resume)", interrupted),
    ):
        reset_db()
        ticket_row("gr", agent="codex:gr")
        ticket_row("gr-review", agent="codex:gr-review")
        update("gr", "progress", "REVIEW READY 1111aaa", "codex:gr")
        update("gr", "finding", "REVIEW VERDICT: FIX abcdef1 one finding", "codex:gr-r")
        update("gr", "blocker", "Waiting on: review", "codex:gr")
        worker = rec("gr", "x1", agent="codex", model="gpt-6.1-sol", effort="xhigh")
        st = fresh_state(gr=worker)
        lines = codex_screen(goal=goal, above=above)
        world(local(term("term-gr", 5, lines, ident="codex")))
        tick(st)
        text = "dev-auto: a review verdict was posted on ticket gr. Read it there and continue."
        expect = [("term-gr", "/goal resume", True), ("term-gr", woke("gr", text, "codex"), True)]
        assert sends() == expect, (goal, sends())


# A live Claude Code 2.1.322 screen: its prompt box and footer, idle or mid-turn.
CLAUDE_FOOTER = [
    "off lite [ full ] ultra" + " " * 89 + "Copy Review Audit Board Compact [-]",
    "─" * 143,
    "cost" + " " * 43 + "tokens" + " " * 41 + "agent time",
    "$0.00" + " " * 42 + "0" + " " * 46 + "1s",
    "last $0.00 · ⌀ $0.00" + " " * 27 + "2 turns" + " " * 40 + "⌀ 1s / turn",
    " " * 126 + "◎ /goal active (31m)",
    "─" * 148,
    "❯",
    "─" * 148,
    "  [PONYTAIL]",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]
# Mid-turn with no "esc to interrupt" anywhere: only the spinner line shows the turn.
CLAUDE_MID_TURN = [
    "✶ Grooving… (7m 20s · ↓ 37.1k tokens)",
    (
        "  ⎿ \xa0Tip: Press shift+↑ or shift+↓ at the prompt to raise or lower the effort"
        " level for this session"
    ),
    *CLAUDE_FOOTER,
]
CLAUDE_LIMITED = [
    "  ⎿  You've hit your 5-hour limit · resets 5am (America/Los_Angeles)",
    "Usage limit reached · continuing automatically at 5am · esc to cancel",
    *CLAUDE_FOOTER,
]


def no_esc_raw(handle):
    raw = sends(include_clear=True)
    assert not any(item[1] == "\x1b" for item in raw), (handle, raw)
    return raw


@scenario
def wake_onto_claude_mid_turn_sends_no_esc():
    """A worker wake onto a real Claude Code mid-turn screen, whose spinner line has no
    interrupt hint, sends no Esc and still types the text."""
    reset_db()
    ticket_row("mt")
    update("mt", "finding", "REVIEW VERDICT: FIX abcdef1 one finding", "codex:mt-r")
    update("mt", "blocker", "Waiting on: review")
    st = fresh_state(mt=rec("mt", "c1"))
    world(local(term("term-mt", 5, CLAUDE_MID_TURN)))
    tick(st)
    text = "dev-auto: a review verdict was posted on ticket mt. Read it there and continue."
    raw = no_esc_raw("term-mt")
    assert ("term-mt", woke("mt", text), True) in raw, raw


@scenario
def orchestrator_event_onto_claude_mid_turn_sends_no_esc():
    """An orchestrator event through tell() onto the same mid-turn screen sends no Esc,
    nor anything else: it waits for the orchestrator's prompt (da-docs-13)."""
    world([], orch_tail=CLAUDE_MID_TURN)
    event = "CLEARED REVIEW mt: push and open its pull request"
    assert not da.tell({"terminal": "term-orch", "host": "local"}, event)
    assert no_esc_raw("term-orch") == []


@scenario
def wake_onto_claude_usage_limit_sends_no_esc():
    """A wake or a nudge onto Claude's usage-limit screen, whose footer offers esc to
    cancel the automatic continue, sends no Esc."""
    reset_db()
    ticket_row("ul")
    worker = rec("ul", "c1")
    for key, text in (
        ("ul|wake|1", "dev-auto: a review verdict was posted on ticket ul."),
        ("ul|nudge|1", "dev-auto: no output for 20 minutes while ticket ul is open."),
    ):
        world(local(term("term-ul", 5, CLAUDE_LIMITED)))
        assert da.emit(fresh_state(), key, text, worker=worker, rearm=False), key
        assert no_esc_raw("term-ul") == [
            ("term-ul", "\x15", False),
            ("term-ul", text, True),
        ]


@scenario
def idle_claude_stray_character_is_cleared_by_ctrl_u():
    """An idle Claude prompt holding a stray character is cleared by Ctrl-U alone, then
    gets the text as its whole line."""
    reset_db()
    ticket_row("sc")
    row, screen = term("term-sc", 5, CLAUDE_FOOTER)
    world(local((row, {**screen, "input": "x"})))
    text = "dev-auto: a review verdict was posted on ticket sc."
    assert da.emit(fresh_state(), "sc|wake|1", text, worker=rec("sc", "c1"), rearm=False)
    assert no_esc_raw("term-sc") == [("term-sc", "\x15", False), ("term-sc", text, True)]
    assert fixture()["screens"]["term-sc"]["tail"][-1] == text

def launch_on(tid, agent, model, host="local"):
    wt = tmp / "wt" / tid
    wt.mkdir(parents=True, exist_ok=True)
    argv = ["launch", tid, "--agent", agent, "--model", model, "--worktree", str(wt)]
    ticket_row(tid, status="proposed", title=f"updates / {tid}")
    da.main([*argv, "--host", host])
    return json.loads(state_file.read_text())


@scenario
def daily_update_before_launch():
    """A launch first updates its agent's CLI on that host, once a day per host and tool.
    A change is noted on the ticket; a failure is one STUCK and the launch goes on."""
    reset_db()
    state_file.unlink(missing_ok=True)
    set_fixture(versions={"codex": "0.1"}, latest={"codex": "0.2"})
    saved = launch_on("up-1", "codex", "gpt-6.1-sol")
    assert [r["args"] for r in calls("codex")] == [["--version"], ["update"], ["--version"]]
    assert notes() == ["dev-auto: updated codex on local: 0.1 -> 0.2"], notes()
    assert {"local|codex", "local|orca"} <= set(saved["updates"]), saved["updates"]
    assert "up-1" in saved["workers"] and created()

    set_fixture(versions={"codex": "0.2"}, latest={"codex": "0.3"})
    launch_on("up-2", "codex", "gpt-6.1-sol")
    assert calls("codex") == [] and notes() == [], "checked once a day"

    data = json.loads(state_file.read_text())
    data["updates"]["local|codex"] -= da.DAY
    state_file.write_text(json.dumps(data))
    set_fixture(versions={"codex": "0.2"}, update_fail={"codex": "brew: no network"})
    saved = launch_on("up-3", "codex", "gpt-6.1-sol")
    stuck = "STUCK up-3: codex update on local failed: brew: no network"
    assert notes() == [f"dev-auto: {stuck}"], notes()
    assert "up-3" in saved["workers"] and created(), "a failed update still launches"
    assert time.time() - saved["updates"]["local|codex"] < 60, "a failure counts as the check"
    state_file.unlink()


def raises_nothing(fn, *args):
    """fn's result; any exception but an assertion is a failed scenario, not a crash."""
    try:
        return fn(*args)
    except AssertionError:
        raise
    except BaseException as error:
        raise AssertionError(f"{fn.__name__} raised {type(error).__name__}: {error}") from error


@scenario
def interrupted_launch_leaves_slot_launch_and_tick_working():
    """A launch stopped inside its daily update, by Ctrl-C in the CLI update or SIGTERM's
    SystemExit in the Orca one, saves a record that already holds its account and launch
    time. slot, another launch, a relaunch of that ticket and tick all still run."""
    for tool, stop in (("update_cli", KeyboardInterrupt), ("update_orca", SystemExit)):
        reset_db()
        state_file.unlink(missing_ok=True)
        world([])
        real = getattr(da, tool)

        def interrupted(*_args, stop=stop):
            raise stop()

        setattr(da, tool, interrupted)
        try:
            launch_on("ip-1", "codex", "gpt-6.1-sol")
        except stop:
            pass
        else:
            raise AssertionError(f"{tool}: the launch was not interrupted")
        finally:
            setattr(da, tool, real)
        saved = json.loads(state_file.read_text())["workers"]["ip-1"]
        assert saved.get("account") == "x1" and saved.get("launched_at"), (tool, saved)
        assert saved["terminal"] is None and not created(), (tool, saved)
        done = cli("slot", "--agent", "codex", "--model", "m")
        assert done.returncode == 0 and done.stdout.startswith("x"), (tool, done)
        saved = raises_nothing(launch_on, "ip-2", "codex", "gpt-6.1-sol")
        assert saved["workers"]["ip-2"]["terminal"] == "term-local-1", (tool, saved)
        raises_nothing(da.main, ["tick"])
        saved = raises_nothing(launch_on, "ip-1", "codex", "gpt-6.1-sol")
        assert saved["workers"]["ip-1"]["terminal"], (tool, saved["workers"]["ip-1"])
        state_file.unlink()


@scenario
def lost_create_answer_keeps_its_launch_record():
    """da-5-4: Orca starts the terminal but its create answer is lost. The launch keeps
    the record it saved, with no handle, instead of deleting it, and a second launch
    refuses while a terminal no record holds is open in that worktree."""
    reset_db()
    state_file.unlink(missing_ok=True)
    world([], create_lost=True)
    try:
        launch_on("lc", "codex", "gpt-6.1-sol")
    except SystemExit:
        pass
    saved = json.loads(state_file.read_text())["workers"].get("lc")
    assert saved and saved["terminal"] is None and saved["account"] == "x1", saved
    wt = str(tmp / "wt" / "lc")
    world(local(term("term-local-1", 5, ident=None, worktree=wt)))
    try:
        launch_on("lc", "codex", "gpt-6.1-sol")
    except SystemExit as error:
        assert "term-local-1" in str(error.code), error.code
    else:
        raise AssertionError("the second launch ran")
    assert created() == [], created()
    state_file.unlink()

    # An answer without a handle is no proof either: the record is kept.
    world([], create_no_handle=True)
    try:
        launch_on("lc", "codex", "gpt-6.1-sol")
    except SystemExit:
        pass
    saved = json.loads(state_file.read_text())["workers"].get("lc")
    assert saved and saved["terminal"] is None, saved
    state_file.unlink()


@scenario
def resume_over_a_lost_create_refuses_while_a_pane_is_open():
    """A Claude record whose create answer was lost has no terminal. `resume` with a pane
    no record holds open in its worktree refuses, as `launch` does, and starts nothing."""
    reset_db()
    ticket_row("lr")
    wt = str(tmp / "wt" / "lr")
    state_file.write_text(json.dumps({"workers": {"lr": rec("lr", "c2", terminal=None, pty=None)}}))
    world(local(term("term-local-1", 5, ["❯ "], worktree=wt)))
    done = cli("resume", "lr")
    assert done.returncode != 0 and "term-local-1" in done.stderr, done
    assert not created() and not calls("orca", ["terminal", "close"]), calls()
    assert json.loads(state_file.read_text())["workers"]["lr"]["terminal"] is None
    state_file.unlink()

@scenario
def malformed_record_is_one_stuck_and_skipped():
    """A saved record without its account or launch time, as an interrupted launch once
    left, is one STUCK naming its ticket. slot, check-in and tick skip it and raise
    nothing, and a new launch replaces it."""
    reset_db()
    ticket_row("mf")
    bad = rec("mf", "x1", agent="codex", model="m", terminal=None)
    del bad["account"], bad["launched_at"]
    st = fresh_state(mf=bad)
    world([])
    assert raises_nothing(pick, st["workers"], "codex", "m") == "x1"
    two = {"mf": bad, "b": rec("b", "x1", agent="codex")}
    assert raises_nothing(pick, two, "codex", "m") == "x1", "the bad record holds no slot"
    _count, text = raises_nothing(da.checkin_text, cfg, st, da.read_tickets(root), NOW)
    assert "mf owner claude:mf state active" in text and "not launched" in text, text
    raises_nothing(tick, st)
    raises_nothing(tick, st)
    stuck = [s for s in sends() if s[1].startswith("STUCK mf")]
    assert stuck == [
        ("term-orch", "STUCK mf: its record has no account or launched_at. It is skipped until a new launch", True)
    ], sends()
    assert notes() == [f"dev-auto: {stuck[0][1]}"], notes()
    state_file.write_text(json.dumps({"workers": {"mf": bad}}))
    saved = raises_nothing(launch_on, "mf", "codex", "gpt-6.1-sol")
    assert saved["workers"]["mf"]["account"] and saved["workers"]["mf"]["terminal"], saved

    # Its terminal still listed: a bare shell proves no exit without a launch time, so the
    # launch refuses; a process that ended is proof, so the launch closes it and replaces it.
    shelled = rec("mf", "x1", agent="codex", model="gpt-6.1-sol")
    del shelled["launched_at"]
    state_file.write_text(json.dumps({"workers": {"mf": shelled}}))
    world(local(term("term-mf", 300, ["% "], ident=None)))
    try:
        launch_on("mf", "codex", "gpt-6.1-sol")
    except SystemExit as stop:
        assert "term-mf may still hold its agent" in str(stop.code), stop.code
    except BaseException as error:
        raise AssertionError(f"launch raised {type(error).__name__}: {error}") from error
    else:
        raise AssertionError("a bare shell without a launch time was taken as an exit")
    assert not created() and calls("orca", ["terminal", "close"]) == [], calls()
    world(local(term("term-mf", 300, ["% "], status="exited", ident=None)))
    saved = raises_nothing(launch_on, "mf", "codex", "gpt-6.1-sol")
    assert saved["workers"]["mf"]["launched_at"] and created(), saved
    closed = [opt(r, "--terminal") for r in calls("orca", ["terminal", "close"])]
    assert closed == ["term-mf"], closed
    state_file.unlink()


@scenario
def daily_update_on_relaunch_and_desktop():
    """A relaunch from tick runs the same daily check as launch. Orca desktop behind the
    latest release is one STUCK: it installs updates when it restarts, which dev-auto
    never does."""
    reset_db()
    ticket_row("e2")
    (tmp / "wt" / "e2").mkdir(parents=True, exist_ok=True)
    st = fresh_state(e2=rec("e2", "c1"))
    st["updates"] = {}
    claude_usage("c1", 1, NOW + 600)
    world(
        local(term("term-e2", 300, ["% "], ident=None)),
        versions={"claude": "2.1.300"},
        latest={"claude": "2.1.322"},
        orca_version={"local": "1.4.250"},
    )
    tick(st)
    assert ["update"] in [r["args"] for r in calls("claude")]
    assert notes() == [
        "dev-auto: updated claude on local: 2.1.300 -> 2.1.322",
        ("dev-auto: STUCK e2: orca update on local failed: Orca desktop runs 1.4.250, "
        "latest is 1.4.257; it installs that on restart"),
        "dev-auto: relaunched claude:e2 on pro1 after its terminal exited",
    ], notes()
    assert {"local|claude", "local|orca"} <= set(st["updates"]), st["updates"]
    assert st["workers"]["e2"]["terminal"] == "term-local-1"
    clear_usage()


@scenario
def daily_update_on_wsl():
    """WSL: an npm global under a root-owned prefix updates through sudo -n. A headless
    Orca behind the latest release is replaced after its sha512 matches, restarted outside
    its unit, and counts as updated only once the runtime reports the new version. A
    failed restart is tried again on the next day's check; Orca is left alone while an
    agent terminal runs on that host."""
    home = tmp / "wsl-home"
    opt_dir = home / ".local" / "opt" / "orca"
    url = "https://github.com/stablyai/orca/releases/latest/download"
    image = "#!/bin/sh\nmkdir -p squashfs-root && echo new > squashfs-root/marker\n"
    digest = base64.b64encode(hashlib.sha512(image.encode()).digest()).decode()
    yml = (
        "version: 1.4.257\nfiles:\n  - url: orca-linux.AppImage\n    sha512: x\n"
        f"path: orca-linux.AppImage\nsha512: {digest}\n"
    )
    pkg = "@anthropic-ai/claude-code"
    npm = {
        f"ls -g --depth=0 {pkg}": [0, ""],
        "root -g": [0, "/nonexistent/node_modules\n"],
    }
    sudo_ok = {f"-n npm install -g {pkg}@latest": [0, ""]}
    restart = "--user --on-active=3 systemctl --user restart orca-serve.service"
    files = {f"{url}/latest-linux.yml": yml, f"{url}/orca-linux.AppImage": image}
    wsl = "Juzzark-WSL"
    da.ORCA_RESTART = 0

    def fresh(**extra):
        shutil.rmtree(home, ignore_errors=True)
        (opt_dir / "squashfs-root").mkdir(parents=True)
        (opt_dir / "squashfs-root" / "marker").write_text("old\n")
        reset_db()
        state_file.unlink(missing_ok=True)
        extra.setdefault("sudo", sudo_ok)
        extra.setdefault("curl", files)
        set_fixture(
            probe_env={"HOME": str(home)},
            npm=npm,
            orca_version={wsl: "1.4.250"},
            orca_mode={wsl: "unsupported-headless-serve"},
            **extra,
        )

    def stuck(tid, tool, error):
        return f"dev-auto: STUCK {tid}: {tool} update on {wsl} failed: {error}"

    fresh(restart_to={wsl: "1.4.257"}, **{"systemd-run": {restart: [0, ""]}})
    saved = launch_on("wsl-1", "claude", OPUS, host=wsl)
    assert [r["args"] for r in calls("sudo")] == [
        ["-n", "npm", "install", "-g", f"{pkg}@latest"]
    ]
    assert not any(r["args"] == ["update"] for r in calls("claude"))
    assert (opt_dir / "squashfs-root" / "marker").read_text() == "new\n"
    assert (opt_dir / "squashfs-root.old" / "marker").read_text() == "old\n"
    assert [r["args"] for r in calls("systemd-run")] == [restart.split()]
    assert notes() == [f"dev-auto: updated orca on {wsl}: 1.4.250 -> 1.4.257"], notes()
    assert {f"{wsl}|claude", f"{wsl}|orca"} <= set(saved["updates"])
    assert "wsl-1" in saved["workers"]

    fresh(
        sudo={f"-n npm install -g {pkg}@latest": [1, "sudo: a password is required\n"]},
        curl={**files, f"{url}/orca-linux.AppImage": "tampered"},
    )
    saved = launch_on("wsl-2", "claude", OPUS, host=wsl)
    assert notes() == [
        stuck("wsl-2", "claude", "sudo: a password is required"),
        stuck(
            "wsl-2",
            "orca",
            "orca-linux.AppImage sha512 does not match latest-linux.yml",
        ),
    ], notes()
    assert (opt_dir / "squashfs-root" / "marker").read_text() == "old\n"
    assert not calls("systemd-run") and "wsl-2" in saved["workers"]

    fresh()
    launch_on("wsl-3", "claude", OPUS, host=wsl)
    assert notes() == [
        stuck("wsl-3", "orca", "systemd-run could not schedule the orca-serve restart")
    ], notes()
    data = json.loads(state_file.read_text())
    data["updates"][f"{wsl}|orca"] -= da.DAY
    state_file.write_text(json.dumps(data))
    current = fixture()
    current["systemd-run"] = {restart: [0, ""]}
    fixture_file.write_text(json.dumps(current))
    log_file.write_text("")
    launch_on("wsl-4", "claude", OPUS, host=wsl)
    assert notes() == [
        stuck("wsl-4", "orca", "runs 1.4.250 after the restart, latest is 1.4.257")
    ], "a scheduled restart counts only once the runtime reports the new version"

    fresh()
    busy, _ = term("term-busy", 5, ident="codex", worktree="/elsewhere")
    current = fixture()
    current["terminals"] = {wsl: [busy]}
    fixture_file.write_text(json.dumps(current))
    launch_on("wsl-5", "claude", OPUS, host=wsl)
    saved = launch_on("wsl-6", "claude", OPUS, host=wsl)
    assert calls("curl") == [], "a busy host fetches no release, start after start"
    assert f"{wsl}|orca" not in saved["updates"] and f"{wsl}|claude" in saved["updates"]
    assert notes() == [], notes()
    current = fixture()
    current.update(terminals={}, restart_to={wsl: "1.4.257"})
    data = json.loads(state_file.read_text())
    data["workers"] = {}
    state_file.write_text(json.dumps(data))
    current["systemd-run"] = {restart: [0, ""]}
    fixture_file.write_text(json.dumps(current))
    saved = launch_on("wsl-7", "claude", OPUS, host=wsl)
    assert notes() == [f"dev-auto: updated orca on {wsl}: 1.4.250 -> 1.4.257"], notes()
    assert f"{wsl}|orca" in saved["updates"], "applied once the host is free"
    state_file.unlink()


@scenario
def daily_update_leaves_orca_under_an_unlisted_agent():
    """dev-auto-fixes-50 (d): a headless Orca is not restarted while a terminal lists no
    agent but may run one: a Claude screen whose identity expired, or a prompt that
    printed 5 seconds ago. A bare shell quiet for 2 minutes proves no agent runs."""
    wsl = "Juzzark-WSL"
    cases = (
        ("expired identity", 120, CLAUDE_BOX, False),
        ("just started", 5, ["$ "], False),
        ("bare shell", 120, ["$ "], True),
    )
    for n, (label, quiet, tail, fetched) in enumerate(cases):
        reset_db()
        state_file.unlink(missing_ok=True)
        row, screen = term("term-quiet", quiet, tail, ident=None, worktree="/elsewhere")
        # update_orca reads the wall clock, not the scenarios' NOW.
        row["lastOutputAt"] = int((time.time() - quiet) * 1000)
        set_fixture(
            orca_version={wsl: "1.4.250"},
            orca_mode={wsl: "unsupported-headless-serve"},
            terminals={wsl: [row]},
            screens={"term-quiet": screen},
        )
        launch_on(f"wsl-q{n}", "claude", OPUS, host=wsl)
        assert bool(calls("curl")) == fetched, (label, calls("curl"))
    state_file.unlink()


def evidence_tree(repo, slug, payload=b"x" * 42):
    """A main checkout with one evidence folder of a known size. Returns that folder."""
    folder = repo / ".work" / "evidence" / slug
    folder.mkdir(parents=True)
    (folder / "run.bin").write_bytes(payload)
    return folder


def landed_fetch(slug, base="staging"):
    """The fetch that brings feature/<slug> and the repo's base in before merge-base."""
    ref = f"refs/heads/feature/{slug}"
    return (
        "fetch -q origin "
        f"{ref}:refs/remotes/origin/feature/{slug} "
        f"refs/heads/{base}:refs/remotes/origin/{base}"
    )


def landed_git(slug, sha="abc123def456"):
    """origin has feature/<slug> and it is an ancestor of origin/staging."""
    ref = f"refs/heads/feature/{slug}"
    return {
        f"ls-remote --heads origin {ref}": [0, f"{sha}\t{ref}\n"],
        landed_fetch(slug): [0, ""],
        f"rev-parse -q --verify refs/remotes/origin/feature/{slug}": [0, f"{sha}\n"],
        f"merge-base --is-ancestor {sha} refs/remotes/origin/staging": [0, ""],
    }


def absent_git(slug):
    """feature/<slug> is not on origin, which counts as landed."""
    ref = f"refs/heads/feature/{slug}"
    return {f"ls-remote --heads origin {ref}": [0, ""]}


def unmerged_checkout(slug):
    """Clone of a local origin where feature/<slug> is not in staging and not local.

    The tip exists only on origin, so merge-base exits 128 until that ref is fetched
    and exits 1 afterwards. Call git through /usr/bin/git; the scenario's PATH git is the stub.
    """
    base = tmp / f"unmerged-{slug}"
    origin = base / "origin.git"
    builder = base / "builder"
    clone = base / "checkout"
    git = "/usr/bin/git"
    base.mkdir()
    subprocess.run(
        [git, "init", "--bare", "-b", "staging", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [git, "init", "-b", "staging", str(builder)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [git, "-C", str(builder), "remote", "add", "origin", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    (builder / "README").write_text("base\n")
    command = [
        git,
        "-c",
        "user.email=dev-auto@example.com",
        "-c",
        "user.name=dev-auto",
        "-C",
        str(builder),
    ]

    def g(*args):
        subprocess.run([*command, *args], check=True, capture_output=True, text=True)

    g("add", "README")
    g("commit", "-m", "base")
    g("push", "origin", "HEAD:refs/heads/staging")
    g("checkout", "-b", f"feature/{slug}")
    (builder / "tip.txt").write_text("tip\n")
    g("add", "tip.txt")
    g("commit", "-m", "tip")
    g("push", "origin", f"HEAD:refs/heads/feature/{slug}")
    # --no-local: a local path otherwise hardlinks every object, including the
    # unmerged tip, and merge-base would exit 1 without a fetch.
    subprocess.run(
        [
            git,
            "clone",
            "--no-local",
            "--branch",
            "staging",
            "--single-branch",
            str(origin),
            str(clone),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        [git, "-C", str(builder), "rev-parse", f"refs/heads/feature/{slug}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    present = subprocess.run(
        [git, "-C", str(clone), "cat-file", "-t", sha],
        check=False,
        capture_output=True,
        text=True,
    )
    if present.returncode == 0:
        raise AssertionError(f"feature tip {sha} is already in the clone")
    # Ignored, so the unmerged result is what keeps the folder.
    (clone / ".gitignore").write_text(".work/\n")
    return clone


def quiet_lsof(*paths):
    return {f"+D {path}": [1, ""] for path in paths}


@scenario
def evidence_removed_once_when_the_feature_has_landed():
    """A tick removes a finished feature's evidence folder once the branch has landed.

    Landed means merged into origin/staging, or absent on origin. A citation on a DONE
    ticket does not block. The next tick, with the folder gone, prints nothing and
    deletes nothing. A missing .work/specs directory is not a STUCK.
    """
    reset_db()
    repo = tmp / "evidence-landed"
    shipped = evidence_tree(repo, "shipped-feature")
    absent = evidence_tree(repo, "absent-branch", payload=b"y" * 7)
    kept = repo / ".work" / "evidence" / "still-open"
    kept.mkdir()
    (kept / "stay.txt").write_text("stay")
    ticket_row(
        "ev1",
        status="complete",
        title="Shipped Feature / lane",
        notes="archive note .work/evidence/shipped-feature stays on a done ticket",
    )
    ticket_row("ev1-review", status="closed", title="Shipped Feature / lane-review")
    ticket_row("ev2", status="complete", title="Absent Branch / lane")
    ticket_row("ev2-review", status="closed", title="Absent Branch / lane-review")
    ticket_row("ev3", status="active", title="Still Open / lane")
    git = allow_remove(
        {
            "rev-parse --path-format=absolute --git-common-dir": [
                0,
                f"{repo / '.git'}\n",
            ],
            **ROOT_GIT,
            **landed_git("shipped-feature"),
            **absent_git("absent-branch"),
        },
        shipped,
        absent,
    )
    lsof = quiet_lsof(shipped, absent)
    st = fresh_state()
    record_checkout(repo)
    world([], orch_worktree=str(repo), git=git, lsof=lsof)
    text = tick_text(st)
    assert not shipped.exists() and not absent.exists(), text
    assert (kept / "stay.txt").read_text() == "stay"
    lines = [line for line in text.splitlines() if "removed" in line]
    assert lines == [
        f"Absent Branch: removed {absent} (7 bytes)",
        f"Shipped Feature: removed {shipped} (42 bytes)",
    ], text
    assert "STUCK" not in text, text
    world([], orch_worktree=str(repo), git=git, lsof=lsof)
    again = tick_text(st)
    assert again.strip() == "", again
    assert not shipped.exists() and (kept / "stay.txt").is_file()


def finish_feature(tid, feature, review="closed"):
    ticket_row(tid, status="complete", title=f"{feature} / lane")
    ticket_row(f"{tid}-review", status=review, title=f"{feature} / lane-review")


def allow_remove(git, *folders):
    """The ignore check says these folders are gitignored and untracked."""
    for folder in folders:
        git = {
            **git,
            f"check-ignore -q -- {folder}": [0, ""],
            f"ls-files -- {folder}": [0, ""],
        }
    return git


ROOT_SHA = "1" * 40
ORIGIN_URL = "git@github.com:o/ledger.git"
# The raw config value is stubbed too: same here, so only the fetch URL is under test.
ORIGIN_GIT = {
    "remote get-url origin": [0, f"{ORIGIN_URL}\n"],
    "config --get remote.origin.url": [0, f"{ORIGIN_URL}\n"],
}
# A stubbed checkout's identity: the root commit and origin record_checkout writes.
ROOT_GIT = {"rev-list --max-parents=0 HEAD": [0, f"{ROOT_SHA}\n"], **ORIGIN_GIT}


def record_checkout(checkout, url=None):
    """What `ledger-init --checkout` writes: the ledger's main checkout, its repo's root
    commit and origin's fetch URL, read by real git for a real repo, else ROOT_SHA and
    url or ORIGIN_URL."""

    def read(*args):
        done = subprocess.run(
            ["/usr/bin/git", "-C", str(checkout), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        return done.stdout.split() if done.returncode == 0 else []

    first = (sorted(read("rev-list", "--max-parents=0", "HEAD")) or [ROOT_SHA])[0]
    url = (read("remote", "get-url", "origin") or [url or ORIGIN_URL])[0]
    (root / "supervisor").mkdir(exist_ok=True)
    (root / "supervisor" / "checkout").write_text(
        f"{os.path.realpath(checkout)}\n{first}\n{url}\n"
    )


def evidence_tick(repo, git, lsof, *folders, checkout=None, url=None):
    record_checkout(checkout or repo, url)
    git = allow_remove(
        {
            "rev-parse --path-format=absolute --git-common-dir": [
                0,
                f"{repo / '.git'}\n",
            ],
            **ROOT_GIT,
            **git,
        },
        *folders,
    )
    world([], orch_worktree=str(repo), git=git, lsof=lsof)
    return tick_text(fresh_state())


@scenario
def evidence_merged_check_follows_repo_base():
    """Only the configured base can release evidence; missing settings still use staging."""
    original = accounts.read_text()
    development = '[repos."o/r"]\nbase = "development"\n'
    cases = (
        ("development", development, 0, 1, 0, "development", True),
        ("staging-only", development, 1, 0, 0, "development", False),
        ("neither", development, 1, 1, 0, "development", False),
        ("no-entry", '[repos."other/repo"]\nbase = "development"\n', 1, 0, 0, "staging", True),
        ("no-base", '[repos."o/r"]\n', 1, 0, 0, "staging", True),
        ("no-repos", "", 1, 0, 0, "staging", True),
        ("origin-failed", development, 0, 0, 2, None, False),
    )
    try:
        for i, (name, config, dev, staging, origin, base, removed) in enumerate(cases):
            reset_db()
            accounts.write_text(original + "\n" + config)
            repo = tmp / f"evidence-repo-base-{name}"
            folder = evidence_tree(repo, "base-feature")
            finish_feature("rb", "Base Feature")
            url = "git@github.com:o/r.git" if i % 2 else "https://github.com/o/r.git"
            git = {
                **landed_git("base-feature"),
                "remote get-url origin": [origin, url + "\n"],
                landed_fetch("base-feature", "development"): [0, ""],
                "merge-base --is-ancestor abc123def456 refs/remotes/origin/development": [dev, ""],
                "merge-base --is-ancestor abc123def456 refs/remotes/origin/staging": [staging, ""],
            }
            text = evidence_tick(repo, git, quiet_lsof(folder), folder, url=url)
            assert folder.exists() != removed, (name, text)
            assert ("STUCK" in text) == (origin != 0), (name, text)
            fetches = [
                " ".join(c["args"]) for c in calls("git") if c["args"][:1] == ["fetch"]
            ]
            expected = [landed_fetch("base-feature", base)] if base else []
            assert fetches == expected, (name, fetches)
    finally:
        accounts.write_text(original)


@scenario
def evidence_removed_from_the_main_checkout_of_a_worktree():
    """A linked worktree with no .work still names the main checkout through git."""
    reset_db()
    repo = tmp / "evidence-main"
    worktree = tmp / "evidence-linked"
    worktree.mkdir()
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("lk", "Shipped Feature")
    git = {
        "rev-parse --path-format=absolute --git-common-dir": [
            0,
            f"{repo / '.git'}\n",
        ],
        **landed_git("shipped-feature"),
    }
    text = evidence_tick(worktree, git, quiet_lsof(folder), folder, checkout=repo)
    assert not folder.exists(), text
    assert text.splitlines() == [
        f"Shipped Feature: removed {folder} (42 bytes)"
    ], text
    assert worktree.is_dir()


@scenario
def evidence_removed_only_from_this_ledgers_repo():
    """A finished feature's folder goes from this ledger's checkout only.

    Another terminal's repo can hold the same evidence/<slug> path. An absent
    feature/<slug> on origin counts as landed, so cleaning every local terminal
    would delete that unrelated folder. The tick removes the orchestrator's
    checkout and leaves the other one.
    """
    reset_db()
    feature = "Shipped Feature"
    slug = "shipped-feature"
    ledger = tmp / "evidence-ledger-repo"
    other = tmp / "evidence-other-repo"
    ours = evidence_tree(ledger, slug)
    theirs = evidence_tree(other, slug, payload=b"z" * 9)
    finish_feature("lr", feature)
    record_checkout(ledger)
    world(
        local(term("term-stranger", 5, worktree=str(other))),
        orch_worktree=str(ledger),
        git=allow_remove(
            {
                "rev-parse --path-format=absolute --git-common-dir": [
                    0,
                    f"{ledger / '.git'}\n",
                ],
                **ROOT_GIT,
                **absent_git(slug),
            },
            ours,
        ),
        lsof=quiet_lsof(ours, theirs),
    )
    text = tick_text(fresh_state())
    assert not ours.exists(), text
    assert theirs.is_dir() and (theirs / "run.bin").is_file(), text
    assert (theirs / "run.bin").read_bytes() == b"z" * 9
    assert text.splitlines() == [f"{feature}: removed {ours} (42 bytes)"], text
    assert "STUCK" not in text, text


@scenario
def evidence_cleaned_only_in_the_recorded_checkout():
    """da-4-5: the registered orchestrator and a local worker sit in another repo's
    checkout, even one whose folder is named like the ledger, with its own ignored
    evidence/<slug> and no feature branch. Only the main checkout `ledger-init --checkout`
    recorded is cleaned; with no record, a record whose path is no longer that main
    checkout, or another repo put at the recorded path, nothing is: an unrelated one
    (another root commit) or a fork that shares the history (another origin)."""
    feature, slug = "Shipped Feature", "shipped-feature"
    cases = (
        # case, recorded, git's main checkout for every path, its root commit and origin,
        # ours removed
        ("unrecorded", False, "foreign", ROOT_SHA, ORIGIN_URL, False),
        ("recorded", True, "ours", ROOT_SHA, ORIGIN_URL, True),
        ("moved", True, "foreign", ROOT_SHA, ORIGIN_URL, False),
        ("replaced", True, "ours", "2" * 40, ORIGIN_URL, False),
        ("fork", True, "ours", ROOT_SHA, "git@github.com:someone/ledger.git", False),
    )
    for i, (case, recorded, common, root_sha, url, removed) in enumerate(cases):
        reset_db()
        ours = tmp / f"evidence-record-{case}" / "a" / "ledger"
        foreign = tmp / f"evidence-record-{case}" / "b" / "ledger"
        mine = evidence_tree(ours, slug)
        theirs = evidence_tree(foreign, slug, payload=b"z" * 9)
        finish_feature(f"rc{i}", feature)
        if recorded:
            record_checkout(ours)
        resolved = ours if common == "ours" else foreign
        git = allow_remove(
            {
                "rev-parse --path-format=absolute --git-common-dir": [
                    0,
                    f"{resolved / '.git'}\n",
                ],
                "rev-list --max-parents=0 HEAD": [0, f"{root_sha}\n"],
                "remote get-url origin": [0, f"{url}\n"],
                **absent_git(slug),
            },
            mine,
            theirs,
        )
        world(
            local(term("term-sw", 5, worktree=str(foreign))),
            orch_worktree=str(foreign),
            git=git,
            lsof=quiet_lsof(mine, theirs),
        )
        text = tick_text(fresh_state(sw=rec("sw", "c1", worktree=str(foreign))))
        assert (theirs / "run.bin").is_file(), (case, text)
        assert (theirs / "run.bin").read_bytes() == b"z" * 9, (case, text)
        assert mine.exists() != removed, (case, text)
        assert "STUCK Shipped" not in text, (case, text)


@scenario
def evidence_kept_when_origin_fetches_elsewhere():
    """da-4-5: the recorded checkout still has the recorded root commit and the recorded
    URL as a raw remote.origin.url value, but git fetches from a fork: through an
    insteadOf rewrite, or a first URL ahead of the recorded one. The identity is origin's
    effective fetch URL, so the folder stays though the feature is absent there too."""
    feature, slug = "Shipped Feature", "shipped-feature"
    for case in ("insteadof", "first-url"):
        reset_db()
        main = real_main(f"evidence-fetch-{case}")
        ignore_work(main)
        upstream = str(tmp / f"evidence-fetch-{case}" / "origin.git")
        fork = str(tmp / f"evidence-fetch-{case}" / "fork.git")
        subprocess.run([GIT, "clone", "-q", "--bare", upstream, fork], check=True)
        folder = evidence_tree(main, slug)
        finish_feature(f"fe{case[0]}", feature)
        record_checkout(main)
        if case == "insteadof":
            git_at(main, "config", f"url.{fork}.insteadOf", upstream)
        else:
            git_at(main, "config", "remote.origin.url", fork)
            git_at(main, "config", "--add", "remote.origin.url", upstream)
        world([], orch_worktree=str(main), git={}, git_bin=GIT, lsof=quiet_lsof(folder))
        text = tick_text(fresh_state())
        assert (folder / "run.bin").is_file(), (case, text)
        assert f"{feature}: removed" not in text, (case, text)


@scenario
def evidence_kept_while_the_feature_is_unpushed():
    """dev-auto-fixes-50 (e): every ticket done and feature/<slug> absent on origin, but
    the checkout's own feature/<slug> holds commits outside origin/staging: not pushed
    yet, so its evidence stays. Absent on origin and absent here, or a local branch inside
    origin/staging, still counts as landed."""
    feature, slug = "Local Feature", "local-feature"
    ref = f"refs/heads/feature/{slug}"
    local_tip = {
        **absent_git(slug),
        f"rev-parse -q --verify {ref}": [0, "fedcba987654\n"],
        "fetch -q origin refs/heads/staging:refs/remotes/origin/staging": [0, ""],
    }
    for case, merged, removed in (("unpushed", 1, False), ("merged", 0, True)):
        reset_db()
        repo = tmp / f"evidence-{case}"
        folder = evidence_tree(repo, slug)
        finish_feature(f"lf{merged}", feature)
        git = {**local_tip, "merge-base --is-ancestor fedcba987654 refs/remotes/origin/staging": [merged, ""]}
        text = evidence_tick(repo, git, quiet_lsof(folder), folder)
        assert folder.exists() != removed and "STUCK" not in text, (case, text)


@scenario
def evidence_kept_while_either_feature_tip_is_unmerged():
    """da-4-4, da-5-3: origin's feature/<slug> at A is already in origin/staging, but the
    checkout's own feature/<slug> has moved on to B, an unpushed reviewed merge. Both tips
    must be in the base: B outside it keeps the evidence, both inside removes it."""
    feature, slug = "Both Tips", "both-tips"
    ref = f"refs/heads/feature/{slug}"
    for case, local_merged, removed in (("local-ahead", 1, False), ("both-merged", 0, True)):
        reset_db()
        repo = tmp / f"evidence-tips-{case}"
        folder = evidence_tree(repo, slug)
        finish_feature(f"bt{local_merged}", feature)
        git = {
            **landed_git(slug),
            f"rev-parse -q --verify {ref}": [0, "fedcba987654\n"],
            "merge-base --is-ancestor fedcba987654 refs/remotes/origin/staging": [local_merged, ""],
        }
        text = evidence_tick(repo, git, quiet_lsof(folder), folder)
        assert folder.exists() != removed and "STUCK" not in text, (case, text)
        merges = [c["args"][-2] for c in calls("git") if c["args"][:1] == ["merge-base"]]
        assert merges == ["abc123def456", "fedcba987654"], (case, merges)


@scenario
def evidence_kept_when_origin_advances_before_the_fetch():
    """da-4-4, da-5-3: ls-remote finds origin's feature/<slug> at A, already in the base;
    before the fetch it advances to B, which is not. The tip tested is the one the fetch
    wrote, B, so the folder stays."""
    feature, slug = "Moving Tip", "moving-tip"
    reset_db()
    repo = tmp / "evidence-moving-tip"
    folder = evidence_tree(repo, slug)
    finish_feature("mt", feature)
    git = {
        **landed_git(slug),
        f"rev-parse -q --verify refs/remotes/origin/feature/{slug}": [0, "b0b0b0b0b0b0\n"],
        "merge-base --is-ancestor b0b0b0b0b0b0 refs/remotes/origin/staging": [1, ""],
    }
    text = evidence_tick(repo, git, quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file() and "STUCK" not in text, text
    merges = [c["args"][-2] for c in calls("git") if c["args"][:1] == ["merge-base"]]
    assert merges == ["b0b0b0b0b0b0"], merges


@scenario
def evidence_kept_while_a_tickets_own_branch_is_unmerged():
    """da-5-3: a done standalone ticket (`Feature branch: none`) shipped on its own
    `Branch:`, and feature/<slug> is absent on origin and here. That branch must be in the
    base its pull request goes into, as delivery reads it, else the Branch line's `from`
    base; the repo's base proves nothing. Absent here and on origin, the commit of the
    ticket's latest review request must have a PUSH from another agent after it and be in
    that base: a merged pull request that once used the same branch name, or an older
    request's PUSH on a reopened ticket, proves nothing. The PUSH must name a SHA, resolved
    to one full commit: `<sha>~1` naming a landed parent proves nothing. No base named, no
    PUSH for the latest request, a commit not here, or one outside the base keeps the
    folder; a check that cannot run is a STUCK that keeps it."""
    feature, slug = "Solo Fix", "solo-fix"
    fix = "refs/heads/fix/solo"
    sha = "0123456789ab"
    present = {
        f"ls-remote --heads origin {fix}": [0, f"{sha}\t{fix}\n"],
        "rev-parse -q --verify refs/remotes/origin/fix/solo": [0, f"{sha}\n"],
    }
    absent = {f"ls-remote --heads origin {fix}": [0, ""]}

    def fetched(base):
        return {
            f"fetch -q origin {fix}:refs/remotes/origin/fix/solo "
            f"refs/heads/{base}:refs/remotes/origin/{base}": [0, ""]
        }

    def in_base(main, staging):
        return {
            f"merge-base --is-ancestor {sha} refs/remotes/origin/main": [main, ""],
            f"merge-base --is-ancestor {sha} refs/remotes/origin/staging": [staging, ""],
        }

    reviewed, owned = "fedcba9876543210", "0000000aaaaaaa"
    full, owned_full = reviewed.ljust(40, "e"), owned.ljust(40, "a")

    twin = reviewed[:7]
    other_full = twin.ljust(40, "1")

    def landed_review(fetch=0, known=0, merged=0):
        return {
            **absent,
            # As commit_landed resolves each token: an object id naming one commit.
            f"rev-parse --disambiguate={reviewed}": [0, f"{full}\n" if not known else ""],
            f"cat-file -t {full}": [0, "commit\n"],
            # A 7-digit request token names A and B; B's full id names B, which is in main.
            f"rev-parse --disambiguate={twin}": [0, f"{full}\n{other_full}\n"],
            f"rev-parse --disambiguate={other_full}": [0, f"{other_full}\n"],
            f"cat-file -t {other_full}": [0, "commit\n"],
            f"merge-base --is-ancestor {other_full} refs/remotes/origin/main": [0, ""],
            f"rev-parse -q --verify {other_full}^{{commit}}": [0, f"{other_full}\n"],
            f"cat-file -e {other_full}^{{commit}}": [0, ""],
            "fetch -q origin refs/heads/main:refs/remotes/origin/main": [fetch, ""],
            f"rev-parse -q --verify {reviewed}^{{commit}}": [known, f"{full}\n"],
            f"merge-base --is-ancestor {full} refs/remotes/origin/main": [merged, ""],
            # An older or the owner's own PUSH names a commit in main; it clears nothing.
            f"rev-parse -q --verify {owned}^{{commit}}": [0, f"{owned_full}\n"],
            f"merge-base --is-ancestor {owned_full} refs/remotes/origin/main": [0, ""],
            # The same answers for the short spelling an older reader passes to git.
            f"cat-file -e {reviewed}^{{commit}}": [known, ""],
            f"merge-base --is-ancestor {reviewed} refs/remotes/origin/main": [merged, ""],
            f"cat-file -e {owned}^{{commit}}": [0, ""],
            f"merge-base --is-ancestor {owned} refs/remotes/origin/main": [0, ""],
            # A verdict token that is no SHA would name reviewed's parent, which landed.
            f"rev-parse -q --verify {reviewed}~1^{{commit}}": [0, f"{owned_full}\n"],
            f"cat-file -e {reviewed}~1^{{commit}}": [0, ""],
            f"merge-base --is-ancestor {reviewed}~1 refs/remotes/origin/main": [0, ""],
        }

    # An old pull request from fix/solo into main merged; gh would count it.
    stale_pr = {
        "pr list --head fix/solo --base main --state merged --json number --jq length": [0, "1\n"]
    }

    compact = "Repo: here.\nFeature branch: none · Branch: `fix/solo` from `main`\n"
    into = "Repo: here.\nBranch: `fix/solo` · Feature branch: none (pull request into main)\n"
    bare = "Repo: here.\nBranch: `fix/solo` · Feature branch: none (pull request)\n"
    cases = (
        # case, notes, git, review history, removed, stuck
        ("unmerged", compact, {**present, **fetched("main"), **in_base(1, 1)}, "pushed", False, False),
        ("merged", compact, {**present, **fetched("main"), **in_base(0, 1)}, "pushed", True, False),
        ("into-main", into, {**present, **fetched("main"), **in_base(1, 0)}, "pushed", False, False),
        ("no-base", bare, {**present, **fetched("staging"), **in_base(0, 0)}, "pushed", False, False),
        ("absent-reviewed-in-base", compact, landed_review(), "pushed", True, False),
        ("absent-reused-name", compact, landed_review(merged=1), "pushed", False, False),
        ("absent-unreviewed", compact, landed_review(), "unreviewed", False, False),
        ("absent-reopened", compact, landed_review(), "reopened", False, False),
        ("absent-ancestor-token", compact, landed_review(merged=1), "parent-token", False, False),
        ("absent-commit-unknown", compact, landed_review(known=1), "pushed", False, False),
        ("absent-ambiguous-tokens", compact, landed_review(merged=1), "twin", False, False),
        ("absent-fetch-fails", compact, landed_review(fetch=2), "pushed", False, True),
        ("unreadable", compact, {f"ls-remote --heads origin {fix}": [2, ""]}, "pushed", False, True),
    )
    for i, (case, notes, branch_git, history, removed, stuck) in enumerate(cases):
        reset_db()
        repo = tmp / f"evidence-solo-{case}"
        folder = evidence_tree(repo, slug)
        ticket_row(f"so{i}", status="complete", title=f"{feature} / lane", notes=notes)
        ticket_row(f"so{i}-review", status="closed", title=f"{feature} / lane-review")
        owner, reviewer = f"claude:so{i}", "codex:so-r"
        posts = {
            "pushed": [(owner, f"REVIEW READY {reviewed}"), (reviewer, f"REVIEW VERDICT: PUSH {reviewed}")],
            # A short request for A, a full PUSH of B sharing its 7 digits; B is in main.
            "twin": [(owner, f"REVIEW READY {twin}"), (reviewer, f"REVIEW VERDICT: PUSH {other_full}")],
            "parent-token": [
                (owner, f"REVIEW READY {reviewed}"),
                (reviewer, f"REVIEW VERDICT: PUSH {reviewed}~1"),
            ],
            # Only the owner's own PUSH, of a commit in main, follows the request.
            "unreviewed": [(owner, f"REVIEW READY {reviewed}"), (owner, f"REVIEW VERDICT: PUSH {owned}")],
            # Cleared and landed at the older commit, reopened, and the new one sent back.
            "reopened": [
                (owner, f"REVIEW READY {owned}"),
                (reviewer, f"REVIEW VERDICT: PUSH {owned}"),
                (owner, f"REVIEW READY {reviewed}"),
                (reviewer, f"REVIEW VERDICT: FIX {reviewed}"),
            ],
        }[history]
        for agent, content in posts:
            update(f"so{i}", "progress" if "READY" in content else "finding", content, agent=agent)
        record_checkout(repo)
        git = allow_remove(
            {
                "rev-parse --path-format=absolute --git-common-dir": [0, f"{repo / '.git'}\n"],
                **ROOT_GIT,
                **absent_git(slug),
                **branch_git,
            },
            folder,
        )
        world([], orch_worktree=str(repo), git=git, gh=stale_pr, lsof=quiet_lsof(folder))
        text = tick_text(fresh_state())
        assert folder.exists() != removed, (case, text)
        assert ("STUCK" in text) == stuck, (case, text)




@scenario
def reviewed_sha_needs_a_standing_push_of_the_latest_request():
    """reviewed_sha names the latest request's commit only while a standing PUSH of it, by
    someone other than the owner, is the newest live verdict after that request: a PUSH
    later superseded, followed by a FIX of that SHA, of another SHA, or the owner's own
    receipt names nothing. Every case is checked; the failing ones are listed."""
    bad = []
    sha = "fedcba9876543210"
    for case, want in (
        ("push", (sha, sha)), ("superseded", None), ("fix", None), ("other", None), ("owner", None)
    ):
        reset_db()
        ticket_row("rs")
        update("rs", "progress", f"REVIEW READY {sha}")
        if case == "owner":
            update("rs", "finding", f"REVIEW VERDICT: PUSH {sha} received")
        else:
            push = update("rs", "finding", f"REVIEW VERDICT: PUSH {sha if case != 'other' else '0a1b2c3d'}", "codex:rs-r")
            if case == "superseded":
                supersede(push, update("rs", "decision", "withdrawn", "codex:rs-r"))
            if case == "fix":
                update("rs", "finding", f"REVIEW VERDICT: FIX {sha} missed a check", "codex:rs-r")
        try:
            got = da.reviewed_sha(da.read_tickets(root)["", "rs"])
        except Exception as error:  # noqa: BLE001 - each case is reported, not the first
            got = f"{type(error).__name__}: {error}"
        if got != want:
            bad.append(f"{case}: {got}")
    assert not bad, bad

@scenario
def evidence_kept_when_a_local_ref_shadows_the_base():
    """da-5-3: a local tag named origin/staging contains the reviewed commit X; origin's
    real staging does not. Ancestry is read from refs/remotes/origin/staging, which the
    fetch wrote, so the tag proves nothing: the folder stays whether the ticket's
    standalone branch is gone (its reviewed commit is checked) or still here at X."""
    feature, slug = "Shadow Fix", "shadow-fix"
    for case in ("reviewed-commit", "branch-tip"):
        reset_db()
        main = real_main(f"evidence-shadow-{case}")
        ignore_work(main)
        git_at(main, "checkout", "-q", "-b", "side")
        (main / "x.txt").write_text("x\n")
        git_at(main, "add", "x.txt")
        git_at(main, "commit", "-q", "-m", "x")
        x = subprocess.run(
            [GIT, "-C", str(main), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        git_at(main, "checkout", "-q", "staging")
        git_at(main, "tag", "origin/staging", x)
        if case == "branch-tip":
            git_at(main, "branch", "fix/shadow", x)
        folder = evidence_tree(main, slug)
        notes = "Feature branch: none · Branch: `fix/shadow` from `staging`\n"
        ticket_row("sh", status="complete", title=f"{feature} / lane", notes=notes)
        ticket_row("sh-review", status="closed", title=f"{feature} / lane-review")
        update("sh", "progress", f"REVIEW READY {x}", agent="claude:sh")
        update("sh", "finding", f"REVIEW VERDICT: PUSH {x}", agent="codex:sh-r")
        record_checkout(main)
        world([], orch_worktree=str(main), git={}, git_bin=GIT, lsof=quiet_lsof(folder))
        text = tick_text(fresh_state())
        assert (folder / "run.bin").is_file(), (case, text)
        assert "STUCK" not in text, (case, text)


@scenario
def evidence_kept_when_the_local_feature_ref_is_broken():
    """dev-auto-fixes-50 (e), review 8052: a local feature/<slug> ref git warns is broken
    is not proof of absence; with the branch absent on origin the evidence stays and the
    check is a STUCK."""
    feature, slug = "Broken Feature", "broken-feature"
    reset_db()
    repo = tmp / "evidence-broken-ref"
    folder = evidence_tree(repo, slug)
    real = "/usr/bin/git"
    subprocess.run([real, "init", "-q", "-b", "main", str(repo)], check=True)
    commit = [real, "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a"]
    subprocess.run([*commit, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run([real, "-C", str(repo), "branch", f"feature/{slug}"], check=True)
    (repo / ".git" / "refs" / "heads" / "feature" / slug).write_text("not-a-valid-object-id\n")
    finish_feature("bf", feature)
    record_checkout(repo)
    git = allow_remove(
        {
            "rev-parse --path-format=absolute --git-common-dir": [0, f"{repo / '.git'}\n"],
            **ORIGIN_GIT,
            **absent_git(slug),
        },
        folder,
    )
    world([], orch_worktree=str(repo), git=git, git_bin=real, lsof=quiet_lsof(folder))
    text = tick_text(fresh_state())
    assert folder.exists() and f"{feature}: removed" not in text, text
    assert f"STUCK {feature}: evidence check failed. Nothing removed" in text, text


@scenario
def evidence_kept_when_a_gate_is_closed():
    """Each closed gate keeps the folder: an open ticket, an unmerged branch, a citation
    on a not-DONE ticket, a citation in .work/specs, or a file lsof reports open."""
    feature = "Shipped Feature"
    slug = "shipped-feature"

    reset_db()
    repo = tmp / "evidence-open-ticket"
    folder = evidence_tree(repo, slug)
    ticket_row("op", status="complete", title=f"{feature} / lane")
    ticket_row("op-review", status="active", title=f"{feature} / lane-review")
    text = evidence_tick(repo, landed_git(slug), quiet_lsof(folder))
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text

    reset_db()
    # The feature tip is on origin and not in this clone. merge-base on that SHA
    # exits 128 until the ref is fetched, and 1 once it is. A stubbed exit 1
    # would pass without the fetch.
    unmerged = "Unmerged Feature"
    unmerged_slug = "unmerged-feature"
    repo = unmerged_checkout(unmerged_slug)
    folder = evidence_tree(repo, unmerged_slug)
    finish_feature("um", unmerged)
    record_checkout(repo)
    world(
        [],
        orch_worktree=str(repo),
        git={},
        git_bin="/usr/bin/git",
        lsof=quiet_lsof(folder),
    )
    text = tick_text(fresh_state())
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text
    assert any(
        row["args"][:2] == ["merge-base", "--is-ancestor"] for row in calls("git")
    ), [row["args"] for row in calls("git")]

    reset_db()
    repo = tmp / "evidence-cited-ticket"
    folder = evidence_tree(repo, slug)
    finish_feature("ct", feature)
    ticket_row("other", status="active", title="Other / lane", notes="no path here")
    update("other", "progress", f"still reading evidence/{slug}")
    text = evidence_tick(repo, landed_git(slug), quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text

    reset_db()
    repo = tmp / "evidence-cited-spec"
    folder = evidence_tree(repo, slug)
    finish_feature("cs", feature)
    specs = repo / ".work" / "specs"
    specs.mkdir()
    (specs / "note.md").write_text(f"see .work/evidence/{slug} before deleting\n")
    text = evidence_tick(repo, landed_git(slug), quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text
    assert (specs / "note.md").is_file()

    reset_db()
    repo = tmp / "evidence-open-file"
    folder = evidence_tree(repo, slug)
    finish_feature("of", feature)
    lsof = {f"+D {folder}": [1, f"zsh 9 user 3r REG {folder / 'run.bin'}\n"]}
    text = evidence_tick(repo, landed_git(slug), lsof, folder)
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text


@scenario
def evidence_citation_names_its_own_folder_only():
    """da-4-14: an open ticket and a spec cite evidence/worker-queue-v2 or
    evidence/worker-queue.v2, other folders, so finished worker-queue's evidence goes. A
    citation of the folder itself, of a file in it, or at the end of a sentence keeps it."""
    feature, slug = "Worker Queue", "worker-queue"
    cases = (
        ("longer", "see evidence/worker-queue-v2 and .work/evidence/worker-queue_old/x", True),
        ("inside", "see .work/evidence/worker-queue/run.bin", False),
        ("dotted", "see evidence/worker-queue.v2/run.bin", True),
        ("sentence", "kept in evidence/worker-queue. Then more.", False),
    )
    for i, (case, cite, removed) in enumerate(cases):
        reset_db()
        repo = tmp / f"evidence-cite-{case}"
        folder = evidence_tree(repo, slug)
        finish_feature(f"wq{i}", feature)
        ticket_row("busy", status="active", title="Worker Queue V2 / lane", notes=cite)
        specs = repo / ".work" / "specs"
        specs.mkdir()
        (specs / "v2.md").write_text("report: .work/evidence/worker-queue-v2/report.md\n")
        text = evidence_tick(repo, landed_git(slug), quiet_lsof(folder), folder)
        assert folder.exists() != removed and "STUCK" not in text, (case, text)


@scenario
def evidence_stuck_when_a_check_cannot_run():
    """lsof or the merge check failing prints one STUCK line and removes nothing."""
    feature = "Shipped Feature"
    slug = "shipped-feature"
    ref = f"refs/heads/feature/{slug}"
    sha = "abc123def456"
    present = {f"ls-remote --heads origin {ref}": [0, f"{sha}\t{ref}\n"]}
    fetched = {
        landed_fetch(slug): [0, ""],
        f"rev-parse -q --verify refs/remotes/origin/feature/{slug}": [0, f"{sha}\n"],
    }
    cases = (
        ("ls-remote", {f"ls-remote --heads origin {ref}": [2, ""]}, quiet_lsof),
        ("fetch", {**present, landed_fetch(slug): [2, ""]}, quiet_lsof),
        (
            "merge-base",
            {**present, **fetched, f"merge-base --is-ancestor {sha} refs/remotes/origin/staging": [2, ""]},
            quiet_lsof,
        ),
        ("lsof", {**landed_git(slug)}, None),
    )
    for name, git, lsof_for in cases:
        reset_db()
        repo = tmp / f"evidence-stuck-{name}"
        folder = evidence_tree(repo, slug)
        finish_feature(name, feature)
        lsof = {f"+D {folder}": [2, ""]} if lsof_for is None else lsof_for(folder)
        text = evidence_tick(repo, git, lsof)
        assert (folder / "run.bin").is_file(), name
        assert text.splitlines() == [
            f"STUCK {feature}: evidence check failed. Nothing removed"
        ], (name, text)


@scenario
def evidence_never_removes_outside_its_own_folder():
    """A symlink, a file, a sibling, specs, archive, operations.md, and `..` stay."""
    reset_db()
    repo = tmp / "evidence-escape"
    outside = tmp / "evidence-outside"
    secret = outside / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("keep-outside")
    work = repo / ".work"
    work.mkdir(parents=True)
    (work / "operations.md").write_text("ops")
    (work / "archive").mkdir()
    (work / "archive" / "keep.txt").write_text("archive")
    (work / "specs").mkdir()
    (work / "specs" / "keep.md").write_text("spec\n")
    evidence = work / "evidence"
    evidence.mkdir()
    (evidence / "loose.txt").write_text("loose")
    sibling = evidence / "other-feature"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling")
    (evidence / "shipped-feature").symlink_to(outside)
    narrow = evidence / "narrow-feature"
    narrow.mkdir()
    (narrow / "run.bin").write_bytes(b"n" * 4)
    finish_feature("sf", "Shipped Feature")
    finish_feature("nf", "Narrow Feature")
    finish_feature("dot", "..")
    git = landed_git("narrow-feature")
    text = evidence_tick(repo, git, quiet_lsof(narrow), narrow, evidence / "shipped-feature")
    assert text.splitlines() == [f"Narrow Feature: removed {narrow} (4 bytes)"], text
    assert not narrow.exists()
    assert secret.read_text() == "keep-outside"
    assert (evidence / "shipped-feature").is_symlink()
    assert (evidence / "loose.txt").read_text() == "loose"
    assert (sibling / "keep.txt").read_text() == "sibling"
    assert (work / "operations.md").read_text() == "ops"
    assert (work / "archive" / "keep.txt").read_text() == "archive"
    assert (work / "specs" / "keep.md").read_text() == "spec\n"
    assert evidence.is_dir() and work.is_dir()

    reset_db()
    repo = tmp / "evidence-linked-root"
    outside = tmp / "evidence-linked-outside"
    hidden = outside / "shipped-feature"
    hidden.mkdir(parents=True)
    (hidden / "secret.txt").write_text("linked-root")
    work = repo / ".work"
    work.mkdir(parents=True)
    (work / "evidence").symlink_to(outside)
    finish_feature("lr", "Shipped Feature")
    text = evidence_tick(repo, landed_git("shipped-feature"), {}, hidden)
    assert hidden.is_dir() and (hidden / "secret.txt").read_text() == "linked-root"
    assert (work / "evidence").is_symlink()
    assert "removed" not in text and "STUCK" not in text, text


GIT = "/usr/bin/git"


def git_at(cwd, *args):
    subprocess.run(
        [
            GIT,
            "-c",
            "user.email=dev-auto@example.com",
            "-c",
            "user.name=dev-auto",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def real_main(name):
    """A real main checkout with origin/staging and no feature branch."""
    base = tmp / name
    origin = base / "origin.git"
    main = base / "checkout"
    base.mkdir()
    subprocess.run(
        [GIT, "init", "--bare", "-b", "staging", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [GIT, "init", "-b", "staging", str(main)],
        check=True,
        capture_output=True,
        text=True,
    )
    (main / "README").write_text("base\n")
    git_at(main, "add", "README")
    git_at(main, "commit", "-m", "base")
    git_at(main, "remote", "add", "origin", str(origin))
    git_at(main, "push", "-q", "origin", "HEAD:refs/heads/staging")
    return main


def ignore_work(main):
    (main / ".gitignore").write_text(".work/\n")
    git_at(main, "add", ".gitignore")
    git_at(main, "commit", "-m", "ignore work")


@scenario
def evidence_removed_from_the_main_checkout_when_a_worktree_has_work():
    """A linked worktree with its own .work still resolves to the main checkout.

    The worktree's evidence folder stays. The main checkout's folder goes.
    """
    reset_db()
    main = real_main("evidence-main-not-worktree")
    ignore_work(main)
    linked = tmp / "evidence-main-not-worktree" / "linked"
    git_at(main, "worktree", "add", "-b", "task/lane", str(linked))
    slug = "shipped-feature"
    main_folder = evidence_tree(main, slug)
    linked_folder = evidence_tree(linked, slug, payload=b"w" * 5)
    finish_feature("mw", "Shipped Feature")
    record_checkout(main)
    world(
        [],
        orch_worktree=str(linked),
        git={},
        git_bin=GIT,
        lsof=quiet_lsof(main_folder, linked_folder),
    )
    text = tick_text(fresh_state())
    assert not main_folder.exists(), text
    assert linked_folder.is_dir() and (linked_folder / "run.bin").is_file(), text
    assert text.splitlines() == [
        f"Shipped Feature: removed {main_folder} (42 bytes)"
    ], text
    assert "STUCK" not in text, text


@scenario
def evidence_kept_when_the_folder_is_tracked():
    """A tracked evidence folder is not the ignored .work tree, so it stays."""
    reset_db()
    main = real_main("evidence-tracked")
    folder = evidence_tree(main, "shipped-feature")
    git_at(main, "add", ".work")
    git_at(main, "commit", "-m", "track evidence")
    finish_feature("tr", "Shipped Feature")
    record_checkout(main)
    world([], orch_worktree=str(main), git={}, git_bin=GIT, lsof=quiet_lsof(folder))
    text = tick_text(fresh_state())
    assert (folder / "run.bin").is_file(), text
    assert "removed" not in text and "STUCK" not in text, text


@scenario
def evidence_kept_when_another_title_shares_the_slug():
    """Titles that slug to one folder all have to be done before that folder goes."""
    reset_db()
    repo = tmp / "evidence-shared-slug"
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("ss", "Shipped Feature")
    ticket_row("ss-open", status="active", title="shipped-feature / lane")
    text = evidence_tick(repo, landed_git("shipped-feature"), quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text


@scenario
def evidence_kept_when_specs_are_linked():
    """A symlinked .work/specs directory still counts when it cites the folder."""
    reset_db()
    repo = tmp / "evidence-specs-link"
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("sp", "Shipped Feature")
    target = tmp / "evidence-specs-target"
    target.mkdir()
    (target / "keep.md").write_text("see .work/evidence/shipped-feature\n")
    (repo / ".work" / "specs").symlink_to(target, target_is_directory=True)
    text = evidence_tick(repo, landed_git("shipped-feature"), quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file() and "removed" not in text and "STUCK" not in text, text


@scenario
def evidence_stuck_when_a_spec_cannot_be_read():
    """A spec that cannot be read is a failed check: one STUCK line, nothing removed."""
    reset_db()
    repo = tmp / "evidence-spec-unreadable"
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("su", "Shipped Feature")
    specs = repo / ".work" / "specs"
    specs.mkdir()
    secret = specs / "keep.md"
    secret.write_text("placeholder\n")
    secret.chmod(0)
    try:
        text = evidence_tick(
            repo, landed_git("shipped-feature"), quiet_lsof(folder), folder
        )
        assert (folder / "run.bin").is_file(), text
        assert text.splitlines() == [
            "STUCK Shipped Feature: evidence check failed. Nothing removed"
        ], text
    finally:
        secret.chmod(0o644)


@scenario
def evidence_stuck_when_the_specs_directory_cannot_be_listed():
    """A specs directory that cannot be listed is a failed check, not an empty one.

    The citing note is inside. A listing that swallows the permission error would
    delete the evidence folder and print a removal line.
    """
    reset_db()
    repo = tmp / "evidence-specs-unreadable"
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("sd", "Shipped Feature")
    specs = repo / ".work" / "specs"
    specs.mkdir()
    (specs / "keep.md").write_text("see .work/evidence/shipped-feature\n")
    specs.chmod(0)
    try:
        text = evidence_tick(
            repo, landed_git("shipped-feature"), quiet_lsof(folder), folder
        )
        assert (folder / "run.bin").is_file(), text
        assert text.splitlines() == [
            "STUCK Shipped Feature: evidence check failed. Nothing removed"
        ], text
    finally:
        specs.chmod(0o755)


@scenario
def list_spec_markdown_matches_glob_star_md():
    """The specs listing is the same set Path.glob('*.md') returns.

    That set includes .md and ..md, which Path.suffix drops, and a directory
    named dir.md. A directory that cannot be listed is None, not an empty list.
    """
    specs = tmp / "spec-names"
    specs.mkdir()
    for name in (
        ".md",
        "..md",
        ".hidden.md",
        "note.md",
        "NOTE.MD",
        "note.md.bak",
        "note.txt",
    ):
        (specs / name).write_text("x\n")
    (specs / "dir.md").mkdir()
    listed = da.list_spec_markdown(specs)
    assert listed is not None
    assert {path.name for path in listed} == {path.name for path in specs.glob("*.md")}
    specs.chmod(0)
    try:
        assert da.list_spec_markdown(specs) is None
    finally:
        specs.chmod(0o755)


@scenario
def evidence_kept_when_a_dot_md_spec_cites_the_folder():
    """A citing specs file named .md keeps the folder, with no removal and no STUCK."""
    reset_db()
    repo = tmp / "evidence-dot-md"
    folder = evidence_tree(repo, "shipped-feature")
    finish_feature("dm", "Shipped Feature")
    specs = repo / ".work" / "specs"
    specs.mkdir()
    (specs / ".md").write_text("see .work/evidence/shipped-feature\n")
    text = evidence_tick(repo, landed_git("shipped-feature"), quiet_lsof(folder), folder)
    assert (folder / "run.bin").is_file(), text
    assert "removed" not in text and "STUCK" not in text, text


@scenario
def review_ticket_publishes_an_unowned_review_ticket():
    """Live (orchestrator handoffs 11-12 to 11-14, publish_review.py and gen-*.py):
    `review-ticket` writes <ticket>-review's notes from the coder ticket and leaves it
    proposed and unowned, released at the version the store holds (start_work answers
    with text). Its Tier adds interrogate on hard and ui-review on ui work; a second run,
    or a coder ticket with no Reviewer line, publishes nothing."""
    reset_db()
    set_fixture(start_version=3)
    wt = str(tmp / "wt" / "rt-01")
    hard = (
        "Outcome: x.\nSkills: duo, tdd\nCoder: claude (claude-opus-5-5, effort high)"
        " · Reviewer: codex (gpt-6.1-sol, effort xhigh) · Tier: hard\nBlocked by: none"
    )
    ticket_row("rt-01", title="feat / lane", notes=hard)
    done = cli("review-ticket", "rt-01", "--worktree", wt, "--host", "Juzzark-WSL")
    assert done.returncode == 0, done
    review = {k[1]: v for k, v in da.read_tickets(root).items()}["rt-01-review"]
    assert review["status"] == "proposed" and owner_of("rt-01-review") is None, review
    assert review["title"] == "feat / lane-review", review
    for want in (
        "Coder's ticket: rt-01.",
        f"on Juzzark-WSL ({wt})",
        "\nSkills: duo, review, interrogate\n",
        "\nCoder: codex (gpt-6.1-sol, effort xhigh) · Reviewer: none · Tier: hard\n",
        "\nBlocked by: none",
    ):
        assert want in review["notes"], (want, review["notes"])
    again = cli("review-ticket", "rt-01", "--worktree", wt)
    assert again.returncode != 0 and "already" in again.stderr, again
    ui = "Coder: claude · Reviewer: claude (claude-sonnet-5-5, effort medium) · Tier: ui"
    ticket_row("rt-02", title="feat / view", notes=ui)
    assert cli("review-ticket", "rt-02", "--worktree", wt).returncode == 0
    notes = {k[1]: v for k, v in da.read_tickets(root).items()}["rt-02-review"]["notes"]
    assert "\nSkills: duo, review, ui-review\n" in notes and "on this Mac" in notes, notes
    ticket_row("rt-03", title="feat / bare", notes="Tier: medium")
    bare = cli("review-ticket", "rt-03", "--worktree", wt)
    assert bare.returncode != 0 and "Reviewer" in bare.stderr, bare
    assert "rt-03-review" not in {k[1] for k in da.read_tickets(root)}
    # One id in two projects names no single coder ticket: nothing is started.
    reviewer = "Reviewer: codex · Tier: medium"
    ticket_row("rt-04", title="feat / dup", notes=reviewer)
    ticket_row("rt-04", title="other / dup", notes=reviewer, project="other")
    log_file.write_text("")
    dup = cli("review-ticket", "rt-04", "--worktree", wt)
    assert dup.returncode != 0 and "several projects" in dup.stderr, dup
    assert "rt-04-review" not in {k[1] for k in da.read_tickets(root)}
    assert not ledger_calls("start_work"), ledger_calls("start_work")


@scenario
def resume_restarts_a_claude_session_in_place():
    """Live (resume_lost.py, restart_claude_session.py): `resume` closes a Claude
    worker's terminal at its prompt, with its tab, and starts the same session on the
    same account and host; a lost record resumes too and is lost no more. A running
    turn, or a record of another agent, is refused before anything is closed, and a close
    Orca refuses before anything is started."""
    reset_db()
    ticket_row("rs")

    def resume(record, *terms, git_fixture=HR_RELAY_GIT):
        state_file.write_text(json.dumps({"workers": {"rs": record}}))
        world(local(*terms), git=git_fixture)
        return cli("resume", "rs")

    done = resume(rec("rs", "c2"), term("term-rs", 600, ["❯ "]))
    assert done.returncode == 0, done
    closed = [opt(c, "--terminal") for c in calls("orca", ["terminal", "close"]) if "--tab" in c["args"]]
    assert "term-rs" in closed, closed
    (made,) = created()
    assert "--resume sess-rs" in opt(made, "--command"), made
    assert_ci_only_prompt(opt(made, "--command"))
    saved = json.loads(state_file.read_text())["workers"]["rs"]
    assert (saved["session"], saved["account"], saved["host"]) == ("sess-rs", "c2", "local"), saved
    assert saved["terminal"] not in (None, "term-rs"), saved
    done = resume(
        rec("rs", "c2", lost="STUCK rs: terminal term-rs went with the Orca restart"),
        git_fixture=UNREAD_ORIGIN_GIT,
    )
    assert done.returncode == 0, done
    closed = [opt(c, "--terminal") for c in calls("orca", ["terminal", "close"])]
    assert len(created()) == 1 and "term-rs" not in closed, closed
    assert_ci_only_prompt(opt(created()[0], "--command"))
    assert "lost" not in json.loads(state_file.read_text())["workers"]["rs"]
    # A close Orca refuses keeps the record and its live terminal: nothing is started.
    state_file.write_text(json.dumps({"workers": {"rs": rec("rs", "c2")}}))
    world(local(term("term-rs", 600, ["❯ "])), close_fails=["term-rs"])
    done = cli("resume", "rs")
    assert done.returncode != 0 and not created(), done
    assert json.loads(state_file.read_text())["workers"]["rs"]["terminal"] == "term-rs"
    busy = term("term-rs", 1, ["✻ Thinking… (12s · esc to interrupt)"])
    done = resume(rec("rs", "c2"), busy)
    assert done.returncode != 0 and not created(), done
    assert not calls("orca", ["terminal", "close"]), calls("orca", ["terminal", "close"])
    done = resume(rec("rs", "x1", agent="codex"), term("term-rs", 600, ident="codex"))
    assert done.returncode != 0 and "launch" in done.stderr and not created(), done
    state_file.unlink()


@scenario
def resume_refuses_a_blocked_account_or_host():
    """`resume` admits the session as a launch would, before anything is closed: a record
    stopped awaiting its restart proposal, an account now disabled, held at its limit or
    with its slot taken by another ticket, and a host under its memory floor are each
    refused with the reason, its terminal left open and nothing started."""
    reset_db()
    ticket_row("rs")
    row, shown = term("term-rs", 600, ["❯ "])

    def refused(workers, host="local", limits=None, **fixture):
        state_file.write_text(json.dumps({"workers": workers, "limits": limits or {}}))
        world([(host, "term-rs", row, shown)], **fixture)
        done = cli("resume", "rs")
        assert done.returncode != 0 and not created(), done
        assert not calls("orca", ["terminal", "close"]), calls("orca", ["terminal", "close"])
        assert json.loads(state_file.read_text())["workers"]["rs"]["terminal"] == "term-rs"
        return done.stderr

    try:
        assert "stopped" in refused({"rs": rec("rs", "c2", stopped=int(NOW))})
        assert "off" in refused({"rs": rec("rs", "c4")})
        later = time.time() + 3600
        assert "usage limit until" in refused({"rs": rec("rs", "c2")}, limits={"c2": later})
        taken = {"rs": rec("rs", "c2"), "ot": rec("ot", "c2")}
        assert "opus 1 of 1" in refused(taken)
        wsl = {"rs": rec("rs", "c3", host="Juzzark-WSL")}
        low = refused(wsl, host="Juzzark-WSL", memory={"Juzzark-WSL": host_memory(0.5)})
        assert "under its floor" in low, low
    finally:
        state_file.unlink(missing_ok=True)



@scenario
def resume_refuses_a_record_replaced_while_its_host_is_read():
    """A record replaced, here on another host, while resume reads the first host's
    memory is refused before anything is closed or started."""
    reset_db()
    ticket_row("rs")
    row, shown = term("term-rs", 600, ["❯ "])
    state_file.write_text(json.dumps({"workers": {"rs": rec("rs", "c2", lost="STUCK rs: gone")}}))
    world([("Juzzark-WSL", "term-rs", row, shown)])
    real = da.memory_refusal

    def replaced(_cfg, _host):
        wsl = rec("rs", "c3", host="Juzzark-WSL", launched_at=NOW)
        state_file.write_text(json.dumps({"workers": {"rs": wsl}}))
        return None, []

    da.memory_refusal = replaced
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            da.main(["resume", "rs"])
        raise AssertionError("resumed the replacement")
    except SystemExit as stop:
        assert "changed" in str(stop.code), stop.code
    finally:
        da.memory_refusal = real
    assert not created() and not calls("orca", ["terminal", "close"]), calls()
    state_file.unlink(missing_ok=True)


@scenario
def run_prints_a_hosts_output_and_exit_code():
    """Live (ron.py): `run` runs a shell command in a worktree on another host through an
    Orca probe terminal, prints its output and exits with its code; here it runs directly."""
    set_fixture()
    wt = tmp / "wt" / "rn"
    wt.mkdir(parents=True, exist_ok=True)
    done = cli("run", "Juzzark-WSL", str(wt), "echo hello; exit 3")
    assert done.returncode == 3 and done.stdout.strip() == "hello", done
    probes = [r for r in calls("orca", ["terminal", "create"]) if opt(r, "--title") == "dev-auto probe"]
    assert len(probes) == 1 and opt(probes[0], "--environment") == "Juzzark-WSL", probes
    here = cli("run", "local", str(wt), "pwd")
    assert here.returncode == 0 and here.stdout.strip() == str(wt), here


@scenario
def run_collects_output_beyond_the_probe_tail():
    set_fixture(probe_cursors=True)
    wt = tmp / "wt" / "long-run"
    wt.mkdir(parents=True, exist_ok=True)
    command = "i=0; while [ \"$i\" -lt 500 ]; do echo \"line $i\"; i=$((i+1)); done; exit 7"
    done = cli("run", "Juzzark-WSL", str(wt), command, "--timeout", "2")
    assert done.returncode == 7 and done.stdout.splitlines() == [
        f"line {i}" for i in range(500)
    ], (done.returncode, done.stdout[:100], done.stdout[-100:])


@scenario
def run_applies_its_deadline_to_a_slow_probe_read():
    set_fixture(probe_read_delay=3)
    began = time.monotonic()
    done = cli("run", "Juzzark-WSL", str(tmp), "echo late", "--timeout", "1")
    assert (done.returncode, done.stdout) == (124, ""), done
    assert time.monotonic() - began < 2.5


@scenario
def screen_prints_a_workers_last_lines():
    """Live (screens.py, scr.py): `screen <ticket>` reads the recorded terminal on the
    record's host and prints its last lines; a ticket with no record is an error."""
    state_file.write_text(json.dumps({"workers": {"sc": rec("sc", "c1", host="Mac2")}}))
    row, shown = term("term-sc", 5, [f"line {i}" for i in range(30)])
    world([("Mac2", "term-sc", row, shown)])
    done = cli("screen", "sc", "--lines", "3")
    assert done.returncode == 0, done
    lines = done.stdout.splitlines()
    assert lines[-3:] == ["line 27", "line 28", "line 29"] and len(lines) == 4, lines
    assert "Mac2" in lines[0] and "term-sc" in lines[0], lines
    reads = calls("orca", ["terminal", "read"])
    assert reads and opt(reads[0], "--environment") == "Mac2", reads
    assert cli("screen", "nope").returncode != 0
    state_file.unlink()


def merge_checkout(name):
    """A checkout with origin/feature/a and a local feature/a one --no-ff merge ahead,
    of task/a/x's single commit, whose SHA is returned."""
    base = tmp / name
    origin, main = base / "origin.git", base / "checkout"
    base.mkdir()
    for argv in ([GIT, "init", "--bare", "-b", "main", str(origin)], [GIT, "init", "-b", "main", str(main)]):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    (main / "README").write_text("base\n")
    git_at(main, "add", "README")
    git_at(main, "commit", "-m", "base")
    git_at(main, "remote", "add", "origin", str(origin))
    git_at(main, "checkout", "-q", "-b", "feature/a")
    git_at(main, "push", "-q", "origin", "feature/a")
    git_at(main, "checkout", "-q", "-b", "task/a/x")
    (main / "x").write_text("x\n")
    git_at(main, "add", "x")
    git_at(main, "commit", "-m", "task")
    sha = subprocess.run(
        [GIT, "-C", str(main), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    git_at(main, "checkout", "-q", "feature/a")
    git_at(main, "merge", "-q", "--no-ff", "-m", "merge task", "task/a/x")
    return main, sha


@scenario
def verify_merges_passes_only_cleared_merges():
    """Live (verify-merges.sh): before a feature push, `verify-merges` passes when every
    commit the branch has over origin is a merge of a SHA someone other than the owner
    cleared with PUSH on a ticket of that feature, and fails on a direct commit, a merge
    of an uncleared SHA, or a fetch from origin that fails."""
    reset_db()
    main, sha = merge_checkout("vm")
    ticket_row("vm-01", notes="Branch: `task/a/x` · Feature branch: `feature/a`")
    set_fixture(git_bin=GIT)
    own = update("vm-01", "finding", f"REVIEW VERDICT: PUSH {sha}")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "FAILED" in done.stdout, (own, done)
    update("vm-01", "finding", f"REVIEW VERDICT: PUSH {sha[:9]}", agent="codex:vm-01-review")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 0 and "VERIFIED" in done.stdout, done
    remote = cli("verify-merges", "feature/a", "--worktree", str(main), "--host", "Juzzark-WSL")
    assert remote.returncode == 0 and remote.stdout == done.stdout, (remote, done)
    (main / "y").write_text("y\n")
    git_at(main, "add", "y")
    git_at(main, "commit", "-m", "direct")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "FAILED" in done.stdout, done
    git_at(main, "reset", "-q", "--hard", "HEAD~1")
    # An origin that cannot be fetched proves nothing about the remote branch.
    git_at(main, "remote", "set-url", "origin", str(tmp / "vm" / "gone.git"))
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "VERIFIED" not in done.stdout, done
    git_at(main, "remote", "set-url", "origin", str(tmp / "vm" / "origin.git"))
    git_at(main, "checkout", "-q", "task/a/x")
    (main / "z").write_text("z\n")
    git_at(main, "add", "z")
    git_at(main, "commit", "-m", "uncleared")
    git_at(main, "checkout", "-q", "feature/a")
    git_at(main, "merge", "-q", "--no-ff", "-m", "merge again", "task/a/x")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "FAILED" in done.stdout, done


@scenario
def verify_merges_takes_only_a_standing_exact_push():
    """A PUSH followed by a FIX on that SHA, a superseded PUSH, an indented PUSH or a line
    reading `REVIEW VERDICT: PUSHED <sha>` clears nothing; a later PUSH clears it again, a
    FIX naming no SHA withdraws nothing, and a FIX on one ticket leaves another ticket's
    PUSH for that SHA standing. Every case is checked; the failing ones are listed."""
    reset_db()
    main, sha = merge_checkout("vs")
    set_fixture(git_bin=GIT)
    notes = "Branch: `task/a/x` · Feature branch: `feature/a`"
    ticket_row("vs-01", notes=notes)
    bad = []

    def check(want, why):
        done = cli("verify-merges", "feature/a", "--worktree", str(main))
        ok = done.returncode == 0 and "VERIFIED" in done.stdout
        assert ok or (done.returncode == 1 and "FAILED" in done.stdout), done
        if ok != want:
            bad.append(why)

    def say(text, tid="vs-01"):
        return update(tid, "finding", text, agent=f"codex:{tid}-review")

    say(f"REVIEW VERDICT: PUSH {sha}")
    check(True, "a PUSH clears")
    say(f"REVIEW VERDICT: FIX {sha[:9]} missed a check")
    check(False, "a later FIX withdraws the PUSH")
    say(f"REVIEW VERDICT: PUSH {sha}")
    check(True, "a PUSH after the FIX clears it again")
    say("REVIEW VERDICT: FIX one finding")
    check(True, "a FIX naming no SHA withdraws nothing")
    reset_db()
    ticket_row("vs-01", notes=notes)
    supersede(say(f"REVIEW VERDICT: PUSH {sha}"), say("withdrawn"))
    check(False, "a superseded PUSH")
    say(f"REVIEW VERDICT: PUSHED {sha}")
    check(False, "PUSHED is no verdict")
    say(f"  REVIEW VERDICT: PUSH {sha}")
    check(False, "an indented PUSH is no verdict")
    ticket_row("vs-02", notes=notes)
    say(f"REVIEW VERDICT: PUSH {sha}", "vs-02")
    say(f"REVIEW VERDICT: FIX {sha}")
    check(True, "another ticket's PUSH stands")
    assert not bad, bad


@scenario
def verify_merges_refuses_an_ambiguous_short_sha():
    """A cleared short SHA that names two commits in the checkout clears neither; the full
    SHA of one clears that one. A branch named like a cleared SHA clears nothing: the SHA
    is read as an object id."""
    reset_db()
    main, sha = merge_checkout("va")
    set_fixture(git_bin=GIT)
    git_at(main, "reset", "-q", "--hard", "origin/feature/a")
    _, twin = sha_twins(main, head_of(main, "HEAD"))
    git_at(main, "merge", "-q", "--no-ff", "-m", "merge twin", twin)
    ticket_row("va-01", notes="Branch: `task/a/x` · Feature branch: `feature/a`")
    update("va-01", "finding", f"REVIEW VERDICT: PUSH {twin[:7]}", agent="codex:va-01-review")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "FAILED" in done.stdout, done
    update("va-01", "finding", f"REVIEW VERDICT: PUSH {twin}", agent="codex:va-01-review")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 0 and "VERIFIED" in done.stdout, done
    # A branch named like the cleared SHA of the task, on another commit, merged instead.
    reset_db()
    ticket_row("va-01", notes="Branch: `task/a/x` · Feature branch: `feature/a`")
    update("va-01", "finding", f"REVIEW VERDICT: PUSH {sha[:9]}", agent="codex:va-01-review")
    git_at(main, "reset", "-q", "--hard", "origin/feature/a")
    git_at(main, "branch", sha[:9], twin)
    git_at(main, "merge", "-q", "--no-ff", "-m", "merge shadow", sha[:9])
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "FAILED" in done.stdout, done


def verify_rerun(main, merge):
    """Reset feature/a to its origin tip, give it `merge` as its one commit and verify:
    True when VERIFIED."""
    git_at(main, "checkout", "-q", "feature/a")
    git_at(main, "reset", "-q", "--hard", "origin/feature/a")
    merge()
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    ok = done.returncode == 0 and "VERIFIED" in done.stdout
    assert ok or (done.returncode == 1 and "FAILED" in done.stdout), done
    return ok


@scenario
def verify_merges_refuses_an_extra_parent_or_a_whitespace_twin():
    """A merge with a third parent, even one adding nothing, a merge whose result is not
    the clean merge of its two parents though its patch id matches, and a merge that drops
    the first parent's work to equal the cleared tree are each refused; the clean merge
    over unrelated feature work passes. Every case is checked; the failing ones are
    listed."""
    reset_db()
    main, sha = merge_checkout("vo")
    set_fixture(git_bin=GIT)
    ticket_row("vo-01", notes="Branch: `task/a/x` · Feature branch: `feature/a`")
    update("vo-01", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:vo-01-review")
    git_at(main, "checkout", "-q", "-b", "extra", "main")
    git_at(main, "commit", "-q", "--allow-empty", "-m", "uncleared")
    bad = []

    def octopus():
        git_at(main, "merge", "-q", "--no-ff", "-m", "octopus", "task/a/x", "extra")

    if verify_rerun(main, octopus):
        bad.append("an octopus merge")
    # Feature work origin already has, then a task that indents b() under the if.
    git_at(main, "checkout", "-q", "feature/a")
    git_at(main, "reset", "-q", "--hard", "origin/feature/a")
    (main / "f.py").write_text("if x:\n    a()\nb()\n")
    git_at(main, "add", "f.py")
    git_at(main, "commit", "-q", "-m", "f")
    git_at(main, "checkout", "-q", "-b", "task/a/y")
    (main / "f.py").write_text("if x:\n    a()\n    b()\n")
    git_at(main, "commit", "-q", "-am", "indent b")
    task = head_of(main, "HEAD")
    git_at(main, "checkout", "-q", "feature/a")
    (main / "g").write_text("g\n")
    git_at(main, "add", "g")
    git_at(main, "commit", "-q", "-m", "unrelated")
    git_at(main, "push", "-q", "origin", "feature/a")
    update("vo-01", "finding", f"REVIEW VERDICT: PUSH {task}", agent="codex:vo-01-review")

    def clean():
        git_at(main, "merge", "-q", "--no-ff", "-m", "merge y", "task/a/y")

    def twin():
        clean()
        (main / "f.py").write_text("if x:\n    a()\n        b()\n")
        git_at(main, "commit", "-q", "--amend", "-am", "merge y")

    def dropped():
        clean()
        git_at(main, "rm", "-q", "g")
        git_at(main, "commit", "-q", "--amend", "-m", "merge y")

    if not verify_rerun(main, clean):
        bad.append("the clean merge")
    if verify_rerun(main, twin):
        bad.append("a whitespace twin")
    if verify_rerun(main, dropped):
        bad.append("a merge dropping the first parent's file")
    assert not bad, bad


@scenario
def verify_merges_reads_a_new_feature_branch_from_its_base():
    """A feature branch origin has never had is verified from its repo's configured base,
    read from what the fetch returned even when the fetch refspec leaves no origin/<base>;
    without a base set, it fails."""
    reset_db()
    main, sha = merge_checkout("vn")
    set_fixture(git_bin=GIT)
    git_at(main, "push", "-q", "origin", "main")
    git_at(main, "push", "-q", "origin", "--delete", "feature/a")
    ticket_row("vn-01", notes="Branch: `task/a/x` · Feature branch: `feature/a`")
    update("vn-01", "finding", f"REVIEW VERDICT: PUSH {sha}", agent="codex:vn-01-review")
    done = cli("verify-merges", "feature/a", "--worktree", str(main))
    assert done.returncode == 1 and "VERIFIED" not in done.stdout, done
    based = tmp / "vn-accounts.toml"
    based.write_text(accounts.read_text() + '[repos."vn/origin"]\nbase = "main"\n')
    git_at(main, "config", "remote.origin.fetch", "+refs/heads/none:refs/remotes/origin/none")
    git_at(main, "update-ref", "-d", "refs/remotes/origin/main")
    done = cli("verify-merges", "feature/a", "--worktree", str(main), DEV_AUTO_ACCOUNTS=str(based))
    assert done.returncode == 0 and "VERIFIED" in done.stdout, done
    (main / "y").write_text("y\n")
    git_at(main, "add", "y")
    git_at(main, "commit", "-q", "-m", "direct")
    done = cli("verify-merges", "feature/a", "--worktree", str(main), DEV_AUTO_ACCOUNTS=str(based))
    assert done.returncode == 1 and "FAILED" in done.stdout, done


# limit_handoff: a Claude account near its 5-hour limit hands off all but one worker.
HANDOFF_ON = {**cfg, "limit_handoff": True}
ASK = "/goal Write the handoff of Concord ticket {} with the handoff skill, then end your turn."


def tick_on(st, now=NOW):
    da.tick(HANDOFF_ON, root, st, da.read_tickets(root), now)


def handoff_world(*items, percent=87, reset=NOW + 3 * 3600):
    """The max account (c3) read at `percent` of its 5-hour window, and these terminals."""
    clear_usage()
    claude_usage("c3", percent, reset)
    world(local(*items))


def tier_ticket(tid, tier):
    ticket_row(tid, notes=f"Outcome: x\nTier: {tier}")


def asks():
    return [(h, t) for h, t, _ in sends() if t.startswith("/goal Write the handoff")]


def to_orchestrator():
    return [t for h, t, _ in sends() if h == "term-orch"]


@scenario
def handoff_off_leaves_the_tick_as_today():
    """limit_handoff off: an account past handoff_percent asks nobody and nothing is stored."""
    reset_db()
    for tid in ("oa", "ob"):
        tier_ticket(tid, "medium")
    st = fresh_state(
        oa=rec("oa", "c3", model=SONNET), ob=rec("ob", "c3", model=SONNET, launched_at=NOW - 3600)
    )
    handoff_world(term("term-oa", 120), term("term-ob", 120))
    tick(st)
    assert sends() == [] and created() == [] and tab_closes() == [], sends()
    assert "proposals" not in st and not any("handoff_ask" in r for r in st["workers"].values())
    clear_usage()


@scenario
def handoff_asks_every_active_claude_worker_but_one_once():
    """At the trigger every active Claude worker on the account but the kept one, the
    highest tier, is asked once; a waiting, a silent, a Codex worker and one on another
    account are not."""
    reset_db()
    for tid, tier in (("ha", "hard"), ("hm", "medium"), ("he", "easy"), ("hw", "medium"), ("hs", "medium"), ("hc", "hard")):
        tier_ticket(tid, tier)
    ticket_row("hx", agent="codex:hx", notes="Tier: easy")
    update("hw", "blocker", "Waiting on: review of 1a2b3c4")
    st = fresh_state(
        ha=rec("ha", "c3", model=SONNET, launched_at=NOW - 3600),
        hm=rec("hm", "c3", model=SONNET),
        he=rec("he", "c3", model=SONNET),
        hw=rec("hw", "c3", model=SONNET),
        hs=rec("hs", "c3", model=SONNET),
        hc=rec("hc", "c1"),
        hx=rec("hx", "x1", agent="codex", model="gpt-6.1-sol"),
    )
    items = [term(f"term-{t}", 120) for t in ("ha", "hm", "he", "hw", "hc")]
    items += [term("term-hs", 61 * 60), term("term-hx", 120, ident="codex")]
    handoff_world(*items)
    tick_on(st)
    assert sorted(h for h, _ in asks()) == ["term-he", "term-hm"], sends()
    ((_, text),) = [a for a in asks() if a[0] == "term-hm"]
    assert text.startswith(ASK.format("hm")) and "at 87% of its 5-hour limit" in text, text
    assert st["workers"]["hm"]["handoff_ask"]["percent"] == 87
    handoff_world(*items)
    tick_on(st, NOW + 60)
    assert asks() == [], "asked once per window"
    clear_usage()


@scenario
def usage_handoff_keeps_a_worker_below_the_context_limit():
    reset_db()
    clear_transcripts()
    tier_ticket("ctx", "hard")
    tier_ticket("keeper", "medium")
    context_worker = rec("ctx", "c3", model=SONNET)
    st = fresh_state(ctx=context_worker, keeper=rec("keeper", "c3", model=SONNET))
    context_transcript(homes["c3"], "-wt-ctx", context_worker["session"], 300_000)
    handoff_world(term("term-ctx", 120), term("term-keeper", 120))
    tick_on(st)
    assert [handle for handle, _ in asks()] == ["term-ctx"], asks()
    assert "context is at 300k" in asks()[0][1], asks()
    clear_transcripts()
    clear_usage()


@scenario
def unshown_handoff_ask_is_rechecked_with_its_own_text():
    """da-4-10: a handoff ask Orca accepts but the screen does not show is rechecked like a
    wake, though it sets no goal: typed once more with its own text. Missed again, the
    next tick raises one STUCK naming it, and the ask no longer holds the worker."""
    reset_db()
    ticket_row("ha")
    worker = rec("ha", "c1")
    st = fresh_state(ha=worker)
    ask = (
        "/goal Write the handoff of Concord ticket ha with the handoff skill, then end your "
        "turn. dev-auto: max is at 91% of its 5-hour limit."
    )
    key = f"ha|handoff-ask|{int(worker['launched_at'])}|{int(NOW + 3600)}"
    (tmp / "delivered.log").unlink(missing_ok=True)
    worker["handoff_ask"] = {"at": NOW, "window": NOW + 3600, "percent": 91}
    world(local((term("term-ha", 5)[0], unshown())))
    with recheck_wait() as clock:
        assert da.emit(st, key, ask, worker=worker, note_on="ha", rearm=False)
    assert [s for s in sends() if s[0] == "term-ha" and s[2]] == [("term-ha", ask, True)] * 2, sends()
    assert clock.slept == [10] and "wake" not in worker, (clock.slept, worker)
    assert delivered_rows() == [["ha", ask, "missed"]], delivered_rows()
    with settings(limit_handoff=True):
        for later in (60, 120):
            world(local((term("term-ha", 5)[0], unshown())))
            tick(st, NOW + later)
            assert [s for s in sends() if s[0] == "term-ha"] == [], (later, sends())
    stucks = [l for l in event_lines() if l.split(" ", 2)[2].startswith("STUCK ha") and "handoff-ask" in l]
    assert len(stucks) == 1 and "never showed" in stucks[0], event_lines()
    assert "handoff_ask" not in worker and "missed" not in worker, worker



@scenario
def unproved_handoff_ask_starts_no_timeout():
    """da-4-10: an ask Orca accepted that the screen has not shown yet, or never showed,
    raises no "asked … no handoff" STUCK on a later tick while the worker sits at a
    dialog that keeps supervise from rechecking it."""
    clear_usage()
    minutes = cfg["handoff_wait_minutes"]
    for tid, pending in (("hp", "unshown"), ("hq", "missed")):
        reset_db()
        ticket_row(tid)
        worker = rec(tid, "c1")
        st = fresh_state(**{tid: worker})
        key = f"{tid}|handoff-ask|{int(worker['launched_at'])}|{int(NOW + 3600)}"
        worker["handoff_ask"] = {"at": NOW, "window": NOW + 3600, "percent": 91}
        if pending == "unshown":
            entry = {"key": key, "text": "ask", "typed": "ask", "seen": 0, "at": NOW, "update": 0, "output": None}
            worker["unshown"] = [entry]
        else:
            worker["missed"] = [key]
        st["sent"][key] = int(NOW)
        with settings(limit_handoff=True):
            world(local(term(f"term-{tid}", 600, ["Enter to confirm · Esc to cancel"])))
            tick(st, NOW + minutes * 60 + 60)
        late = [l for l in event_lines() if f"STUCK {tid}" in l and "no handoff" in l]
        assert late == [], (tid, event_lines())

@scenario
def handoff_keeps_the_named_worker_else_the_earliest_of_its_tier():
    """The worker `keep` names stays; with none named, the earliest launched of the
    highest tier."""
    reset_db()
    for tid in ("k1", "k2"):
        tier_ticket(tid, "medium")

    def workers():
        return {"k1": rec("k1", "c3", model=SONNET), "k2": rec("k2", "c3", model=SONNET, launched_at=NOW - 3600)}

    handoff_world(term("term-k1", 120), term("term-k2", 120))
    tick_on(fresh_state(**workers()))
    assert [h for h, _ in asks()] == ["term-k2"], sends()
    state_file.write_text(json.dumps({"workers": workers()}))
    done = cli("keep", "k2")
    assert done.returncode == 0 and json.loads(state_file.read_text())["keep"] == {"c3": "k2"}, done
    refused = cli("keep", "nobody")
    assert refused.returncode and "no Claude worker" in refused.stderr, refused
    state_file.unlink()
    st = fresh_state(**workers())
    st["keep"] = {"c3": "k2"}
    handoff_world(term("term-k1", 120), term("term-k2", 120))
    tick_on(st)
    assert [h for h, _ in asks()] == ["term-k1"], sends()
    clear_usage()


@scenario
def handoff_needs_handoff_percent_and_an_hour_left():
    """No ask below handoff_percent, nor with an hour or less to the reset."""
    reset_db()
    for tid in ("n1", "n2"):
        tier_ticket(tid, "medium")

    def asked_at(percent, reset):
        st = fresh_state(n1=rec("n1", "c3", model=SONNET), n2=rec("n2", "c3", model=SONNET, launched_at=NOW - 3600))
        handoff_world(term("term-n1", 120), term("term-n2", 120), percent=percent, reset=reset)
        tick_on(st)
        return [h for h, _ in asks()]

    assert asked_at(84, NOW + 3 * 3600) == []
    assert asked_at(87, NOW + 3600) == [], "an hour or less to the reset"
    assert asked_at(85, NOW + 3601) == ["term-n2"]
    clear_usage()


@scenario
def handoff_slot_refuses_an_account_at_its_handoff_trigger():
    """Audit #8612: with limit_handoff on, slot gives no new worker to an account at its
    handoff trigger and names it; launch_refusal refuses it as before; off, slot takes it."""
    reset_db()
    handoff_world(percent=87, reset=NOW + 3 * 3600)
    refusal = da.launch_refusal(HANDOFF_ON, fresh_state(), "nw", SONNET, da.account_row(HANDOFF_ON, "c3"), NOW)
    why = []
    row = da.slot(HANDOFF_ON, {}, "claude", SONNET, memory=False, now=NOW, account="max", why=why)
    assert refusal and "handoff_percent" in refusal, refusal
    assert row is None and any(w.startswith("max: ") and "handoff_percent" in w for w in why), why
    assert da.slot(cfg, {}, "claude", SONNET, memory=False, now=NOW, account="max")["label"] == "max"
    clear_usage()


@scenario
def handoff_idle_reviewer_after_its_verdict_is_not_asked():
    """Audit #8613: a Claude reviewer whose verdict answers every review request has
    nothing to review and is not asked to hand off, even after a progress note; once a
    newer REVIEW READY lands it is reviewing again and is asked."""
    reset_db()
    ticket_row("hk", notes="Outcome: x\nTier: hard")
    ticket_row("ui1", notes="Outcome: x\nTier: ui\nReviewer: claude")
    ticket_row("ui1-review", agent="claude:ui1-review")
    update("ui1", "progress", "REVIEW READY 1a2b3c4")
    update("ui1", "blocker", "Waiting on: review of 1a2b3c4")
    update("ui1-review", "blocker", "Waiting on: ticket:ui1 to post REVIEW READY", agent="claude:ui1-review")
    fix = update("ui1", "finding", "REVIEW VERDICT: FIX 1a2b3c4 one finding", "claude:ui1-review")
    update("ui1-review", "progress", "Posted FIX; waiting for the next REVIEW READY", agent="claude:ui1-review")
    update("ui1", "progress", "fixing the finding")
    st = fresh_state(
        hk=rec("hk", "c3", model=SONNET, launched_at=NOW - 7200),
        **{"ui1-review": rec("ui1-review", "c3", model=SONNET, launched_at=NOW - 3600)},
    )
    st["cleared_floor"] = fix
    handoff_world(term("term-hk", 30), term("term-ui1-review", 600))
    tick_on(st)
    assert asks() == [], asks()
    update("ui1", "progress", "REVIEW READY 5d6e7f8")
    handoff_world(term("term-hk", 30), term("term-ui1-review", 600))
    tick_on(st, NOW + 60)
    assert [h for h, _ in asks()] == ["term-ui1-review"], sends()
    clear_usage()


@scenario
def handoff_closes_a_worker_only_once_its_handoff_is_on_the_ticket():
    """An asked worker is closed, its worktree kept, only after its Handoff: update is on
    the ticket and its terminal has settled; a stopped worker is left alone after."""
    reset_db()
    for tid in ("ca", "cb"):
        tier_ticket(tid, "medium")
    st = fresh_state(ca=rec("ca", "c3", model=SONNET, launched_at=NOW - 7260), cb=rec("cb", "c3", model=SONNET))
    handoff_world(term("term-ca", 120), term("term-cb", 120))
    tick_on(st)
    assert [h for h, _ in asks()] == ["term-cb"], sends()
    handoff_world(term("term-ca", 120), term("term-cb", -90))
    tick_on(st, NOW + 120)
    assert tab_closes() == [] and "stopped" not in st["workers"]["cb"], "no handoff yet"
    hid = update("cb", "progress", "Handoff: done X, next Y", at=NOW + 200)
    handoff_world(term("term-ca", 120), term("term-cb", -270))
    tick_on(st, NOW + 300)
    assert tab_closes() == [], "a session still writing is closed later"
    handoff_world(term("term-ca", 120), term("term-cb", -270))
    tick_on(st, NOW + 400)
    assert tab_closes() == [("term-cb", True)], tab_closes()
    assert calls("orca", ["worktree", "rm"]) == [] and created() == []
    stopped = st["workers"]["cb"]["stopped"]
    assert (stopped["why"], stopped["handoff"]) == ("usage handoff at 87%", hid), stopped
    assert st["proposals"]["cb"]["why"] == "usage handoff at 87%"
    handoff_world(term("term-ca", 120))
    tick_on(st, NOW + 500)
    assert created() == [] and [s for s in sends() if s[0] != "term-orch"] == [], sends()
    assert "lost" not in st["workers"]["cb"]
    clear_usage()


@scenario
def handoff_late_is_one_event_and_nothing_is_typed_again():
    """An asked worker with no handoff after handoff_wait_minutes is one STUCK; it is
    never typed into again that window, not even a silence nudge."""
    reset_db()
    for tid in ("la", "lb"):
        tier_ticket(tid, "medium")
    st = fresh_state(la=rec("la", "c3", model=SONNET, launched_at=NOW - 7260), lb=rec("lb", "c3", model=SONNET))
    handoff_world(term("term-la", 120), term("term-lb", 120))
    tick_on(st)
    assert [h for h, _ in asks()] == ["term-lb"], sends()
    handoff_world(term("term-la", -800), term("term-lb", -800))
    tick_on(st, NOW + 14 * 60)
    assert sends() == [], sends()
    handoff_world(term("term-la", -800), term("term-lb", -800))
    tick_on(st, NOW + 15 * 60)
    late = (
        "STUCK lb claude:lb: asked 15 minutes ago to write its handoff and stop, and no "
        "handoff is on its ticket. It is left running. Nothing is typed into it again"
    )
    assert sends() == [("term-orch", late, True)], sends()
    world(local(term("term-la", 120), term("term-lb", 120)))
    tick_on(st, NOW + 40 * 60)
    assert [s for s in sends() if s[0] == "term-lb" or late in s[1]] == [], sends()
    assert tab_closes() == []
    clear_usage()


@scenario
def handoff_after_the_context_limit_ends_in_a_proposal():
    """A Handoff: update this launch wrote, with no usage trigger, closes the session with a
    context-handoff proposal; a handoff from before the launch closes nothing."""
    reset_db()
    clear_usage()
    for tid in ("cx", "co"):
        ticket_row(tid, notes="Tier: hard")
    update("co", "progress", "Handoff: from an earlier session", at=NOW - 7260)
    hid = update("cx", "progress", "Handoff: context at 300k", at=NOW - 300)
    st = fresh_state(cx=rec("cx", "c1"), co=rec("co", "c2"))
    world(local(term("term-cx", 120), term("term-co", 120)))
    tick_on(st)
    assert tab_closes() == [("term-cx", True)] and created() == [], tab_closes()
    stopped = st["workers"]["cx"]["stopped"]
    assert (stopped["why"], stopped["handoff"]) == ("context handoff", hid), stopped
    assert "stopped" not in st["workers"]["co"] and list(st["proposals"]) == ["cx"]


@scenario
def measured_context_handoff_ends_in_a_proposal():
    """da-docs-4: with limit_handoff on, a worker whose transcript reads 310k and whose
    handoff is on its ticket is stopped with a context-handoff proposal like any other
    handoff; the tick starts no fresh session for it."""
    reset_db()
    clear_usage()
    clear_transcripts()
    ticket_row("cm", notes="Tier: hard")
    hid = update("cm", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(cm=rec("cm", "c1"))
    context_transcript(homes["c1"], "-wt-cm", "sess-cm", 310_000)
    world(local(term("term-cm", 120)), git=git)
    tick_on(st)
    assert created() == [], created()
    stopped = st["workers"]["cm"].get("stopped") or {}
    assert (stopped.get("why"), stopped.get("handoff")) == ("context handoff", hid), st["workers"]["cm"]
    assert list(st.get("proposals", {})) == ["cm"], st.get("proposals")
    clear_transcripts()

@scenario
def handoff_limit_screen_ends_in_a_proposal():
    """With limit_handoff on a Claude limit screen is a stop with a proposal: no move, no
    resume, no WAITING, and the account's hold is still recorded. Codex still moves."""
    reset_db()
    clear_usage()
    ticket_row("ls")
    ticket_row("lx", agent="codex:lx")
    st = fresh_state(ls=rec("ls", "c1"), lx=rec("lx", "x1", agent="codex", model="gpt-6.1-sol"))
    codex_limit = ["■ You've hit your usage limit. Try again later."]
    world(local(
        term("term-ls", 120, ["You've hit your limit · resets 5pm"]),
        term("term-lx", 120, codex_limit, ident="codex"),
    ))
    tick_on(st)
    made = created()
    assert len(made) == 1 and " codex " in opt(made[0], "--command"), made
    assert ("term-ls", True) in tab_closes(), tab_closes()
    stopped = st["workers"]["ls"]["stopped"]
    assert (stopped["why"], stopped["handoff"]) == ("usage limit reached", None), stopped
    assert st["limits"].get("c1"), "the account's hold is still recorded"
    assert not any(t.startswith("WAITING") for t in to_orchestrator()), sends()
    assert st["proposals"]["ls"]["pick"] == "pro2", st["proposals"]["ls"]


@scenario
def handoff_limit_after_an_ask_proposes_the_limit_reset():
    """da-4-9: a worker asked for its usage handoff hits a longer limit before it stops.
    Its proposal and the RESET it waits for use the account's held limit, not the earlier
    5-hour ask window."""
    reset_db()
    clear_usage()
    ticket_row("lk")
    weekly = NOW + 4 * 86400
    ask = {"at": NOW - 600, "window": NOW + 3600, "percent": 87}
    st = fresh_state(lk=rec("lk", "c1", handoff_ask=ask))
    st["limits"]["c1"] = weekly
    world(local(term("term-lk", 120, ["You've hit your limit · resets 5pm"])))
    tick_on(st)
    assert st["workers"]["lk"]["stopped"]["why"] == "usage limit reached", st["workers"]["lk"]
    assert st["proposals"]["lk"]["reset"] == weekly, st["proposals"]["lk"]

@scenario
def handoff_proposal_names_the_command_and_the_accounts():
    """The proposal: reason, handoff id, the fresh launch command, every Claude account's
    use and free slots, the pick and why; written to the supervisor folder. A stopped
    record holds no slot."""
    reset_db()
    clear_usage()
    ticket_row("pq", notes="Tier: hard")
    hid = update("pq", "progress", "Handoff: next", at=NOW - 300)
    claude_usage("c1", 50, NOW + 3 * 3600, week=20)
    claude_usage("c2", 87, NOW + 2 * 3600, week=30)
    st = fresh_state(pq=rec("pq", "c1"))
    world(local(term("term-pq", 120)))
    tick_on(st)
    p = st.get("proposals", {}).get("pq")
    assert p, "a proposal is made"
    assert (p["ticket"], p["why"], p["handoff"], p["account"]) == ("pq", "context handoff", hid, "c1"), p
    worktree = st["workers"]["pq"]["worktree"]
    command = shlex.join([
        "python3", str(HERE / "dev_auto.py"), "launch", "pq", "--agent", "claude",
        "--model", OPUS, "--effort", "high", "--host", "local", "--worktree", worktree,
        "--account", "pro1",
    ])
    assert p["command"] == command, p["command"]
    rows = {a["label"]: a for a in p["accounts"]}
    assert sorted(rows) == ["max", "pro1", "pro2"], "only accounts on its host"
    assert (rows["pro1"]["five"], rows["pro1"]["weekly"], rows["pro1"]["reset"], rows["pro1"]["free"]) == (50, 20, NOW + 3 * 3600, 1), rows["pro1"]
    assert (rows["pro2"]["five"], rows["max"]["free"], rows["max"]["five"]) == (87, 2, None), rows
    assert p["pick"] == "pro1" and "pro1" in p["pick_why"], p
    written = (tmp / "proposals.md").read_text()
    assert command in written and "pro2" in written and "context handoff" in written, written
    stopped = {"why": "context handoff", "at": NOW, "handoff": None}
    assert pick({"a": rec("a", "c1", stopped=stopped)}) == "pro1"
    clear_usage()


@scenario
def proposals_file_is_replaced_whole():
    """proposals.md is written beside itself and moved into place: a write cut short by a
    full disk leaves the earlier list whole for a reader."""
    st = fresh_state()
    path = da.proposals_file(st)
    before = "# Restart proposals\n\n## kept\n\n- why: context handoff\n"
    path.write_text(before)
    real = Path.write_text

    def cut(self, data, *args, **kwargs):
        real(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError(28, "No space left on device")

    Path.write_text = cut
    try:
        da.write_proposals(st)
    except OSError:
        pass
    finally:
        Path.write_text = real
    try:
        assert path.read_text() == before, path.read_text()
    finally:
        path.unlink()
        path.with_suffix(".tmp").unlink(missing_ok=True)


@scenario
def handoff_proposals_accept_change_and_drop():
    """One command accepts one, several or all proposals through launch, or drops one; a
    proposal that no longer fits says why; each end is logged in delivered.log."""
    reset_db()
    clear_usage()
    for tid in ("pa", "pb", "pc", "pd"):
        ticket_row(tid, notes="Tier: hard")
        update(tid, "progress", "Handoff: next steps", at=NOW - 60)
    claude_usage("c3", 87, NOW + 3 * 3600)
    st = fresh_state(
        pa=rec("pa", "c1", model=SONNET), pb=rec("pb", "c1", model=SONNET),
        pc=rec("pc", "c3", model=SONNET), pd=rec("pd", "c1", model=SONNET),
    )
    st.path = state_file
    world(local(*(term(f"term-{t}", 120) for t in ("pa", "pb", "pc", "pd"))))
    tick_on(st)
    st.save()
    assert sorted(st.get("proposals", {})) == ["pa", "pb", "pc", "pd"], st.get("proposals")
    log = root / "supervisor" / "delivered.log"
    log.unlink(missing_ok=True)
    world(local())
    listed = cli("proposals")
    assert listed.returncode == 0 and "launch pa" in listed.stdout, listed
    refused = cli("proposals", "accept", "pc", "--account", "max")
    assert refused.returncode and "pc: not launched: max:" in refused.stdout, refused
    assert "handoff_percent" in refused.stdout and created() == [], refused
    assert "pc" in json.loads(state_file.read_text())["proposals"]
    assert cli("proposals", "drop", "pc").returncode == 0
    changed = cli("proposals", "accept", "pb", "--account", "pro2")
    assert changed.returncode == 0, changed
    (new,) = created()
    command = opt(new, "--command")
    assert "--session-id" in command and "--resume" not in command, command
    assert f"CLAUDE_CONFIG_DIR={shlex.quote(str(homes['c2']))} " in command, command
    world(local())
    done = cli("proposals", "accept", "--all")
    assert done.returncode == 0 and len(created()) == 2, done
    saved = json.loads(state_file.read_text())
    assert saved["proposals"] == {} and not any("stopped" in r for r in saved["workers"].values() if r["ticket"] != "pc")
    ends = [line.split("\t")[1:3] for line in log.read_text().splitlines()]
    assert sorted((t, e.split(":")[0]) for t, e in ends) == [
        ("pa", "PROPOSAL accepted"), ("pb", "PROPOSAL changed"),
        ("pc", "PROPOSAL dropped"), ("pd", "PROPOSAL accepted"),
    ], ends
    state_file.unlink()
    clear_usage()


@scenario
def handoff_proposals_tell_the_orchestrator_twice():
    """One event when proposals appear and one at the account's reset listing every
    proposal that waits; neither repeats."""
    reset_db()
    for tid in ("ta", "tb"):
        tier_ticket(tid, "medium")
    st = fresh_state(ta=rec("ta", "c3", model=SONNET, launched_at=NOW - 7260), tb=rec("tb", "c3", model=SONNET))
    handoff_world(term("term-ta", 120), term("term-tb", 120))
    tick_on(st)
    update("tb", "progress", "Handoff: x", at=NOW + 10)
    handoff_world(term("term-ta", 120), term("term-tb", 120))
    tick_on(st, NOW + 120)
    told = to_orchestrator()
    assert len(told) == 1 and told[0].startswith("PROPOSALS: 1 restart proposal waits in "), told
    assert "tb" in told[0]
    handoff_world(term("term-ta", 120))
    tick_on(st, NOW + 180)
    assert to_orchestrator() == [], sends()
    world(local(term("term-ta", 120)))
    tick_on(st, NOW + 3 * 3600 + 1)
    told = [t for t in to_orchestrator() if not t.startswith("STUCK")]
    assert len(told) == 1 and told[0].startswith("RESET max: ") and "tb" in told[0], told
    world(local(term("term-ta", 120)))
    tick_on(st, NOW + 3 * 3600 + 61)
    assert [t for t in to_orchestrator() if t.startswith(("RESET", "PROPOSALS"))] == [], sends()
    clear_usage()


@scenario
def handoff_stopped_workers_are_no_restart_and_an_exit_is_no_relaunch():
    """A session that exits right after its handoff is stopped, not relaunched; stopped
    records, whose terminals are closed, never read as an Orca restart."""
    reset_db()
    clear_usage()
    for tid in ("e1", "e2"):
        ticket_row(tid, notes="Tier: hard")
        update(tid, "progress", "Handoff: next", at=NOW - 30)
    st = fresh_state(e1=rec("e1", "c1"), e2=rec("e2", "c2"))
    world(local(term("term-e1", 5, status="exited"), term("term-e2", 120)))
    tick_on(st)
    assert created() == [], "no relaunch"
    assert all("stopped" in st["workers"][t] for t in ("e1", "e2")), st["workers"]
    world(local())
    tick_on(st, NOW + 60)
    assert not any("lost" in st["workers"][t] for t in ("e1", "e2")), st["workers"]
    assert [t for t in to_orchestrator() if "Orca restarted" in t] == [], sends()


@scenario
def handoff_later_wait_keeps_the_worker():
    """Review #7954: a Waiting on note after this launch's handoff keeps the worker; it is
    not closed and no proposal is made."""
    reset_db()
    clear_usage()
    ticket_row("wk", notes="Tier: hard")
    update("wk", "progress", "Handoff: checkpoint", at=NOW - 300)
    update("wk", "blocker", "Waiting on: operator decision", at=NOW - 200)
    st = fresh_state(wk=rec("wk", "c1"))
    world(local(term("term-wk", 120)))
    tick_on(st)
    assert tab_closes() == [] and "stopped" not in st["workers"]["wk"], tab_closes()
    assert not st.get("proposals"), st.get("proposals")


@scenario
def handoff_later_wait_cancels_a_refused_close():
    """Review #7990: a handoff whose close Orca refused, followed by the owner's Waiting on
    note, is not closed on the retry while its terminal is still listed."""
    reset_db()
    clear_usage()
    ticket_row("rt", notes="Tier: hard")
    update("rt", "progress", "Handoff: checkpoint", at=NOW - 300)
    st = fresh_state(rt=rec("rt", "c1"))
    world(local(term("term-rt", 120)), close_fails=["term-rt"])
    tick_on(st)
    assert "stopping" in st["workers"]["rt"], st["workers"]["rt"]
    update("rt", "blocker", "Waiting on: operator decision", at=NOW + 10)
    world(local(term("term-rt", 120)))
    tick_on(st, NOW + 60)
    worker = st["workers"]["rt"]
    assert tab_closes() == [] and "stopped" not in worker and "stopping" not in worker, (
        tab_closes(), worker,
    )
    assert not st.get("proposals"), st.get("proposals")


@scenario
def superseded_handoff_stops_nothing():
    """da-4-8: a worker that superseded its Handoff: update with progress naming unfinished
    work is not stopped once quiet, and a refused close of that handoff is not retried."""
    for pending in (False, True):
        reset_db()
        clear_usage()
        ticket_row("sv", notes="Tier: hard")
        hid = update("sv", "progress", "Handoff: checkpoint", at=NOW - 300)
        later = update("sv", "progress", "Not handing off: still fixing the parser", at=NOW - 200)
        con = sqlite3.connect(db)
        con.execute("UPDATE task_updates SET superseded_by = ? WHERE id = ?", (later, hid))
        con.commit()
        con.close()
        extra = {"stopping": {"why": "context handoff", "handoff": hid}} if pending else {}
        st = fresh_state(sv=rec("sv", "c1", **extra))
        world(local(term("term-sv", 120)))
        tick_on(st)
        worker = st["workers"]["sv"]
        assert tab_closes() == [] and "stopped" not in worker and "stopping" not in worker, (pending, worker)
        assert not st.get("proposals"), st.get("proposals")


@scenario
def refused_close_retry_names_the_current_handoff():
    """da-4-8: a refused close retried after its handoff #A was superseded proposes from the
    handoff that stands now, #B, and from none once #A was withdrawn and its pane is gone."""
    for successor in (True, False):
        reset_db()
        clear_usage()
        ticket_row("sw", notes="Tier: hard")
        hid = update("sw", "progress", "Handoff: checkpoint", at=NOW - 300)
        text = "Handoff: newer checkpoint" if successor else "Not handing off: still fixing"
        later = update("sw", "progress", text, at=NOW - 200)
        con = sqlite3.connect(db)
        con.execute("UPDATE task_updates SET superseded_by = ? WHERE id = ?", (later, hid))
        con.commit()
        con.close()
        st = fresh_state(sw=rec("sw", "c1", stopping={"why": "context handoff", "handoff": hid}))
        world(local(term("term-sw", 120)) if successor else local())
        tick_on(st)
        assert "stopped" in st["workers"]["sw"], (successor, st["workers"]["sw"])
        proposed = st["proposals"]["sw"]["handoff"]
        assert proposed == (later if successor else None), (successor, proposed, hid, later)

@scenario
def handoff_gone_terminals_finish_their_stops():
    """Review #7954: handed-off workers whose terminals are already gone, two at once, are
    stopped with proposals before the Orca-restart sweep; a stop whose close went through
    but whose save did not is finished too. Unrelated missing workers are still a restart."""
    reset_db()
    clear_usage()
    for tid in ("g1", "g2", "g3", "m1", "m2"):
        ticket_row(tid, notes="Tier: hard")
    for tid in ("g1", "g2"):
        update(tid, "progress", "Handoff: next", at=NOW - 300)
    pending = {"why": "usage limit reached", "handoff": None}
    st = fresh_state(
        g1=rec("g1", "c1"), g2=rec("g2", "c2"), g3=rec("g3", "c3", stopping=pending),
        m1=rec("m1", "c3", model=SONNET), m2=rec("m2", "c3", model=SONNET),
    )
    world(local())
    tick_on(st)
    for tid in ("g1", "g2", "g3"):
        worker = st["workers"][tid]
        assert "stopped" in worker and "lost" not in worker and "stopping" not in worker, worker
    assert sorted(st["proposals"]) == ["g1", "g2", "g3"], st["proposals"]
    assert st["workers"]["g3"]["stopped"]["why"] == "usage limit reached"
    assert all("lost" in st["workers"][t] for t in ("m1", "m2")), "still an Orca restart"


@scenario
def handoff_accept_racing_a_tick_logs_its_own_end():
    """Review #7954: a tick that runs between an accept's launch and its log sees the
    accepted proposal's new worker and logs the acceptance, not a drop."""
    reset_db()
    clear_usage()
    ticket_row("ar", notes="Tier: hard")
    update("ar", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(ar=rec("ar", "c1"))
    world(local(term("term-ar", 120)))
    tick_on(st)
    assert "ar" in st.get("proposals", {}), "a proposal is made"
    log = tmp / "delivered.log"
    log.unlink(missing_ok=True)
    st["proposals"]["ar"]["accepting"] = {"how": "changed", "detail": "launched on pro2"}
    st["workers"]["ar"] = rec("ar", "c2", launched_at=NOW + 30)
    world(local(term("term-ar", 5)))
    tick_on(st, NOW + 60)
    assert st["proposals"] == {}
    ends = [line.split("\t")[2] for line in log.read_text().splitlines() if "PROPOSAL" in line]
    assert ends == ["PROPOSAL changed: launched on pro2"], ends
@scenario
def drop_during_accept_never_launches():
    """da-4-7, da-5-11, da-docs-9: a drop that arrives while an accept is between its
    check and its launch waits for the accept to finish; it never removes the proposal
    under a launch that then starts anyway."""
    reset_db()
    clear_usage()
    ticket_row("dp", notes="Tier: hard")
    update("dp", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(dp=rec("dp", "c1"))
    st.path = state_file
    world(local(term("term-dp", 120)))
    tick_on(st)
    st.save()
    assert "dp" in st.get("proposals", {}), "a proposal is made"
    log = root / "supervisor" / "delivered.log"
    log.unlink(missing_ok=True)
    world(local())
    raced, real = [], da.cmd_launch

    def racing(args, locked=None, cfg=None):
        drop = [sys.executable, str(HERE / "dev_auto.py"), "proposals", "drop", "dp"]
        try:
            raced.append(subprocess.run(drop, capture_output=True, text=True, timeout=3).returncode)
        except subprocess.TimeoutExpired:
            raced.append("waited")
        return real(args, locked=locked, cfg=cfg)

    da.cmd_launch = racing
    try:
        da.main(["proposals", "accept", "dp"])
    except SystemExit:
        pass
    finally:
        da.cmd_launch = real
    assert raced == ["waited"] and len(created()) == 1, (raced, created())
    ends = [line.split("\t")[2] for line in log.read_text().splitlines() if "PROPOSAL" in line]
    assert [e.split(":")[0] for e in ends] == ["PROPOSAL accepted"], ends
    assert json.loads(state_file.read_text())["proposals"] == {}
    state_file.unlink()


@scenario
def accept_launches_the_proposal_it_selected():
    """A drop and a newer proposal for the same ticket that land between accept's choice
    and its launch never swap the launch for that newer one: accept selects, launches
    and ends one proposal under one state lock."""
    reset_db()
    clear_usage()
    ticket_row("dq", notes="Tier: hard")
    update("dq", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(dq=rec("dq", "c1"))
    st.path = state_file
    world(local(term("term-dq", 120)))
    tick_on(st)
    st.save()
    chosen = json.loads(state_file.read_text())["proposals"]["dq"]
    other = next(r for r in cfg["account"] if r["agent"] == "claude" and r["label"] != chosen["launch"]["account"])
    world(local())
    entries, real = [], da.state

    @contextmanager
    def gapped(where):
        entries.append(where)
        if len(entries) == 2:
            drop = [sys.executable, str(HERE / "dev_auto.py"), "proposals", "drop", "dq"]
            subprocess.run(drop, capture_output=True, text=True, timeout=10)
            data = json.loads(state_file.read_text())
            data["proposals"]["dq"] = {**chosen, "launch": {**chosen["launch"], "account": other["label"]}}
            state_file.write_text(json.dumps(data))
        with real(where) as held:
            yield held

    da.state = gapped
    try:
        da.main(["proposals", "accept", "dq"])
    except SystemExit:
        pass
    finally:
        da.state = real
    saved = json.loads(state_file.read_text())
    assert len(created()) == 1, created()
    picked = next(r for r in cfg["account"] if r["label"] == chosen["launch"]["account"])
    assert saved["workers"]["dq"]["account"] == picked["id"], (saved["workers"]["dq"]["account"], picked)
    state_file.unlink()


@scenario
def accept_refuses_a_ticket_completed_before_its_launch():
    """da-docs-9: a ticket that completes in Concord after accept checked it, before the
    launch reads it again, is refused at launch: no terminal is created and the proposal stays."""
    reset_db()
    clear_usage()
    ticket_row("dz", notes="Tier: hard")
    update("dz", "progress", "Handoff: next", at=NOW - 300)
    st = fresh_state(dz=rec("dz", "c1"))
    st.path = state_file
    world(local(term("term-dz", 120)))
    tick_on(st)
    st.save()
    assert "dz" in json.loads(state_file.read_text())["proposals"]
    world(local())
    reads, real = [], da.read_tickets

    def completing(where):
        reads.append(where)
        if len(reads) == 2:
            con = sqlite3.connect(db)
            con.execute("UPDATE tasks SET status = 'complete' WHERE task_id = 'dz'")
            con.commit()
            con.close()
        return real(where)

    da.read_tickets = completing
    try:
        da.main(["proposals", "accept", "dz"])
    except SystemExit:
        pass
    finally:
        da.read_tickets = real
    assert len(reads) >= 2 and created() == [], (reads, created())
    assert "dz" in json.loads(state_file.read_text())["proposals"]
    state_file.unlink()



def main(names):
    chosen = [fn for fn in SCENARIOS if not names or fn.__name__ in names]
    unknown = set(names) - {fn.__name__ for fn in SCENARIOS}
    assert not unknown, f"no such scenario: {', '.join(sorted(unknown))}"
    failed = []
    for fn in chosen:
        try:
            fn()
        except AssertionError as error:
            failed.append(fn.__name__)
            print(f"FAIL {fn.__name__}: {type(error).__name__}: {error}")
    if failed:
        sys.exit(f"{len(failed)} of {len(chosen)} scenarios failed: {' '.join(failed)}")
    print(f"dev-auto scenarios passed ({len(chosen)})")


def test_scenarios():
    main([])


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
