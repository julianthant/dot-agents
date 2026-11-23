"""Account configuration and usage readings for the Orca picker."""

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
import uuid
import zlib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCOUNTS = Path(os.environ.get("DEV_AUTO_ACCOUNTS") or HERE.parent / "accounts.toml")
ORCA = os.environ.get("ORCA_CLI_COMMAND") or "orca"
WINDOWS = ("five_hour", "weekly")
ORCA_USAGE = {}


def load_config(path=ACCOUNTS):
    with open(path, "rb") as stream:
        cfg = tomllib.load(stream)
    rows = []
    for given in cfg.get("account", []):
        row = {"host": "local", "home": "", "plan": "", "enabled": True, "note": ""}
        row.update(given)
        if row["agent"] == "claude":
            opus, sonnet = (2, 4) if row["plan"] == "max" else (1, 2)
            row.setdefault("opus", opus)
            row.setdefault("sonnet", sonnet)
        else:
            row.setdefault("slots", 1)
        # The operator resets a Claude login's weekly use; another kind's stops at 100.
        row.setdefault("weekly_reset_by_operator", row["agent"] == "claude")
        if row["agent"] == "sdk":
            missing = [key for key in ("key_file", "monthly_usd", "ticket_usd") if key not in row]
            if missing:
                sys.exit(f"account {row.get('label')}: agent sdk needs {', '.join(missing)}")
        rows.append(row)
    cfg["account"] = rows
    return cfg


def family(model):
    text = (model or "").lower()
    return "sonnet" if "sonnet" in text or "haiku" in text else "opus"


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def newest_rollouts(home, limit=5):
    """The most recently written rollout files of every day folder: a session started
    before midnight, or resumed days later, keeps writing to its own day's folder. A
    folder or file Codex removes while it is read is skipped, the rest still read."""
    # shortcut: one stat per rollout file ever kept; cache by folder mtime if that grows slow.
    def walk(folder, depth):
        try:
            children = list(folder.iterdir())
        except OSError:
            return []
        if not depth:
            return [path for path in children if path.suffix == ".jsonl"]
        return [path for child in children if child.is_dir() for path in walk(child, depth - 1)]

    found = walk(home / "sessions", 3)
    dated = []
    for path in found:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return [path for _, path in sorted(dated, reverse=True)[:limit]]


def latest_rate(home):
    """The rate limits of the newest usage line among the recent rollouts, by the epoch
    that line was written, and that epoch (None when no line has a readable timestamp):
    a file written last with only other events never hides a newer line in another."""
    best = ({}, None)
    for path in newest_rollouts(home):
        try:
            data = path.read_bytes()[-200_000:]
        except OSError:
            continue
        for line in reversed(data.splitlines()):
            if b"rate_limits" not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            limits = (entry.get("payload") or {}).get("rate_limits")
            if isinstance(limits, dict) and (
                limits.get("primary") or limits.get("secondary")
            ):
                try:
                    written = datetime.fromisoformat(entry["timestamp"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    written = None
                if not best[0] or (written or 0) > (best[1] or 0):
                    best = (limits, written)
                break
    return best


def usage_windows(row):
    """(read epoch, [(used percent, reset epoch, window)]) from the CLI's own file, or
    None. The window is "five_hour" or "weekly"."""
    if not row["home"]:
        return None
    home = Path(row["home"])
    if row["agent"] == "claude":
        path = home / "rate-limits.json"
        data = load_json(path) or {}
        limits = data.get("rate_limits") or {}
        windows, key = (
            (limits.get("five_hour"), limits.get("seven_day")),
            "used_percentage",
        )
        read_at = data.get("saved_at")
        if not read_at and data:
            try:
                read_at = path.stat().st_mtime
            except OSError:
                read_at = None
    elif row["agent"] == "codex":
        limits, read_at = latest_rate(home)
        windows, key = (limits.get("primary"), limits.get("secondary")), "used_percent"
    else:
        return None
    found = [
        (w[key], w.get("resets_at"), name)
        for w, name in zip(windows, WINDOWS)
        if w and w.get(key) is not None
    ]
    return (read_at, found) if found and read_at else None


def host_orca(host, *args):
    reply = subprocess.run([ORCA, *args, "--environment", host, "--json"], capture_output=True,
                           text=True, timeout=120)
    data = json.loads(reply.stdout)
    if reply.returncode or not data.get("ok"):
        raise OSError(f"{host}: {' '.join(args[:2])} failed: {data.get('error')}")
    return data["result"]


def host_checkout(host, repo):
    matches = [r["path"] for r in host_orca(host, "repo", "list")["repos"]
               if (r.get("gitRemoteIdentity") or {}).get("canonicalKey") == "github.com/julianthant/" + repo]
    if len(matches) != 1:
        raise ValueError(f"{host} needs one registered julianthant/{repo} checkout")
    return matches[0]


def remote_output(host, checkout, argv):
    """Run argv on the execution host. A nonce and digest distinguish complete output from shell echo or truncation."""
    tag = uuid.uuid4().hex
    script = """import base64, hashlib, json, subprocess, sys, textwrap, zlib
p = subprocess.run(json.loads(sys.argv[2]), capture_output=True, timeout=100)
data = p.stdout if p.returncode == 0 else b''
tag = sys.argv[1]
for chunk in textwrap.wrap(base64.b64encode(zlib.compress(data)).decode(), 48):
    print(tag + ' ' + chunk)
print(tag + ' END', p.returncode, hashlib.sha256(data).hexdigest())
"""
    # One physical line: an interactive shell must not execute a partial multiline paste.
    command = shlex.join(["python3", "-c", "exec(" + repr(script) + ")", tag, json.dumps(argv)])
    result = host_orca(host, "terminal", "create", "--worktree", "path:" + str(checkout),
                       "--title", "dev-auto", "--command", command)
    handle = result["terminal"]["handle"]
    try:
        until = time.monotonic() + 115
        while time.monotonic() < until:
            lines = host_orca(host, "terminal", "read", "--terminal", handle, "--limit", "5000")["terminal"]["tail"]
            parts = [line[len(tag) + 1:] for line in lines if line.startswith(tag + " ")]
            if parts and parts[-1].startswith("END "):
                _, code, digest = parts.pop().split()
                if code != "0":
                    raise OSError(f"{host}: {argv[0]} exited {code}")
                raw = zlib.decompress(base64.b64decode("".join(parts), validate=True))
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError(f"{host}: incomplete command output")
                return raw.decode()
            time.sleep(1)
        raise TimeoutError(f"{host}: no receipt for {argv[0]}")
    finally:
        host_orca(host, "terminal", "close", "--terminal", handle)


def remote_account(host, *args):
    # Keep account identities and provider metadata out of the scratch terminal's transcript.
    script = """import json, subprocess, sys
r = json.loads(subprocess.check_output(["orca", "account", *sys.argv[1:], "--json"]))
if not r["ok"]: sys.exit(1)
out = {}
if sys.argv[1] == "list":
    data = r["result"]
    out["rateLimits"] = {}
    def windows(reading):
        reading = reading or {}
        return {k: reading.get(k) for k in ("updatedAt", "session", "weekly")}
    for agent in ("claude", "codex"):
        out[agent] = {"activeAccountId": data[agent].get("activeAccountId")}
        limits = data["rateLimits"]
        out["rateLimits"][agent] = windows(limits.get(agent))
        key = "inactive" + agent.title() + "Accounts"
        out["rateLimits"][key] = [{"accountId": a["accountId"], "rateLimits": windows(a.get("rateLimits"))}
                                  for a in limits.get(key) or []]
print(json.dumps(out))
"""
    return json.loads(remote_output(host, host_checkout(host, "hr-relay"), ["python3", "-c", script, *args]))


def orca_usage(now, host="local"):
    """Read each host once per picker snapshot, including inactive managed accounts."""
    if (now, host) in ORCA_USAGE:
        return ORCA_USAGE[now, host]
    if host != "local":
        result = remote_account(host, "list")
    else:
        try:
            reply = subprocess.run([ORCA, "account", "list", "--json"], capture_output=True,
                                   text=True, timeout=120, check=False)
            data = json.loads(reply.stdout) or {}
            result = (data.get("result") or {}) if data.get("ok", True) else {}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            result = {}
    limits = result.get("rateLimits") or {}
    readings = {}
    found = [("grok", None, limits.get("grok"))]
    for agent in ("claude", "codex"):
        selected = (result.get(agent) or {}).get("activeAccountId")
        found.append((agent, selected, limits.get(agent)))
        for other in limits.get(f"inactive{agent.title()}Accounts") or []:
            found.append((agent, other.get("accountId"), other.get("rateLimits")))
    # Orca's own logins: (agent, id) -> home, so a row whose id is not Orca's can be found
    # through the profile it runs in.
    for agent in ("claude", "codex"):
        for login in (result.get(agent) or {}).get("accounts") or []:
            if login.get("id"):
                readings["login", agent, login["id"]] = login.get("home") or ""
    for agent, key, reading in found:
        reading = reading or {}
        windows = [
            (w["usedPercent"], w["resetsAt"] / 1000 if w.get("resetsAt") else None, name)
            for w, name in zip((reading.get("session"), reading.get("weekly")), WINDOWS)
            if w and w.get("usedPercent") is not None
        ]
        if windows and reading.get("updatedAt") and (key or agent == "grok"):
            readings[agent, key] = (reading["updatedAt"] / 1000, windows)
    if len(ORCA_USAGE) > 32:
        ORCA_USAGE.clear()
    ORCA_USAGE[now, host] = readings
    return readings


def orca_key(row, now):
    """The key of the row's reading in orca_usage: its own id when Orca reads that id, else,
    for a row on this Mac, the Orca login whose home is the row's home or whose id names a
    folder of that home (Orca keeps each login in `claude-profiles/<id>` or
    `codex-accounts/<id>`), so a row kept under an older id still reads its login."""
    agent = row["agent"]
    if agent == "grok":
        return agent, None
    readings = orca_usage(now, row["host"])
    if (agent, row["id"]) in readings or row["host"] != "local" or not row["home"]:
        return agent, row["id"]
    parts = Path(row["home"]).parts
    for key, home in readings.items():
        if key[:2] != ("login", agent) or (agent, key[2]) not in readings:
            continue
        if (home and os.path.realpath(home) == os.path.realpath(row["home"])) or key[2] in parts:
            return agent, key[2]
    return agent, row["id"]


FILE_USAGE = {}


def file_usage(row):
    """Parse usage again only when the account's CLI files change."""
    home = Path(row["home"]) if row["home"] else None
    if home is None or row["agent"] not in ("claude", "codex"):
        return None
    paths = [home / "rate-limits.json"] if row["agent"] == "claude" else newest_rollouts(home)
    sign = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        sign.append((str(path), info.st_mtime_ns, info.st_size))
    key = (row["agent"], str(home), tuple(sign))
    if key not in FILE_USAGE:
        if len(FILE_USAGE) > 256:
            FILE_USAGE.clear()
        FILE_USAGE[key] = usage_windows(row)
    return FILE_USAGE[key]
