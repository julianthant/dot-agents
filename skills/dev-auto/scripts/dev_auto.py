#!/usr/bin/env python3
"""Mechanical supervisor for Concord-ticketed workers in Orca terminals.

  slot --agent A --model M [--host H]   an account with a free slot, or "no capacity"
  launch TICKET --agent A [--model M] [--effort E] --worktree PATH [--host H]
  tick                                  one pass over live workers; run it from a loop
  set-orchestrator TERMINAL [--host H] [--account ID] [--session ID] [--new-cycle]
  features [--repo PATH]                done and open tickets per feature, with specs
  ledger-init REPO_NAME [--checkout P]  create ~/.concord-ledger/REPO_NAME; P, the repo's
                                        main checkout, is where its evidence is cleaned

Every command but ledger-init reads the ledger at $CONCORD_REPO_ROOT. Policy lives in
../accounts.toml; small state lives in <ledger>/supervisor/state.json.
"""

import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import random
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import contextmanager, nullcontext, redirect_stdout
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent


def sibling(name):
    """A script beside this one, loaded as a module."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# sdk_worker.py, for its fixed rows and the reservation sum slot shares with it.
SDK = sibling("sdk_worker")
# roadmap.py, for the owned-path overlap rule auto-fill shares with launch.
ROADMAP = sibling("roadmap")
ACCOUNTS = Path(os.environ.get("DEV_AUTO_ACCOUNTS") or HERE.parent / "accounts.toml")
ORCA = os.environ.get("ORCA_CLI_COMMAND") or "orca"
QUIET = {"CONCORD_TELEMETRY_DISABLED": "1", "CONCORD_NO_UPDATE_CHECK": "1"}
SUPERVISOR = "script:dev-auto"
DONE = ("complete", "closed")
WORKING = ("active", "proposed", "assigned")
# An agent's own limit message opens its line, after only ⎿, ■ or ⚠, a "chatMessage:" key or
# an italic *; a bullet, quote or other text before the words is output quoting them.
# Its wordings: Claude's "You've hit your 5-hour limit" and "⚠ Usage limit reached ·", Codex's
# "You’ve hit your usage limit" and goal status "Goal hit usage limits", Cursor's "Weekly
# usage limit reached.".
# shortcut: Grok's limit screen is unseen, so a limited Grok is nudged, then STUCK; add its
# wording here and to RESET_IN once a live Grok shows it.
LIMIT = re.compile(
    r"(?:\w+: )?[\s⎿■⚠\ufe0f]*(?:\*(?=\w))?(?:(?:you['’]ve |you have )?hit your (?:\S+ )?limit"
    r"|(?:weekly )?usage limit reached(?: again)?(?= ?[·.*]|$)|goal hit usage limits\b)",
    re.IGNORECASE,
)
# A lost Concord server on a Claude screen: the documented tool results
# (code.claude.com/docs/en/errors.md and mcp.md), and live Claude Code 2.1.325's against a
# concord that exits mid-call, "Connection closed" with no call line above it, then
# /mcp reconnect concord's refusal. Its footer notification lasts 12 seconds.
CONCORD_LOST = re.compile(
    r'⎿\s+Error: No such tool available: mcp__concord__'
    r'|⎿\s+(?:Error: )?MCP server "concord" (?:connection closed|is disabled)\b'
    r"|⎿\s+Failed to reconnect to concord\b"
)
CONNECTION_CLOSED = re.compile(r"⎿\s+Error: Connection closed$")
# Another tool's call line: a failure result under it is that tool's output, never Concord's.
OTHER_CALL = re.compile(
    r"[⏺●] (?:\S+ - \w+ \(MCP\)|mcp__\w+__\w+|Call(?:ing|ed) \S|[\w.-]+\()"
)
# A Concord call Claude draws (⏺ on the Mac, ● on Linux; 2.1.325 folds calls into
# "Called concord"), or a reconnect that worked: newer than any failure above it.
CONCORD_CALL = re.compile(
    r"[⏺●] (?:concord - \w+ \(MCP\)|mcp__concord__\w+|Call(?:ing|ed) concord\b)"
)
CONCORD_BACK = re.compile(r"⎿\s+Successfully reconnected to concord\b")
# A turn still running, whatever the output time says: Claude's spinner line
# ("✶ Grooving… (7m 20s · ↓ 37.1k tokens)") or an interrupt hint (Codex's "Working").
IN_TURN = re.compile(r"^\S \w[\w ]*… \(|esc to interrupt")
CONCORD_NOTICE = 'MCP server "concord" disconnected · open /mcp to reconnect'
# Codex 0.169.0's Concord calls. It never restarts a dead stdio server, so each later call
# fails as "• Failed concord.inspect_work", then "Caused by:" and "Transport closed".
CODEX_CONCORD = re.compile(r"• (Called|Failed) concord\.\w+")
RECONNECT = "/mcp reconnect concord"
# A host whose ledger self-check failed is not probed again for this long.
LEDGER_RECHECK = 300
# The Codex CLI's home when CODEX_HOME is unset.
CODEX_DEFAULT = str(Path.home() / ".codex")
# A line that opens Claude's or Codex's next output; a limit line above one is history.
OUTPUT = ("⏺", "•")
# Live 2026-11-15: Claude Code's own status lines under its weekly-limit line, never output.
LIMIT_STATUS = re.compile(r"[⏺●] (?:Goal paused · )?usage limit reached · ", re.IGNORECASE)
# A limit screen's reset: Cursor's "It resets in 1 day", Codex's "try again in 3 days 4
# hours", Claude's "resets 5pm", "limit resets 4am" and "resets Nov 16 at 8pm".
RESET_IN = re.compile(
    r"(?:resets?|try again) in ((?:\d+ ?(?:days?|hours?|hrs?|h|minutes?|mins?|m)\b,? ?)+)",
    re.IGNORECASE,
)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
RESET_AT = re.compile(
    r"(?:resets|try again at|continuing automatically at) "
    r"(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2}) at )?"
    r"(\d{1,2})(?::(\d\d))? ?([ap]m)\b",
    re.IGNORECASE,
)
# sdk_worker.py's busy and idle rows, and the rows it exits on. Only the runner starts a
# row with "· ", and each fits a 40-column terminal, so a whole row matches.
SDK_SCREEN = (SDK.BUSY, SDK.IDLE)
# Codex's switch-model menu; its second item keeps the model.
MENU = ("Approaching rate limits", "Keep current model")
# Codex's status line under its input box: "GPT-6.1-Sol xhigh · ~/repo   Goal stalled
# (/goal resume)". A stalled or paused goal starts no turn from a typed wake.
CODEX_STATUS = re.compile(r"\s*([\w.-]+)(?: (\w+))? · (?=[~/])")
RESUME = ("Goal stalled (/goal resume)", "Goal paused (/goal resume)")
# A question, permission or trust prompt, which an Enter would answer.
ASKING = re.compile(
    r"\? [0-9]+ question|Do you want to |Do you trust|Trust this folder\?|Hooks need review"
    r"|Allow command|Quick safety check"
)
# A shell's own prompt as a whole line: oh-my-zsh's "➜  dir git:(branch) ✗", or
# "user@host:dir$", "user@host dir %" or a bare "%", "$" or "#"; not a status line such
# as "opus@max: 87%".
SHELL = re.compile(
    r"➜ +\S+(?: git:\([^)\s]*\))?(?: ✗)?|\S+@\S+:\S*[$#]|\S+@\S+ \S+ [%#]|[$%#]"
)
# Everything typed into a terminal matches this: nothing a shell would quote, expand or split.
SAFE = re.compile(r"[\w .,:/@%-]*")
SAFE_OUT = re.compile(r"[^\w .,:/@%-]")
SHA = re.compile(r"[0-9a-f]{7,40}")
# A worker that exits sooner than this after launch, or after this many relaunches,
# waits for a new launch.
FAST_EXIT = 600
MAX_RELAUNCHES = 3
# A prompt or limit on screen counts only after this many quiet seconds.
SETTLED = 60
# A wake with no update from its worker and no terminal output this long after it is one
# STUCK; accounts.toml's wake_minutes overrides it.
WAKE_MINUTES = 3
# Relaunch a limited worker this long after its reset, as wake.sh did.
RESUME_AFTER = 300
# No reset read from a screen keeps an account out longer than this.
MAX_WAIT = 7 * 86400
# A refused cleanup with nothing changed is checked again after this many seconds.
RECHECK = 15 * 60
# A finished record's retired entry ties panes Orca revives to its ticket this long.
RETIRED = 7 * 86400


def run(argv, cwd=None, env=None, timeout=120):
    try:
        return subprocess.run(
            argv,
            check=False,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def orca_reply(host, *args, timeout=120):
    """Orca's whole JSON reply, or None when none came: a timeout or a lost answer, which
    proves nothing about what Orca did."""
    argv = [ORCA, *args, "--json"]
    if host != "local":
        argv += ["--environment", host]
    try:
        return json.loads(run(argv, timeout=timeout).stdout)
    except ValueError:
        return None


def orca(host, *args, timeout=120):
    data = orca_reply(host, *args, timeout=timeout)
    if data is None:
        return None
    return data.get("result") if data.get("ok", True) else None


def stamp(epoch):
    return datetime.fromtimestamp(int(epoch)).strftime("%m-%d %H:%M")


# Accounts and slots

SDK_KEYS = ("key_file", "monthly_usd", "ticket_usd")
# Auto-fill defaults by tier or kind: [agent, model, effort]. "reviewer" is for
# Claude reviewers. [fill.models] in accounts.toml overrides these defaults.
FILL_MODELS = {
    "hard": ["codex", "gpt-6.2-astra", "high"],
    "ui": ["claude", "claude-sonnet-5-5", "medium"],
    "claude": ["claude", "claude-opus-5-5", "medium"],
    "codex": ["codex", "gpt-6.2-sol", "xhigh"],
    "grok": ["grok", "grok-4.7", "xhigh"],
    "cursor": ["cursor", "grok-4.7-xhigh", ""],
    "reviewer": ["claude", "claude-sonnet-5-5", "medium"],
}


def load_config(path=ACCOUNTS):
    with open(path, "rb") as stream:
        cfg = tomllib.load(stream)
    cfg.setdefault("usage_full_percent", 90)
    cfg.setdefault("hard_pro_full_percent", 70)
    cfg.setdefault("relay_percent", 95)
    cfg.setdefault("silent_minutes", 30)
    # A worker idle at its own prompt, no turn running and no wait note, is nudged sooner.
    cfg.setdefault("idle_minutes", 10)
    cfg.setdefault("checkin_minutes", 30)
    cfg.setdefault("checkin_quiet_minutes", 45)
    cfg.setdefault("checkin_fix_rounds", 3)
    cfg.setdefault("checkin_long_minutes", 240)
    # A Claude account near its 5-hour limit hands off all but one worker; off by default.
    cfg.setdefault("limit_handoff", False)
    cfg.setdefault("handoff_percent", 85)
    cfg.setdefault("handoff_minutes_left", 60)
    cfg.setdefault("handoff_wait_minutes", 15)
    # Off until the orchestrator sets them: a cleared head's merge wait, and a gone worker's
    # STUCK ending with its launch command.
    cfg.setdefault("merge_wait_hours", 0)
    cfg.setdefault("stuck_launch_command", False)
    # Launch rules the operator may lift: unset, slot applies no 5-hour stop and launch no cap.
    for key in ("usage_reset_minutes", "max_hard_tickets", "max_medium_tickets"):
        cfg.setdefault(key, None)
    cfg.setdefault("hosts", {})
    # The tick pushes cleared heads and opens their pull requests only while this is on.
    cfg.setdefault("auto_push", False)
    # Per "owner/name" from origin's URL: check, base, protected and migrations.
    cfg.setdefault("repos", {})
    # A Linux host's available memory, and a WSL host's Windows side's free memory, in GB.
    cfg.setdefault("memory_floor_gb", 8)
    # The tick starts ready tickets up to each host's target only while this is on.
    fill = cfg.setdefault("fill", {})
    fill.setdefault("enabled", False)
    fill["models"] = {**FILL_MODELS, **fill.get("models", {})}
    cfg.setdefault("local_target_tickets", None)
    cfg.setdefault("windows_floor_gb", 4)
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
            missing = [key for key in SDK_KEYS if key not in row]
            if missing:
                sys.exit(f"account {row.get('label')}: agent sdk needs {', '.join(missing)}")
        rows.append(row)
    cfg["account"] = rows
    return cfg


def spend_file(root):
    """The sdk runner's spend on this ledger's host, which slot reads and the runner writes."""
    return Path(root) / "supervisor" / "sdk-spend.json"


def month_spent(now, account=None):
    """The runner's monthly spend and holds for the SDK row. Unreadable spend blocks slots."""
    path = spend_file(ledger())
    if not path.exists():
        return 0.0
    try:
        data = SDK.read_spend(path)
        return SDK.account_month_spent(data, now, account)
    except (AttributeError, KeyError, TypeError, ValueError):
        return float("inf")


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


WINDOWS = ("five_hour", "weekly")
ORCA_USAGE = {}
# Account id -> epoch of the operator's `usage-reset`, and account id -> the latest epoch
# the account was seen at its limit; the State's own dicts, bound when it loads.
USAGE_RESETS = {}
USAGE_FULL = {}


def seen_full(account_id, at):
    """The account was seen at its limit at `at`: by a reading of any age or a new limit
    screen. The time only grows; going unread never takes it back."""
    USAGE_FULL[account_id] = max(USAGE_FULL.get(account_id, 0), at)


def reset_stands(cfg, row, now, account_id=None):
    """The operator's usage-reset of the account is newer than anything that showed it at
    its limit: the one answer for every wait and stored screen on it. With no row, the
    stored times alone."""
    if row:
        usage_reading(cfg, row, now)
        account_id = row["id"]
    mark = USAGE_RESETS.get(account_id)
    return mark is not None and mark > USAGE_FULL.get(account_id, 0)


def orca_usage(now):
    """This Mac's Orca readings, fetched once per `now`: (read epoch, [(used percent,
    reset epoch, window)]) per (agent, Orca account id); Grok's one reading has no id."""
    if now in ORCA_USAGE:
        return ORCA_USAGE[now]
    result = orca("local", "account", "list") or {}
    limits = result.get("rateLimits") or {}
    found = [("grok", None, limits.get("grok"))]
    for agent in ("claude", "codex"):
        selected = (result.get(agent) or {}).get("activeAccountId")
        found.append((agent, selected, limits.get(agent)))
        for other in limits.get(f"inactive{agent.title()}Accounts") or []:
            found.append((agent, other.get("accountId"), other.get("rateLimits")))
    readings = {}
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
    ORCA_USAGE.clear()
    ORCA_USAGE[now] = readings
    return readings


def orca_key(row, now):
    """The key of the row's reading in orca_usage: its own id when Orca reads that id, else,
    for a row on this Mac, the Orca login whose home is the row's home or whose id names a
    folder of that home (Orca keeps each login in `claude-profiles/<id>` or
    `codex-accounts/<id>`), so a row kept under an older id still reads its login."""
    agent = row["agent"]
    if agent == "grok":
        return agent, None
    readings = orca_usage(now)
    if (agent, row["id"]) in readings or row["host"] != "local" or not row["home"]:
        return agent, row["id"]
    parts = Path(row["home"]).parts
    for key, home in readings.items():
        if key[:2] != ("login", agent) or (agent, key[2]) not in readings:
            continue
        if (home and same_path(home, row["home"], "local")) or key[2] in parts:
            return agent, key[2]
    return agent, row["id"]


FILE_USAGE = {}


def file_usage(row):
    """usage_windows of the row, parsed again only when its CLI file changed: every slot
    question in a tick reads a stat per file, not the file."""
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


def usage_reading(cfg, row, now):
    """[(percent, reset, window)] of the account's current reading, or None when unread.
    Orca's reading of the account comes first, then, for a row on this Mac only, its
    CLI's file. A reading older than usage_max_age_minutes, or with every window reset,
    is skipped. Every reading in hand at usage_full_percent, of any age, is seen_full.
    After the operator's usage-reset no reading from before it counts, and while the reset
    stands with no newer reading in hand every window reads 0%."""
    max_age = cfg.get("usage_max_age_minutes", 5) * 60
    full = cfg["usage_full_percent"]
    key = orca_key(row, now)
    file = row["host"] == "local" and file_usage(row)
    readings = [found for found in (orca_usage(now).get(key), file) if found]
    for read_at, windows in readings:
        if any(float(p) >= full and (not r or float(r) > now) for p, r, _w in windows):
            seen_full(row["id"], float(read_at))
    mark = USAGE_RESETS.get(row["id"])
    if mark is not None:
        stands = mark > USAGE_FULL.get(row["id"], 0)
        if not stands and now - mark > max_age:
            # The reset is over and every reading from before it is past the age limit.
            del USAGE_RESETS[row["id"]]
        readings = [found for found in readings if float(found[0]) > mark]
        if not readings:
            return [(0.0, None, window) for window in WINDOWS] if stands else None
    for read_at, windows in readings:
        if now - float(read_at) > max_age:
            continue
        if any(not r or float(r) > now for _p, r, _w in windows):
            return [(float(p), r, w) for p, r, w in windows]
    return None


def live_windows(cfg, row, now):
    """(percent, reset) per usage window not yet reset, or None when unread."""
    found = usage_reading(cfg, row, now)
    if found is None:
        return None
    return [(p, r) for p, r, _w in found if not r or float(r) > now]


def five_hour(cfg, row, now):
    """The 5-hour window's (percent, reset); (0.0, None) once it has reset, and (None,
    None) when unread, which a current reading without that window also is."""
    for percent, reset, window in usage_reading(cfg, row, now) or []:
        if window == "five_hour":
            return (percent, reset) if not reset or float(reset) > now else (0.0, None)
    return None, None


def peak(cfg, row, now):
    """Highest use among windows not yet reset: (percent, reset), or (None, None) unread."""
    live = live_windows(cfg, row, now)
    if live is None:
        return None, None
    return max(live, key=lambda w: w[0])


def limit_reset(cfg, row, now):
    """The latest reset among windows at the limit, so a wake never lands while another
    window still blocks; else the busiest window's reset."""
    live = live_windows(cfg, row, now) or []
    full = [r for p, r in live if r and p >= cfg["usage_full_percent"]]
    return max(full, default=None) or peak(cfg, row, now)[1]


def at_limit(cfg, row, now):
    percent, _ = peak(cfg, row, now)
    return percent is not None and percent >= cfg["usage_full_percent"]


def pro_opus_full(cfg, row, model, now):
    """Claude Pro asked for an Opus-family model, with its 5-hour use unread or above the
    Pro cap. A window equal to the cap still takes the worker; weekly use never counts."""
    if row["agent"] != "claude" or row["plan"] != "pro" or family(model) != "opus":
        return False
    percent, _reset = five_hour(cfg, row, now)
    return percent is None or percent > cfg["hard_pro_full_percent"]


def five_hour_stop(cfg, row, now):
    """Why the account takes no new worker: its 5-hour window at usage_full_percent and
    not resetting within usage_reset_minutes. None while that key is unset or the window
    is unread. Weekly use is weekly_stop's rule."""
    minutes, full = cfg["usage_reset_minutes"], cfg["usage_full_percent"]
    percent, reset = five_hour(cfg, row, now)
    if minutes is None or percent is None or percent < full:
        return None
    if reset and float(reset) - now <= minutes * 60:
        return None
    when = f"resets {stamp(reset)}" if reset else "no reset time"
    return f"5-hour use {percent:.0f}% at usage_full_percent {full}, {when}"


def weekly_stop(cfg, row, workers, now):
    """Why a row the operator does not reset takes no new worker: a limit screen stored
    for its account, finished ticket or not, whose reset is ahead, whatever its window
    and the reading, or its weekly window at 100 percent. The operator's standing
    usage-reset answers every stored screen, and a screen its worker is freed from (a
    fresh reading below the limit after a full one) blocks no more. Unread usage alone
    stops nothing."""
    if row["weekly_reset_by_operator"]:
        return None
    windows = usage_reading(cfg, row, now) or []
    percent, reset = next(
        ((p, r) for p, r, w in windows if w == "weekly" and (not r or float(r) > now)),
        (None, None),
    )
    use = "unread" if percent is None else f"{percent:.0f}%"
    screens = [] if reset_stands(cfg, row, now) else [
        at
        for rec in workers.values()
        if rec["account"] == row["id"]
        and (at := rec.get("limit_reset") or 0) > now
        and not freed(cfg, row, rec, now)
    ]
    if screens:
        return f"weekly use {use}, limit screen until {stamp(max(screens))}"
    if percent is None or percent < 100:
        return None
    return f"weekly use {use} at 100, {f'resets {stamp(reset)}' if reset else 'no reset time'}"


def screen_reset(text, now):
    """(reset, given): the reset a usage-limit screen gives, the latest of its clock times,
    each read in the zone it prints after the time, else in this Mac's, and whether it gave
    one; with none, 24 hours for a weekly limit and 5 hours otherwise. Never more than
    MAX_WAIT away."""
    found = RESET_IN.search(text)
    if found:
        units = {"d": 86400, "h": 3600, "m": 60}
        amounts = re.findall(r"(\d+) ?([dhm])", found[1], re.IGNORECASE)
        when = now + sum(int(n) * units[u.lower()] for n, u in amounts)
    elif matches := list(RESET_AT.finditer(text)):
        # A weekly line and Claude's "continuing automatically at 4am" both print one: the
        # account is free only once the latest passes. A dated reset outranks a time-only
        # one, which rolls to tomorrow once its hour has passed.
        dated = [found for found in matches if found[1]] or matches
        clocks = [clock_reset(found, text, now) for found in dated]
        if None in clocks:
            return now + (86400 if "weekly" in text.lower() else 5 * 3600), False
        when = max(clocks)
    else:
        return now + (86400 if "weekly" in text.lower() else 5 * 3600), False
    return min(when, now + MAX_WAIT), True


def clock_reset(found, text, now):
    """The epoch of one RESET_AT match, in the zone printed after it, else this Mac's;
    None for a date that does not exist."""
    month, day, hour, minute, half = found.groups()
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    printed = re.match(r" ?\(([\w+-]+(?:/[\w+-]+)*)\)", text[found.end() :])
    try:
        zone = ZoneInfo(printed[1]) if printed else None
    except (ValueError, ZoneInfoNotFoundError):
        zone = None
    clock = datetime.fromtimestamp(now, zone).replace(
        hour=hour, minute=int(minute or 0), second=0, microsecond=0
    )

    def ahead(clock):
        # A wall time the fall-back repeats is two instants: the next of them counts.
        return min((t for f in (0, 1) if (t := clock.replace(fold=f).timestamp()) > now), default=None)

    try:
        if month:
            clock = clock.replace(month=MONTHS.index(month.title()) + 1, day=int(day))
            if ahead(clock) is None:
                clock = clock.replace(year=clock.year + 1)
        elif ahead(clock) is None:
            clock += timedelta(days=1)
    except ValueError:
        return None
    return ahead(clock) or clock.timestamp()


def concord_shown(agent, lines):
    """True when the newest Concord news above the input box is a failure in the agent's
    own wording, Claude's or Codex's for Cursor and Grok, or Claude's footer gave up
    reconnecting. A fence holds a quoted example, its opening scrolled off or not."""
    claude, codex = agent != "codex", agent != "claude"
    box = max(
        (i for i, line in enumerate(lines) if line.startswith(("❯", "›"))),
        default=len(lines),
    )
    rule = next((i for i in range(box + 1, len(lines)) if lines[i].startswith("─")), len(lines))
    if claude and any(CONCORD_NOTICE in line for line in lines[rule + 1 :]):
        return True
    above = [line.strip() for line in lines[:box]]
    fenced = sum(line.startswith("```") for line in above) % 2 == 1
    lost = failed = other = False
    for line in above:
        call = codex and CODEX_CONCORD.match(line)
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            continue
        elif claude and (CONCORD_CALL.match(line) or CONCORD_BACK.match(line)):
            lost = other = False
        elif claude and not other and (CONCORD_LOST.match(line) or CONNECTION_CLOSED.match(line)):
            lost = True
        elif claude and line.startswith(("⏺", "●", "❯")):
            other = bool(OTHER_CALL.match(line))
        elif call:
            lost, failed = False, call[1] == "Failed"
        elif line.startswith("•"):
            failed = False
        elif failed and line == "Transport closed":
            lost = True
    return lost


def in_turn(lines):
    """True while the screen shows a running turn: its spinner or an interrupt hint."""
    return any(IN_TURN.search(line.strip()) for line in lines[-15:])


def ledger_up(cfg, st, rec, now):
    """True when the ledger answers this supervisor's own Concord call and, for a worker
    on another host, that host's ssh self-check passes. Each failure raises one STUCK
    naming the host until a check passes again. A failed check is kept per host, the
    local call's apart from each remote self-check's, and no worker retries it before
    LEDGER_RECHECK."""
    down = st.setdefault("ledger_down", {})
    hosts = {"local", rec["host"]}
    if any(now - down.get(host, 0) < LEDGER_RECHECK for host in hosts):
        return False
    host, text, detail = "local", None, ""
    try:
        concord_call("inspect_work", {"task_id": rec["ticket"]}, attempts=3)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        text = (
            "STUCK local: the Concord call of dev-auto itself to the ledger fails, so it "
            "repairs the Concord tools of no worker. Remedy: check concord-mcp and the "
            "ledger here. dev-auto types and relaunches nothing until the call works"
        )
        detail = str(error)
    if text is None:
        down.pop("local", None)
        st["sent"].pop("local|ledger", None)
    if text is None and rec["host"] != "local":
        host = rec["host"]
        check = ledger_selfcheck(cfg, host)
        code, _output = run_on(host, rec["worktree"], check)
        if code:
            text = (
                f"STUCK {host}: its ssh self-check to the ledger fails, so the Concord "
                f"tools of its workers cannot be repaired. Remedy: restore the ssh link "
                f"from {host} to the ledger. dev-auto types and relaunches nothing there "
                "until the check passes"
            )
            detail = shlex.join(check)
    key = f"{host}|ledger"
    if text is None:
        down.pop(host, None)
        st["sent"].pop(key, None)
        st.save()
        return True
    down[host] = now
    st.save()
    emit(st, key, text, detail=detail)
    return False


def concord_lost(cfg, root, st, tid, rec, ticket, tickets, lines, quiet, now):
    """True while the worker's screen shows its Concord tools failing and it has recorded
    nothing since the first sighting; nothing else is done for it that tick. Once idle and
    with its host reaching the ledger, a Claude worker gets /mcp reconnect concord typed
    once, and a Codex worker is relaunched once after a note on its ticket. With no repair
    left, and for Cursor and Grok, one STUCK on a later tick. A newer update from it
    clears the failure until the screen stops showing it."""
    if rec["agent"] == "sdk" or not concord_shown(rec["agent"], lines):
        marks = [key for key in ("concord", "concord_typed", "concord_check") if key in rec]
        for key in marks:
            del rec[key]
        if marks:
            st.save()
        return False
    mine = {rec["agent_id"], ticket["agent_id"]}
    newest = max(
        (u["id"] for t in tickets.values() for u in t["updates"] if u["agent"] in mine),
        default=0,
    )
    first = "concord" not in rec
    if first:
        rec["concord"] = newest
        st.save()
    elif rec["concord"] is None or newest > rec["concord"]:
        if rec["concord"] is not None:
            rec["concord"] = None
            rec.pop("concord_typed", None)
            st.save()
        return False
    agent = rec["agent"]
    repair = (agent == "claude" and not rec.get("concord_typed")) or (
        agent == "codex" and not rec.get("concord_relaunched")
    )
    if repair:
        if quiet < SETTLED or in_turn(lines) or not ledger_up(cfg, st, rec, now):
            return True
        if agent == "claude":
            key = f"{tid}|concord-reconnect|{rec['launched_at']}"
            if defer_stale_claude(st, rec, key, RECONNECT):
                return True
            # Saved before the keys: a crash mid-send never types it twice.
            rec["concord_typed"] = True
            st.save()
            sent = claude_keys(st, rec, key, RECONNECT, (("\x15", False), (RECONNECT, True)))
            if sent is True:
                say(tid, f"typed {RECONNECT} into {rec['agent_id']}: its Concord tools fail")
            elif sent == "deferred":
                rec.pop("concord_typed", None)
                st.save()
            return True
        rec["concord_relaunched"] = launched = rec["launched_at"]
        st.save()
        coder = tid.removesuffix("-review")
        read = f"ticket {tid} and the coder's ticket {coder}" if coder != tid else f"ticket {tid}"
        told = f"{tid}|concord-note|{launched}"
        if told not in st["sent"]:
            say(
                tid,
                f"{rec['agent_id']}'s Concord transport closed on its screen, so it is "
                f"relaunched. Read {read} first, then re-run only the checks of a SHA that "
                "has no verdict yet.",
            )
            st["sent"][told] = int(now)
        relaunch(cfg, root, st, tid, rec, ticket, now, repair=True)
        if rec["launched_at"] == launched:
            # No relaunch happened; relaunch reported why, and a later tick tries again.
            rec.pop("concord_relaunched")
            st.save()
        return True
    if first:
        return True
    if agent == "claude":
        why, remedy = f" after {RECONNECT}", "reconnect concord from /mcp, or relaunch it"
        remedy += ". dev-auto types nothing more"
    elif agent == "codex":
        why = " again after its relaunch"
        remedy = (
            "check the ssh link from its host to the ledger, then relaunch it. "
            "dev-auto relaunches it no more"
        )
    else:
        why, remedy = "", "restart its Concord server or relaunch it. dev-auto types nothing"
    text = (
        f"STUCK {tid} {rec['agent_id']} on {rec['host']} {rec['terminal']}: its Concord "
        f"tools fail on its screen{why} and it has recorded nothing since. Remedy: {remedy}"
    )
    emit(st, f"{tid}|concord|{rec['launched_at']}|{newest}", text, note_on=tid)
    return True


def env_value(command, name):
    """The last NAME=value in a `ps eww` line; a value runs to the next NAME= or the end."""
    found = re.findall(rf"(?:^|\s){name}=(.*?)(?=\s[A-Za-z_]\w*=|$)", command)
    return found[-1] if found else None


def runs(command, binary):
    """True when a ps line's program, or the script its interpreter runs, is that binary."""
    return bool(command) and binary in {
        Path(word).name for word in command.split(" ", 2)[:2]
    }


def foreign(cfg, workers):
    """Models of live agents on this Mac's accounts that no record holds, per account id:
    Claude sessions whose pid runs, and Codex, Cursor or Grok processes in Orca terminals."""
    out = run(["ps", "eww", "-ax", "-o", "pid=,command="], timeout=10).stdout
    procs = {}
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        procs[pid] = command
    # A process keeps the handle its terminal had at launch, also after a rebind.
    ours = {
        h for r in workers.values() for h in (r.get("terminal"), *r.get("handles", ()))
    }
    ours.discard(None)
    sessions = {r.get("session") for r in workers.values()}
    found = {}
    for row in cfg["account"]:
        if row["host"] != "local":
            continue
        models = found.setdefault(row["id"], [])
        if row["agent"] == "claude":
            for path in (
                Path(row["home"]).glob("sessions/*.json") if row["home"] else ()
            ):
                session = load_json(path) or {}
                command = procs.get(str(session.get("pid")))
                if not runs(command, "claude") or session.get("sessionId") in sessions:
                    continue
                if env_value(command, "ORCA_TERMINAL_HANDLE") in ours:
                    continue
                model = re.search(r"(?:^|\s)--model[= ](\S+)", command)
                models.append(model.group(1) if model else "opus")
            continue
        binary = {"codex": "codex", "cursor": "cursor-agent"}.get(row["agent"], "grok")
        handles = set()
        for command in procs.values():
            if not runs(command, binary):
                continue
            if row["agent"] == "codex":
                given = env_value(command, "CODEX_HOME")
                if not same_path(given or CODEX_DEFAULT, row["home"] or CODEX_DEFAULT, "local"):
                    continue
            elif row["home"] and env_value(command, "CODEX_HOME") != row["home"]:
                continue
            handles.add(env_value(command, "ORCA_TERMINAL_HANDLE"))
        models += [row["agent"]] * len(handles - ours - {None})
    return found


def slot(cfg, workers, agent, model, host="local", memory=True, **picks):
    """free_row's row, held back when the host is short of memory (memory_refusal). Every
    caller that starts a worker comes through here; the host is read once per call, and
    not at all with `memory` false, when the caller has read it. The refusal, or the
    note on an unread Windows side, opens `why`."""
    if picks.get("why") is None:
        picks["why"] = []
    row = free_row(cfg, workers, agent, model, host, **picks)
    if row is None or not memory:
        return row
    refusal, told = memory_refusal(cfg, host)
    picks["why"][:0] = [refusal] if refusal else told
    return None if refusal else row


def free_row(
    cfg,
    workers,
    agent,
    model,
    host="local",
    exclude=(),
    ignore=None,
    now=None,
    limits=None,
    account=None,
    why=None,
):
    """First enabled row, in file order, with usage below the limit and a free slot; an
    account in `limits` takes nothing until its reset. `account` is an id or label, and
    no other row is used. A row's `max_workers` caps its workers on any model and lets a
    Claude row hold Opus and Sonnet together, each within its own cap; with
    `usage_grant = true` as well, the row takes workers past its usage rules up to that
    cap. An sdk row is overflow: it is used only when no enabled Claude row on its host
    can take the model, unless `account` names it, and never once the month's spend
    reaches its monthly_usd. A row with `weekly_reset_by_operator` false (the default
    off Claude) takes nothing, grant or not, while its weekly window is full or a limit
    screen stored for its account has not reset. A host whose [hosts.<name>] table sets
    max_workers takes no worker of any kind while that many live records run there.
    Each passed-over
    row's reason, and a grant used, is appended to `why`."""
    now = time.time() if now is None else now
    limits = limits or {}
    why = [] if why is None else why
    want = family(model)
    held_by = foreign(cfg, workers)
    workers = {t: r for t, r in workers.items() if not missing(r)}
    host_cap = cfg["hosts"].get(host, {}).get("max_workers")
    if host_cap is not None:
        live = [t for t, r in workers.items() if t != ignore and r["host"] == host and holds_slot(r)]
        if len(live) >= host_cap:
            why.append(f"{host}: max_workers {len(live)} of {host_cap} on the host, every agent kind")
            return None
    for ticket, rec in workers.items():
        if ticket != ignore and holds_slot(rec):
            held_by.setdefault(rec["account"], []).append(rec["model"])
    for row in cfg["account"]:
        if account and account not in (row["id"], row["label"]):
            continue
        if row["agent"] != agent or row["host"] != host or not row["enabled"]:
            continue
        if row["id"] in exclude:
            continue
        name = row["label"]
        if limits.get(row["id"], 0) > now:
            why.append(f"{name}: usage limit until {stamp(limits[row['id']])}")
            continue
        if agent == "sdk":
            # A ticket stays on the key it ran on: another key starts its cache cold.
            own = workers.get(ignore) if ignore else None
            kept = own["account"] if own and own["agent"] == "sdk" else None
            if kept and kept != row["id"]:
                label = (account_row(cfg, kept) or {}).get("label", kept)
                why.append(f"{name}: ticket {ignore} keeps its key on {label}")
                continue
            spent = month_spent(now, row["id"])
            if spent >= row["monthly_usd"]:
                cap = row["monthly_usd"]
                why.append(f"{name}: monthly_usd {cap:g} spent or reserved ({spent:.2f})")
                continue
            if not account and not kept:
                sub = free_row(cfg, workers, "claude", model, host, ignore=ignore, now=now, limits=limits)
                if sub:
                    why.append(f"{name}: overflow only; {sub['label']} can take {model}")
                    continue
            # Only dev-auto starts a runner; foreign() would count Grok processes here.
            held = [
                r["model"]
                for t, r in workers.items()
                if t != ignore and holds_slot(r) and r["account"] == row["id"]
            ]
            total = row.get("max_workers")
            if total is not None and len(held) >= total:
                why.append(f"{name}: max_workers {len(held)} of {total}")
                continue
            if len(held) >= row["slots"]:
                why.append(f"{name}: slots {len(held)} of {row['slots']}")
                continue
            return row
        total = row.get("max_workers")
        over = []
        weekly = weekly_stop(cfg, row, workers, now)
        if weekly:
            # No grant lifts it: the account cannot work until the reset.
            why.append(f"{name}: {weekly}")
            continue
        # No grant lifts it either: the next tick would ask the new worker to hand off.
        hit = agent == "claude" and cfg["limit_handoff"] and handoff_trigger(cfg, row, now)
        if hit:
            why.append(
                f"{name}: 5-hour use {hit[0]:.0f}% at handoff_percent "
                f"{cfg['handoff_percent']}, resets {stamp(hit[1])}"
            )
            continue
        stop = five_hour_stop(cfg, row, now)
        if stop:
            over.append(stop)
        if pro_opus_full(cfg, row, model, now):
            cap = cfg["hard_pro_full_percent"]
            over.append(f"Pro Opus over hard_pro_full_percent {cap} or unread")
        granted = row.get("usage_grant") is True and total is not None
        if over and not granted:
            needs = " (usage_grant needs max_workers)" if row.get("usage_grant") else ""
            why.append(f"{name}: {over[0]}{needs}")
            continue
        held = held_by.get(row["id"], [])
        if total is not None and len(held) >= total:
            why.append(f"{name}: max_workers {len(held)} of {total}")
            continue
        if agent == "claude":
            others = {family(m) for m in held} - {want}
            if total is None and others:
                why.append(f"{name}: holds {', '.join(sorted(others))}, no max_workers")
                continue
            same = sum(family(m) == want for m in held)
            if same >= row[want]:
                why.append(f"{name}: {want} {same} of {row[want]}")
                continue
        elif len(held) >= row["slots"]:
            why.append(f"{name}: slots {len(held)} of {row['slots']}")
            continue
        if over:
            why.append(f"{name}: usage_grant past {'; '.join(over)}")
        return row
    return None


MEMORY_TIMEOUT = 30


def read_memory(host):
    """(host block of `orca diagnostics memory`, None), or (None, why it is unread)."""
    argv = [ORCA, "diagnostics", "memory", "--json"]
    if host != "local":
        argv += ["--environment", host]
    done = run(argv, timeout=MEMORY_TIMEOUT)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return None, done.stderr.strip() or f"exit {done.returncode} with no reading"
    found = (data.get("result") or {}).get("host") if data.get("ok", True) else None
    if not isinstance(found, dict):
        error = data.get("error") or {}
        return None, error.get("message") or error.get("code") or "no host block in the reading"
    return found, None


def host_system(host):
    """A remote host's `uname -s`, run in its first Orca worktree, or None."""
    listed = (orca(host, "worktree", "list") or {}).get("worktrees") or []
    paths = [w["path"] for w in listed if w.get("path")]
    if not paths:
        return None
    code, out = run_on(host, paths[0], ["uname", "-s"], timeout=MEMORY_TIMEOUT)
    return (out.strip() or None) if code == 0 else None


# A refused host is read once more this long after, so a dip (an `npm ci` in a new
# worktree) does not refuse a batch; a refusal stands for the same time without a read.
MEMORY_RECHECK = float(os.environ.get("DEV_AUTO_MEMORY_RECHECK", 60))
# A host refused under a floor takes workers again only this much above it.
MEMORY_RESUME_GB = 1
TREND = 10


def memory_file():
    return ledger() / "supervisor" / "memory.json"


def memory_store():
    data = load_json(memory_file())
    return data if isinstance(data, dict) else {}


def save_memory(host, seen, keys):
    """Write the `keys` this caller set in one host's entry into the memory file as it is
    now, under memory.lock, so a launch's recheck and a tick never save an older copy over
    each other; both callers' trend points are kept, the newest TREND. With `read_at`
    among the keys, an entry the file holds from a later reading keeps its other keys:
    two admission checks of one host never save the older one's floors and refusal last.
    Each save writes a temporary file of its own."""
    path = memory_file()
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = memory_store()
            entry = data.get(host) if isinstance(data.get(host), dict) else {}
            data[host] = entry
            stale = "read_at" in keys and entry.get("read_at", 0) > seen.get("read_at", 0)
            for key in keys:
                if key == "windows_trend":
                    points = {tuple(p) for p in (*(entry.get(key) or ()), *(seen.get(key) or ()))}
                    if points:
                        entry[key] = [list(p) for p in sorted(points)][-TREND:]
                elif stale:
                    continue
                elif key in seen:
                    entry[key] = seen[key]
                else:
                    entry.pop(key, None)
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
            tmp.replace(path)
    except OSError as error:
        print(f"memory file not saved: {error}", file=sys.stderr)
    finally:
        tmp.unlink(missing_ok=True)


def floor_text(floor, held):
    if held == floor:
        return f"its floor of {floor:g} GB"
    return f"its floor of {floor:g} GB ({held:g} GB to take workers again)"


def under(seen, side, have, floor):
    """Hysteresis: a side already under this floor stays under until MEMORY_RESUME_GB
    above it. Returns the floor it was held to, or None when it has room."""
    held = floor + MEMORY_RESUME_GB if seen.get(side) == floor else floor
    seen[side] = floor if have < held else None
    return held if have < held else None


def trend_text(points):
    return " then ".join(f"{gb:g}" for _at, gb in points[-5:]) + " GB"


def windows_reading(cfg, host, seen, now):
    """(free GB, side, floor) of the WSL host's Windows side, recorded in its trend, or
    (None, side, why it is unread); (None, None, None) when the host names no side."""
    info = cfg["hosts"].get(host, {})
    side = info.get("windows")
    side_floor = info.get("windows_floor_gb", cfg["windows_floor_gb"])
    if not side or not side_floor:
        return None, None, None
    reading, failed = read_memory(side)
    if failed:
        return None, side, failed
    free = reading["freeMemory"] / 2**30
    points = seen.setdefault("windows_trend", [])
    points.append([int(now), round(free, 1)])
    del points[:-TREND]
    return free, side, side_floor


def memory_once(cfg, host, seen, now):
    """One reading of the host, and of its Windows side: (refusal, notes)."""
    info = cfg["hosts"].get(host, {})
    floor = info.get("memory_floor_gb", cfg["memory_floor_gb"])
    reading, failed = read_memory(host)
    if failed:
        return f"{host}: memory unread ({failed}); no worker starts there", []
    source = reading.get("availableMemorySource")
    if source == "memory-pressure":
        return None, []
    if source != "proc-meminfo" and host != "local":
        system = host_system(host)
        if system is None:
            why = f"a {source} reading, and no uname -s from the host"
            return f"{host}: memory unread ({why}); no worker starts there", []
        if system != "Linux":
            return None, []
    marks = seen.setdefault("under", {})
    have = reading["availableMemory"] / 2**30
    held = under(marks, "linux", have, floor)
    if held:
        return (
            f"{host}: {have:.1f} GB memory available, under {floor_text(floor, held)};"
            " no worker starts there"
        ), []
    free, side, side_floor = windows_reading(cfg, host, seen, now)
    if side is None:
        return None, []
    if free is None:
        return None, [
            f"{host}: its Windows side {side} is unread ({side_floor}); the Linux reading alone decides"
        ]
    held = under(marks, "windows", free, side_floor)
    if held:
        return (
            f"{host}: its Windows side {side} has {free:.1f} GB memory free, under"
            f" {floor_text(side_floor, held)}; no worker starts there"
        ), []
    return None, []


def memory_refusal(cfg, host):
    """(refusal, notes): one reading of the host and, for a WSL host whose entry names
    its Windows side, one of that side. A Linux host refuses a worker when its available
    memory is under memory_floor_gb or unread; the Windows side when its free memory is
    under windows_floor_gb, and an unread side leaves the Linux reading to decide. A side
    once under its floor is held to the floor plus MEMORY_RESUME_GB until a reading is
    above that. A refusal is read once more MEMORY_RECHECK seconds later and stands only
    when that reading refuses too; for MEMORY_RECHECK seconds after, the same host is
    refused without a read, so a batch of launches waits once. Orca reads /proc/meminfo
    on Linux alone and memory pressure on macOS alone; when either fails it falls back
    to free-memory on both, and then the remote host's `uname -s` decides: Linux is held
    to the floor, another system is not, and no answer is unread. A non-Linux host is
    not checked, and a local one is not read. A floor of 0 turns its check off."""
    info = cfg["hosts"].get(host, {})
    floor = info.get("memory_floor_gb", cfg["memory_floor_gb"])
    if not floor or (host == "local" and not sys.platform.startswith("linux")):
        return None, []
    data = memory_store()
    seen = data.setdefault(host, {})
    now = time.time()
    last = seen.get("refused") or {}
    if last and now - last.get("at", 0) < MEMORY_RECHECK:
        return last["text"], []
    marks = dict(seen.get("under") or {})
    refusal, notes = memory_once(cfg, host, seen, now)
    if refusal:
        # The second reading is held to the floors the first one was.
        seen["under"] = marks
        time.sleep(MEMORY_RECHECK)
        now = time.time()
        refusal, notes = memory_once(cfg, host, seen, now)
    if refusal:
        seen["refused"] = {"at": now, "text": refusal}
    else:
        seen.pop("refused", None)
    seen["read_at"] = now
    save_memory(host, seen, ("read_at", "under", "refused", "windows_trend"))
    return refusal, notes


def windows_watch(cfg, st, now):
    """Each tick, the Windows side of every WSL host with live workers is read and its
    free memory added to the host's trend. Under windows_floor_gb it is one STUCK per
    stretch under the floor, with the trend, so workers can be closed before the VM
    runs out; the stretch ends once a reading is MEMORY_RESUME_GB above the floor."""
    data = memory_store()
    hosts = sorted({r["host"] for r in st["workers"].values() if holds_slot(r)})
    for host in hosts:
        seen = data.setdefault(host, {})
        free, side, side_floor = windows_reading(cfg, host, seen, now)
        if free is None:
            continue
        low = seen.get("windows_low")
        if low and free >= side_floor + MEMORY_RESUME_GB:
            seen.pop("windows_low")
            continue
        if free >= side_floor:
            continue
        since = seen.setdefault("windows_low", int(now))
        count = sum(r["host"] == host and holds_slot(r) for r in st["workers"].values())
        text = (
            f"STUCK {host}: its Windows side {side} has {free:.1f} GB memory free, under its "
            f"floor of {side_floor:g} GB, trend {trend_text(seen['windows_trend'])}. Its "
            f"{count} workers keep running and no worker starts there"
        )
        emit(st, f"{host}|windows-low|{since}", text)
    for host in hosts:
        save_memory(host, data[host], ("windows_trend", "windows_low"))


def windows_trend(host):
    """The host's recorded Windows-side trend as text, or ""."""
    points = (memory_store().get(host) or {}).get("windows_trend") or []
    return f". Its Windows side last read {trend_text(points)}" if points else ""


def account_row(cfg, account_id):
    return next((row for row in cfg["account"] if row["id"] == account_id), None)


def account(cfg, account_id, host):
    for row in cfg["account"]:
        if row["id"] == account_id and row["host"] == host:
            return row
    return None


# Ledger


def ledger():
    root = os.environ.get("CONCORD_REPO_ROOT")
    if not root:
        sys.exit("set CONCORD_REPO_ROOT to the ledger folder (ledger-init prints it)")
    return Path(root).expanduser().resolve()


# A ledger read another writer's lock refuses is tried LEDGER_TRIES times, each waiting
# LEDGER_TIMEOUT seconds in SQLite and LEDGER_WAIT between tries.
LEDGER_TRIES, LEDGER_TIMEOUT, LEDGER_WAIT = 3, 5.0, 1.0


def ledger_read(root, read):
    """read(connection) in one read transaction on the store opened read-only, tried again
    while another writer holds it locked; the last lock error is raised."""
    db = root / ".concord" / "concord.db"
    for attempt in range(1, LEDGER_TRIES + 1):
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=LEDGER_TIMEOUT)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            return read(con)
        except sqlite3.OperationalError as error:
            busy = (error.sqlite_errorname or "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED"))
            if not busy or attempt == LEDGER_TRIES:
                raise
        finally:
            con.close()
        time.sleep(LEDGER_WAIT)


def read_tickets(root):
    """Tickets keyed by (project, task_id), the store's own key, each with its updates."""
    return ledger_read(root, tickets_from)


def tickets_from(con):
    tickets = {
        (row["project"], row["task_id"]): {**dict(row), "updates": []}
        for row in con.execute(
            "SELECT project, task_id, title, status, agent_id, assigned_agent_id,"
            " notes, updated_at FROM tasks"
        )
    }
    for row in con.execute(
        "SELECT id, project, task_id, kind, content, agent, superseded_by,"
        " created_at FROM task_updates ORDER BY id"
    ):
        ticket = tickets.get((row["project"], row["task_id"]))
        if ticket:
            ticket["updates"].append(dict(row))
    return tickets


def waiting(ticket):
    """The owner's latest live update, when it is a `Waiting on:` blocker note."""
    mine = [
        u
        for u in ticket["updates"]
        if u["agent"] == ticket["agent_id"] and u["superseded_by"] is None
    ]
    last = mine[-1] if mine else None
    if last and last["kind"] == "blocker" and last["content"].startswith("Waiting on:"):
        return last
    return None


def wait_answered(st, tid, ticket, tickets, blocker):
    """True when something after the wait note answers it: a verdict by someone other
    than the owner, a review request on the coder's ticket a reviewer waits on, a wake
    dev-auto typed for this note, or, for a reviewer's wait on its coder's ticket while that
    ticket is done, the wake for its completion."""
    if answers_wait(blocker, verdicts(ticket)):
        return True
    coder = tid.endswith("-review") and tickets.get(
        (ticket.get("project", ""), tid.removesuffix("-review"))
    )
    if coder and review_requests(coder, blocker["id"], coder["agent_id"]):
        return True
    on_coder = coder and wait_target(blocker) == f"ticket:{coder['task_id']}"
    if on_coder and coder["status"] in DONE and f"{tid}|coder-done" in st["sent"]:
        return True
    key = f"{tid}|wake|{blocker['id']}"
    return any(k == key or k.startswith(f"{key}|") for k in st["sent"])


def wait_target(note):
    words = note["content"].removeprefix("Waiting on:").split()
    target = words[0].rstrip(".,;") if words else ""
    return target if target.startswith("ticket:") else target.lower()


def latest(ticket, prefix, since):
    """The newest update after id `since` whose content starts with prefix."""
    found = [
        u
        for u in ticket["updates"]
        if u["id"] > since and u["content"].startswith(prefix)
    ]
    return found[-1] if found else None


def alike(a, b):
    """True when two SHAs, full or short, can name one commit."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def verdict(update):
    """(PUSH or FIX, its SHA or "") when the update's first line is a review verdict,
    `REVIEW VERDICT: PUSH <sha>` or `REVIEW VERDICT: FIX <sha>` word for word from its
    first character, else None."""
    line = update["content"].split("\n", 1)[0]
    words = line.split()
    if not line.startswith("REVIEW VERDICT: ") or words[2:3] not in (["PUSH"], ["FIX"]):
        return None
    sha = (words[3:4] or [""])[0]
    return words[2], sha if SHA.fullmatch(sha) else ""


def verdicts(ticket):
    """(update, PUSH or FIX, SHA) for each unsuperseded verdict by someone other than the
    ticket's owner, oldest first."""
    return [
        (u, *found)
        for u in ticket["updates"]
        if u["agent"] != ticket["agent_id"]
        and u.get("superseded_by") is None
        and (found := verdict(u))
    ]


def answers_wait(blocker, live):
    """True when one of the live verdicts (see verdicts) after the wait note answers it:
    one naming the SHA a review wait asks about, or any when the note names none."""
    asked = next((s for line in blocker["content"].splitlines() if (s := review_request(line))), None)
    return any(u["id"] > blocker["id"] and (not asked or alike(asked, s)) for u, _, s in live)


def standing_pushes(ticket):
    """(update, SHA) for each live PUSH naming a SHA that no later live FIX naming that
    SHA withdraws on this ticket."""
    live = verdicts(ticket)
    return [
        (u, sha)
        for u, kind, sha in live
        if kind == "PUSH"
        and sha
        and not any(k == "FIX" and v["id"] > u["id"] and alike(s, sha) for v, k, s in live)
    ]


def review_request(line):
    """The SHA a review request line names, or None when the line is no request: a line
    whose first words are REVIEW READY and a SHA, or a review-wait line that names
    exactly one full or short SHA, REVIEW READY or not. A digit-only run id, date or PR
    number is not a SHA."""
    if line.startswith("REVIEW READY"):
        words = line.split()
        found = words[:2] == ["REVIEW", "READY"] and len(words) > 2 and SHA.fullmatch(words[2])
        return words[2] if found else None
    if not line.startswith("Waiting on:"):
        return None
    if wait_target({"content": line}) != "review":
        return None
    # One SHA may be written short and long; any two that are not prefixes conflict.
    # A digit-only run id, date or PR number is not a SHA: it needs a letter a-f.
    found = sorted(
        {
            token
            for token in re.findall(r"\b[0-9a-f]{7,40}\b", line)
            if re.search(r"[a-f]", token)
        },
        key=len,
    )
    return found[-1] if found and all(found[-1].startswith(s) for s in found) else None


def open_request(ticket):
    """The coder's newest review request's SHA, when no live verdict by anyone else
    answers it, with the first request for that SHA since its last verdict: (update,
    SHA), or None."""
    requests = review_requests(ticket, 0, ticket.get("agent_id"))
    if not requests:
        return None
    sha = requests[-1][1]
    floor = max((u["id"] for u, _, s in verdicts(ticket) if alike(sha, s)), default=0)
    # Requests for one SHA since its last verdict are one request, keyed by the first.
    first = next((u for u, s in requests if u["id"] > floor and alike(s, sha)), None)
    return (first, sha) if first else None


def unreviewed(st, tickets):
    """One STUCK per coder review request whose review ticket is open while no record
    of it runs a reviewer: none was launched, or its terminal is gone. A review ticket
    claimed by an id no record holds, such as the orchestrator's, is worked outside
    dev-auto and raises nothing."""
    ours = {r.get("agent_id") for r in st["workers"].values()}
    for tid, rec in list(st["workers"].items()):
        if tid.endswith("-review"):
            continue
        project = rec.get("project", "")
        coder = tickets.get((project, tid))
        review = tickets.get((project, f"{tid}-review"))
        if not coder or not review or coder["status"] in DONE or review["status"] in DONE:
            continue
        if review["status"] == "active" and review.get("agent_id") not in ours:
            continue
        found = open_request(coder)
        reviewer = st["workers"].get(f"{tid}-review")
        if not found or (reviewer and holds_slot(reviewer)):
            continue
        posted, sha = found
        text = f"STUCK {tid}: review requested, no reviewer running"
        detail = f"REVIEW READY {sha}, update {posted['id']}; {tid}-review has no live reviewer."
        emit(st, f"{tid}|no-reviewer|{posted['id']}", text, note_on=tid, detail=detail)


def review_requests(ticket, since, agent):
    """(update, SHA) for each review request line by agent after id `since`, oldest
    first."""
    return [
        (u, sha)
        for u in ticket["updates"]
        if u["id"] > since and u["agent"] == agent and u.get("superseded_by") is None
        for line in u["content"].splitlines()
        if (sha := review_request(line)) is not None
    ]


def concord_call(tool, args, attempts=8, agent=None):
    """Call one Concord tool as the supervisor, or as agent, retrying while the store is
    locked. Concord acts as the env identity and ignores an agent_id argument."""
    env = {
        **os.environ,
        **QUIET,
        "CONCORD_REPO_ROOT": str(ledger()),
        "CONCORD_AGENT_ID": agent or SUPERVISOR,
        "CONCORD_AGENT_KIND": "script",
    }
    hello = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "dev-auto", "version": "1"},
    }
    messages = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": hello},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    ]
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    for attempt in range(1, attempts + 1):
        done = subprocess.run(
            ["concord-mcp"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        reply = {}
        for line in done.stdout.splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == 1:
                reply = message
        if not reply:
            raise RuntimeError(f"concord-mcp gave no reply to {tool}")
        result = reply.get("result") or {"isError": True}
        text = "".join(part.get("text", "") for part in result.get("content", []))
        if not result.get("isError"):
            return text
        text = text or str(reply.get("error"))
        if "database is locked" not in text or attempt == attempts:
            raise RuntimeError(f"{tool}: {text}")
        time.sleep(min(2.0, 0.05 * 2**attempt) * (0.5 + random.random()))


def note(ticket, text):
    """Record a supervisor action on the ticket, as full-auto's progress log did."""
    content = text if text.startswith("dev-auto:") else f"dev-auto: {text}"
    try:
        concord_call(
            "update_work", {"task_id": ticket, "kind": "finding", "content": content}
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"ledger note failed for {ticket}: {error}")


# State


class State(dict):
    """The supervisor state. save() replaces the file whole, so a step is recorded before
    the action it guards and a crash never repeats that action."""

    def __init__(self, data, path):
        super().__init__(data)
        self.path = path
        # Set when an orchestrator event could not be delivered; it holds the check-in.
        self.held = False
        # Ticket id -> status() arguments this tick; tick() records them on the workers.
        self.statuses = {}
        # Set once a wake's recheck has waited: the lock is held for one wait at most.
        self.waited = False
        global USAGE_RESETS, USAGE_FULL
        USAGE_RESETS = self.setdefault("usage_resets", {})
        USAGE_FULL = self.setdefault("usage_full", {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=1, sort_keys=True) + "\n")
        tmp.replace(self.path)


@contextmanager
def state(root):
    """The state under an exclusive lock, so two ticks or a tick and a launch never overlap."""
    folder = root / "supervisor"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / "state.json"
        # Only a missing file starts empty: an unreadable one stops before the save below
        # would replace its records.
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as error:
            sys.exit(f"state file {path} cannot be read: {error}; nothing was changed")
        if not isinstance(data, dict):
            sys.exit(f"state file {path} holds no JSON object; nothing was changed")
        st = State(data, path)
        st.setdefault("workers", {})
        st.setdefault("sent", {})
        st.setdefault("orchestrator", None)
        st.setdefault("relay", {"used": False})
        st.setdefault("stopped", False)
        # Account id -> reset epoch, from limit screens: Cursor's use shows nowhere else.
        st.setdefault("limits", {})
        try:
            yield st
        finally:
            st.save()


# Sent keys of orchestrator alerts tied to a ticket update or a head: a dropped record
# keeps them, so a reopened ticket's same wait or a PR on the same red head is no new event.
DURABLE = ("needs", "wait", "cleared", "orch-review", "pr-red", "refused", "migration", "unpushed")


def drop(st, ticket):
    st["workers"].pop(ticket, None)
    for key in [k for k in st["sent"] if k.startswith(f"{ticket}|")]:
        if key.split("|")[1] not in DURABLE:
            del st["sent"][key]


def retire(st, tid, rec, terminals, now):
    """Drop a finished record, keeping what ties a pane Orca revives later to its ticket.
    The panes already listed in its worktree were not revived later: their handles and
    ptyIds go into the host's seen panes, as Orca may list such a pane again under a new
    handle or in another worktree. Each retirement there renews every seen pane, so none
    is forgotten while an entry retired after it was seen still ties panes."""
    keep = ("project", "host", "worktree", "branch", "agent", "session", "terminal", "handles", "pty")
    entry = {"ticket": tid, "at": now, **{k: rec.get(k) for k in keep}}
    entry["owned"] = rec.get("owns_worktree") is not False
    seen = st.setdefault("seen_panes", {}).setdefault(rec["host"], {})
    listed = {
        key
        for h, r in terminals.items()
        if same_path(r.get("worktreePath"), rec["worktree"], rec["host"])
        for key in (h, r.get("ptyId"))
        if key
    }
    seen.update({key: now for key in {*seen, *listed}})
    st.setdefault("retired", []).append(entry)
    # Its cleanup removed the worktree, found it gone, or hands it to the record it shares.
    st.setdefault("worktrees", {}).pop(f"{rec['host']}|{rec['worktree']}", None)
    drop(st, tid)


def remember_worktree(st, rec, now):
    """Keep a worktree dev-auto owns past its record, so sweep_worktrees removes it once
    it is merged even when the record was dropped or replaced elsewhere."""
    key = f"{rec['host']}|{rec['worktree']}"
    known = st.setdefault("worktrees", {})
    entry = known.setdefault(key, {"host": rec["host"], "worktree": rec["worktree"], "at": int(now)})
    entry.update(
        ticket=rec["ticket"],
        project=rec.get("project", ""),
        owned_branch=rec.get("owned_branch"),
        owned_common=rec.get("owned_common"),
    )


# Launch


def slug(text):
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-")


def lane_slug(ticket):
    title = ticket["title"]
    lane = title.split(" / ", 1)[1] if " / " in title else ticket["task_id"]
    return slug(lane) or "lane"


def worktree_branch(host, worktree):
    """The branch checked out in the worktree, or None when git cannot name one."""
    code, out = run_on(host, worktree, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = out.strip()
    return branch if code == 0 and branch else None


def worktree_common(host, worktree):
    """The worktree's repository (git's common dir), or None when git cannot name it."""
    argv = ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
    code, out = run_on(host, worktree, argv)
    common = (out.strip().splitlines() or [""])[-1]
    return common if code == 0 and common else None


def branch_fields(notes):
    """The brief's `Branch:` declarations, at a line start or after the line's leading
    `Field: value ·` fields, never in prose: matches whose group 1 is the branch and group
    2 the `from` base, if named."""
    return list(
        re.finditer(
            r"^(?:[A-Z][\w ]*:[^·\n]*·\s*)*Branch:\s*`([^`]+)`(?:[^\n·]*?\bfrom\s+`([^`]+)`)?",
            notes,
            re.MULTILINE,
        )
    )


def own_branches(ticket, tickets):
    """task/<feature>/<lane>, and the branch its brief's `Branch:` field names; a review
    ticket's are its coder ticket's."""
    if ticket["task_id"].endswith("-review"):
        coder = ticket["task_id"].removesuffix("-review")
        ticket = tickets.get((ticket["project"], coder)) or ticket
    title = ticket["title"]
    feature = slug(title.split(" / ", 1)[0]) if " / " in title else ""
    named = branch_fields(ticket.get("notes") or "")
    return {f"task/{feature}/{lane_slug(ticket)}", *([named[0].group(1)] if named else [])}


def check_features(rec, ticket, tickets):
    """CHECK's features for a record or a remembered worktree: those of the task branches
    it may hold, the branch its launch owned, else its ticket's own branches."""
    owned = rec.get("owned_branch")
    branches = {owned} if owned else own_branches(ticket, tickets) if ticket else set()
    return " ".join(sorted({b.split("/")[1] for b in branches if re.fullmatch(r"task/[^/]+/.+", b)}))


def owns_checkout(rec, branch, common):
    """The checkout is the branch of the repository the launch owned. A record from before
    launches recorded them proves nothing, so it is kept: its launch may have owned another
    branch, and its path may now hold another repository."""
    owned = rec.get("owned_branch"), rec.get("owned_common")
    return all(owned) and owned == (branch, common)


# Run just before removal: the checkout CHECK judged, still clean, as "branch head common".
IDENTITY = (
    "b=$(git rev-parse --abbrev-ref HEAD) && h=$(git rev-parse HEAD) && "
    "c=$(git rev-parse --path-format=absolute --git-common-dir) && "
    's=$(git status --porcelain --untracked-files=all) && [ -z "$s" ] && echo "$b $h $c"'
)


def removable_now(host, worktree, branch, head, common):
    """At the removal boundary, after its shells closed: the worktree still has the clean
    checkout CHECK judged, and a fresh listing shows no terminal there at all, the
    record's own handle listed again included."""
    code, out = run_on(host, worktree, ["sh", "-c", IDENTITY])
    if code != 0 or (out.strip().splitlines() or [""])[-1] != f"{branch} {head} {common}":
        return False
    fresh = listing(host)
    return fresh is not None and not any(
        same_path(r.get("worktreePath"), worktree, host) for r in fresh.values()
    )


def id_slug(ticket):
    """The title-based slug used by workers launched before ticket IDs were used."""
    title = ticket["title"]
    feature = slug(title.split(" / ", 1)[0]) if " / " in title else ""
    return "-".join(part for part in (feature, lane_slug(ticket)) if part)


def agent_id(cfg, agent, host, slug):
    prefix = "" if host == "local" else cfg["hosts"][host]["prefix"] + "-"
    full = f"{agent}:{prefix}{slug}"
    if len(full) <= 64:
        return full
    # A stable hash of the whole slug keeps long ids that differ near the end apart.
    return f"{full[:55]}-{hashlib.sha1(slug.encode()).hexdigest()[:8]}"


def ticket_agent_id(cfg, agent, host, tid):
    name = slug(tid)
    if name != tid:
        name += "-" + hashlib.sha1(tid.encode()).hexdigest()[:8]
    return agent_id(cfg, agent, host, f"ticket-{name}")


# The kinds concord-ssh-gate.sh accepts; it takes the kind from the agent id's prefix.
GATE_KINDS = ("claude", "codex", "cursor")


def mcp_server(cfg, root, rec):
    """The worker's concord-mcp server. Exits when that server would start without the
    ledger root, the agent id or the agent kind, so no terminal is created for it."""
    if rec["host"] != "local":
        if rec["agent"] not in GATE_KINDS or not rec["agent_id"].startswith(
            f"{rec['agent']}:"
        ):
            sys.exit(
                f"{rec['ticket']}: the ledger's ssh gate cannot start concord-mcp for "
                f"{rec['agent_id']} on {rec['host']}"
            )
        ssh = cfg["hosts"][rec["host"]]["ledger_ssh"]
        return {"command": ssh[0], "args": [*ssh[1:], f"concord-mcp {rec['agent_id']}"]}
    if not Path(root).is_absolute():
        sys.exit(
            f"{rec['ticket']}: the ledger root {str(root)!r} is not an absolute path"
        )
    env = {
        **QUIET,
        "CONCORD_REPO_ROOT": str(root),
        "CONCORD_AGENT_ID": rec["agent_id"],
        "CONCORD_AGENT_KIND": rec["agent"],
    }
    command = shutil.which("concord-mcp") or "concord-mcp"
    return {"command": command, "args": [], "env": env}


GOAL_WAIT = (
    "or record a new Waiting on: blocker note on it after this goal is set, then end "
    "your turn."
)
# Codex keeps pursuing a goal its model has not marked complete, so a goal met by a new
# note made it record one every few seconds until the goal stalled.
CODEX_GOAL_WAIT = (
    "or until it holds a Waiting on: blocker note that nothing has answered yet. Record "
    "such a note only when none is open, then mark this goal complete and end your turn."
)
# A Claude worker without its Concord tools can meet no other goal, so its goal hook would
# restart it for ever.
CLAUDE_CONCORD_LOST = (
    "If the Concord tools are unavailable, the goal is also met once you write the "
    "handoff to the file the handoff skill names for a session without a ticket and end "
    "your turn, without retrying them."
)


GOAL_DONE = "as its brief defines done, "
# The goal of a worker whose ticket has no feature branch, from its PUSH wake on: such
# coders parked at review_ready or an operator wait until the orchestrator shipped.
FINISH_DONE = (
    "as its brief defines done, which with no feature branch means its PR title and body "
    "recorded on it and the ticket completed with finish_work outcome complete, not "
    "REVIEW READY, a review_ready outcome or a wait for the orchestrator to push, "
)


def goal_line(tid, title=""):
    """The worker's goal. It is also met by a fresh Waiting on note, so a waiting worker's
    goal loop ends its turn; every wake sets the goal again."""
    name = f"{tid} ({title})" if title else tid
    return f"Finish Concord ticket {name} {GOAL_DONE}{GOAL_WAIT}"


def armed(rec, goal, text):
    """Text that sets this worker's goal, then says text."""
    if rec.get("finish"):
        goal = goal.replace(GOAL_DONE, FINISH_DONE)
    if rec["agent"] == "codex":
        # Callers such as emit build the goal without knowing the agent.
        goal = goal.replace(GOAL_WAIT, CODEX_GOAL_WAIT)
    if rec["agent"] == "claude":
        goal = goal.replace(GOAL_WAIT, f"{GOAL_WAIT} {CLAUDE_CONCORD_LOST}")
    if rec["agent"] == "cursor":
        return (
            f"{goal} Create this as your goal with your goal tool, update it as you "
            f"work, and stop only when it is met. {text}"
        )
    return f"/goal {goal} {text}"


# On every host, the wrapper that lets one heavy check run at a time.
CHECK_LOCK = "~/.agents/skills/dev-auto/scripts/host-check-lock"
CI_ONLY_CHECKS = (
    "Run all tests, lint, and typecheck only in GitHub Actions, including named integration "
    "and smoke tests. Do not run any of these checks on this machine."
)


def worker_checks_in_ci(rec):
    # Only a known non-HR Relay origin permits local integration checks.
    worktree = rec.get("worktree")
    origin = origin_url(worktree, host=rec["host"]) if worktree else None
    repo = gh_repo(origin.lower()) if origin else None
    return repo is None or repo == "github.com/julianthant/hr-relay"


def brief(rec, ticket, situation=""):
    tid = ticket["task_id"]
    body = (
        f"{situation}Claim ticket {tid} with start_work (task_id {tid}), read the brief "
        "in its notes, its latest handoff and updates (inspect_work), and load the "
        "skills on its Skills: line. Record progress on the ticket. When you wait, "
        "record a blocker note starting 'Waiting on: review', 'Waiting on: ci', "
        "'Waiting on: operator' or 'Waiting on: ticket:<id>', end your turn and stay "
        "quiet until dev-auto wakes you; never repeat a Waiting on note. "
    )
    if worker_checks_in_ci(rec):
        body += CI_ONLY_CHECKS
    else:
        body += (
            "Run no typecheck, lint, unit, architecture or whole-suite tests on this machine; "
            "GitHub Actions runs those. Run only the named integration test files the "
            "ticket needs, one file at a time, each through "
            f"{CHECK_LOCK} <command>, which waits while another check runs on this host. "
            "A busy machine or a waiting check is never an operator wait."
        )
    return armed(rec, goal_line(tid, ticket["title"]), body)


def worker_command(cfg, root, rec, row, text, resume=None):
    """The shell line that starts this worker with Concord registered for this launch."""
    q = shlex.quote
    server = mcp_server(cfg, root, rec)
    # The worker's own hooks act only in a session that carries its ledger identity.
    me = f"CONCORD_AGENT_ID={q(rec['agent_id'])} "
    if rec["agent"] == "claude":
        # Not --strict-mcp-config: that would drop the worker's other MCP servers.
        argv = [
            "claude",
            "--mcp-config",
            json.dumps({"mcpServers": {"concord": server}}),
        ]
        argv += ["--model", rec["model"]]
        if rec.get("effort"):
            argv += ["--effort", rec["effort"]]
        argv += ["--resume", resume] if resume else ["--session-id", rec["session"]]
        argv += ["--dangerously-skip-permissions", text]
        home = f"CLAUDE_CONFIG_DIR={q(row['home'])} " if row["home"] else ""
        return f"{me}{home}{shlex.join(argv)}"
    if rec["agent"] == "sdk":
        # The runner reads the key from key_file; only that path is on this line.
        argv = ["uv", "run", "--quiet", "--no-project", "--with", "claude-agent-sdk"]
        argv += [str(HERE / "sdk_worker.py"), "--ticket", rec["ticket"]]
        argv += ["--account", row["id"]]
        argv += ["--model", rec["model"]]
        if rec.get("effort"):
            argv += ["--effort", rec["effort"]]
        argv += ["--key-file", str(Path(row["key_file"]).expanduser())]
        argv += ["--ticket-usd", f"{row['ticket_usd']:g}"]
        argv += ["--monthly-usd", f"{row['monthly_usd']:g}"]
        argv += ["--spend", str(spend_file(root)), "--mcp", json.dumps(server), text]
        home = f"CLAUDE_CONFIG_DIR={q(row['home'])} " if row["home"] else ""
        return f"{me}{home}{shlex.join(argv)}"
    if rec["agent"] == "codex":
        argv = ["codex", "-m", rec["model"]]
        if rec.get("effort"):
            argv += ["-c", f"model_reasoning_effort={rec['effort']}"]
        argv += ["-c", 'service_tier="default"']
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
        argv += ["--dangerously-bypass-hook-trust"]
        # A repo the account never trusted opens a trust prompt; trusting it here
        # instead writes nothing to the account's config.
        trust = f'{json.dumps(rec["worktree"])}={{trust_level="trusted"}}'
        argv += ["-c", f"projects={{{trust}}}"]
        # Codex gives MCP servers only the env its config names, so the env goes in -c.
        argv += ["-c", f"mcp_servers.concord.command={json.dumps(server['command'])}"]
        argv += ["-c", f"mcp_servers.concord.args={json.dumps(server['args'])}"]
        if server.get("env"):
            pairs = ", ".join(
                f"{k} = {json.dumps(v)}" for k, v in server["env"].items()
            )
            argv += ["-c", f"mcp_servers.concord.env={{{pairs}}}"]
        argv.append(text)
        home = f"CODEX_HOME={q(row['home'])} " if row["home"] else ""
        return me + home + shlex.join(argv)
    if rec["agent"] == "grok":
        # Grok's one user-scope concord server has no identity; it inherits this env.
        # A shared leader process would spawn it with the leader's env instead.
        env = " ".join(f"{k}={q(v)}" for k, v in server["env"].items())
        argv = ["grok", "-m", rec["model"]]
        if rec.get("effort"):
            argv += ["--effort", rec["effort"]]
        argv += ["--permission-mode", "bypassPermissions", "--trust", "--no-leader"]
        argv.append(text)
        return f"{env} {shlex.join(argv)}"
    # Cursor has no MCP flag; a throwaway plugin dir carries the server for this launch.
    plugin = json.dumps({"name": "concord-ledger", "mcpServers": {"concord": server}})
    argv = ["cursor-agent", "--model", rec["model"], "--force", "--trust"]
    argv += ["--approve-mcps"]
    return (
        'd=$(mktemp -d) && mkdir "$d/.cursor-plugin" && printf %s '
        f'{q(plugin)} > "$d/.cursor-plugin/plugin.json" && '
        f'{me}{shlex.join(argv)} --plugin-dir "$d" {q(text)}'
    )


def same_path(a, b, host):
    """The same folder; on this Mac also through symlinks, as /tmp is /private/tmp."""
    if not a:
        return False
    if host == "local":
        return os.path.realpath(a) == os.path.realpath(b)
    return PurePosixPath(a) == PurePosixPath(b)


def listing(host):
    """{handle: row} for every terminal on that host; None when the list failed or was cut short."""
    listed = orca(host, "terminal", "list", "--limit", "10000")
    if listed is None or listed.get("truncated"):
        return None
    return {row["handle"]: row for row in listed.get("terminals") or []}


def close_terminal(host, handle):
    """Close a terminal with its tab, so Orca keeps no resume record a restart would bring
    back. A tab that holds another terminal, or one that could not be listed, loses only
    this pane. The result is orca()'s: None when Orca refused."""
    terminals = listing(host)
    if terminals is None:
        print(f"{handle}: its tab could not be listed; closed the pane only")
        return orca(host, "terminal", "close", "--terminal", handle)
    tab = (terminals.get(handle) or {}).get("tabId")
    others = [h for h, r in terminals.items() if h != handle and tab and r.get("tabId") == tab]
    if others:
        print(f"{handle}: its tab also holds {' '.join(others)}; closed the pane only")
        return orca(host, "terminal", "close", "--terminal", handle)
    return orca(host, "terminal", "close", "--terminal", handle, "--tab")


# Seconds an sdk runner gets to exit on end of input before cleanup keeps its terminal.
SDK_EXIT = 20


def sdk_idle(lines):
    """The runner waits at its prompt: IDLE, then only resets it ignored, each echoed or not
    after a prompt, then the prompt."""
    prompt = SDK.PROMPT.strip()
    ignored = {f"· sdk worker: ignored {r}" for r in SDK.RESETS}
    if not lines or not lines[-1].startswith(prompt):
        return False
    for line in reversed(lines[:-1]):
        if line == SDK.IDLE:
            return True
        text = line.removeprefix(prompt).strip()
        if text not in ignored and (text.split() or [""])[0] not in SDK.RESETS:
            return False
    return False


def end_runner(st, tid, rec):
    """True once a done ticket's sdk runner has exited, to its shell or with its terminal,
    so a close cannot end its session. A close sends SIGHUP, and a session ended that way
    has no result and leaves its spend held open. An idle runner gets end of input, which
    releases its reservation on proof; a busy one is left for a later tick. A tick waits
    for one runner at most, so the lock is held for one wait; a runner still up SDK_EXIT
    seconds after end of input is one STUCK, and its terminal stays."""
    host, handle = rec["host"], rec["terminal"]

    def gone(screen):
        return bool(screen) and (screen["status"] == "exited" or bare_shell(screen))

    screen = read_screen(host, handle)
    if gone(screen):
        return True
    if "ended" not in rec:
        if not screen or screen["source"] == "screen-unavailable" or not sdk_idle(screen["lines"]):
            return False
        # Ctrl-U first: Ctrl-D ends input only on an empty line. send() types no Ctrl-D.
        for key in ("\x15", "\x04"):
            sent = orca(host, "terminal", "send", "--terminal", handle, "--text", key)
            if sent is None or (sent.get("send") or {}).get("accepted") is False:
                return False
        rec["ended"] = time.time()
        st.save()
    deadline = rec["ended"] + SDK_EXIT
    if not st.waited and time.time() < deadline:
        st.waited = True
        while time.time() < deadline:
            time.sleep(1)
            if gone(read_screen(host, handle)):
                return True
    if time.time() >= deadline:
        text = (
            f"STUCK {tid} {rec['agent_id']}: its runner did not exit {SDK_EXIT}s after end "
            "of input, and closing its terminal would leave its spend reservation held open. "
            "Terminal kept"
        )
        emit(st, f"{tid}|sdk-exit|{rec['ended']}", text, note_on=tid)
    return False


def unproved_terminal(st, tid, rec, terminals):
    """For a record with no terminal, whose create gave no usable answer or was cut short:
    the first terminal in its worktree that no other record, its recorded shells or the
    orchestrator holds, which may be its worker whether or not an agent is listed yet."""
    if rec.get("terminal"):
        return None
    held = {r.get("terminal") for t, r in st["workers"].items() if t != tid and r["host"] == rec["host"]}
    held |= {s.get("terminal") for s in rec.get("shells", [])}
    pointer = st["orchestrator"]
    if pointer and pointer["host"] == rec["host"]:
        held.add(pointer["terminal"])
    return next(
        (
            h
            for h, r in terminals.items()
            if h not in held and same_path(r.get("worktreePath"), rec["worktree"], rec["host"])
        ),
        None,
    )


def possible_session(st, tid, rec, terminals, now):
    """With the record's own pane gone: a terminal in its worktree that no other record,
    its recorded shells or the orchestrator holds, and that may be its worker under a new
    handle and pty: its agent listed, or none listed and no proof it exited."""
    held = {r.get("terminal") for t, r in st["workers"].items() if t != tid and r["host"] == rec["host"]}
    held |= {s.get("terminal") for s in rec.get("shells", [])}
    pointer = st["orchestrator"]
    if pointer and pointer["host"] == rec["host"]:
        held.add(pointer["terminal"])
    for handle, row in terminals.items():
        if handle in held or not same_path(row.get("worktreePath"), rec["worktree"], rec["host"]):
            continue
        ident = row.get("agentIdentity")
        if ident == rec["agent"]:
            return row
        if not ident and not exited(rec, row, read_screen(rec["host"], handle), now):
            return row
    return None


def unowned_live_text(tid, rec, why, handle):
    return (
        f"STUCK {tid} {rec['agent_id']}: {why} and its terminal is gone, but {handle} in its "
        "worktree may still be that session. It is not restarted while that terminal is open"
    )


def no_terminal_refusal(tid, handle):
    return (
        f"{tid}: its record has no terminal, as its last create gave no usable answer or "
        f"was cut short, and {handle} is open in its worktree; close it first to replace "
        "the worker"
    )


def own_row(rec, terminals):
    """The record's listed row: by its handle, or under a new handle by the ptyId Orca
    gave its terminal at launch. Nothing else proves a terminal is dev-auto's own."""
    row = terminals.get(rec.get("terminal"))
    if row or not rec.get("pty"):
        return row
    return next((r for r in terminals.values() if r.get("ptyId") == rec["pty"]), None)


# Runs on the host that will start Claude. An empty home is that host's default profile
# ($HOME/.claude/settings.json and $HOME/.claude.json). A set home is the account profile,
# the same files local launches write: <home>/settings.json and <home>/.claude.json.
CLAUDE_SEED = r"""
settings=$1
state=$2
wt=$3
only_new=$4
case $settings in
  \$HOME/*) settings="$HOME/${settings#\$HOME/}" ;;
esac
case $state in
  \$HOME/*) state="$HOME/${state#\$HOME/}" ;;
esac
exec python3 -c '
import json, os, sys, tempfile
from pathlib import Path
settings_path, state_path, worktree, only_new = sys.argv[1:]
settings_file, state_file = Path(settings_path), Path(state_path)
settings_file.parent.mkdir(parents=True, exist_ok=True)
state_file.parent.mkdir(parents=True, exist_ok=True)

def load(path):
    # Only a missing file is new. One that does not read as an object, as while Claude
    # rewrites it, is left as it is.
    # With only_new, an existing file may have a writer no scan sees, and is left too.
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    if only_new:
        return None
    return data if isinstance(data, dict) else None

def save(path, before, data):
    # Written only on a change, and whole: a reader never sees half of it.
    if json.dumps(data, sort_keys=True) == before:
        return
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(handle, "w") as out:
        out.write(json.dumps(data, indent=2) + "\n")
    if only_new:
        # Created only if still absent: a session that wrote it meanwhile keeps its bytes.
        try:
            os.link(temp, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(temp)
        return
    if path.exists():
        os.chmod(temp, path.stat().st_mode & 0o777)
    os.replace(temp, path)

settings = load(settings_file)
if settings is not None:
    before = json.dumps(settings, sort_keys=True)
    settings.setdefault("theme", "dark")
    settings["skipDangerousModePermissionPrompt"] = True
    settings.setdefault("tui", "default")
    save(settings_file, before, settings)
state = load(state_file)
if state is not None:
    before = json.dumps(state, sort_keys=True)
    state["hasCompletedOnboarding"] = True
    projects = state.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(worktree)
    if not isinstance(project, dict):
        project = {}
    project["hasTrustDialogAccepted"] = True
    projects[worktree] = project
    state["projects"] = projects
    save(state_file, before, state)
' "$settings" "$state" "$wt" "$only_new"
"""


def claude_seed_paths(home):
    """Settings file and claude.json for this profile. Empty home is the host default."""
    if home:
        return f"{home}/settings.json", f"{home}/.claude.json"
    return "$HOME/.claude/settings.json", "$HOME/.claude.json"


def seed_claude_launch(row, worktree, host):
    """Write the Claude settings that keep first-run screens from appearing.

    theme and tui are set only when missing. hasCompletedOnboarding skips onboarding,
    which is the theme picker, the security notes, and the login step. Trust is stored
    per worktree. A login screen that still appears is never answered. A remote profile
    is written on that host, not on this machine, and only where its files are missing:
    no scan sees a session there that may be writing them.
    """
    settings, state = claude_seed_paths(row.get("home") or "")
    argv = ["sh", "-c", CLAUDE_SEED, "seed", settings, state, worktree, "" if host == "local" else "1"]
    if host == "local":
        run(argv, cwd=worktree, timeout=30)
        return
    run_on(host, worktree, argv)


def profile_in_use(cfg, st, rec, row):
    """Another Claude session may write this account's profile on this host: another record
    on it not proved gone (a lost one, or a finished one whose close was refused, may still
    run), the orchestrator on it, a live session no record holds, or this Mac's default
    profile, whose sessions no scan sees. Closed at a stop, or exited, is proof."""
    gone = ("stopped", "held", "exited_at")
    if any(
        t != rec["ticket"]
        and r.get("account") == row["id"]
        and r["host"] == rec["host"]
        and not any(r.get(key) for key in gone)
        for t, r in st["workers"].items()
    ):
        return True
    if rec["host"] == "local" and not row.get("home"):
        return True
    pointer = st["orchestrator"] or {}
    if pointer.get("account") == row["id"] and pointer.get("host") == rec["host"]:
        return True
    return rec["host"] == "local" and bool(foreign(cfg, st["workers"]).get(row["id"]))


def missing(rec):
    """The keys a saved record lacks that its readers index. A launch stopped in its
    update once saved a record without them; tick reports it and every reader skips it."""
    return [key for key in ("account", "launched_at") if key not in rec]


def holds_slot(rec):
    """The record's worker may be running, so it holds a slot on its account on every host
    that shares the id: not finished, not stopped at a handoff, and its terminal not gone
    (lost, exited and held for a new launch, or exited with no slot to relaunch it in)."""
    if missing(rec):
        return False
    return not any(rec.get(key) for key in ("done", "stopped", "lost", "held", "exited_at"))


def start(cfg, root, st, rec, row, text, now, resume=None, auto_fill=False):
    """Open the worker's terminal. The record is saved first without a handle, so a crash
    after the create leaves a record tick reports and leaves alone, never a second launch.
    Its account and launch time are set before fresh_tools, which saves: an update stopped
    midway leaves a whole record. True when started, False when Orca refused the create,
    None when its answer never came: the terminal may run, so the record stays."""
    previous = rec
    if resume:
        rec = rec.copy()
        if worker_checks_in_ci(rec):
            text = f"{text} {CI_ONLY_CHECKS}"
    rec.update(terminal=None, account=row["id"], launched_at=now)
    for key in ("pty", "nudged_at", "limit_reset", "limit_full", "limit_answered", "stopped", "stopping", "handoff_ask", "exited_at", "context"):
        rec.pop(key, None)
    if not resume:
        # The old pane's pending wakes; their sent keys and delivered lines stay.
        for key in ("unshown", "wake", "missed", "context_hit"):
            rec.pop(key, None)
    fresh_tools(st, rec["ticket"], rec["host"], rec["worktree"], rec["agent"], now)
    if rec["agent"] == "claude":
        if profile_in_use(cfg, st, rec, row):
            # A live session may be writing these files; its own first launch seeded them,
            # and a trust prompt for this worktree is answered on its screen.
            print(f"{rec['ticket']}: {row['label']}'s profile is in use; its settings are left as they are")
        else:
            seed_claude_launch(row, rec["worktree"], rec["host"])
        if not resume:
            rec["session"] = str(uuid.uuid4())
            rec.pop("claude_input", None)
    command = worker_command(cfg, root, rec, row, text, resume)
    if auto_fill:
        require_fill_ready(root, rec["project"], rec["ticket"])
        if rec["owns_worktree"]:
            remember_worktree(st, rec, now)
    sent_at = time.time()
    if resume:
        # Resume callers have already closed the old pane or proved it gone.
        if defer_stale_claude(st, previous, f"{rec['ticket']}|resume|{resume}", text, sent_at, pane_closed=True):
            stop_worker(cfg, st, rec["ticket"], previous, CLAUDE_CUTOFF_STOP, None, now, None)
            return False
        previous.clear()
        previous.update(rec)
        rec = previous
    st.save()
    reply = orca_reply(
        rec["host"],
        "terminal",
        "create",
        "--worktree",
        f"path:{rec['worktree']}",
        "--title",
        rec["title"],
        "--command",
        command,
    )
    refused = reply is not None and not reply.get("ok", True)
    created = None if reply is None or refused else reply.get("result")
    handle = ((created or {}).get("terminal") or {}).get("handle")
    if not handle and refused:
        print(f"{rec['ticket']}: orca terminal create failed in {rec['worktree']}")
        return False
    if not handle:
        # No answer, or one without a handle: the terminal may have started.
        print(
            f"{rec['ticket']}: orca terminal create gave no usable answer in "
            f"{rec['worktree']}; its record is kept, as the terminal may have started"
        )
        return None
    rec["terminal"] = handle
    if rec["agent"] == "claude":
        # The accepted create dispatched the initial prompt with this session ID.
        rec["claude_input"] = [rec["session"], sent_at]
    pty = ((listing(rec["host"]) or {}).get(handle) or {}).get("ptyId")
    if pty:
        rec["pty"] = pty
    st.save()
    return True


# Tick


def run_on(host, worktree, argv, timeout=60):
    """Run argv in the worktree on that host: directly here, else in a probe terminal."""
    if host == "local":
        done = run(argv, cwd=worktree, timeout=timeout)
        return done.returncode, done.stdout
    marker = uuid.uuid4().hex
    begin, end = f"__DA_B_{marker}__", f"__DA_E_{marker}__"
    command = f"echo {begin}; {shlex.join(argv)} 2>&1; echo {end}$?"
    deadline = time.monotonic() + timeout
    created = orca(
        host,
        "terminal",
        "create",
        "--worktree",
        f"path:{worktree}",
        "--title",
        "dev-auto probe",
        "--command",
        command,
        timeout=timeout,
    )
    handle = ((created or {}).get("terminal") or {}).get("handle")
    if not handle:
        return (124 if time.monotonic() >= deadline else 127), ""
    try:
        cursor = None
        started = False
        output = []
        while (remaining := deadline - time.monotonic()) > 0:
            paging = ["--cursor", str(cursor)] if cursor is not None else []
            read = orca(
                host, "terminal", "read", "--terminal", handle, "--limit", "400",
                *paging, timeout=remaining,
            )
            if time.monotonic() >= deadline:
                break
            page = (read or {}).get("terminal") or {}
            if cursor is None:
                oldest = page.get("oldestCursor")
                if page.get("limited") and isinstance(oldest, int):
                    cursor = oldest
                    continue
                # Older Orca hosts may only have a tail preview.
                started, output = False, []
            for line in page.get("tail") or []:
                line = line.rstrip()
                if line == begin:
                    started, output = True, []
                elif started:
                    code = line.removeprefix(end)
                    if code != line and code.isdecimal():
                        return int(code), "\n".join(output)
                    output.append(line)
            next_cursor = page.get("nextCursor")
            if isinstance(next_cursor, int) and next_cursor != cursor:
                cursor = next_cursor
                if page.get("limited"):
                    continue
            time.sleep(max(0, min(0.5, deadline - time.monotonic())))
        return 124, ""
    finally:
        close_terminal(host, handle)


# Arrow keys for a Claude first-run dialog whose default is the refusing choice.
DIALOG_KEYS = frozenset({"\x1b[A", "\x1b[B"})
# Ctrl-U clears a stray character on the prompt. Esc is never sent: it interrupts a
# running turn, and no screen read tells every agent's running turn apart.
CLEAR_KEYS = frozenset({"\x15"})


def send(host, terminal, text, enter=True):
    """Return 'accepted' for an explicit acknowledgement, or a boolean for legacy replies."""
    if text not in DIALOG_KEYS and text not in CLEAR_KEYS and not SAFE.fullmatch(text):
        print(f"refused to type text a shell could run into {terminal}: {text!r}")
        return False
    args = ["terminal", "send", "--terminal", terminal, "--text", text]
    result = orca(host, *args, *(["--enter"] if enter else []))
    accepted = ((result or {}).get("send") or {}).get("accepted")
    return "accepted" if accepted is True else result is not None and accepted is not False


CLAUDE_INPUT_MAX_AGE = 3600
CLAUDE_CUTOFF_STOP = "Claude input cutoff"


def defer_stale_claude(st, rec, key, action, now=None, *, pane_closed=False):
    """Keep an action for the orchestrator when this Claude session cannot take input."""
    if rec["agent"] != "claude":
        return False
    session = rec.get("session")
    receipt = rec.get("claude_input")
    now = time.time() if now is None else now
    if (
        session
        and isinstance(receipt, list)
        and len(receipt) == 2
        and receipt[0] == session
        and type(receipt[1]) in (int, float)
        and 0 <= now - receipt[1] < CLAUDE_INPUT_MAX_AGE
    ):
        return False
    rec.pop("claude_input", None)
    if pane_closed:
        rec.pop("lost", None)
        rec["stopping"] = {"why": CLAUDE_CUTOFF_STOP, "handoff": None}
    tid = rec["ticket"]
    emit(
        st,
        f"{tid}|stale-claude|{session or rec.get('launched_at')}|{key}",
        f"FRESH SESSION {tid}: Claude on {rec['host']} terminal {rec.get('terminal')}, "
        f"session {session or 'unknown'}, has no proven message sent in the last hour. "
        f"Pending action {key}: {action} Read its transcript before arranging a fresh session.",
    )
    return True


def claude_keys(st, rec, key, action, keys, message=False):
    for text, enter in keys:
        at = time.time()
        if defer_stale_claude(st, rec, key, action, at):
            return "deferred"
        sent = send(rec["host"], rec["terminal"], text, enter=enter)
        if not sent:
            return False
        if message and enter and sent == "accepted":
            rec["claude_input"] = [rec["session"], at]
            st.save()
    return True


def read_screen(host, handle):
    read = orca(host, "terminal", "read", "--terminal", handle, "--screen")
    terminal = (read or {}).get("terminal")
    if not terminal:
        return None
    lines = [line.rstrip() for line in terminal.get("tail") or [] if line.strip()]
    return {
        "status": terminal.get("status"),
        "lines": lines,
        "source": terminal.get("source"),
    }


def overlay(lines, agent="codex"):
    """What sits in front of the agent's own prompt: "menu", "asking" or None. MENU is
    Codex's wording; on another agent's screen it is output, such as a quoted diff."""
    shown = "\n".join(lines[-15:])
    if agent == "codex" and all(text in shown for text in MENU):
        return "menu"
    return "asking" if ASKING.search(shown) and choice_list(lines[-15:]) else None


def unknown_menu(lines):
    """A switch-model menu whose wording no longer matches MENU, so "2" may not keep."""
    shown = "\n".join(lines[-15:])
    known = all(text in shown for text in MENU)
    return not known and any(text in shown for text in MENU) and choice_list(lines[-15:])


def codex_status(lines):
    """Codex's status line, the bottom one that shows the model, or None."""
    rows = reversed(lines)
    following = ""
    for line in islice(rows, 6):
        match = CODEX_STATUS.match(line)
        if not match:
            following = line + following
            continue
        # Wrapping can split /goal itself, and read_screen strips a trailing space.
        field = line[match.end():]
        if field == "/goal" or (field + following).startswith("/goal "):
            for previous in rows:
                if previous.lstrip().startswith(AGENT_PROMPTS):
                    break
                if CODEX_STATUS.match(previous):
                    return previous
        return line
    return None


def model_drift(rec, lines):
    """The model and effort Codex's status line shows, when they are not the launched ones."""
    status = codex_status(lines)
    if not status:
        return None
    model, effort = CODEX_STATUS.match(status).groups()
    if model.lower() == rec["model"].lower() and effort == (rec.get("effort") or effort):
        return None
    return f"{model} {effort}" if effort else model


NUMBERED = re.compile(r"\d+\.\s+\S")
# An unnumbered permission dialog answers Yes or No: "Yes", "No, exit", ...
ANSWER = re.compile(r"(?:Yes|No)\b")


def choice_list(lines):
    """True when a dialog's choice rows are on screen: numbered rows, one highlighted,
    or unnumbered Yes/No rows, one highlighted, right under a line that opens with the
    question. An agent's prose question above its idle input box has neither."""
    rows = [choice_line(line) for line in lines]
    numbered = [selected for selected, body in rows if NUMBERED.match(body)]
    if len(numbered) >= 2 and numbered.count(True) == 1:
        return True
    heads = [index for index, (_on, body) in enumerate(rows) if ASKING.match(body)]
    below = rows[heads[-1] + 1 :] if heads else []
    if below and ("Enter to confirm" in below[-1][1] or "Esc to cancel" in below[-1][1]):
        below = below[:-1]
    picked = [body for on, body in below if on]
    answers = all(ANSWER.match(body) for _on, body in below)
    return len(below) >= 2 and answers and len(picked) == 1


MARK = re.compile(r"^[❯›]\s*")
# A dialog's footer. Live 2026-11-14: a weekly-limit dialog read as this line alone.
FOOTER = re.compile(r"(?:Enter to confirm · )?Esc to cancel")


def footer_only(lines):
    """True when the screen shows nothing but a dialog's footer: its choice rows and the
    text that explains them are off screen."""
    return bool(lines) and all(FOOTER.fullmatch(line.strip()) for line in lines)


# Run with the transcript folder and session id on the worker's host: prints the last
# assistant entry's epoch and its text, cut to fit one 80-column line.
LAST_ENTRY = """
import glob, json, os, sys
from datetime import datetime
last = None
folder = os.path.join(os.path.expanduser(sys.argv[1]), "projects", "*")
for path in glob.glob(os.path.join(folder, sys.argv[2] + ".jsonl"))[:1]:
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 2_000_000))
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "assistant":
                last = entry
if last:
    content = (last.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"text": content}]
    text = " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
    print(datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00")).timestamp())
    print(" ".join(text.split())[:72])
"""


def last_entry(cfg, rec):
    """(epoch, text) of the last assistant entry in a Claude worker's transcript, read on
    its host from <row home, else ~/.claude>/projects/*/<session>.jsonl; None when there
    is none or it cannot be read. No other agent's transcript is known."""
    row = account(cfg, rec["account"], rec["host"])
    if rec["agent"] != "claude" or not rec.get("session") or not row:
        return None
    # One line, so a remote probe's shell takes it whole.
    script = f"exec(bytes.fromhex('{LAST_ENTRY.encode().hex()}'))"
    argv = ["python3", "-c", script, row["home"] or "~/.claude", rec["session"]]
    code, out = run_on(rec["host"], rec["worktree"], argv)
    lines = out.splitlines()
    try:
        return (float(lines[0]), lines[1]) if code == 0 and len(lines) == 2 else None
    except ValueError:
        return None


def choice_line(line):
    """Whether this row is highlighted, and its text without the selection mark."""
    text = line.strip()
    return bool(MARK.match(text)), MARK.sub("", text).strip()


def header_kind(body):
    """The dialog this line opens. login and other are not answered."""
    if re.search(r"Select login method|sign in to Claude|Log in to Claude", body, re.IGNORECASE):
        return "login"
    if body.startswith(("Do you want to", "Allow command")):
        return "other"
    if body.startswith("Quick safety check"):
        return "trust"
    if "Bypass Permissions mode" in body:
        return "bypass"
    if body == "Security notes:":
        return "security"
    if body == "Try the new fullscreen renderer?":
        return "fullscreen"
    if "Choose the text style" in body:
        return "theme"
    return None


def bottom_dialog(lines):
    """The dialog that owns the bottom of the screen. A later prompt hides an earlier one."""
    found = []
    for index, line in enumerate(lines):
        _selected, body = choice_line(line)
        kind = header_kind(body)
        if kind:
            found.append((index, kind))
    if not found:
        return None, None
    start, kind = found[-1]
    if kind in ("login", "other"):
        return None, None
    return kind, lines[start:]


def choice_rows(slice_lines, is_choice):
    """Choice rows of the bottom dialog. None when another prompt follows them."""
    rows = []
    for index, line in enumerate(slice_lines):
        selected, body = choice_line(line)
        if is_choice(body):
            rows.append((index, selected, body))
    if not rows:
        return None
    after = slice_lines[rows[-1][0] + 1 :]
    if len(after) > 1:
        return None
    if after:
        _selected, hint = choice_line(after[0])
        if "Enter to confirm" not in hint and "Esc to cancel" not in hint:
            return None
    for line in slice_lines[1 : rows[0][0]]:
        selected, body = choice_line(line)
        if not body or selected or header_kind(body):
            return None
    return [(selected, body) for _index, selected, body in rows]


def keys_for_selection(choices, accept):
    """Enter when accept is highlighted. Down, then Enter, when it is the next row."""
    picked = [index for index, (selected, _body) in enumerate(choices) if selected]
    if len(picked) != 1:
        return None
    index = picked[0]
    if choices[index][1] == accept:
        return (("", True),)
    if index + 1 < len(choices) and choices[index + 1][1] == accept:
        return (("\x1b[B", False), ("", True))
    return None


def claude_first_run_keys(lines):
    """Keystrokes for the first-run dialog at the bottom of the screen, or None.

    Login is never answered. A dialog left above a later prompt is not the one
    on screen. Trust, bypass, and the fullscreen offer move down only when the
    refusing choice is the row highlighted now.
    """
    kind, slice_lines = bottom_dialog(lines)
    if not slice_lines:
        return None
    if kind == "security":
        bodies = [choice_line(line)[1] for line in slice_lines]
        if bodies[-1] != "Press Enter to continue":
            return None
        if any(header_kind(body) not in (None, "security") for body in bodies):
            return None
        return "security", (("", True),)
    if kind == "theme":
        choices = choice_rows(
            slice_lines,
            lambda body: body.endswith(("Dark mode", "Light mode")),
        )
        if not choices:
            return None
        bodies = [body for _selected, body in choices]
        if not any(body.endswith("Dark mode") for body in bodies) or not any(
            body.endswith("Light mode") for body in bodies
        ):
            return None
        selected = [body for on, body in choices if on]
        if len(selected) != 1:
            return None
        return "theme", (("", True),)
    accept = {
        "trust": "Yes, I trust this folder",
        "bypass": "Yes, I accept",
        "fullscreen": "Not now",
    }[kind]
    names = {
        "trust": {"Yes, I trust this folder", "No, exit"},
        "bypass": {"Yes, I accept", "No, exit"},
        "fullscreen": {"Not now", "Yes"},
    }[kind]
    choices = choice_rows(slice_lines, lambda body: body in names)
    if not choices or {body for _selected, body in choices} != names:
        return None
    keys = keys_for_selection(choices, accept)
    if not keys:
        return None
    return kind, keys


def answer_claude_first_run(st, tid, rec, screen, last):
    """Answer one named first-run screen once. True when this screen is handled.

    An unrendered screen is not a dialog: its tail can be an old frame.
    """
    if not screen or screen.get("source") == "screen-unavailable":
        return False
    found = claude_first_run_keys(screen["lines"])
    if not found:
        return False
    kind, keys = found
    # The launch and the dialog, not lastOutputAt: a redraw must not send the keys again.
    launch = rec.get("session") or rec.get("launched_at")
    token = f"{tid}|firstrun|{launch}|{kind}"
    if token in st["sent"]:
        return True
    action = f"Answer Claude's {kind} screen."
    if defer_stale_claude(st, rec, token, action):
        return True
    st["sent"][token] = int(time.time())
    st.save()
    sent = claude_keys(st, rec, token, action, keys)
    if sent is not True:
        del st["sent"][token]
        st.save()
        return sent == "deferred"
    print(f"{tid}: answered Claude's {kind} screen")
    return True


def bare_shell(screen):
    """True when a running screen's last line is a shell prompt and no dialog is open. An
    accumulated stream Orca returns when it cannot render the screen proves nothing."""
    lines = screen["lines"] if screen else []
    return (
        bool(lines)
        and screen["status"] == "running"
        and screen["source"] != "screen-unavailable"
        and bool(SHELL.fullmatch(lines[-1].strip()))
        and not overlay(lines)
    )


def exited(rec, row, screen, now):
    """True only on proof the record's agent is gone: its terminal's process ended, or no
    agent is listed, no dialog is open and the screen is a bare shell a minute after
    launch. A listed agent that drops out of the listing proves nothing: Orca's identity
    expires while the agent runs, and Claude lists none for its first minute or so."""
    if screen is None:
        return False
    if screen["status"] == "exited":
        return True
    if row.get("agentIdentity") or screen["status"] != "running":
        return False
    if overlay(screen["lines"]):
        return False
    # A record saved without its launch time proves no bare-shell exit.
    launched = rec.get("launched_at")
    return bare_shell(screen) and launched is not None and now - launched >= SETTLED


def at_prompt(host, handle, row):
    """True only when Orca sees an agent, not a bare shell, and no menu or question is open."""
    if not row or not row.get("agentIdentity"):
        return False
    screen = read_screen(host, handle)
    return (
        bool(screen) and screen["status"] == "running" and not overlay(screen["lines"])
    )


def agent_ui(agent, lines):
    """The agent's own idle screen. Orca drops agentIdentity when a session retitles its
    terminal; the prompt box and footer still show who is running."""
    shown = "\n".join(lines)
    if agent == "claude":
        return "bypass permissions on" in shown.lower()
    if agent == "codex":
        return "Ask Codex to do anything" in shown
    if agent == "grok":
        return "Ask Grok" in shown
    if agent == "sdk":
        return any(line in SDK_SCREEN for line in lines)
    return False


def times_shown(screen, text):
    """How many times the screen shows text's own last 80 characters, the part a goal
    prefix shared by every wake does not hold. Spaces are dropped on both sides, so a line
    the screen wrapped still counts. None for an unread screen or Orca's accumulated
    stream, which proves nothing, and for an Unknown command screen, whose old copies of
    the text may be anywhere."""
    if not screen or screen.get("source") == "screen-unavailable":
        return None
    shown = "\n".join(screen["lines"])
    if "Unknown command:" in shown:
        return None
    return "".join(shown.split()).count("".join(text.split())[-80:])


# An agent's own input line starts with one of these; a shell prompt does not.
AGENT_PROMPTS = ("❯", "›")
# A shell's prompt SHELL may not know, such as bash-3.2$: it ends in its sigil, though a
# percentage ending a status line (opus@max: 87%) is no sigil.
SHELL_TAIL = re.compile(r"(?:.*\S)?[$#>]|(?:.*\D)?%")
# Starship's two-line prompt: a path with its context, then a bare prompt glyph.
STARSHIP = re.compile(r"[~/.]\S* (?:on|via|is) \S.*")


def typeable(screen, agent="codex"):
    """A rendered, running agent screen: not Orca's accumulated stream, not exited, no
    bare shell, menu, question or footer-only dialog in front, and showing the agent
    itself: its own idle screen, a running turn or its input line. An empty frame or an
    unrecognized prompt shows no agent, and a shell prompt on the bottom line is the active
    input whatever agent lines its history holds above it."""
    lines = screen["lines"] if screen else []
    shown = [line.strip() for line in lines if line.strip()]
    last = shown[-1] if shown else ""
    starship = last in AGENT_PROMPTS and len(shown) > 1 and STARSHIP.fullmatch(shown[-2])
    return (
        bool(screen)
        and screen["status"] == "running"
        and screen.get("source") != "screen-unavailable"
        and not overlay(lines, agent)
        and not footer_only(lines)
        and not (lines and SHELL.fullmatch(lines[-1].strip()))
        and not (SHELL_TAIL.fullmatch(last) and not last.startswith(AGENT_PROMPTS))
        and not starship
        and (
            agent_ui(agent, lines)
            or in_turn(lines)
            or any(line.lstrip().startswith(AGENT_PROMPTS) for line in lines[-15:])
        )
    )


def type_message(host, terminal, text, worker=None, seen=None, idle=False, delivery=None):
    """Clear the input with Ctrl-U, then type text. A leftover slash must not become
    part of it. /goal is the only typed line that starts with a slash, and only after
    the clear. No Esc is sent, so a running turn is never interrupted.
    "confirmed" when the screen after Enter shows text more times than the screen read
    just before it did, so an earlier copy of the same text never confirms this one;
    "deferred" when the Claude cutoff holds the action for the orchestrator;
    "unconfirmed" when the keys were accepted but the screen does not show it, or the
    screen before was unknown (see times_shown), False when nothing was typed. seen, when
    given, gets that earlier count, None when unknown; a recheck compares against it.
    emit's recheck types an unconfirmed wake once more.
    That last frame before the keys decides: nothing is typed unless it is read and
    typeable (see typeable), with idle nor while it shows a turn; the caller keeps the
    send pending. Without a worker it is the Claude orchestrator."""
    resume = False
    before = read_screen(host, terminal)
    base = times_shown(before, text)
    if seen is not None:
        seen["count"] = base
    if not typeable(before, (worker or {}).get("agent", "claude")):
        print(f"{terminal}: no running agent screen is in front; nothing is typed")
        return False
    if (idle or (delivery and "|context-ask|" in delivery[1])) and in_turn(before["lines"]):
        print(f"{terminal}: it is in a turn; nothing is typed")
        return False
    if worker and worker.get("agent") == "codex":
        # An Enter would confirm whatever menu or question is open, a model switch too.
        lines = before["lines"] if before else []
        if overlay(lines) or unknown_menu(lines):
            print(f"{terminal}: a menu or prompt is open; the wake is held")
            return False
        if model_drift(worker, lines):
            print(f"{terminal}: its status line shows another model; nothing is typed")
            return False
        status = codex_status(lines)
        resume = bool(status) and any(state in status for state in RESUME)
    if text.startswith("/") and not text.startswith("/goal"):
        print(f"refused to start a wake with a slash in {terminal}")
        return False
    if worker and worker["agent"] == "claude":
        st, key, action = delivery
        sent = claude_keys(st, worker, key, action, (("\x15", False), (text, True)), message=True)
        if sent is not True:
            return sent
    else:
        if not send(host, terminal, "\x15", enter=False):
            return False
        if resume and not send(host, terminal, "/goal resume", enter=True):
            return False
        if not send(host, terminal, text, enter=True):
            return False
    after = times_shown(read_screen(host, terminal), text)
    if after is None or base is None or after <= base:
        print(f"{terminal}: typed, but the screen has not confirmed it")
        return "unconfirmed"
    return "confirmed"


# An orchestrator send its screen never showed is typed at most this often.
TELL_TRIES = 2


def tell(pointer, text, st=None, key=None, **extra):
    """Type text into the orchestrator; truthy only once its screen shows it: the sent
    entry, {"text": …, **extra}. With a key, a send Orca accepted that the screen did not
    show waits in st["told"]: each later call first reads the screen for that text, which
    counts as delivered once it shows, and types again only while the same extra has been
    typed fewer than TELL_TRIES times. A new orchestrator terminal drops the wait."""
    told = st.setdefault("told", {}) if st is not None and key else None
    pending = told.get(key) if told is not None else None
    if pending and pending["terminal"] != pointer["terminal"]:
        del told[key]
        pending = None
    if pending:
        count = times_shown(read_screen(pointer["host"], pointer["terminal"]), pending["text"])
        if count is not None and pending["seen"] is not None and count > pending["seen"]:
            del told[key]
            st.save()
            return {"text": pending["text"], **pending["extra"]}
        if pending["tries"] >= TELL_TRIES and pending["extra"] == extra:
            print(f"orchestrator {pointer['terminal']} never showed it; held: {pending['text']}")
            return False
    terminals = listing(pointer["host"]) or {}
    row = terminals.get(pointer["terminal"])
    if not at_prompt(pointer["host"], pointer["terminal"], row):
        print(f"orchestrator {pointer['terminal']} is not at its prompt; held: {text}")
        return False
    seen = {}
    sent = type_message(pointer["host"], pointer["terminal"], text, seen=seen, idle=True)
    if sent == "confirmed":
        if told is not None and told.pop(key, None):
            st.save()
        return {"text": text, **extra}
    if sent == "unconfirmed" and told is not None:
        again = pending and pending["extra"] == extra
        told[key] = {
            "text": text,
            "seen": seen["count"],
            "tries": pending["tries"] + 1 if again else 1,
            "terminal": pointer["terminal"],
            "extra": extra,
        }
        st.save()
    return False


def tell_orchestrator(st, text, key=None, **extra):
    if not st["orchestrator"]:
        print(f"no orchestrator set; held: {text}")
        return False
    return tell(st["orchestrator"], text, st, key, **extra)


# An orchestrator's event is appended here; event-done removes each line once handled.
EVENT_REMIND = 30 * 60


def events_file():
    return ledger() / "supervisor" / "events.md"


def append_event(st, text, line=None, key=None):
    """Append one event line; dev-auto appends, and removes only the lines the
    orchestrator names to event-done. The tick's wake_events names it."""
    return event_line(st, text, line, key)


# A reservation older than this and no longer queued is dropped: a line carries its
# full UTC minute, so no later event's line can equal it.
EVENT_KEEP = 24 * 3600


def event_text(st, text):
    """The event's line, never one an earlier event had: it carries its UTC date and
    minute, and within the minute a repeat of a line reserved in the state (or shown in
    the file, for one written before the reservations) ends in (2), (3) and so on."""
    now = time.time()
    try:
        taken = set(events_file().read_text(errors="replace").splitlines())
    except OSError:
        taken = set()
    queued = {entry["line"] for entry in st.get("outbox", {}).values()}
    held = st.setdefault("event_lines", {})
    for old, at in list(held.items()):
        if old not in queued and now - at["at"] > EVENT_KEEP:
            del held[old]
    base = f"- {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(now))} {text}"
    line, n = base, 1
    while line in held or line in taken:
        n += 1
        line = f"{base} ({n})"
    held[line] = {"at": int(now)}
    return line


def event_line(st, text, line=None, key=None):
    """The append itself: an event typed in full (RELAY) is written here directly. The
    whole file is written beside it and moved into place, so a full disk leaves no part
    of a line; the line is counted after the move, see event_counted."""
    line = line or event_text(st, text)
    path = events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = path.read_text(errors="replace")
    except FileNotFoundError:
        before = ""
    if before and not before.endswith("\n"):
        before += "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(before + line + "\n")
    tmp.replace(path)
    event_counted(st, line, key)
    # As a shown send is marked in delivered.log: a written line is delivered.
    return "confirmed"


def event_counted(st, line, key=None):
    """A line now in the file counts for the next wake, and its outbox entry is marked
    counted in the same save: a line in the file with an uncounted entry was never counted.
    STOPPED's line is not counted: its full send is its wake, so no EVENTS wake repeats it."""
    if not re.fullmatch(r"stopped\|\d+", key or ""):
        st["events_new"] = st.get("events_new", 0) + 1
    st.setdefault("events_at", {})[line] = int(time.time())
    if key in st.get("outbox", {}):
        st["outbox"][key]["counted"] = True
    st.save()


def withdrawn(key, entry, tickets):
    """True for an unwritten CLEARED REVIEW or UNPUSHED whose clearance does not stand now.
    A CLEARED REVIEW stands while the PUSH its key names stands on a ticket the ledger
    holds, closed or not; with no such ticket it is withheld. An UNPUSHED stands only on the owner and PUSH ids saved with its
    reservation: with either missing, or its owner's ticket gone, it is withheld."""
    tid, kind = key.split("|")[:2]
    if kind == "cleared":
        mine = [t for (_, other), t in tickets.items() if other == tid]
        return key not in {
            f"{tid}|cleared|{u['id']}" for t in mine for u, _ in standing_pushes(t)
        }
    if kind != "unpushed":
        return False
    owner, backs = entry.get("owner"), entry.get("backs")
    ticket = owner and tickets.get(tuple(owner.split("|", 1)))
    if not ticket or not backs:
        return True
    standing = {u["id"] for u, _ in standing_pushes(ticket)}
    return not any(i in standing for i in backs)


def flush_outbox(st, tickets):
    """Finish each event a kill or a full disk cut short after emit saved its key: append
    its line unless it landed before the kill or event-done acknowledged it, count it
    unless it was counted, then the rest of its delivery, as emit does. An unwritten
    clearance withdrawn since (see withdrawn) is dropped, never written."""
    outbox = st.get("outbox")
    if not outbox:
        return
    try:
        lines = set(events_file().read_text(errors="replace").splitlines())
    except FileNotFoundError:
        lines = set()
    except OSError as error:
        print(f"events file unread, its unwritten events wait: {error}")
        return
    for key, entry in list(outbox.items()):
        if not entry.get("acked") and entry["line"] not in lines and withdrawn(key, entry, tickets):
            print(f"{entry['text']}: withdrawn before it was written, dropped")
            known = st.get("unpushed", {})
            owner, head = entry.get("owner"), key.split("|")[2]
            if owner in known and known[owner].get("head") == head:
                known[owner].pop("head")
            del outbox[key]
            st.save()
            continue
        if not entry.get("acked") and entry["line"] not in lines:
            try:
                event_line(st, None, entry["line"], key)
            except OSError as error:
                print(f"events file not written, its unwritten events wait: {error}")
                st.held = True
                return
        elif not entry.get("counted") and not entry.get("acked"):
            event_counted(st, entry["line"], key)
        delivery_done(st, key, entry["text"], "confirmed", entry["detail"], entry["note_on"])
        del outbox[key]
        st.save()


def wake_events(st):
    """One short line into the orchestrator after a tick that appended events: how many
    are new, how many lines are open, and, once per line, how many have waited over
    EVENT_REMIND. Not typed, the count waits for the next tick."""
    new = st.get("events_new", 0)
    if not new or not st["orchestrator"]:
        return
    path = events_file()
    try:
        lines = [l.rstrip("\n") for l in path.read_text().splitlines() if l.startswith("- ")]
    except OSError:
        lines = []
    st["events_at"] = {l: at for l, at in st.get("events_at", {}).items() if l in lines}
    reminded = [l for l in st.get("events_reminded", []) if l in lines]
    old = [
        l
        for l in lines
        if time.time() - st["events_at"].get(l, time.time()) >= EVENT_REMIND and l not in reminded
    ]
    text = f"EVENTS: {new} new in {path}, {len(lines)} open"
    if old:
        text += f", {len(old)} older than 30 min"
    # Its batch number tells this wake from an earlier one with the same counts on screen:
    # each snapshot gets its own, and only a retry of a pending one keeps that one's.
    pending = (st.get("told") or {}).get("events")
    same = pending and {k: pending["extra"].get(k) for k in ("new", "old")} == {"new": new, "old": old}
    if same and "batch" in pending["extra"]:
        batch = pending["extra"]["batch"]
    else:
        # Reserved before any key is typed: a stop mid-send never hands it out again.
        batch = st["events_batch"] = st.get("events_batch", 0) + 1
        st.save()
    text += f", batch {batch}"
    # Counted as told only once the screen shows it, then only what that wake counted.
    landed = tell_orchestrator(st, text, "events", new=new, old=old, batch=batch)
    if landed:
        st["events_new"] = max(0, st.get("events_new", 0) - landed["new"])
        reminded += [l for l in landed["old"] if l not in reminded]
    else:
        st.held = True
    st["events_reminded"] = reminded
    st.save()


def status(st, tid, state, detail="", since=None, proved=False):
    """What this tick saw of the worker; the last call in a tick wins. since, when given,
    is when the state began if it is new; proved replaces a standing STUCK."""
    st.statuses[tid] = (state, detail, since, proved)


def emit(st, key, text, worker=None, note_on=None, detail="", rearm=True, proof=None):
    """Deliver one event once, to that worker or else the orchestrator's events file; a
    worker's stays pending until accepted. The text is one fixed line; detail is printed and noted, never typed.
    The key is saved before the send: a crash mid-send loses the event, never repeats it.
    A worker's event also sets its goal again unless rearm is False; such an armed event
    is a wake, pending on the worker's record until it answers."""
    # Each tick re-raises a STUCK that still stands, so its text stays on the record.
    if text.startswith("STUCK") and key.split("|")[0] in st["workers"]:
        status(st, key.split("|")[0], "stuck", text)
    if key in st["sent"]:
        return False
    action = f"{text} {detail}" if detail else text
    if worker and defer_stale_claude(st, worker, key, action):
        return False
    st["sent"][key] = int(time.time())
    if not worker:
        # Kept until its delivery is done: the next tick finishes one a kill cut short.
        line = event_text(st, text)
        # proof: what a replay must still find standing (see withdrawn), saved with the line.
        entry = {"line": line, "text": text, "note_on": note_on, "detail": detail, **(proof or {})}
        st.setdefault("outbox", {})[key] = entry
    st.save()
    if worker:
        # A text typed into this record before carries this event's key, so a copy of the
        # earlier one on its screen can never confirm this one. A record launched before
        # typed_texts was kept has no such history, so each of its sends carries it.
        history = worker.get("typed_texts")
        if history is None:
            # Unknown for the rest of this launch, whatever is typed into it from now on.
            history = worker["typed_texts"] = ["unknown"]
        digest = hashlib.sha1(text.encode()).hexdigest()[:12]
        if "unknown" in history or digest in history:
            text = f"{text} Event {SAFE_OUT.sub('', ' '.join(key.split('|')[1:]))}."
        typed = armed(worker, goal_line(worker["ticket"]), text) if rearm else text
        update, at, seen = worker_update(worker), time.time(), {}
        sent = type_message(
            worker["host"], worker["terminal"], typed, worker, seen, delivery=(st, key, action)
        )
    else:
        try:
            sent = append_event(st, text, line, key)
        except OSError as error:
            print(f"events file not written, appended by a later tick: {error}")
            st.held = True
            return False
    if not sent or sent == "deferred":
        del st["sent"][key]
        st.save()
        st.held = st.held or not worker
        return False
    if worker and digest not in worker["typed_texts"]:
        # Every bare text this launch was typed, so a repeat is known however old.
        worker["typed_texts"].append(digest)
    if worker and (rearm or sent == "unconfirmed"):
        # Read after the send, so the wake's own echo is not output from the worker.
        row = (listing(worker["host"]) or {}).get(worker["terminal"]) or {}
        if rearm:
            worker["wake"] = {"key": key, "output": row.get("lastOutputAt")}
        if sent == "unconfirmed":
            # Rechecked whether or not it set the goal, against what it typed.
            worker.setdefault("unshown", []).append(
                {
                    "key": key,
                    "text": text,
                    "typed": typed,
                    "seen": seen["count"],
                    "at": at,
                    "update": update,
                    "output": row.get("lastOutputAt"),
                }
            )
        st.save()
        if sent == "unconfirmed":
            sent = recheck(st, worker, worker["unshown"][-1]) or sent
    delivery_done(st, key, text, sent, detail, note_on)
    if not worker:
        del st["outbox"][key]
        st.save()
    return True


def delivery_done(st, key, text, sent, detail, note_on):
    # Append-only, never read back: drop() forgets the keys, this keeps what was delivered.
    mark = "" if sent == "confirmed" else f"\t{sent}"
    delivered(st, key.split("|")[0], f"{text}{mark}")
    full = f"{text}\n{detail}" if detail else text
    print(full)
    if note_on:
        note(note_on, full)


WAKE_RECHECK = 10


def recheck(st, rec, unshown):
    """One of the record's unshown sends, WAKE_RECHECK seconds after it: "rechecked" when
    the screen now shows its text more times than the screen before the send did, or the
    worker made output or posted an update since; otherwise it is typed once more, the
    same text, "retried" when that shows and "missed" when not or when no running agent
    screen is rendered (see typeable), so nothing can be typed safely. Its
    key is on the record's missed list before that second send, so a crash mid-send never
    types it a third time, and unanswered_wake raises the missed list's STUCKs.
    "deferred" moves a stale Claude retry to the orchestrator's outbox. A tick
    waits for one recheck only; None leaves a later one to the next tick's unanswered_wake."""
    left = unshown["at"] + WAKE_RECHECK - time.time()
    if left > 0:
        if st.waited:
            return None
        st.waited = True
        time.sleep(left)
    # An entry saved before "typed" was kept is a wake that set the goal.
    typed = unshown.get("typed") or armed(rec, goal_line(rec["ticket"]), unshown["text"])
    screen = read_screen(rec["host"], rec["terminal"])
    count = times_shown(screen, typed)
    row = (listing(rec["host"]) or {}).get(rec["terminal"]) or {}
    shown = (
        (count is not None and unshown.get("seen") is not None and count > unshown["seen"])
        or (row.get("lastOutputAt") or 0) > (unshown["output"] or 0)
        or worker_update(rec) > unshown["update"]
    )
    key, action = unshown["key"], unshown["text"]
    deferred = not shown and defer_stale_claude(st, rec, key, action)
    rec["unshown"].remove(unshown)
    if not rec["unshown"]:
        del rec["unshown"]
    if shown:
        st.save()
        return "rechecked"
    if not deferred:
        missed = rec.setdefault("missed", [])
        missed.append(key)
        st.save()
        # Nothing is typed into a screen that is unread, unrendered, exited, a bare shell or
        # a dialog.
        if not typeable(screen, rec["agent"]):
            return "missed"
        if FINISH_DONE in typed and not rec.get("finish"):
            # Its finish was withdrawn since (withdraw_finish): retyped with the plain goal.
            typed = typed.replace(FINISH_DONE, GOAL_DONE)
        sent = type_message(
            rec["host"], rec["terminal"], typed, rec, delivery=(st, key, action)
        )
        if sent not in ("confirmed", "deferred"):
            return "missed"
        deferred = sent == "deferred"
        missed.remove(key)
        if not missed:
            del rec["missed"]
    wake = rec.get("wake") or {}
    if wake.get("key") == key:
        if deferred:
            del rec["wake"]
        else:
            # An unread listing keeps the baseline it had.
            after = ((listing(rec["host"]) or {}).get(rec["terminal"]) or {}).get("lastOutputAt")
            wake["output"] = after if after is not None else wake.get("output")
    if deferred and "|handoff-ask|" in key:
        rec.pop("handoff_ask", None)
    st.save()
    return "deferred" if deferred else "retried"


def worker_update(rec):
    """The id of the latest ledger update by the worker or its ticket's owner; 0 when none
    or the ledger is unread."""
    try:
        con = sqlite3.connect(f"{(ledger() / '.concord' / 'concord.db').as_uri()}?mode=ro", uri=True)
        try:
            found = con.execute(
                "SELECT MAX(id) FROM task_updates WHERE agent = ? OR agent IN"
                " (SELECT agent_id FROM tasks WHERE task_id = ? AND project = ?)",
                (rec["agent_id"], rec["ticket"], rec.get("project", "")),
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    return found or 0


def delivered(st, ticket, text):
    """The audit line; one the disk refuses is printed and lost, and the tick goes on."""
    try:
        with open(st.path.parent / "delivered.log", "a") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')}\t{ticket}\t{text}\n")
    except OSError as error:
        print(f"delivered.log not written ({error}): {ticket}\t{text}")


def say(ticket, text):
    print(f"{ticket}: {text}")
    note(ticket, text)


RELAY_UNKNOWN_RESET = 7 * 86400
STOP = (
    "dev-auto: stop. Finish your current command, record your state on your ticket, "
    "then stop and wait."
)


def check_orchestrator(cfg, st, now):
    """Relay once per usage window, then stop everything (guard.sh's rule)."""
    pointer = st["orchestrator"]
    row = pointer and account(cfg, pointer.get("account"), pointer["host"])
    if not row or st["stopped"]:
        return
    if st["relay"]["used"] and st["relay"].get("reset") is None:
        # A record from before resets were stored, or one saved with a null reset, names
        # no window; every window that held it has reset once the longest, a week, passed.
        st["relay"] = {**st["relay"], "reset": now + RELAY_UNKNOWN_RESET}
    if st["relay"]["used"] and now >= st["relay"]["reset"]:
        # The window that raised the relay has reset, so the next reading relays again.
        st["relay"] = {"used": False}
    percent, reset = peak(cfg, row, now)
    if percent is None or percent < cfg["relay_percent"]:
        return
    if not st["relay"]["used"]:
        when = f", resets {stamp(reset)}" if reset else ""
        text = (
            f"RELAY: orchestrator account {row['label']} is at {percent:.0f}%{when}. "
            "Hand off once to a fresh orchestrator, which runs set-orchestrator with "
            "its own terminal and account."
        )
        # Recorded only once its screen shows it: a kill mid-send or an unshown send
        # repeats RELAY instead of losing it, and the pointer stays until then.
        landed = tell(pointer, text, st, "relay", reset=reset)
        if landed:
            event_line(st, landed["text"])
            print(landed["text"])
            until = landed["reset"] or now + RELAY_UNKNOWN_RESET
            st["relay"] = {"used": True, "by": pointer["terminal"], "reset": until}
            # Events wait for the successor's set-orchestrator instead of waking this one.
            st["orchestrator"] = None
        return
    if st["relay"].get("by") != pointer["terminal"]:
        st["stopped"] = int(now)


def stop_workers(st, terminals):
    """After a stop: tell the orchestrator, and each worker once its agent is in front."""
    cycle = st["stopped"]
    text = (
        "STOPPED: the relayed orchestrator reached its usage limit and every worker is "
        "told to stop. The operator starts a new cycle with set-orchestrator --new-cycle."
    )
    # It changes what the orchestrator must do at once, so its full text is typed too,
    # on the first tick that finds the orchestrator able to take it. That send is its
    # wake: event_counted leaves its line out of every EVENTS count.
    emit(st, f"stopped|{cycle}", text)
    full = f"stopped|{cycle}|full"
    if f"stopped|{cycle}" in st["sent"] and full not in st["sent"] and tell_orchestrator(st, text, full):
        st["sent"][full] = int(time.time())
        st.save()
    for tid, rec in st["workers"].items():
        key = f"{tid}|stop|{cycle}"
        if rec.get("lost"):
            continue
        row = own_row(rec, terminals.get(rec["host"], {}))
        if row:
            rebind(tid, rec, row)
        if key in st["sent"]:
            # A STOP its screen has not shown yet is rechecked on this cycle's ticks, once
            # the listing shows the worker's own row.
            if row:
                recheck_unshown(st, tid, rec, only=key)
            continue
        if at_prompt(rec["host"], rec.get("terminal"), row):
            emit(st, key, STOP, worker=rec, rearm=False)
    print("stopped: waiting for set-orchestrator --new-cycle")


def tick(cfg, root, st, tickets, now):
    began = time.monotonic()
    st.held = False
    st.statuses = {}
    flush_outbox(st, tickets)
    check_orchestrator(cfg, st, now)
    read_contexts(cfg, st, now)
    terminals = {}
    hosts = st.setdefault("hosts", {})
    restarts = st.setdefault("restarts", {})
    for host in sorted({rec["host"] for rec in st["workers"].values()}):
        listed = listing(host)
        if listed is None:
            failed = hosts.setdefault(host, {}).get("failed", 0) + 1
            hosts[host]["failed"] = failed
            if failed == 1:
                print(f"{host}: terminal list failed or was cut short; its workers wait")
                continue
            count = sum(r["host"] == host and not r.get("lost") for r in st["workers"].values())
            text = (
                f"STUCK {host}: its terminal list failed twice in a row, its {count} workers "
                f"wait until it answers{windows_trend(host)}"
            )
            emit(st, f"{host}|down|{hosts[host].setdefault('since', int(now))}", text)
            continue
        # Before the sweep: a worker whose stop is pending must not be marked lost.
        handoff_stops(cfg, st, tickets, {host: listed}, now)
        was_down = hosts.pop(host, None)
        if was_down and "since" in was_down:
            # The outage is over: its STUCK key dedups nothing more.
            st["sent"].pop(f"{host}|down|{was_down['since']}", None)
        restart_sweep(cfg, st, tickets, host, listed, bool(was_down), now)
        terminals[host] = listed
    for key, text in list(restarts.items()):
        if emit(st, key, text) or key in st["sent"]:
            del restarts[key]
            st["sent"].pop(key, None)
    if st["stopped"]:
        stop_workers(st, terminals)
        clean_evidence(root, tickets)
        write_status(cfg, root, st, tickets, now)
        wake_events(st)
        spent = time.monotonic() - began
        st["tick"] = {"at": int(now + spent), "seconds": round(spent, 1)}
        return
    lift_limits(cfg, st, now)
    windows_watch(cfg, st, now)
    # Before supervise, whose cleanup may remove a worktree this check still reads, and
    # before unpushed, so a head pushed here raises no UNPUSHED.
    if cfg["auto_push"]:
        deliver(cfg, st, tickets, now)
    unpushed(st, tickets)
    clear_verdicts(st, tickets)
    withdraw_finish(st, tickets)
    unreviewed(st, tickets)
    orchestrator_reviews(st, tickets)
    # The listing as read: cleanup drops each terminal it closes from it.
    shown = {host: dict(rows) for host, rows in terminals.items()}
    for ticket_id, rec in list(st["workers"].items()):
        if gaps := missing(rec):
            text = f"STUCK {ticket_id}: its record has no {' or '.join(gaps)}. It is skipped until a new launch"
            emit(st, f"{ticket_id}|partial", text, note_on=ticket_id)
        elif rec["host"] in terminals:
            supervise(
                cfg, root, st, ticket_id, rec, tickets, terminals[rec["host"]], now
            )
    context_restarts(cfg, root, st, tickets, terminals, now)
    orchestrator_context(st, now)
    if cfg["limit_handoff"]:
        handoff_asks(cfg, st, tickets, terminals, now)
    if cfg["limit_handoff"] or st.get("proposals"):
        proposals_step(cfg, st, tickets, now)
    close_revived(st, tickets, now)
    for host, listed in terminals.items():
        idle_shells(st, host, listed, now)
    sweep_worktrees(st, tickets, terminals, now)
    checkin(cfg, root, st, tickets, now)
    clean_evidence(root, tickets)
    # After cleanup, so a session closed this tick frees its place.
    fill(cfg, st, tickets, now)
    for tid, (state, detail, since, proved) in st.statuses.items():
        rec = st["workers"].get(tid)
        if rec is None:
            continue
        old = rec.get("status") or {}
        row = shown.get(rec["host"], {}).get(rec.get("terminal")) or {}
        output = row.get("lastOutputAt")
        output_at = int(output / 1000) if output else old.get("output_at")
        kinds = ("working", "quiet", "waiting", "nudged", "done")
        if old.get("state") == "stuck" and state in kinds and not proved:
            # A STUCK raised once (an unanswered wake, a cached cleanup check) stands until
            # the worker outputs or updates, cleanup rechecks, or a missing review ticket
            # is no longer blocking review tracking.
            ticket = tickets.get((rec.get("project", ""), tid)) or {"updates": []}
            mine = {rec["agent_id"], ticket.get("agent_id")}
            spoke = any(
                (epoch(u["created_at"]) or 0) > old["since"]
                for u in ticket["updates"]
                if u["agent"] in mine
            )
            checked = (rec.get("checked") or {}).get("at") == now
            if not (spoke or checked or (output_at or 0) > old["since"]):
                blocker = waiting(ticket)
                resolved = (
                    blocker
                    and wait_target(blocker) == "review"
                    and old["detail"] == review_wait_stuck_text(tid, blocker, MISSING_REVIEW_TICKET)
                    and review_wait_gap(rec, tid, ticket, tickets, blocker) is None
                )
                if not resolved:
                    state, detail = "stuck", old["detail"]
        rec["status"] = {
            "state": state,
            "detail": detail,
            "since": old["since"]
            if (old.get("state"), old.get("detail")) == (state, detail)
            else int(since or now),
            "seen": int(now),
            "output_at": output_at,
        }
    write_status(cfg, root, st, tickets, now)
    wake_events(st)
    spent = time.monotonic() - began
    st["tick"] = {"at": int(now + spent), "seconds": round(spent, 1)}


# Claude context limits in tokens: a coder or reviewer is restarted from its handoff at
# CONTEXT_LIMIT, the orchestrator relays at ORCHESTRATOR_CONTEXT_LIMIT.
CONTEXT_LIMIT = 300_000
ORCHESTRATOR_CONTEXT_LIMIT = 400_000
# A remote host's transcripts are read at most this often: each read opens a probe.
CONTEXT_REMOTE_SECONDS = 300

# Run on a host with one key<TAB>home<TAB>session<TAB>cwd argument per Claude session:
# prints "key tokens" for each whose transcript has an assistant entry with usage, the
# last one's input plus cache read and cache creation tokens. With no session, the
# newest transcript of the cwd's project folder is read.
CONTEXT_READ = r"""
import glob, json, os, re, sys
for arg in sys.argv[1:]:
    key, home, session, cwd = arg.split("\t")
    folder = os.path.join(os.path.expanduser(home), "projects")
    if session:
        paths = glob.glob(os.path.join(folder, "*", session + ".jsonl"))[:1]
    else:
        own = os.path.join(folder, re.sub(r"[^A-Za-z0-9]", "-", cwd), "*.jsonl")
        paths = sorted(glob.glob(own), key=os.path.getmtime)[-1:]
    used = None
    for path in paths:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 2_000_000))
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                usage = (entry.get("message") or {}).get("usage")
                if isinstance(usage, dict) and not entry.get("isSidechain"):
                    names = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                    used = sum(int(usage.get(n) or 0) for n in names)
    if used is not None:
        print(key, used)
"""


def read_contexts(cfg, st, now):
    """Each live Claude record's context size, and the orchestrator's, from its transcript
    on its host: one read per host per tick, a remote host's at most every
    CONTEXT_REMOTE_SECONDS. Stored as `context` on the record or the pointer."""
    wanted = {}
    for tid, rec in st["workers"].items():
        if rec["agent"] != "claude" or not rec.get("session") or missing(rec):
            continue
        if rec.get("done") or rec.get("stopped") or rec.get("lost"):
            continue
        row = account(cfg, rec["account"], rec["host"])
        home = (row or {}).get("home") or "~/.claude"
        wanted.setdefault(rec["host"], []).append((tid, home, rec["session"], rec["worktree"], rec))
    pointer = st["orchestrator"]
    row = pointer and account(cfg, pointer.get("account"), pointer["host"])
    # Only the session set-orchestrator recorded: the newest transcript in a shared
    # project folder may be the predecessor's, still writing.
    if row and row["agent"] == "claude" and pointer.get("session"):
        shown = (listing(pointer["host"]) or {}).get(pointer["terminal"]) or {}
        if shown.get("worktreePath"):
            home = row.get("home") or "~/.claude"
            wanted.setdefault(pointer["host"], []).append(
                ("orchestrator", home, pointer["session"], shown["worktreePath"], pointer)
            )
    read_at = st.setdefault("context_read", {})
    for host, items in wanted.items():
        if host != "local" and now - read_at.get(host, 0) < CONTEXT_REMOTE_SECONDS:
            continue
        read_at[host] = now
        script = f"exec(bytes.fromhex('{CONTEXT_READ.encode().hex()}'))"
        argv = ["python3", "-c", script, *("\t".join(i[:4]) for i in items)]
        place = next((i[3] for i in items if i[0] != "orchestrator"), items[0][3])
        code, out = run_on(host, place, argv)
        if code:
            continue
        found = {}
        for line in out.splitlines():
            key, _, tokens = line.partition(" ")
            if tokens.isdigit():
                found[key] = int(tokens)
        for key, _, session, _, holder in items:
            if key in found:
                used = found[key]
                holder["context"] = used
                if holder is not pointer and used >= CONTEXT_LIMIT:
                    holder["context_hit"] = {session: used}


def context_for_handoff(rec):
    hit = rec.get("context_hit")
    observed = hit.get(rec.get("session"), 0) if isinstance(hit, dict) else 0
    return max(rec.get("context") or 0, observed)


def context_restarts(cfg, root, st, tickets, terminals, now):
    """A Claude coder or reviewer at CONTEXT_LIMIT is restarted from its handoff: once a
    Handoff: update of this launch is on its ticket and its terminal has settled or is
    gone, the session is closed with its tab and a fresh one starts on the same ticket,
    worktree, model, effort and host, on its own account first. A close Orca refuses starts
    nothing until a later tick's close succeeds. With no handoff, ask once as soon as
    the worker is settled, or report a stale session for recovery. With limit_handoff on,
    handoff_stops stops a handed-off one with a proposal instead."""
    for tid, rec in list(st["workers"].items()):
        if rec["agent"] != "claude" or missing(rec) or rec["host"] not in terminals:
            continue
        if rec.get("done") or rec.get("stopped") or rec.get("lost"):
            continue
        used = context_for_handoff(rec)
        ticket = tickets.get((rec.get("project", ""), tid))
        if used < CONTEXT_LIMIT or ticket is None or ticket["status"] in DONE:
            continue
        role = "reviewer" if tid.endswith("-review") else "coder"
        handoff = handed_off(rec, ticket)
        listed = terminals[rec["host"]]
        shown = own_row(rec, listed)
        if not handoff:
            session = rec.get("session") or int(rec["launched_at"])
            key = f"{tid}|context-ask|{session}"
            if key in st["sent"] or rec.get("stopping"):
                continue
            context = "is at" if rec.get("context") == used else "reached"
            text = (
                f"/goal Write the handoff of Concord ticket {tid} with the handoff skill, "
                f"then end your turn. dev-auto: your context {context} {used // 1000}k tokens, "
                f"at or above the {CONTEXT_LIMIT // 1000}k limit. Finish your current step, "
                f"write your handoff on ticket {tid} with the handoff skill, then stop. A fresh "
                "session continues from it."
            )
            if defer_stale_claude(st, rec, key, text) or asked(rec, now) or not shown:
                continue
            if st.statuses.get(tid, (None,))[0] != "limited":
                emit(st, key, text, worker=rec, note_on=tid, rearm=False)
            continue
        if cfg["limit_handoff"]:
            # handoff_stops stops it with a proposal; the tick starts no session for a handoff.
            continue
        if shown and now - (shown.get("lastOutputAt") or 0) / 1000 < SETTLED:
            continue
        other = None if shown else possible_session(st, tid, rec, listed, now)
        if other:
            # Not its own pane by handle or pty, but it may be that session: never a second one.
            why = f"its context handoff #{handoff['id']} is on its ticket"
            text = unowned_live_text(tid, rec, why, other["handle"])
            emit(st, f"{tid}|context-live|{int(rec['launched_at'])}", text, note_on=tid)
            continue
        picks = dict(ignore=tid, now=now, limits=st["limits"])
        row = slot(cfg, st["workers"], "claude", rec["model"], rec["host"], account=rec["account"], **picks)
        row = row or slot(cfg, st["workers"], "claude", rec["model"], rec["host"], **picks)
        launched = int(rec["launched_at"])
        if row is None:
            text = f"NO CAPACITY {tid}: its {role} reached its context limit and no claude account has a free slot"
            emit(st, f"{tid}|context-no-capacity|{launched}", text)
            continue
        if shown:
            if close_terminal(rec["host"], shown["handle"]) is None:
                print(f"{tid}: closing {shown['handle']} failed; tried again next tick")
                continue
            listed.pop(shown["handle"], None)
        situation = (
            f"The previous session on this ticket reached its context limit and wrote "
            f"handoff #{handoff['id']}; read the latest Handoff: update on the ticket and "
            "continue from it. "
        )
        if start(cfg, root, st, rec, row, brief(rec, ticket, situation), now):
            text = f"RESTARTED {tid} {role}: context {used // 1000}k, handoff #{handoff['id']}"
            emit(st, f"{tid}|restarted|{launched}", text)


def orchestrator_context(st, now):
    """The orchestrator at ORCHESTRATOR_CONTEXT_LIMIT is told RELAY once per terminal, and
    the line goes to events.md; it hands off itself and is never stopped."""
    pointer = st["orchestrator"]
    used = (pointer or {}).get("context") or 0
    if used < ORCHESTRATOR_CONTEXT_LIMIT:
        return
    key = f"orchestrator|context-relay|{pointer['terminal']}"
    if key in st["sent"]:
        return
    text = (
        f"RELAY: context {used // 1000}k. Write your handoff and start a fresh "
        "orchestrator, which runs set-orchestrator with its own terminal and account."
    )
    landed = tell(pointer, text, st, key)
    if landed:
        st["sent"][key] = int(now)
        event_line(st, landed["text"])
        print(landed["text"])


def status_rows(cfg, st, tickets, now):
    """One row per record and one for the orchestrator: what status.json holds and both
    status.md and the roadmap page render."""
    rows = []
    for tid, rec in sorted(st["workers"].items()):
        row = account(cfg, rec.get("account"), rec["host"]) or {}
        shown = rec.get("status") or {}
        rows.append(
            {
                "ticket": tid,
                "role": "reviewer" if tid.endswith("-review") else "coder",
                "agent": rec["agent"],
                "model": rec.get("model"),
                "effort": rec.get("effort"),
                "account": rec.get("account"),
                "account_label": row.get("label") or rec.get("account"),
                "host": rec["host"],
                "worktree": rec.get("worktree"),
                "launched_at": rec.get("launched_at"),
                "minutes": int((now - rec["launched_at"]) // 60) if rec.get("launched_at") else None,
                "context": rec.get("context") if rec["agent"] == "claude" else None,
                "phase": phase_of(st, tickets, tid, rec),
                "live_slot": holds_slot(rec),
                "lost": rec.get("lost") or False,
                "stopped": bool(rec.get("stopped")),
                "status": {k: shown.get(k) for k in ("state", "detail", "since", "seen")},
            }
        )
    pointer = st["orchestrator"]
    if pointer:
        row = account(cfg, pointer.get("account"), pointer["host"]) or {}
        rows.append(
            {
                "ticket": None,
                "role": "orchestrator",
                "agent": row.get("agent") or "claude",
                "model": None,
                "effort": None,
                "account": pointer.get("account"),
                "account_label": row.get("label") or pointer.get("account"),
                "host": pointer["host"],
                "context": pointer.get("context"),
                "status": {"state": "stopped" if st["stopped"] else "running"},
            }
        )
    return rows


def phase_of(st, tickets, tid, rec):
    """roadmap.phase of the record's coder ticket, from the ledger and its reviewer's
    record; None when the coder ticket is not in the ledger or a stamp cannot be read."""
    project = rec.get("project", "")
    base = tid.removesuffix("-review")
    coder = tickets.get((project, base))
    if coder is None:
        return None
    view = {**coder, "state": "done" if coder["status"] in DONE else "open", "wait": ROADMAP.latest_wait(coder)}
    review = tickets.get((project, f"{base}-review"))
    by = {f"{base}-review": {**review, "worker": st["workers"].get(f"{base}-review")}} if review else {}
    try:
        return ROADMAP.phase(view, by)
    except ValueError:
        return None


STATUS_COLUMNS = ("ticket", "role", "agent", "model", "effort", "account", "host", "state", "minutes", "context", "phase")


def status_cells(row):
    """A status row's cells as status.md shows them."""
    context = row.get("context")
    return {
        "ticket": row["ticket"] or "-",
        "role": row["role"],
        "agent": row["agent"] or "-",
        "model": row.get("model") or "-",
        "effort": row.get("effort") or "-",
        "account": row.get("account_label") or "-",
        "host": row["host"],
        "state": "lost" if row.get("lost") else (row.get("status") or {}).get("state") or "-",
        "minutes": "-" if row.get("minutes") is None else str(row["minutes"]),
        "context": "-" if context is None else f"{context // 1000}k",
        "phase": row.get("phase") or "-",
    }


def status_text(data):
    """status.md from status.json's data."""
    lines = [
        f"# dev-auto status, tick at {stamp(data['at'])}",
        "",
        "| " + " | ".join(STATUS_COLUMNS) + " |",
        "|" + "---|" * len(STATUS_COLUMNS),
    ]
    for row in data["rows"]:
        cells = status_cells(row)
        lines.append("| " + " | ".join(cells[c].replace("|", "/") for c in STATUS_COLUMNS) + " |")
    return "\n".join(lines) + "\n"


def write_status(cfg, root, st, tickets, now):
    """supervisor/status.json, then status.md rendered from it, each written atomically."""
    data = {"at": int(now), "rows": status_rows(cfg, st, tickets, now)}
    folder = root / "supervisor"
    for name, text in (("status.json", json.dumps(data, indent=1)), ("status.md", status_text(data))):
        ROADMAP.write_atomic(folder / name, text)


def close_revived(st, tickets, now):
    """Close with its tab each agent pane Orca revived for a finished ticket after cleanup
    dropped its record: no record or the orchestrator holds it, and a retired entry names its
    handle or launch ptyId, or, in a worktree the record owned and for a pane not listed
    on that host when any entry retired, its worktree, branch and agent. Every entry that names it must be
    of a ticket still complete or closed. Entries go after RETIRED."""
    retired = [e for e in st.get("retired", []) if now - e["at"] < RETIRED]
    st["retired"] = retired
    seen = {
        host: kept
        for host, keys in st.get("seen_panes", {}).items()
        if (kept := {key: at for key, at in keys.items() if now - at < RETIRED})
    }
    st["seen_panes"] = seen
    for host in sorted({e["host"] for e in retired}):
        terminals = listing(host)
        if terminals is None:
            continue
        records = [r for r in st["workers"].values() if r["host"] == host]
        held = {h for r in records for h in (r.get("terminal"), *r.get("handles", []))}
        ptys = {r.get("pty") for r in records}
        pointer = st["orchestrator"]
        if pointer and pointer["host"] == host:
            held.add(pointer["terminal"])
        # A pane any retirement on this host saw listed is never tied by its worktree alone.
        known = seen.get(host, {})
        for handle, row in sorted(terminals.items()):
            pty = row.get("ptyId")
            if handle in held or (pty and pty in ptys) or not row.get("agentIdentity"):
                continue
            branch = (row.get("branch") or "").removeprefix("refs/heads/")
            tied = [
                e
                for e in retired
                if e["host"] == host
                and (
                    (pty and pty == e.get("pty"))
                    or handle in (e.get("terminal"), *(e.get("handles") or []))
                    or (
                        e.get("owned")
                        and handle not in known
                        and pty not in known
                        and e.get("branch") == branch
                        and row["agentIdentity"] == e.get("agent")
                        and same_path(row.get("worktreePath"), e["worktree"], host)
                    )
                )
            ]
            status = [tickets.get((e.get("project") or "", e["ticket"]), {}).get("status") for e in tied]
            if not tied or any(s not in DONE for s in status):
                continue
            tid = tied[0]["ticket"]
            if close_terminal(host, handle) is None:
                print(f"{tid}: closing revived {handle} on {host} failed; tried again next tick")
            else:
                print(f"{tid}: closed revived {handle} on {host}; its ticket is {status[0]}")


def restart_sweep(cfg, st, tickets, host, terminals, was_down, now):
    """Every open record on a host unlisted at once, after a failed listing or for two or
    more records, is an Orca restart. Its records are lost under one STUCK instead of one
    each, and the tabs Orca restored are closed, once: every unrecorded agent terminal in
    a recorded worktree there, and every unrecorded agent or bare shell in a worktree of a
    finished or moved ticket that no open record owns."""
    live = {
        tid: rec
        for tid, rec in st["workers"].items()
        if rec["host"] == host
        and not rec.get("lost")
        and not rec.get("stopped")
        and not missing(rec)
        and tickets.get((rec.get("project", ""), tid), {"status": "closed"})["status"] not in DONE
        # Its gone pane is context_restarts' or handoff_stops' to handle.
        and (own_row(rec, terminals) or not kept_for_handoff(cfg, rec, tickets.get((rec.get("project", ""), tid))))
    }
    if not live or any(own_row(rec, terminals) for rec in live.values()):
        return
    if len(live) < 2 and not was_down:
        return
    names = " ".join(sorted(live))
    key = f"{host}|restart|{int(now)}"
    text = f"STUCK {host}: Orca restarted and lost {len(live)} workers: {names}. Each is left alone until a new launch"
    for tid, rec in live.items():
        rec["lost"] = (
            f"STUCK {tid} {rec['agent_id']}: terminal {rec.get('terminal')} went with the Orca "
            f"restart on {host}. Left alone until a new launch"
        )
        # The host's STUCK reports it, so locate's own never goes out.
        st["sent"].setdefault(f"{tid}|lost|{rec['launched_at']}", int(now))
    # Recorded before any close: a crash mid-sweep never sweeps again.
    st["restarts"][key] = text
    st.save()
    records = [r for r in st["workers"].values() if r["host"] == host]
    held = {h for r in records for h in (r.get("terminal"), *r.get("handles", []))}
    # A record listed under a new handle with its launch pty, one left out above too.
    held |= {row["handle"] for r in records if (row := own_row(r, terminals))}
    pointer = st["orchestrator"]
    if pointer and pointer["host"] == host:
        held.add(pointer["terminal"])
    open_trees = [
        r["worktree"]
        for t, r in st["workers"].items()
        if r["host"] == host
        and tickets.get((r.get("project", ""), t), {"status": "closed"})["status"] not in DONE
    ]
    # Worktrees of finished or moved tickets: Orca restores shells there too.
    left = [
        e["worktree"]
        for e in [*st.get("worktrees", {}).values(), *st.get("retired", [])]
        if e["host"] == host and not any(same_path(e["worktree"], w, host) for w in open_trees)
    ]
    restored = []
    for handle, row in terminals.items():
        if handle in held:
            continue
        where = row.get("worktreePath")
        if row.get("agentIdentity") and any(same_path(where, r["worktree"], host) for r in records):
            restored.append(handle)
        elif any(same_path(where, w, host) for w in left) and (
            row.get("agentIdentity") or bare_shell(read_screen(host, handle))
        ):
            restored.append(handle)
    failed = [handle for handle in restored if close_terminal(host, handle) is None]
    if failed:
        st["restarts"][key] = f"{text}. Closing {' '.join(failed)} failed"
    st.save()


def epoch(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def checkin_text(cfg, st, tickets, now):
    """(flagged count, checkin.txt): a line per open ticket, then the flagged ones."""
    workers = {
        (r.get("project", ""), t): r for t, r in st["workers"].items() if not missing(r)
    }
    lines, flagged = [], []
    for key, ticket in sorted(tickets.items()):
        if ticket["status"] in DONE:
            continue
        rec = workers.get(key)
        times = [epoch(u["created_at"]) for u in ticket["updates"]]
        last = max(filter(None, [*times, epoch(ticket["updated_at"])]), default=None)
        quiet = None if last is None else int((now - last) // 60)
        fixes = sum(
            u["content"].startswith("REVIEW VERDICT: FIX") for u in ticket["updates"]
        )
        ran = int((now - rec["launched_at"]) // 60) if rec else None
        name = f"{key[0]}/{key[1]}" if key[0] else key[1]
        owner = ticket["agent_id"] or (rec or {}).get("agent_id") or "-"
        lines.append(
            f"{name} owner {owner} state {ticket['status']} quiet "
            f"{'-' if quiet is None else f'{quiet}m'} fix rounds {fixes} "
            f"{'not launched' if ran is None else f'launched {ran}m ago'}"
        )
        why = []
        owned = ticket["agent_id"] or rec
        if owned and quiet is not None and quiet >= cfg["checkin_quiet_minutes"]:
            why.append(f"no ledger update for {quiet}m")
        if fixes > cfg["checkin_fix_rounds"]:
            why.append(f"{fixes} FIX rounds")
        if ran is not None and ran > cfg["checkin_long_minutes"]:
            why.append(f"running {ran}m")
        if why:
            flagged.append(f"{name}: {', '.join(why)}")
    body = ["open tickets:", *lines, "", f"flagged ({len(flagged)}):", *flagged]
    return len(flagged), "\n".join(body) + "\n"


def checkin(cfg, root, st, tickets, now):
    """Every checkin_minutes, write checkin.txt and tell the orchestrator one fixed line;
    never while an event waits for it. The line carries no ticket text."""
    every = cfg["checkin_minutes"] * 60
    last = st.get("checkin_at", 0)
    if not every or not st["orchestrator"] or st.held or now - last < every:
        return
    count, text = checkin_text(cfg, st, tickets, now)
    path = root / "supervisor" / "checkin.txt"
    path.write_text(text)
    # Saved before the send, as emit does: a crash mid-send skips one, never repeats it.
    st["checkin_at"] = now
    st.save()
    # The key carries the time: emit sends each key once, and every interval is new.
    line = f"CHECK-IN: {count} flagged, see {path}"
    if not emit(st, f"checkin|{now}", line):
        st["checkin_at"] = last
    # checkin_at, not the key, keeps it to one per interval.
    st["sent"].pop(f"checkin|{now}", None)


def launch_command(cfg, tid, rec):
    """(text, detail): the `launch` that starts this record's worker again, for the
    orchestrator to run or drop; dev-auto never runs it. A command send would refuse to
    type goes in the ticket note only. Both are empty with stuck_launch_command off."""
    if not cfg["stuck_launch_command"]:
        return "", ""
    effort = ["--effort", rec["effort"]] if rec.get("effort") else []
    command = shlex.join([
        "python3", str(HERE / "dev_auto.py"), "launch", tid, "--agent", rec["agent"],
        "--model", rec["model"], *effort, "--worktree", rec["worktree"], "--host", rec["host"],
    ])
    if SAFE.fullmatch(command):
        return f": {command}", ""
    return ". Its launch command is on the ticket", command


def locate(cfg, st, tid, rec, terminals):
    """The record's terminal row, rebound to a new handle only by its launch ptyId. With
    no such row the record is lost: reported once and never acted on until a launch
    replaces it."""
    row = own_row(rec, terminals)
    if row:
        rebind(tid, rec, row)
        return row
    command, detail = launch_command(cfg, tid, rec)
    rec["lost"] = (
        f"STUCK {tid} {rec['agent_id']}: terminal {rec.get('terminal')} is not listed "
        f"and no listed terminal has its pty. Left alone until a new launch{command}"
    )
    emit(st, f"{tid}|lost|{rec['launched_at']}", rec["lost"], note_on=tid, detail=detail)
    return None


def rebind(tid, rec, row):
    """The record follows its own row (see own_row) to the handle it is listed under."""
    if row["handle"] != rec.get("terminal"):
        print(f"{tid}: {rec.get('terminal')} is gone; its pty is {row['handle']}")
        if rec.get("terminal"):
            rec.setdefault("handles", []).append(rec["terminal"])
        rec["terminal"] = row["handle"]
    elif not rec.get("pty") and row.get("ptyId"):
        # Listed under the handle its create returned, so this pty is its own.
        rec["pty"] = row["ptyId"]


CLEARED_TRIES = 3


def unpushed_owner(event, tid, head, known):
    """The "<project>|<ticket>" key in st["unpushed"] that queued this UNPUSHED event: the
    one it names, or for an event queued before it named one, the only entry for that
    ticket at that head; None when that cannot be told."""
    if len(event) > 4:
        return event[4]
    owners = [k for k, e in known.items() if k.split("|", 1)[1] == tid and e.get("head") == head]
    return owners[0] if len(owners) == 1 else None


def clear_verdicts(st, tickets):
    """Each PUSH someone other than the ticket's owner posts is one CLEARED REVIEW, whether
    or not the ticket has a record. Every update id passes the floor once; one the
    orchestrator does not take waits in the state, not on a record, and is tried each tick.
    A first run starts after the newest update, except what a record had yet to send."""
    newest = max((u["id"] for t in tickets.values() for u in t["updates"]), default=0)
    first = "cleared_floor" not in st
    floor = st.setdefault("cleared_floor", newest)
    pending = st.setdefault("cleared_pending", {})
    since = {(r.get("project", ""), t): r.get("since", 0) for t, r in st["workers"].items()}
    standing = set()
    for (project, tid), ticket in tickets.items():
        # A state from before the floor keeps each record's own since marker.
        start = since.get((project, tid), newest) if first else floor
        for update, sha in standing_pushes(ticket):
            key = f"{tid}|cleared|{update['id']}"
            standing.add(key)
            if update["id"] > start and key not in st["sent"]:
                pending[key] = [tid, f"CLEARED REVIEW {tid}: PUSH {sha}", 0]
    st["cleared_floor"] = max(floor, newest)
    for key, event in list(pending.items()):
        tid, kind, ref = key.split("|")[:3]
        # An UNPUSHED stands while a PUSH that cleared its head does. One queued before
        # those were kept names none and is dropped like a withdrawn one; forgetting its
        # head lets unpushed raise it again from the PUSHes standing then.
        backs = event[3] if len(event) > 3 else []
        if kind == "unpushed" and not any(f"{tid}|cleared|{i}" in standing for i in backs):
            del pending[key]
            known = st.get("unpushed", {})
            owner = unpushed_owner(event, tid, ref, known)
            # Only while the owner still remembers this head: a newer delivered head keeps its mark.
            if owner in known and known[owner].get("head") == ref:
                known[owner].pop("head")
            continue
        if kind == "cleared" and key not in standing:
            # Withdrawn since it was queued: superseded, or a later FIX on its SHA.
            del pending[key]
            continue
        tid, text, tries = event[:3]
        proof = None
        if kind == "unpushed":
            # Saved with the outbox reservation: a replay is judged on these PUSHes alone.
            proof = {"backs": backs, "owner": unpushed_owner(event, tid, ref, st.get("unpushed", {}))}
        if emit(st, key, text, proof=proof) or key in st["sent"]:
            # Past the floor, its update is never read again.
            del pending[key]
            st["sent"].pop(key, None)
            continue
        event[2] = tries + 1
        if event[2] == CLEARED_TRIES:
            say(
                tid,
                f"STUCK {tid}: {text} has not reached the orchestrator after "
                f"{CLEARED_TRIES} tries. Tried again each tick",
            )


# Per (dir, branch, cleared SHAs) triple: the repo's common dir, which outlives a removed
# worktree, each cleared SHA that names one commit there as an object id (never a ref of
# that name), then the branch's head and parents and its commits not on origin's ref of
# it; with no such ref, those on no origin ref, or `new` when origin has them all but
# its default branch lacks the head (a merged branch whose origin ref went is done). Git
# verifies a missing ref and an unreadable one alike, so a triple with no U line was not read.
UNPUSHED = (
    "i=0; while [ $# -gt 2 ]; do i=$((i+1)); d=$1; b=$2; l=$3; shift 3; "
    'cd "$d" 2>/dev/null || continue; '
    "c=$(git rev-parse --path-format=absolute --git-common-dir) || continue; "
    'echo "C $i $c"; '
    'for s in $l; do f=$(git rev-parse --disambiguate="$s" 2>/dev/null); '
    '[ -n "$f" ] && [ "$f" = "${f%%[[:space:]]*}" ] && [ "$(git cat-file -t "$f")" = commit ] && echo "D $i $f $s"; done; '
    'x=$(git rev-list --parents -n 1 "refs/heads/$b" --) || continue; '
    'if git rev-parse -q --verify "refs/remotes/origin/$b" >/dev/null; '
    'then n=$(git rev-list --count "refs/heads/$b" --not "refs/remotes/origin/$b") || continue; '
    'else n=$(git rev-list --count "refs/heads/$b" --not --remotes=origin) || continue; '
    'if [ "$n" = 0 ] && ! git merge-base --is-ancestor "refs/heads/$b" refs/remotes/origin/HEAD 2>/dev/null; '
    'then n=new; fi; fi; '
    'echo "U $i $n $x"; done; echo __unpushed_read'
)
BRANCH = re.compile(r"[\w.-]+(?:/[\w.-]+)*")


def unpushed(st, tickets):
    """A ticket's feature branch, or with `Feature branch: none` its own Branch, whose
    local head is ahead of origin and is, or merged, a SHA someone other than the owner
    cleared with PUSH: one UNPUSHED event per head, to the orchestrator through the
    CLEARED REVIEW writer. One git read per host and tick; an unread host raises
    nothing. Each ticket's host and git dir are stored, so a removed worktree is still
    read through its repo."""
    known = st.setdefault("unpushed", {})
    records = {(r.get("project", ""), t): r for t, r in st["workers"].items()}
    asks = {}
    for (project, tid), ticket in tickets.items():
        notes = ticket.get("notes") or ""
        feature = FEATURE_BRANCH.search(notes)
        named = re.search(r"^Branch: `([^`]+)`", notes, re.MULTILINE)
        branch = feature[1] if feature and feature[1] != "none" else named and named[1]
        cleared = {}
        for update, sha in standing_pushes(ticket):
            cleared.setdefault(sha, []).append(update["id"])
        key, rec = f"{project}|{tid}", records.get((project, tid))
        entry = known.get(key)
        if not cleared or not branch or not BRANCH.fullmatch(branch):
            continue
        if rec and (not entry or entry["host"] != rec["host"]):
            entry = known[key] = {"host": rec["host"], "dir": rec["worktree"]}
        if not entry:
            continue
        where = rec["worktree"] if rec else entry["dir"]
        asks.setdefault(entry["host"], []).append((key, tid, where, branch, cleared, rec))
    for key in set(known) - {f"{p}|{t}" for p, t in tickets}:
        del known[key]
    pending = st.setdefault("cleared_pending", {})
    for host, wanted in asks.items():
        live = [r["worktree"] for r in st["workers"].values() if r["host"] == host]
        probe = live[0] if live else str(Path(wanted[0][2]).parent)
        if host == "local":
            probe = "/"
        pairs = [part for w in wanted for part in (w[2], w[3], " ".join(sorted(w[4])))]
        code, out = run_on(host, probe, ["sh", "-c", UNPUSHED, "unpushed", *pairs])
        lines = [line.split(" ", 2) for line in out.splitlines()]
        if code != 0 or ["__unpushed_read"] not in lines:
            continue
        read = {(x[0], x[1]): x[2] for x in lines if len(x) == 3 and x[0] != "D"}
        resolved = {}
        for x in lines:
            if x[0] == "D" and len(x) == 3 and len(found := x[2].split()) == 2:
                resolved.setdefault(x[1], {}).setdefault(found[0], set()).add(found[1])
        for n, (key, tid, _, branch, cleared, rec) in enumerate(wanted, 1):
            entry = known[key]
            entry["dir"] = read.get(("C", str(n)), entry["dir"])
            if ("U", str(n)) not in read:
                # shortcut: a branch deleted before a record-less read keeps its entry
                # until the ticket leaves the ledger; age it out if entries pile up.
                continue
            ahead, *shas = read["U", str(n)].split()
            if not (ahead.isdigit() or ahead == "new") or not all(map(SHA.fullmatch, shas)):
                continue
            # The standing PUSHes whose SHA, resolved in this repo, is the head or a merged parent.
            mine = resolved.get(str(n), {})
            hit = sorted(
                {i for s in [shas[0], *shas[2:]] for c in mine.get(s, ()) for i in cleared.get(c, ())}
            )
            if not hit or ahead == "0":
                if not rec:
                    del known[key]
                continue
            if entry.get("head") == shas[0]:
                # Still unsent: it stands on the PUSHes that clear its head now.
                # Its own queued notice, or one queued before notices named their project.
                for queued_key in (f"{tid}|unpushed|{shas[0]}|{key.split('|', 1)[0]}", f"{tid}|unpushed|{shas[0]}"):
                    queued = pending.get(queued_key)
                    if queued is not None and unpushed_owner(queued, tid, shas[0], known) == key:
                        queued[3:] = [hit, key]
                continue
            entry["head"] = shas[0]
            plural = "" if ahead == "1" else "s"
            where = "not on origin" if ahead == "new" else f"{ahead} commit{plural} ahead of origin"
            text = f"UNPUSHED {tid}: {branch} on {host} at {shas[0][:7]}, {where}"
            # Keyed by project too: one ticket id at one head in two projects is two notices.
            pending[f"{tid}|unpushed|{shas[0]}|{key.split('|', 1)[0]}"] = [tid, text, 0, hit, key]


# Delivery (auto_push): push a cleared head, open its pull request, turn on auto-merge.

FULL_SHA = re.compile(r"[0-9a-f]{40}")
PR_INTO = re.compile(
    r"Feature branch:\s*none\b[^\n]*?\b(?:PR|pull request) into (?:dot-agents )?"
    r"`?([\w./-]+?)`?(?=[\s,.;)]|$)"
)
ORIGIN_SLUG = re.compile(r"([\w.-]+)[/:]([\w.-]+?)(?:\.git)?/?$")
# A remote URL gh can name as host/owner/repo with nothing dropped: no port, no deeper path.
GH_REMOTE = re.compile(
    r"(?:(?:https?|ssh|git)://(?:[^@/]*@)?([^/:@]+)/|(?:[^@/:]+@)?([^/:@]+):)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?"
)


def gh_repo(url):
    """The host/owner/repo gh names for origin's URL, or None when none names it losslessly.
    A local path names no gh repository: its owner/repo suffix proves nothing about one."""
    site = GH_REMOTE.fullmatch(url)
    return f"{site[1] or site[2]}/{site[3]}/{site[4]}" if site else None

# A remote URL as git reads it: with a scheme, or scp-style host:path; else a local path.
REMOTE_URL = re.compile(r"[a-z][\w+.-]*://|[^/]+:")
# The most pull requests one list reads; a full page is an unread history.
PR_PAGE = 200
MIGRATIONS = r"(^|/)migrations/"
MIGRATION_REGISTRY = "src/backend/storage/sqlite/migrations.ts"
# The first-parent chain read below origin's base; one more than this is a refusal.
CHAIN = 500
# Per branch and base, then the PUSH SHAs: origin's URL with any user and password cut
# (R), the repo's common dir, each SHA resolved to one commit (V) and those whose tree is
# the head's (E), the branch head, origin's tip of it (O, or X when it is no ancestor of
# the head), the worktree holding it and, when git there pushes origin to exactly its
# fetch URL and nowhere else, that push URL's hash (Q, the URL itself never leaves the
# host), origin's base tip, whether the head is in it (Y), the head's first-parent chain
# down to it with each 2-parent merge's tree check (T) and parents already in the base
# (I), the chain's commits not on origin's branch (F), and the files the head adds over
# the base, renames as additions, as hex bytes of NUL-ended raw paths (A), plus an
# addition or update to the SQLite migration registry (M). A local branch behind
# origin's (S) is read at origin's tip, and moved there when no worktree has it.
FACTS = (
    "b=$1; t=$2; shift 2; "
    "u=$(git remote get-url origin) || exit 2; "
    "c=$(git rev-parse --path-format=absolute --git-common-dir) || exit 2; "
    'printf "R %s\\n" "$u" | sed -E "s#^(R [a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@#\\1#"; echo "G $c"; '
    "git fetch -q origin || exit 2; "
    'h=$(git rev-parse -q --verify "refs/heads/$b^{commit}") || { echo __facts_read; exit 0; }; '
    'o=$(git ls-remote --heads origin "refs/heads/$b") || exit 2; o=${o%%[[:space:]]*}; '
    'w=$(git worktree list --porcelain | awk -v r="branch refs/heads/$b" '
    "'/^worktree /{p=substr($0,10)} $0==r{print p; exit}'); "
    'p=$(git -C "${w:-.}" remote get-url --push --all origin 2>/dev/null) && [ -n "$p" ] && [ "$p" = "$u" ] '
    '&& echo "Q $(printf %s "$p" | git hash-object --stdin)"; '
    'if [ -n "$o" ] && [ "$o" != "$h" ] && ! git merge-base --is-ancestor "$o" "$h" 2>/dev/null '
    '&& git merge-base --is-ancestor "$h" "$o" 2>/dev/null; then echo "S $h"; '
    '[ -n "$w" ] || git update-ref "refs/heads/$b" "$o" "$h" || exit 2; h=$o; fi; '
    'echo "H $h"; '
    'for s in "$@"; do f=$(git rev-parse --disambiguate="$s" 2>/dev/null); '
    '[ -n "$f" ] && [ "$f" = "${f%%[[:space:]]*}" ] && [ "$(git cat-file -t "$f")" = commit ] || continue; '
    'echo "V $s $f"; [ "$(git rev-parse "$f^{tree}")" = "$(git rev-parse "$h^{tree}")" ] && echo "E $f"; done; '
    'if [ -z "$o" ]; then :; elif git merge-base --is-ancestor "$o" "$h" 2>/dev/null; '
    'then echo "O $o"; else echo "X $o"; fi; '
    'if [ -n "$w" ]; then s=$(git -C "$w" status --porcelain --untracked-files=all) || exit 2; '
    'if [ -z "$s" ]; then echo "W clean $w"; else echo "W dirty $w"; fi; fi; '
    'if [ -n "$t" ]; then x=$(git ls-remote --heads origin "refs/heads/$t") || exit 2; '
    'x=${x%%[[:space:]]*}; if [ -n "$x" ]; then echo "B $x"; '
    'git merge-base --is-ancestor "$h" "$x" 2>/dev/null && echo "Y"; '
    f'git rev-list --first-parent --parents -n {CHAIN + 1} "$h" "^$x" | sed "s/^/C /"; '
    'if [ -n "$o" ] && git merge-base --is-ancestor "$o" "$h" 2>/dev/null; then echo "N"; '
    f'git rev-list --first-parent -n {CHAIN + 1} "$h" "^$x" "^$o" | sed "s/^/F /"; fi; '
    f'git rev-list --first-parent --merges --parents -n {CHAIN + 1} "$h" "^$x" | '
    'while read -r m p q r; do if [ -n "$r" ]; then continue; fi; '
    'z=$(git merge-tree --write-tree "$p" "$q" 2>/dev/null) && '
    '[ "${z%%[[:space:]]*}" = "$(git rev-parse "$m^{tree}")" ] && echo "T $m"; '
    'git merge-base --is-ancestor "$q" "$x" 2>/dev/null && echo "I $q"; done; '
    'git diff --no-renames -z --name-only --diff-filter=A "$x...$h" | od -An -v -tx1 | sed "s/^/A /"; '
    f'm=$(git diff --no-renames --name-only --diff-filter=AM "$x...$h" -- {MIGRATION_REGISTRY}) || exit 2; '
    '[ -z "$m" ] || echo M; fi; fi; '
    "echo __facts_read"
)
# In the branch's worktree: the probed head checked out and clean, the repo's check
# passing, the head and a clean status again after it, then that exact SHA pushed
# without force, once origin's push URLs there still hash as the probe read them. Its
# last line is P <result>.
PUSH = (
    'w=$1; h=$2; b=$3; k=$4; e=$5; cd "$w" || exit 2; '
    'v() { [ "$(git rev-parse HEAD)" = "$h" ] || { echo "P moved"; exit 0; }; '
    "s=$(git status --porcelain --untracked-files=all) || exit 2; "
    '[ -z "$s" ] || { echo "P dirty"; exit 0; }; }; v; '
    'if [ -n "$k" ]; then sh -c "$k" >/dev/null 2>&1 || { echo "P check $?"; exit 0; }; v; fi; '
    '[ "$(printf %s "$(git remote get-url --push --all origin)" | git hash-object --stdin)" = "$e" ] '
    '|| { echo "P remote"; exit 0; }; '
    'if git push -q origin "$h:refs/heads/$b" >/dev/null 2>&1; then echo "P pushed"; '
    'else echo "P push $?"; fi'
)
# A branch no worktree has checked out, from any worktree of its repo: the repo's check
# in a throwaway detached worktree at the head, the ref still at the head, then that
# exact SHA pushed without force, once origin's push URLs still hash as the probe read
# them. Its last line is P <result>.
PUSH_REF = (
    "h=$1; b=$2; k=$3; e=$4; "
    'if [ -n "$k" ]; then t=$(mktemp -d) || exit 2; '
    'git worktree add -q --detach "$t/w" "$h" >/dev/null 2>&1 || { rm -rf "$t"; echo "P worktree"; exit 0; }; '
    '(cd "$t/w" && sh -c "$k") >/dev/null 2>&1; r=$?; '
    'git worktree remove --force "$t/w" >/dev/null 2>&1; rm -rf "$t"; '
    '[ $r = 0 ] || { echo "P check $r"; exit 0; }; fi; '
    '[ "$(git rev-parse -q --verify "refs/heads/$b")" = "$h" ] || { echo "P moved"; exit 0; }; '
    '[ "$(printf %s "$(git remote get-url --push --all origin)" | git hash-object --stdin)" = "$e" ] '
    '|| { echo "P remote"; exit 0; }; '
    'if git push -q origin "$h:refs/heads/$b" >/dev/null 2>&1; then echo "P pushed"; '
    'else echo "P push $?"; fi'
)


def read_facts(code, out):
    """FACTS output as a dict, or None when the probe did not finish."""
    lines = out.splitlines()
    if code != 0 or "__facts_read" not in lines:
        return None
    facts = {
        "chain": [],
        "merges": set(),
        "in_base": set(),
        "resolved": {},
        "same_tree": set(),
        "fresh": None,
        "push": None,
        "migration_registry": "M" in lines,
    }
    hexed = []
    for line in lines:
        tag, _, rest = line.partition(" ")
        if tag == "V":
            short, _, full = rest.partition(" ")
            facts["resolved"][short] = full
        elif tag == "E":
            facts["same_tree"].add(rest)
        elif tag == "S":
            facts["stale"] = rest
        elif tag == "Q":
            facts["push"] = rest
        elif tag == "Y":
            facts["in_base_head"] = True
        elif tag == "N":
            facts["fresh"] = set()
        elif tag == "F":
            facts["fresh"].add(rest)
        if tag in "RGHB" and len(tag) == 1:
            facts[{"R": "url", "G": "common", "H": "head", "B": "base_tip"}[tag]] = rest
        elif tag in ("O", "X"):
            facts["origin"], facts["diverged"] = rest, tag == "X"
        elif tag == "W":
            state, _, path = rest.partition(" ")
            facts["worktree"], facts["dirty"] = path, state == "dirty"
        elif tag == "C":
            sha, *parents = rest.split()
            facts["chain"].append((sha, parents))
        elif tag == "T":
            facts["merges"].add(rest)
        elif tag == "I":
            facts["in_base"].add(rest)
        elif tag == "A":
            hexed.append(rest)
    # Each raw path as text: UTF-8, any other byte kept as a surrogate.
    raw = bytes.fromhex("".join(hexed)).split(b"\0")
    facts["added"] = [p.decode("utf-8", "surrogateescape") for p in raw if p]
    return facts


def uncleared(facts, cleared):
    """The first commit on the head's first-parent chain down to origin's base that no
    clearance covers, or None. A cleared commit covers its whole history, and so does one
    already on origin's branch, as verify-merges reads it; a clean 2-parent merge of a
    cleared commit, or of one already in the base, is passed over."""
    chain, fresh = facts["chain"], facts["fresh"]
    for sha, parents in chain[:CHAIN]:
        if sha in cleared or (fresh is not None and sha not in fresh):
            return None
        merged = parents[1:]
        clean = sha in facts["merges"] and len(merged) == 1
        if clean and (merged[0] in cleared or merged[0] in facts["in_base"]):
            continue
        return sha
    return chain[CHAIN][0] if len(chain) > CHAIN else None


def pr_text(tickets):
    """The newest update with a line starting `PR title: <title>` and a later `PR body:`
    line among these tickets: (update id, title, body), or None."""
    found = None
    for ticket in tickets:
        for u in ticket["updates"]:
            start = re.search(r"^PR title:", u["content"], re.MULTILINE)
            if u["agent"] == SUPERVISOR or not start:
                continue
            head, sep, body = u["content"][start.start():].partition("\nPR body:")
            title = head.split("\n", 1)[0].removeprefix("PR title:").strip()
            if sep and title and body.strip() and (not found or u["id"] > found[0]):
                found = (u["id"], title, body.strip())
    return found


def delivery_groups(tickets):
    """Branches with a standing PUSH (see standing_pushes), keyed by (project,
    branch): the tickets naming it, the base the notes name (None for a feature
    branch, whose base is its repo's), and each clearance as (update id, sha, ticket)."""
    groups = {}
    for (project, tid), ticket in tickets.items():
        notes = ticket.get("notes") or ""
        feature = FEATURE_BRANCH.search(notes)
        named = re.search(r"^Branch: `([^`]+)`", notes, re.MULTILINE)
        if feature and feature[1] != "none":
            branch, base = feature[1], None
        elif named and NO_FEATURE_BRANCH.search(notes):
            into = PR_INTO.search(notes)
            branch, base = named[1], into[1] if into else ""
        else:
            continue
        if not BRANCH.fullmatch(branch):
            continue
        group = groups.setdefault(
            (project, branch), {"tickets": [], "base": base, "clears": []}
        )
        group["tickets"].append((tid, ticket))
        group["clears"] += [(u["id"], sha, tid) for u, sha in standing_pushes(ticket)]
    return {key: g for key, g in groups.items() if g["clears"]}


def deliver(cfg, st, tickets, now):
    """With auto_push on, each branch whose head a reviewer cleared is pushed, gets its
    pull request and auto-merge, once, behind the guards; a feature branch's pull
    request waits until every ticket naming it is complete or closed. A refused head
    is one PUSH REFUSED event per guard. Each stage is stored, so a repeated tick does
    nothing more and a half-done sequence goes on from the missing stage."""
    deliveries = st.setdefault("deliveries", {})
    records = {(r.get("project", ""), t): r for t, r in st["workers"].items()}
    groups = delivery_groups(tickets)
    for key in set(deliveries) - {f"{p}|{b}" for p, b in groups}:
        del deliveries[key]
    for (project, branch), group in groups.items():
        d = deliveries.setdefault(f"{project}|{branch}", {"told": []})
        # Cleanup keeps these tickets' worktrees until the pull request merges.
        d["tickets"] = [t for t, _ in group["tickets"]]
        # One place per recorded host, else the last one read.
        places = []
        for t, _ in group["tickets"]:
            r = records.get((project, t))
            if r and r["host"] not in [h for h, _ in places]:
                places.append((r["host"], r["worktree"]))
        if not places and "dir" in d:
            places = [(d["host"], d["dir"])]
        if not places:
            continue
        members = [ticket for _, ticket in group["tickets"]]
        seen = [
            sorted(sha for _, sha, _ in group["clears"]),
            [t["status"] for t in members],
            max(
                (u["id"] for t in members for u in t["updates"] if u["agent"] != SUPERVISOR),
                default=0,
            ),
        ]
        last = d.get("checked")
        if last and last["seen"] == seen and (last["finished"] or now - last["at"] < RECHECK):
            continue
        finished = advance(cfg, st, branch, group, d, places)
        if finished is not None:
            # An open ticket's coder can still move the branch without a ledger update.
            finished = finished and all(t["status"] in DONE for t in members)
            d["checked"] = {"at": now, "seen": seen, "finished": finished}


PR_VIEW = "state,headRefOid,mergeStateStatus,statusCheckRollup"
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")


def check_state(view, head):
    """(state, failed names) of a pull request's checks at `head`: "green" once every
    check passed, "red" once one failed, "pending" while one runs, none is listed yet or
    GitHub has not caught up with the head."""
    if view.get("headRefOid") != head:
        return "pending", []
    runs = view.get("statusCheckRollup") or []
    failed, waiting = [], False
    for run in runs:
        if run.get("__typename") == "StatusContext":
            done, result = run.get("state") not in ("PENDING", "EXPECTED"), run.get("state")
        else:
            done, result = run.get("status") == "COMPLETED", run.get("conclusion")
        if not done:
            waiting = True
        elif result not in PASSED:
            failed.append(run.get("name") or run.get("context") or "?")
    if failed:
        return "red", failed
    return ("pending" if waiting or not runs else "green"), []


def advance(cfg, st, branch, group, d, places):
    """One branch's next stages, read on each (host, worktree) place and run on the one
    holding its newest head: True when nothing is left until the ledger changes, False
    when it is read again after RECHECK (a refusal, a red check, a merge GitHub holds),
    None to try again next tick."""
    members = [ticket for _, ticket in group["tickets"]]
    clears = sorted(group["clears"])
    tid, (host, where) = clears[-1][2], places[0]
    text = pr_text(members)
    done = all(t["status"] in DONE for t in members)
    own = group["base"] is not None

    def refuse(guard, reason, head=clears[-1][1], detail=""):
        # The typed line keeps to SAFE; a path goes in the printed detail.
        mark = f"{head}|{guard}"
        if mark not in d["told"]:
            line = f"PUSH REFUSED {tid}: {branch} at {head[:7]} on {host}, {guard}: {reason}"
            event = f"{tid}|refused|{head}|{guard}"
            if emit(st, event, line, detail=detail) or event in st["sent"]:
                d["told"].append(mark)
        return False

    named = sorted({sha for _, sha, _ in clears})

    def read(host, where):
        # One place's facts at its repo's base: (facts, slug, repo, base), or None.
        repo = cfg["repos"].get(d.get("slug"), {})
        base = group["base"] if own else repo.get("base", "")
        facts = read_facts(*run_on(host, where, ["sh", "-c", FACTS, "facts", branch, base, *named]))
        if facts is None:
            return None
        slug = ORIGIN_SLUG.search(facts["url"])
        slug = f"{slug[1]}/{slug[2]}" if slug else facts["url"]
        if slug != d.get("slug"):
            # The first read of this repo, or a moved origin: a feature reads again with
            # its repo's base.
            d["slug"] = slug
            repo = cfg["repos"].get(slug, {})
            if not own and repo.get("base", "") != base:
                base = repo.get("base", "")
                argv = ["sh", "-c", FACTS, "facts", branch, base, *named]
                facts = read_facts(*run_on(host, where, argv))
        return facts and (facts, slug, repo, base)

    reads = []
    for place in places:
        got = read(*place)
        if got is None:
            return None
        reads.append((place, *got))
    held = [r for r in reads if r[1].get("head")]

    def older(r, o):
        # r's head is on o's first-parent chain, or git proved it in the base both read
        # from one origin URL at one tip; an empty chain alone proves nothing.
        if r[1]["head"] in {sha for sha, _ in o[1]["chain"]}:
            return True
        same = r[1]["url"] == o[1]["url"] and r[1].get("base_tip") == o[1].get("base_tip")
        return bool(r[1].get("in_base_head") and same and o[1]["chain"])

    newest = [r for r in held if not any(older(r, o) for o in held if o[1]["head"] != r[1]["head"])]
    if len({r[1]["head"] for r in newest}) > 1:
        heads = ", ".join(f"{r[1]['head'][:7]} on {r[0][0]}" for r in newest)
        return refuse("hosts", f"the branch has heads {heads}")
    (host, where), facts, slug, repo, base = (newest or reads)[0]
    d["host"], d["dir"] = host, str(PurePosixPath(facts["common"]).parent)
    # A short SHA counts once it names one commit in this repo.
    cleared = {facts["resolved"][sha] for sha in named if sha in facts["resolved"]}
    head = facts.get("head")
    if not head:
        # No local branch: deleted once merged, or never here; nothing to push.
        return True
    if not cleared:
        return refuse("sha", "no PUSH names a SHA that resolves to one commit here", head)
    if not REMOTE_URL.match(facts["url"]) and not facts["url"].startswith("/"):
        # Read from the probe and the push worktree alike, a relative path can name two repos.
        return refuse("remote", "origin has a relative URL", head)
    selector = gh_repo(facts["url"])
    if not selector:
        # gh must read the same repository git checked; a port, an extra path or a local
        # path has no gh name.
        return refuse("remote", "origin URL maps to no gh repository", head)
    if slug not in cfg["repos"]:
        # Fail closed: a repo's rules (its protected bases first) must be written down.
        return refuse("repo", f"repos.{slug} is not set", head)
    if not facts["push"]:
        # Where the push runs, git must push origin to exactly the fetch URL every check
        # above read; no alias of it counts.
        return refuse("remote", f"origin pushes elsewhere than {slug}", head)

    def gh(*args):
        # The checked repo, never one gh picks from the checkout's other remotes.
        return run_on(host, where, ["gh", *args, "--repo", selector])

    if not base:
        whose = "the notes name none" if own else f"repos.{slug}.base is unset"
        return refuse("base", f"no pull request base: {whose}", head)
    if base in repo.get("protected", ["main"]):
        return refuse("base", f"{base} is protected", head)
    if "base_tip" not in facts:
        return refuse("base", f"{base} is not on origin", head)
    if facts.get("stale"):
        print(f"{branch} on {host}: local ref {facts['stale'][:7]} behind origin, read at {head[:7]}")
    if facts.get("diverged"):
        tip = facts["origin"][:7]
        return refuse("diverged", f"the origin branch at {tip} is not in the head", head)
    if not facts["chain"]:
        if not facts.get("in_base_head"):
            # An empty chain from a failed read proves nothing: read again next tick.
            return None
        d["merged"] = True
        return True
    stray = uncleared(facts, cleared)
    if stray:
        detail = f"{stray[:7]} is no cleared commit or clean merge of one"
        return refuse("cleared", detail, head)
    worktree = facts.get("worktree")
    to_push, opens = facts.get("origin") != head, own or done
    if to_push and worktree and facts["dirty"]:
        return refuse("dirty", "its worktree has uncommitted changes", head, worktree)
    if not to_push and not opens:
        return True
    # Before any write, the branch's open pull request as it is now, never a stored one.
    fields = "number,baseRefName,autoMergeRequest,isCrossRepository"
    code, out = gh("pr", "list", "--head", branch, "--state", "open", "--limit", str(PR_PAGE), "--json", fields)
    try:
        prs = json.loads(out) if code == 0 else None
    except ValueError:
        prs = None
    if not isinstance(prs, list) or not all(isinstance(pr, dict) for pr in prs):
        return None
    if len(prs) >= PR_PAGE:
        return refuse("pull-request", f"{PR_PAGE} or more open pull requests share the branch name", head)
    # A fork's pull request from a branch of the same name is not this branch's.
    prs = [pr for pr in prs if pr.get("isCrossRepository") is not True]
    if len(prs) > 1:
        # A feature has one pull request; two on one head leave any base unchecked.
        numbers = " ".join(str(pr.get("number")) for pr in prs)
        return refuse("pull-request", f"open pull requests {numbers} share the branch", head)
    if prs and prs[0].get("baseRefName") != base:
        number, other = prs[0].get("number"), prs[0].get("baseRefName")
        return refuse("base", f"open pull request {number} has base {other}", head)
    if not prs:
        # An operator's closure stands: a branch whose last pull request into this base
        # was closed unmerged gets no new one, and no push, until a person acts.
        argv = ["pr", "list", "--head", branch, "--base", base, "--state", "all", "--limit", str(PR_PAGE)]
        code, out = gh(*argv, "--json", "number,state,isCrossRepository")
        try:
            past = json.loads(out) if code == 0 else None
        except ValueError:
            past = None
        if not isinstance(past, list) or not all(isinstance(pr, dict) for pr in past):
            return None
        if len(past) >= PR_PAGE:
            # gh lists newest first: forks could fill the page and hide this branch's last.
            reason = f"{PR_PAGE} or more pull requests share the branch name, its history is unread"
            return refuse("pull-request", reason, head)
        past = [pr for pr in past if pr.get("isCrossRepository") is not True]
        last = max(past, key=lambda pr: pr.get("number") or 0, default=None)
        if last and last.get("state") == "CLOSED":
            reason = (
                f"pull request {last.get('number')} into {base} was closed unmerged, "
                "reopen it or open its replacement by hand"
            )
            return refuse("closed", reason, head)
    if not prs and not text:
        # A new pull request needs its title and body; an open one does not.
        if done:
            return refuse("title", "no update holds a PR title: and a PR body: part", head)
        if own:
            # The coder records the title and body after the PUSH: wait for it.
            return True
    if isinstance(d.get("auto"), str):
        # Saved before the marker named its pull request, so which one it was is unknown.
        # Auto-merge on: dropped, so the migration hold reads afresh and arming again is
        # harmless. Off: an operator may have turned it off, or it may be a replacement,
        # so a person decides.
        if d["auto"] != head or not prs or prs[0].get("autoMergeRequest"):
            del d["auto"]
        else:
            reason = (
                f"auto-merge is off on pull request {prs[0].get('number')} and the saved "
                "marker names no pull request, turn it on or merge by hand"
            )
            return refuse("auto-merge", reason, head)
    d["pr"] = prs[0].get("number") if prs else None
    try:
        pattern = re.compile(repo.get("migrations", MIGRATIONS))
    except re.error as error:
        return refuse("migrations", f"its repo migrations setting is no regex, {error}", head)
    migration = MIGRATION_REGISTRY if facts["migration_registry"] else next(
        (f for f in facts["added"] if pattern.search(f)), None
    )
    if migration:
        # Printable: a byte no UTF-8 decodes shows as U+FFFD.
        migration = migration.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    migration_scope = f"|migration|{selector.casefold()}|{head}|"
    held = False
    if migration and d["pr"]:
        suffix = f"{migration_scope}{d['pr']}"
        held = any(key.endswith(suffix) for key in st["sent"])
    if prs and prs[0].get("autoMergeRequest") and (to_push or migration and not held):
        # A new head waits for its own checks, and a migration for the operator: an
        # earlier auto-merge is turned off before the push.
        code, _ = gh("pr", "merge", str(d["pr"]), "--disable-auto")
        if code != 0:
            refuse("auto-merge", f"gh pr merge --disable-auto failed with exit {code}", head)
            return None
        why = "adds a migration" if migration else "a new head waits for its checks"
        delivered(st, tid, f"AUTO-MERGE OFF pull request #{d['pr']}: {branch} {why}")
    if to_push:
        check = repo.get("check", "")
        if worktree:
            argv = ["sh", "-c", PUSH, "push", worktree, head, branch, check, facts["push"]]
        else:
            # Checked out nowhere: pushed from the ref; a head whose tree is a cleared
            # SHA's was checked as that SHA.
            check = "" if facts["same_tree"] & cleared else check
            argv = ["sh", "-c", PUSH_REF, "push", head, branch, check, facts["push"]]
        code, out = run_on(host, where, argv, timeout=600)
        result = next((x[2:] for x in reversed(out.splitlines()) if x.startswith("P ")), "")
        if code != 0 or result in ("", "moved"):
            return None
        if result == "dirty":
            return refuse("dirty", "its worktree has uncommitted changes", head, worktree)
        if result == "remote":
            return refuse("remote", f"origin pushes elsewhere than {slug}", head)
        if result == "worktree":
            return refuse("worktree", "no worktree at the head could be added for its check", head)
        if result.startswith("check "):
            reason = f"the pre-push check failed with exit {result[6:]}"
            return refuse("check", reason, head, f"{check} in {worktree or where}")
        if result != "pushed":
            # A failed stage, not a guard: told once, tried again next tick.
            refuse("push", f"git push failed with exit {result[5:]}", head)
            return None
        d["pushed"], d["merged"] = head, False
        delivered(st, tid, f"PUSHED {branch} at {head} to origin")
    if not opens:
        # A feature's one pull request waits for every ticket naming it.
        return True
    if not d["pr"]:
        _, title, body = text
        code, out = gh("pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body)
        made = re.search(r"/pull/(\d+)", out) if code == 0 else None
        if not made:
            refuse("pull-request", f"gh pr create failed with exit {code}", head)
            return None
        d["pr"] = int(made[1])
        d["merged"] = False
        delivered(st, tid, f"OPENED pull request #{d['pr']}: {branch} into {base}")
    if migration:
        if not held:
            line = f"NO AUTO-MERGE {tid}: pull request {d['pr']} for {branch} adds a migration, auto-merge left off"
            event = f"{tid}{migration_scope}{d['pr']}"
            if not (emit(st, event, line, detail=migration) or event in st["sent"]):
                return None
        d["auto"] = [d["pr"], head]
        return True
    return merge_when_green(st, tid, branch, d, head, gh, refuse)


def merge_when_green(st, tid, branch, d, head, gh, refuse):
    """The pull request merged only once every check passed at `head`: at once when
    GitHub calls it CLEAN, else through auto-merge at that head. A red check is one PR
    RED event per head and failed set; pending waits for the next tick."""
    code, out = gh("pr", "view", str(d["pr"]), "--json", PR_VIEW)
    try:
        view = json.loads(out) if code == 0 else None
    except ValueError:
        view = None
    if not isinstance(view, dict):
        return None
    if view.get("state") == "MERGED":
        d["merged"] = True
        return True
    state, failed = check_state(view, head)
    if state == "pending":
        return None
    if state == "red":
        names = ", ".join(sorted(failed))
        line = f"PR RED {tid}: pull request {d['pr']} for {branch} at {head[:7]}, check {names} failed"
        emit(st, f"{tid}|pr-red|{head}|{names}", line)
        return False
    if d.get("auto") == [d["pr"], head]:
        # Auto-merge is on at this head: read again until GitHub merges it.
        return False
    if view.get("mergeStateStatus") == "CLEAN":
        code, _ = gh("pr", "merge", str(d["pr"]), "--merge", "--match-head-commit", head)
        if code != 0:
            refuse("merge", f"gh pr merge failed with exit {code}", head)
            return None
        d["auto"], d["merged"] = [d["pr"], head], True
        delivered(st, tid, f"MERGED pull request #{d['pr']} at {head}")
        return True
    code, _ = gh("pr", "merge", str(d["pr"]), "--auto", "--merge", "--match-head-commit", head)
    if code != 0:
        refuse("auto-merge", f"gh pr merge --auto failed with exit {code}", head)
        return None
    d["auto"] = [d["pr"], head]
    delivered(st, tid, f"AUTO-MERGE pull request #{d['pr']} at {head}")
    return False


def review_pushed(tid, rec, tickets):
    """A review ticket's coder ticket that is complete with a PUSH as its latest verdict:
    (coder ticket, that verdict update), else None. Such a review is done."""
    if not tid.endswith("-review"):
        return None
    coder = tickets.get((rec.get("project", ""), tid.removesuffix("-review")))
    if not coder or coder["status"] != "complete":
        return None
    verdicts = [
        u
        for u in coder["updates"]
        if u["content"].startswith("REVIEW VERDICT:") and u["agent"] != coder["agent_id"]
    ]
    if not verdicts or not verdicts[-1]["content"].startswith("REVIEW VERDICT: PUSH"):
        return None
    return coder, verdicts[-1]


def close_gone_review(root, st, tid, rec, ticket, tickets, terminals, live=False):
    """A review ticket whose coder ticket is complete with a PUSH as its latest verdict,
    and whose reviewer's terminal is gone (or, with `live`, still open), is closed by
    finish_work run as its owner, with one progress line: "closed" then, or "refused" for
    a close Concord refuses, one STUCK tried again each tick; None when that does not
    hold, as for a review ticket another agent owns now."""
    if not tid.endswith("-review") or ticket["agent_id"] != rec["agent_id"]:
        return None
    # Gone means gone from this tick's listing: an earlier lost flag proves nothing now.
    running = own_row(rec, terminals) is not None
    if running and not live:
        return None
    if running:
        gone = said = stuck = "its review is over"
    else:
        gone, said = "the reviewer's terminal is gone", "its terminal is gone"
        stuck = "its reviewer terminal is gone"
    found = review_pushed(tid, rec, tickets)
    if not found:
        return None
    coder, push = found
    words = push["content"].split()
    verdict = " ".join(words[:4] if len(words) > 3 and SHA.fullmatch(words[3]) else words[:3])
    owner, coder_id = ticket["agent_id"], coder["task_id"]
    what = (
        f"Closed by dev-auto: {gone}, {coder_id} is complete and "
        f"its latest verdict is {verdict}."
    )
    db = root / ".concord" / "concord.db"
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT version FROM tasks WHERE project = ? AND task_id = ?",
                (rec.get("project", ""), tid),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise RuntimeError(f"{tid} is not in the store")
        finish = {
            "task_id": tid,
            "expected_version": row[0],
            "outcome": "complete",
            "what_changed": what,
        }
        concord_call("finish_work", finish, agent=owner)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        text = (
            f"STUCK {tid}: {stuck} after REVIEW VERDICT: PUSH on "
            f"{coder_id}, which is complete, but closing the review ticket failed. Tried "
            "again each tick"
        )
        emit(st, f"{tid}|review-close|{rec['launched_at']}", text, note_on=tid, detail=str(error))
        return "refused"
    line = (
        f"dev-auto: closed {tid} as {owner}: {said}, {coder_id} is complete "
        f"and its latest verdict is {verdict} (update {push['id']})."
    )
    print(line)
    try:
        concord_call("update_work", {"task_id": tid, "kind": "progress", "content": line})
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"ledger note failed for {tid}: {error}")
    return "closed"


def supervise(cfg, root, st, tid, rec, tickets, terminals, now):
    ticket = tickets.get((rec.get("project", ""), tid))
    done = ticket is not None and ticket["status"] in DONE
    if ticket is not None and not done and close_gone_review(root, st, tid, rec, ticket, tickets, terminals):
        if st.statuses.get(tid, ("",))[0] != "stuck":
            status(st, tid, "done", "its review ticket was closed; cleanup follows", proved=True)
        return
    if ticket is not None and not done and review_pushed(tid, rec, tickets):
        # Its coder is complete with a PUSH: the review is over, so its session is
        # closed as a finished one's, whether it is running or exited, once its review
        # ticket is closed.
        closed = close_gone_review(root, st, tid, rec, ticket, tickets, terminals, live=True)
        done = closed == "closed"
    if rec.get("lost") and not done:
        detail = launch_command(cfg, tid, rec)[1]
        emit(st, f"{tid}|lost|{rec['launched_at']}", rec["lost"], note_on=tid, detail=detail)
        status(st, tid, "lost", rec["lost"])
        return
    if ticket is None:
        key = f"{tid}|missing"
        emit(st, key, f"STUCK {tid}: the ticket is not in the ledger")
        if key in st["sent"] and rec.get("terminal") not in terminals:
            drop(st, tid)
        return
    if done:
        # A finished ticket's missing terminal is no STUCK: cleanup drops its record.
        if own_row(rec, terminals):
            locate(cfg, st, tid, rec, terminals)
        live = own_row(rec, terminals) and not rec.get("closed")
        status(st, tid, "done", "its terminal is open" if live else "its worktree is not removed")
        cleanup(cfg, st, tid, rec, ticket, terminals, now, tickets)
        return
    if rec.get("stopped") or rec.get("stopping"):
        # Its session was closed at a handoff, or its close is retried; a proposal waits
        # for the orchestrator.
        status(st, tid, "stopped", (rec.get("stopped") or rec["stopping"])["why"])
        return
    if not own_row(rec, terminals) and kept_for_handoff(cfg, rec, ticket):
        # Gone after its handoff: context_restarts or handoff_stops takes it, not lost.
        status(st, tid, "stopped", "context handoff" if context_handoff(rec, ticket) else handoff_why(rec))
        return
    row = locate(cfg, st, tid, rec, terminals)
    if row is None:
        status(st, tid, "lost", rec["lost"])
        return
    if row.get("branch"):
        rec["branch"] = row["branch"].removeprefix("refs/heads/")
    ident = row.get("agentIdentity")
    screen = read_screen(rec["host"], rec["terminal"])
    stopped = next(
        (row for row in SDK.STOPS if screen and row in screen["lines"][-15:]),
        None,
    )
    if rec["agent"] == "sdk" and (stopped or exited(rec, row, screen, now)):
        # The runner exits at its cap, on a failed API turn, or killed; a relaunch would
        # spend past the cap or fail the same way.
        said = stopped or "· sdk worker: exited with no stop line on its screen"
        text = f"STUCK {tid} {rec['agent_id']}: its runner stopped: {said.removeprefix('· ')}"
        emit(st, f"{tid}|sdk-stop|{rec['launched_at']}", text, note_on=tid)
        return
    if exited(rec, row, screen, now):
        status(st, tid, "exited")
        handoff = cfg["limit_handoff"] and rec["agent"] == "claude" and handed_off(rec, ticket)
        if handoff:
            # A session that exited after its handoff is stopped with a proposal, never relaunched.
            stop_worker(cfg, st, tid, rec, handoff_why(rec), handoff["id"], now, rec["terminal"])
            return
        relaunch(cfg, root, st, tid, rec, ticket, now)
        return
    last = (row.get("lastOutputAt") or 0) / 1000 or rec["launched_at"]
    quiet = now - last
    if screen is None or screen["status"] != "running":
        # An unread screen keeps the status a read one gave; only output time is known.
        if "status" not in rec:
            status(st, tid, "working" if quiet < SETTLED else "quiet")
        # Its pending rechecks and missed STUCKs need no screen.
        unanswered_wake(cfg, st, tid, rec, ticket, tickets, row, now)
        return
    shown = "\n".join(screen["lines"][-15:])
    if rec["agent"] == "claude" and answer_claude_first_run(st, tid, rec, screen, last):
        status(st, tid, "asking", "first-run")
        return
    shown_model = model_drift(rec, screen["lines"]) if rec["agent"] == "codex" else None
    if shown_model:
        # Reported, never fixed by a relaunch: a reviewer must not run below its model.
        launched = " ".join(filter(None, (rec["model"], rec.get("effort"))))
        text = (
            f"STUCK {tid} {rec['agent_id']}: its status line shows {shown_model}, not "
            f"the launched {launched}. Nothing is typed into it"
        )
        emit(st, f"{tid}|model|{rec['launched_at']}|{shown_model}", text, note_on=tid)
        return
    if rec["agent"] == "codex" and unknown_menu(screen["lines"]):
        text = (
            f"STUCK {tid} {rec['agent_id']}: a switch-model menu dev-auto does not know "
            "is open on its screen. Nothing is typed into it"
        )
        emit(st, f"{tid}|menu|{rec['launched_at']}", text, note_on=tid, detail=shown)
        return
    front = overlay(screen["lines"], rec["agent"])
    if front:
        status(st, tid, "asking", "question" if front == "asking" else front)
    if front == "menu" and ident:
        # A digit picks the item without Enter; nothing else is typed this tick.
        if send(rec["host"], rec["terminal"], "2", enter=False):
            print(f"{tid}: answered Codex's switch-model menu with Keep current model")
        return
    if front:
        if quiet >= SETTLED:
            text = (
                f"NEEDS YOU {tid} {rec['agent_id']}: a prompt is open on its screen, "
                "which is recorded on the ticket"
            )
            key = f"{tid}|asking|{int(last)}"
            emit(st, key, text, note_on=tid, detail=shown)
        return
    if not ident and not agent_ui(rec["agent"], screen["lines"]):
        # Its agent is starting, lost its listing, or exited to a prompt SHELL does not
        # know. Past the start, reported once per launch; nothing is typed or closed.
        status(st, tid, "no-agent")
        if quiet >= SETTLED and now - rec["launched_at"] >= FAST_EXIT:
            text = (
                f"STUCK {tid} {rec['agent_id']}: no agent is listed in its terminal and "
                "its screen is no shell prompt or known dialog. Left alone"
            )
            key = f"{tid}|unknown|{rec['launched_at']}"
            emit(st, key, text, note_on=tid, detail=shown)
        return
    # The limit message sits above the input box, under any panel or status bar, and only
    # output after the agent's last message counts. A quiet limit screen outranks the
    # usage reading, and nothing is typed into one.
    tail = screen["lines"][-30:]
    after = max(
        (
            i
            for i, line in enumerate(tail)
            if line.lstrip().startswith(OUTPUT) and not LIMIT_STATUS.match(line.lstrip())
        ),
        default=-1,
    )
    tail = tail[after + 1 :]
    # An sdk worker has no subscription limit; its cap is the budget line above.
    hits = [
        i
        for i, line in enumerate(tail)
        if rec["agent"] != "sdk" and LIMIT.match(line.strip())
    ]
    shown = limit_message(tail[hits[0] :]) if hits else None
    if hits and not answered_limit(cfg, rec, shown, now):
        # A limit screen ends the turn, so it is acted on the tick it appears.
        if quiet >= SETTLED or not in_turn(screen["lines"]):
            # Its reset is read from the limit messages and the screen below them, nothing
            # else; wrapped lines rejoin with one space, so "try" / "again at" still reads.
            message = " ".join(line.strip() for line in tail[hits[0] :])
            usage_limit(cfg, root, st, tid, rec, ticket, now, message, shown, last)
        reset = rec.get("limit_reset")
        status(st, tid, "limited", stamp(reset) if reset else "")
        return
    if footer_only(screen["lines"]):
        # Nothing is typed into a dialog, and no wake or nudge either.
        if quiet >= SETTLED:
            footer_dialog(cfg, root, st, tid, rec, ticket, last, now)
        return
    for key in ("limit_reset", "limit_full", "limit_seen_on"):
        rec.pop(key, None)
    if not hits and after >= 0:
        rec.pop("limit_answered", None)
    status(st, tid, "working" if quiet < SETTLED else "quiet")
    # Before the ask holds the record: an ask its screen never showed is no ask.
    recheck_unshown(st, tid, rec)
    if cfg["limit_handoff"] and asked(rec, now):
        # Nothing is typed into a worker asked for its handoff.
        status(st, tid, "handing off", stamp(rec["handoff_ask"]["at"]))
        return
    if concord_lost(cfg, root, st, tid, rec, ticket, tickets, screen["lines"], quiet, now):
        return
    ready = review_ready(st, tid, rec, tickets)
    blocker = waiting(ticket)
    if blocker:
        target = wait_target(blocker)
        words = blocker["content"].removeprefix("Waiting on:").split()
        aim = " ".join(words[:2]) if target == "process" else target
        if quiet < SETTLED and wait_answered(st, tid, ticket, tickets, blocker):
            status(st, tid, "working")
        else:
            status(st, tid, "waiting", aim, epoch(blocker["created_at"]))
        on_wait(st, tid, rec, blocker, ticket, tickets, ready)
    elif ready or verdict_wake(st, tid, rec, ticket):
        pass
    elif ticket["status"] in WORKING:
        if rec.get("nudged_at") and last <= rec.get("nudge_echo", rec["nudged_at"]):
            status(st, tid, "nudged", since=rec["nudged_at"])
        silence(cfg, st, tid, rec, last, now, screen["lines"], tickets)
        if rec.get("nudged_at") == now:
            status(st, tid, "nudged", since=now)
    unanswered_wake(cfg, st, tid, rec, ticket, tickets, row, now)


def footer_dialog(cfg, root, st, tid, rec, ticket, last, now):
    """A settled frame that shows only a dialog's footer. A usage limit that the worker's
    transcript ends in, written by the time the frame was drawn, is handled as that limit
    screen; one older than the frame keeps a waiting record's wait and raises nothing.
    Otherwise, an old limit with no wait included, one STUCK per frame."""
    key = f"{tid}|footer|{int(last)}"
    if key in st["sent"]:
        return
    # Read once per frame: on another host each read opens a probe terminal.
    frame, found = rec.get("footer_entry") or (None, None)
    if frame != int(last):
        found = last_entry(cfg, rec)
        rec["footer_entry"] = [int(last), found]
    if found and LIMIT.match(found[1]):
        # An entry older than the frame is the limit a waiting record already waits on;
        # with no such wait it explains nothing, and the frame is the STUCK below.
        new = found[0] >= last - SETTLED
        if new or "limit_reset" in rec:
            message = found[1]
            usage_limit(cfg, root, st, tid, rec, ticket, now, message, limit_message([message]), last, same=not new)
            return
    text = (
        f"STUCK {tid} {rec['agent_id']}: a dialog shows only its footer, Enter to confirm "
        "or Esc to cancel, and its transcript shows no usage limit. Nothing is typed into it"
    )
    emit(st, key, text, note_on=tid)


def unanswered_wake(cfg, st, tid, rec, ticket, tickets, row, now):
    """One STUCK for a pending wake that got no update from its worker, on any ticket, and
    no terminal output within wake_minutes. Unshown wakes are rechecked first, and each
    key on the record's missed list raises its own STUCK at once. The wake is never
    typed again, and a nudge is silence's to report. The tick that typed it stamps it: that
    tick read the ledger before the wake."""
    recheck_unshown(st, tid, rec)
    wake = rec.get("wake")
    if not wake or wake["key"] in rec.get("missed", []):
        return
    if wake["key"].split("|")[1] == "nudge":
        del rec["wake"]
        st.save()
        return
    updates = [u for t in tickets.values() for u in t["updates"]]
    output = row.get("lastOutputAt") or 0
    if "at" not in wake:
        wake.update(at=now, update=max((u["id"] for u in updates), default=0))
        # With no listing after the send, the tick's own one is the baseline.
        wake["output"] = wake.get("output") or output
        st.save()
        return
    if wake.get("output") is None:
        # Saved with no baseline: this tick's listing is the baseline.
        wake["output"] = output
        st.save()
    mine = {rec["agent_id"], ticket["agent_id"]}
    answered = any(u["id"] > wake["update"] and u["agent"] in mine for u in updates)
    minutes = cfg.get("wake_minutes", WAKE_MINUTES)
    if not answered and output <= wake["output"]:
        if now - wake["at"] < minutes * 60:
            return
        text = (
            f"STUCK {tid} {rec['agent_id']}: no update and no output {minutes} minutes "
            f"after its wake {wake['key'].replace('|', ' ')}. It is not typed again"
        )
        key = f"{tid}|unanswered|{wake['key']}"
        # One held in the outbox counts as sent: a later tick finishes its delivery.
        if not emit(st, key, text, note_on=tid) and key not in st["sent"]:
            return
    del rec["wake"]
    st.save()


def recheck_unshown(st, tid, rec, only=None):
    """Recheck the record's unshown sends, then raise one STUCK per key on its missed list;
    only that key's, when given. A missed nudge is logged only: silence raises its STUCK a
    full interval after it. A missed handoff ask is no ask: its record is supervised as
    before."""
    for unshown in list(rec.get("unshown", [])):
        if only and unshown["key"] != only:
            continue
        outcome = recheck(st, rec, unshown)
        if not outcome:
            break
        delivered(st, tid, f"{unshown['text']}\t{outcome}")
    for key in list(rec.get("missed", [])):
        if only and key != only:
            continue
        if key.split("|")[1] == "nudge":
            rec["missed"].remove(key)
            if not rec["missed"]:
                del rec["missed"]
            st.save()
            continue
        text = (
            f"STUCK {tid} {rec['agent_id']}: its wake {key.replace('|', ' ')} never showed "
            "on its screen, with no output and no update after it. It was typed at most "
            "twice and is not typed again"
        )
        emit(st, f"{tid}|missed|{key}", text, note_on=tid)
        # In sent once delivered, or once a crash cut that send short: never raised again.
        if f"{tid}|missed|{key}" in st["sent"]:
            rec["missed"].remove(key)
            if not rec["missed"]:
                del rec["missed"]
            if (rec.get("wake") or {}).get("key") == key:
                del rec["wake"]
            if "|handoff-ask|" in key:
                rec.pop("handoff_ask", None)
            st.save()


def relaunch(cfg, root, st, tid, rec, ticket, now, repair=False):
    """`repair`: a live session replaced to mend it, not an exit, so no fast-exit hold."""
    fast = not repair and now - rec["launched_at"] < FAST_EXIT
    if fast or "held" in rec or rec.get("relaunches", 0) >= MAX_RELAUNCHES:
        why = (
            f"within {FAST_EXIT // 60} minutes of launch"
            if fast
            else f"after {rec.get('relaunches', 0)} relaunches"
        )
        command, detail = launch_command(cfg, tid, rec)
        held = f"STUCK {tid} {rec['agent_id']}: its terminal exited {why}. It waits for a new launch{command}"
        rec.setdefault("held", held)
        emit(st, f"{tid}|held|{rec['launched_at']}", rec["held"], note_on=tid, detail=detail)
        return
    row = slot(
        cfg,
        st["workers"],
        rec["agent"],
        rec["model"],
        rec["host"],
        ignore=tid,
        now=now,
        limits=st["limits"],
    )
    if row is None:
        text = f"NO CAPACITY {tid}: its worker exited and no {rec['agent']} account has a free slot"
        rec.setdefault("exited_at", now)
        emit(st, f"{tid}|no-capacity|{rec['launched_at']}", text)
        return
    if close_terminal(rec["host"], rec["terminal"]) is None:
        print(f"{tid}: closing {rec['terminal']} failed; tried again next tick")
        return
    rec["relaunches"] = rec.get("relaunches", 0) + 1
    situation = (
        "The previous session on this ticket exited; continue from the ticket's "
        "latest handoff and updates. "
    )
    if start(cfg, root, st, rec, row, brief(rec, ticket, situation), now):
        say(
            tid,
            f"relaunched {rec['agent_id']} on {row['label']} after its terminal exited",
        )


def hold(cfg, st, row, account_id, until, now, full=False):
    """The account takes no new worker, any row kind, until `until`; the only writer of
    limits and limit_full. Nothing while the operator's usage-reset stands. limit_full
    marks a hold a reading has shown: only such a hold can a later reading lift."""
    if not until or until <= now or reset_stands(cfg, row, now, account_id):
        return
    st["limits"][account_id] = max(st["limits"].get(account_id, 0), until)
    if full or (row and at_limit(cfg, row, now)):
        st.setdefault("limit_full", {})[account_id] = True


def lift_limits(cfg, st, now):
    """A held account whose fresh reading is at usage_full_percent has its hold backed;
    once backed, a fresh reading below it ends the hold. A reading that never showed the
    limit lifts nothing, and no worker's wait is touched here."""
    full = st.setdefault("limit_full", {})
    for account_id in set(st["limits"]) | set(full):
        row = account_row(cfg, account_id)
        if st["limits"].get(account_id, 0) <= now or not row:
            full.pop(account_id, None)
            continue
        percent = peak(cfg, row, now)[0]
        if percent is None:
            continue
        if percent >= cfg["usage_full_percent"]:
            full[account_id] = True
        elif full.pop(account_id, None):
            del st["limits"][account_id]


def limit_seen(cfg, st, rec, row, message, shown, last, now, same=False):
    """Classify one sighting and write what it proves. A new one, its terminal and limit
    text differing from the last one recorded, is seen_full and holds its account at its
    screen's reset, read once now; on a waiting record it also extends the wait. The same
    one, or `same`, writes no reset, so "resets in 1 hour" never slides; a fresh full
    reading under it marks the wait limit_full. A wait saved
    before sightings were kept holds its account at its saved reset, never reread."""
    # Spacing aside: a panel or status bar shown or hidden under it is the same limit.
    sighting = [rec["terminal"], " ".join((shown or message).split())]
    waiting = "limit_reset" in rec
    if waiting and "limit_seen_on" not in rec:
        rec["limit_seen_on"] = sighting
        if row and at_limit(cfg, row, now):
            rec["limit_full"] = True
        if not freed(cfg, row, rec, now):
            hold(cfg, st, row, rec["account"], rec["limit_reset"], now, rec.get("limit_full", False))
        return
    if same or rec.get("limit_seen_on") == sighting:
        # A fresh full reading under the same screen backs the wait, as it backs the hold.
        if waiting and row and at_limit(cfg, row, now):
            rec["limit_full"] = True
        return
    rec["limit_seen_on"] = sighting
    seen_full(rec["account"], max(last, rec["launched_at"]))
    reset = screen_reset(message, now)[0]
    hold(cfg, st, row, rec["account"], reset, now)
    if waiting and not reset_stands(cfg, row, now, rec["account"]):
        rec["limit_reset"] = max(reset, rec["limit_reset"] or 0)
        rec.pop("limit_full", None)
        if row and at_limit(cfg, row, now):
            rec["limit_full"] = True
        hold(cfg, st, row, rec["account"], rec["limit_reset"], now)


def usage_limit(cfg, root, st, tid, rec, ticket, now, message, shown, last, same=False):
    """Move a limited worker to another account; with none free, WAITING once, then the
    move to the first other account that has a free slot, checked every tick, or back
    onto its own after the reset, or sooner once a fresh reading below the limit follows
    a wait its full reading set, or the operator's usage-reset stands. limit_seen first
    records the sighting and its account's hold. Nothing is typed into a limit screen."""
    current = account(cfg, rec["account"], rec["host"])
    limits = st["limits"]
    waiting = "limit_reset" in rec
    limit_seen(cfg, st, rec, current, message, shown, last, now, same)
    if cfg["limit_handoff"] and rec["agent"] == "claude":
        handoff = handed_off(rec, ticket)
        stop_worker(cfg, st, tid, rec, "usage limit reached", handoff and handoff["id"], now, rec["terminal"])
        return
    # An earlier version saved no reset as None; that wait is over.
    reset_at = rec.get("limit_reset") or 0
    early = waiting and now < reset_at + RESUME_AFTER
    own_free = waiting and not (early and not freed(cfg, current, rec, now))
    row = slot(
        cfg,
        st["workers"],
        rec["agent"],
        rec["model"],
        rec["host"],
        exclude=set() if own_free else {rec["account"]},
        ignore=tid,
        now=now,
        limits=limits,
    )
    if row is None and waiting and not own_free:
        return
    if row:
        if rec["agent"] == "claude":
            text = f"dev-auto: this session moved to {row['label']} after a usage limit. Continue ticket {tid}."
            if defer_stale_claude(st, rec, f"{tid}|resume|{rec.get('session')}", text):
                return
        if close_terminal(rec["host"], rec["terminal"]) is None:
            # The old session may still own its terminal and Claude session.
            print(f"{tid}: closing {rec['terminal']} failed; tried again next tick")
            return
        if rec["agent"] == "claude":
            text = armed(rec, goal_line(tid), text)
            moved = start(cfg, root, st, rec, row, text, now, resume=rec["session"])
            if moved and early:
                # The resumed session replays this message; see answered_limit.
                rec["limit_answered"] = shown
        else:
            situation = (
                "The previous session hit a usage limit and was moved to this account. "
            )
            moved = start(cfg, root, st, rec, row, brief(rec, ticket, situation), now)
        if moved:
            old = current["label"] if current else rec["account"]
            say(
                tid,
                f"moved {rec['agent_id']} from {old} to {row['label']} after a usage limit",
            )
        return
    if waiting:
        text = f"NO CAPACITY {tid}: its usage limit reset and no {rec['agent']} account has a free slot"
        emit(st, f"{tid}|limit-reset|{int(reset_at)}", text)
        return
    # A reading below the limit gives no reset for this screen; its own does.
    full = current and at_limit(cfg, current, now)
    files = limit_reset(cfg, current, now) if full else None
    guess, given = screen_reset(message, now)
    reset = files or limits.get(rec["account"]) or guess
    label = current["label"] if current else rec["account"]
    text = (
        f"WAITING {tid} {rec['agent_id']}: usage limit on {label}, resets "
        f"{stamp(reset)}. No other {rec['agent']} account has a free slot"
    )
    detail = ""
    if not files and not given:
        hours = round((guess - now) / 3600)
        detail = f"Its screen gives no reset time, so dev-auto assumed {hours} hours."
    if emit(st, f"{tid}|limit|{int(now)}", text, note_on=tid, detail=detail):
        rec["limit_reset"] = reset
        if full:
            rec["limit_full"] = True
        hold(cfg, st, current, rec["account"], reset, now)


def limit_message(lines):
    """The limit message in these lines: the lines LIMIT matches, then the reset wording
    on them all, rejoined with one space. A status bar, panel or input box under it is
    never part of it."""
    text = " ".join(line.strip() for line in lines)
    shown = [line.strip() for line in lines if LIMIT.match(line.strip())]
    shown += [m.group(0) for regex in (RESET_IN, RESET_AT) for m in regex.finditer(text)]
    return " ".join(shown)


def answered_limit(cfg, rec, shown, now):
    """A Claude session resumed before its stored reset replays the limit message it was
    resumed from. That same message is answered until the session's own output follows
    or its account is seen_full after the launch; any other limit message counts."""
    if shown is None or shown != rec.get("limit_answered"):
        return False
    row = account(cfg, rec["account"], rec["host"])
    if row:
        usage_reading(cfg, row, now)
    return USAGE_FULL.get(rec["account"], 0) <= rec["launched_at"]


def freed(cfg, row, rec, now):
    """A waiting worker's wait can end before its stored reset: the operator's usage-reset
    stands, or a fresh reading below the limit follows a wait a full reading set. Below a
    limit screen alone, a reading never ends one."""
    if reset_stands(cfg, row, now, rec["account"]):
        return True
    percent, _reset = peak(cfg, row, now) if row else (None, None)
    return percent is not None and percent < cfg["usage_full_percent"] and bool(rec.get("limit_full"))


# Limit handoff: a Claude account near its 5-hour limit keeps one worker and hands off
# the rest; every stopped worker becomes a restart proposal for the orchestrator.

HANDOFF = "Handoff:"
TIERS = ("hard", "medium", "ui", "easy")
# A worker with no output for longer is not asked: it is idle, not spending the account.
ACTIVE = 3600


def handoff_trigger(cfg, row, now):
    """(percent, reset) when the account's 5-hour use is at handoff_percent with more than
    handoff_minutes_left to its reset; None otherwise, also unread or with no reset."""
    percent, reset = five_hour(cfg, row, now)
    if percent is None or percent < cfg["handoff_percent"] or not reset:
        return None
    if float(reset) - now <= cfg["handoff_minutes_left"] * 60:
        return None
    return percent, float(reset)


def asked(rec, now):
    """The worker's handoff ask while its usage window lasts, else None."""
    ask = rec.get("handoff_ask")
    return ask if ask and ask["window"] > now else None


def handed_off(rec, ticket):
    """The newest live Handoff: update this launch of the worker wrote on its ticket, or
    None, also when a Waiting on note of its owner follows it: that worker waits, not
    stops. A superseded handoff was withdrawn."""
    mine = {rec["agent_id"], ticket["agent_id"]}
    found = [
        u
        for u in ticket["updates"]
        if u["agent"] in mine
        and u["content"].startswith(HANDOFF)
        and u.get("superseded_by") is None
        and (epoch(u["created_at"]) or 0) >= rec["launched_at"]
    ]
    blocker = waiting(ticket)
    if not found or (blocker and blocker["id"] > found[-1]["id"]):
        return None
    return found[-1]


def context_handoff(rec, ticket):
    """The handoff a Claude worker at CONTEXT_LIMIT wrote on its open ticket, or None.
    context_restarts starts it again from that handoff even when its pane is gone, so
    such a record is never lost."""
    if rec["agent"] != "claude" or context_for_handoff(rec) < CONTEXT_LIMIT:
        return None
    if ticket is None or ticket["status"] in DONE:
        return None
    return handed_off(rec, ticket)


def cutoff_stop_pending(rec):
    return (rec.get("stopping") or {}).get("why") == CLAUDE_CUTOFF_STOP


def kept_for_handoff(cfg, rec, ticket):
    """A Claude record whose gone pane a handoff path handles, so it is never lost:
    context_restarts at the context limit, or with limit_handoff on, handoff_stops for a
    current handoff or a pending stop, also while a pane in its worktree awaits exit proof."""
    if cutoff_stop_pending(rec) or context_handoff(rec, ticket):
        return True
    if not cfg["limit_handoff"] or rec["agent"] != "claude" or ticket is None or ticket["status"] in DONE:
        return False
    return bool(rec.get("stopping") or handed_off(rec, ticket))


def handoff_why(rec):
    ask = rec.get("handoff_ask")
    return f"usage handoff at {ask['percent']:.0f}%" if ask else "context handoff"


def tier_rank(tickets, ticket):
    """hard 0, medium 1, ui 2, easy 3, none last; a review ticket ranks as its coder's."""
    found = TIER.search(ticket.get("notes") or "")
    if not found and ticket["task_id"].endswith("-review"):
        coder = tickets.get((ticket["project"], ticket["task_id"].removesuffix("-review")))
        found = TIER.search((coder or {}).get("notes") or "")
    name = found.group(1).lower() if found else ""
    return TIERS.index(name) if name in TIERS else len(TIERS)


def models_held(cfg, workers, ignore):
    """Models per account id: live agents no record holds, and every record's that
    holds_slot but `ignore`'s."""
    held = foreign(cfg, workers)
    for tid, rec in workers.items():
        if tid != ignore and holds_slot(rec):
            held.setdefault(rec["account"], []).append(rec["model"])
    return held


def free_slots(row, held, model):
    """How many more workers of this model's family the Claude row takes, by its caps."""
    want = family(model)
    free = row.get(want, 0) - sum(family(m) == want for m in held)
    if row.get("max_workers") is not None:
        free = min(free, row["max_workers"] - len(held))
    elif {family(m) for m in held} - {want}:
        free = 0
    return max(free, 0)


def launch_refusal(cfg, st, tid, model, row, now):
    """Why this Claude row cannot take the ticket's fresh session now, or None: its own
    handoff trigger, or slot's reason."""
    hit = handoff_trigger(cfg, row, now)
    if hit:
        return (
            f"{row['label']}: 5-hour use {hit[0]:.0f}% at handoff_percent "
            f"{cfg['handoff_percent']}, resets {stamp(hit[1])}"
        )
    why = []
    taken = slot(
        cfg,
        st["workers"],
        "claude",
        model,
        row["host"],
        memory=False,
        ignore=tid,
        now=now,
        limits=st["limits"],
        account=row["label"],
        why=why,
    )
    if taken:
        return None
    return "; ".join(why) or f"{row['label']}: no free slot"


def proposal(cfg, st, tid, rec, why, handoff, now):
    """The restart of a stopped worker as a fresh session: why it stopped, every Claude
    account on its host with its use and free slots, dev-auto's pick and the launch."""
    source = account(cfg, rec["account"], rec["host"])
    five, reset = five_hour(cfg, source, now) if source else (None, None)
    ask = rec.get("handoff_ask") or {}
    if why == "usage limit reached":
        # The limit's hold, which can outlast an earlier handoff ask's 5-hour window.
        reset = st["limits"].get(rec["account"]) or ask.get("window") or reset
    else:
        reset = ask.get("window") or reset
    held = models_held(cfg, st["workers"], tid)
    accounts, pick = [], None
    for row in cfg["account"]:
        # The worktree is on the worker's host, so only that host's accounts can take it.
        if row["agent"] != "claude" or not row["enabled"] or row["host"] != rec["host"]:
            continue
        percent, resets = five_hour(cfg, row, now)
        weekly = next(
            (p for p, r, w in usage_reading(cfg, row, now) or [] if w == "weekly" and (not r or float(r) > now)),
            None,
        )
        free = free_slots(row, held.get(row["id"], []), rec["model"])
        accounts.append(
            {"label": row["label"], "host": row["host"], "five": percent, "weekly": weekly, "reset": resets, "free": free}
        )
        if pick is None and launch_refusal(cfg, st, tid, rec["model"], row, now) is None:
            pick = row
    if pick:
        pick_why = (
            f"{pick['label']} is the first Claude account in accounts.toml that takes "
            f"{rec['model']} now, below handoff_percent"
        )
    else:
        pick = source or {"label": rec["account"], "host": rec["host"]}
        when = stamp(reset) if reset else "its reset"
        pick_why = f"no Claude account takes {rec['model']} now; {pick['label']} after {when}"
    launch = {
        "agent": "claude",
        "model": rec["model"],
        "effort": rec.get("effort"),
        "host": rec["host"],
        "worktree": rec["worktree"],
        "account": pick["label"],
    }
    argv = ["python3", str(HERE / "dev_auto.py"), "launch", tid, "--agent", "claude"]
    argv += ["--model", rec["model"]] + (["--effort", rec["effort"]] if rec.get("effort") else [])
    argv += ["--host", rec["host"], "--worktree", rec["worktree"], "--account", pick["label"]]
    return {
        "ticket": tid,
        "project": rec.get("project", ""),
        "account": rec["account"],
        "reset": reset,
        "why": why,
        "percent": ask.get("percent", five),
        "handoff": handoff,
        "created": now,
        "accounts": accounts,
        "pick": pick["label"],
        "pick_why": pick_why,
        "launch": launch,
        "command": shlex.join(argv),
    }


def proposals_file(st):
    return st.path.parent / "proposals.md"


def proposals_text(st):
    lines = ["# Restart proposals", ""]
    for tid, p in sorted(st.get("proposals", {}).items()):
        handoff = (
            f"handoff update {p['handoff']}"
            if p["handoff"]
            else "no handoff update; the fresh session reads the latest updates"
        )
        lines += [f"## {tid}", "", f"- why: {p['why']}, {handoff}"]
        lines += [f"- pick: {p['pick']}: {p['pick_why']}", f"- command: `{p['command']}`", "- accounts:"]
        for a in p["accounts"]:
            five = "unread" if a["five"] is None else f"{a['five']:.0f}%"
            weekly = "unread" if a["weekly"] is None else f"{a['weekly']:.0f}%"
            resets = stamp(a["reset"]) if a["reset"] else "no reset time"
            lines.append(
                f"  - {a['label']} on {a['host']}: 5-hour {five}, weekly {weekly}, "
                f"resets {resets}, {a['free']} free"
            )
        lines.append("")
    if not st.get("proposals"):
        lines.append("None wait.")
    return "\n".join(lines) + "\n"


def write_proposals(st):
    """Written beside the file and moved into place: a reader never sees part of it."""
    path = proposals_file(st)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(proposals_text(st))
    tmp.replace(path)


def end_proposal(st, tid, how, detail):
    """Forget a proposal, logging its end as accepted, changed or dropped."""
    st["proposals"].pop(tid)
    delivered(st, tid, f"PROPOSAL {how}: {detail}")
    write_proposals(st)
    st.save()


def stop_worker(cfg, st, tid, rec, why, handoff, now, handle):
    """Close the worker's session with its tab, keep its worktree, and propose a fresh
    restart. The stop is saved before the close, so a close Orca refuses, or a crash after
    it, is finished by handoff_stops on a later tick."""
    rec["stopping"] = {"why": why, "handoff": handoff}
    st.save()
    if handle and close_terminal(rec["host"], handle) is None:
        print(f"{tid}: closing {handle} failed; tried again next tick")
        return False
    st.setdefault("proposals", {})[tid] = proposal(cfg, st, tid, rec, why, handoff, now)
    write_proposals(st)
    del rec["stopping"]
    rec["stopped"] = {"why": why, "at": now, "handoff": handoff}
    st.save()
    say(
        tid,
        f"stopped {rec['agent_id']} at a {why}; its worktree is kept and a restart "
        "proposal waits for the orchestrator",
    )
    return True


def handoff_stops(cfg, st, tickets, terminals, now):
    """Before the restart sweep and supervise: a stop begun on an earlier tick is finished,
    and a Claude worker whose Handoff: update of this launch is on its ticket is stopped
    once its terminal has settled or is gone. An asked worker with none after
    handoff_wait_minutes is one STUCK, and nothing is typed into it. Only a pending
    cutoff stop is processed with usage handoffs disabled."""
    for tid, rec in list(st["workers"].items()):
        skip = rec.get("done") or rec.get("stopped") or rec.get("lost") or missing(rec)
        if rec["agent"] != "claude" or skip or rec["host"] not in terminals:
            continue
        if not cfg["limit_handoff"] and not cutoff_stop_pending(rec):
            continue
        ticket = tickets.get((rec.get("project", ""), tid))
        if ticket is None or ticket["status"] in DONE:
            continue
        listed = terminals[rec["host"]]
        row = own_row(rec, listed)
        other = None if row else possible_session(st, tid, rec, listed, now)
        if other and (rec.get("stopping") or handed_off(rec, ticket)):
            # A stop with no pane to close would leave that session running unsupervised.
            text = unowned_live_text(tid, rec, "it is to be stopped at its handoff", other["handle"])
            emit(st, f"{tid}|stop-live|{int(rec['launched_at'])}", text, note_on=tid)
            continue
        pending = rec.get("stopping")
        current = pending and pending["handoff"] and handed_off(rec, ticket)
        if pending and pending["handoff"] and not current and row:
            # Its owner has waited, or withdrew the handoff, since the refused close: the
            # live worker goes on, not stops.
            del rec["stopping"]
            st.save()
            pending = None
        elif pending and pending["handoff"] and (current or {}).get("id") != pending["handoff"]:
            # The stop names the handoff that stands now; none once it was withdrawn.
            pending["handoff"] = current["id"] if current else None
            st.save()
        if pending:
            if stop_worker(cfg, st, tid, rec, pending["why"], pending["handoff"], now, row and row["handle"]) and row:
                listed.pop(row["handle"], None)
            continue
        handoff = handed_off(rec, ticket)
        if handoff:
            last = (row.get("lastOutputAt") or 0) / 1000 if row else 0
            if now - last < SETTLED:
                continue
            stopped = stop_worker(cfg, st, tid, rec, handoff_why(rec), handoff["id"], now, row and row["handle"])
            if stopped and row:
                listed.pop(row["handle"], None)
            continue
        ask = asked(rec, now)
        minutes = cfg["handoff_wait_minutes"]
        # An ask its screen has not shown, or never showed, is not the worker's to answer.
        unproved = [u["key"] for u in rec.get("unshown", [])] + rec.get("missed", [])
        if any("|handoff-ask|" in key for key in unproved):
            continue
        if ask and now - ask["at"] >= minutes * 60:
            text = (
                f"STUCK {tid} {rec['agent_id']}: asked {minutes} minutes ago to write its "
                "handoff and stop, and no handoff is on its ticket. It is left running. "
                "Nothing is typed into it again"
            )
            emit(st, f"{tid}|handoff-late|{int(ask['at'])}", text, note_on=tid)


def handoff_asks(cfg, st, tickets, terminals, now):
    """After supervise: on each Claude account at its handoff trigger, every active worker
    but the kept one is asked once to write its handoff and stop. Active: output within
    the last hour, working or at its prompt as supervise saw it, not waiting, not a
    reviewer with nothing to review, not asked this window, and below the context limit.
    Kept: the one `keep` named, else the highest tier, then the earliest."""
    seen = set()
    for row in cfg["account"]:
        if row["agent"] != "claude" or not row["enabled"] or row["id"] in seen:
            continue
        seen.add(row["id"])
        hit = handoff_trigger(cfg, row, now)
        if not hit:
            continue
        active = []
        for tid, rec in st["workers"].items():
            if rec["agent"] != "claude" or rec.get("account") != row["id"]:
                continue
            if rec.get("done") or rec.get("stopped") or missing(rec) or asked(rec, now):
                continue
            if context_for_handoff(rec) >= CONTEXT_LIMIT:
                continue
            ticket = tickets.get((rec.get("project", ""), tid))
            listed = own_row(rec, terminals.get(rec["host"], {}))
            shown = st.statuses.get(tid, ("",))[0]
            if ticket is None or listed is None or waiting(ticket):
                continue
            coder = tid.endswith("-review") and tickets.get((rec.get("project", ""), tid.removesuffix("-review")))
            if coder and reviewed_all(coder):
                continue
            if shown not in ("working", "quiet", "nudged"):
                continue
            if now - (listed.get("lastOutputAt") or 0) / 1000 > ACTIVE:
                continue
            active.append((tid, rec, ticket))
        named = st.get("keep", {}).get(row["id"])
        kept = next((a for a in active if a[0] == named), None) or min(
            active, key=lambda a: (tier_rank(tickets, a[2]), a[1]["launched_at"]), default=None
        )
        percent, reset = hit
        for tid, rec, _ticket in active:
            if tid == kept[0]:
                continue
            text = (
                f"/goal Write the handoff of Concord ticket {tid} with the handoff skill, "
                f"then end your turn. dev-auto: {row['label']} is at {percent:.0f}% of its "
                f"5-hour limit, which resets {stamp(reset)}. Finish your current step, "
                f"write your handoff on ticket {tid} with the handoff skill, then stop. A "
                "fresh session continues from it."
            )
            # Saved before the keys: a crash mid-send never asks twice.
            rec["handoff_ask"] = {"at": now, "window": reset, "percent": percent}
            key = f"{tid}|handoff-ask|{int(rec['launched_at'])}|{int(reset)}"
            if not emit(st, key, text, worker=rec, note_on=tid, rearm=False):
                del rec["handoff_ask"]


def proposals_step(cfg, st, tickets, now):
    """Drop each proposal whose ticket finished or whose worker was launched again; tell
    the orchestrator once when proposals appear, and once at each account's reset with
    every proposal that waits."""
    props = st.setdefault("proposals", {})
    for tid, p in list(props.items()):
        ticket = tickets.get((p.get("project", ""), tid))
        rec = st["workers"].get(tid)
        if ticket is None or ticket["status"] in DONE:
            end_proposal(st, tid, "dropped", "its ticket is done or gone")
        elif not rec or not rec.get("stopped"):
            accepting = p.get("accepting")
            if accepting:
                # Its accept launched the worker and has not logged it yet.
                end_proposal(st, tid, accepting["how"], accepting["detail"])
            else:
                end_proposal(st, tid, "dropped", "its ticket was launched again")
    new = sorted(t for t, p in props.items() if not p.get("told"))
    if new:
        count = f"{len(new)} restart proposal{'s wait' if len(new) > 1 else ' waits'}"
        text = (
            f"PROPOSALS: {count} in {proposals_file(st)}: {', '.join(new)}. Check each, "
            "then run proposals accept with tickets or --all, or proposals drop with a ticket"
        )
        key = "proposals|" + ",".join(f"{t}@{int(props[t]['created'])}" for t in new)
        if emit(st, key, text) or key in st["sent"]:
            for tid in new:
                props[tid]["told"] = True
    resets = {(p["account"], p["reset"]) for p in props.values() if p.get("reset") and p["reset"] <= now}
    for account_id, reset in sorted(resets):
        row = account_row(cfg, account_id)
        label = row["label"] if row else account_id
        text = (
            f"RESET {label}: its 5-hour window reset at {stamp(reset)}. Restart proposals "
            f"wait: {', '.join(sorted(props))}. Check them in {proposals_file(st)}, then "
            "accept or drop each"
        )
        emit(st, f"proposals-reset|{account_id}|{int(reset)}", text)


RUN_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
RUN_ID = re.compile(r"\brun\s+#?(\d{6,})\b", re.IGNORECASE)
FEATURE_BRANCH = re.compile(r"Feature branch:\s*`([^`\s]+)`")
NO_FEATURE_BRANCH = re.compile(r"Feature branch:\s*none\b")


def pr_checks(rec, branch):
    """The finished checks' counts on that branch's pull request, "" while they run, or
    None when they cannot be read."""
    argv = ["gh", "pr", "checks", branch, "--json", "bucket", "--jq", ".[].bucket"]
    code, out = run_on(rec["host"], rec["worktree"], argv)
    buckets = out.split()
    if not buckets:
        return "" if code == 8 else None
    known = {"pass", "fail", "pending", "skipping", "cancel"}
    if set(buckets) - known:
        # A probe on another host shows gh's error text with its output.
        return "" if code in (0, 8) else None
    if "pending" in buckets:
        return ""
    counts = {b: buckets.count(b) for b in sorted(set(buckets))}
    return ", ".join(f"{n} {b}" for b, n in counts.items())


def run_conclusion(rec, repo, run_id):
    """A workflow run's conclusion once it completes, "" before, or None when unread."""
    where = ["-R", repo] if repo else []
    argv = ["gh", "run", "view", run_id, *where, "--json", "status,conclusion"]
    code, out = run_on(rec["host"], rec["worktree"], argv)
    try:
        run = json.loads(out) if code == 0 else None
    except ValueError:
        return None
    if not isinstance(run, dict):
        return None
    if run.get("status") != "completed":
        return ""
    return f"{run.get('conclusion') or 'no conclusion'} on run {run_id}"


def ci_result(rec, ticket, blocker):
    """CI for a wait: the run its note names, else the task branch's pull request, else,
    when the task branch has none, the feature branch's from the ticket notes. The
    finished result, "" while it runs, or None when nothing can be read."""
    url = RUN_URL.search(blocker["content"])
    if url:
        return run_conclusion(rec, url[1], url[2])
    named = RUN_ID.search(blocker["content"])
    if named:
        return run_conclusion(rec, None, named[1])
    branch = rec.get("branch")
    if branch:
        result = pr_checks(rec, branch)
        if result is not None:
            return result
        # Only gh's own count of no pull request in any state lets the wait move on.
        prs = ["gh", "pr", "list", "--head", branch, "--state", "all"]
        code, out = run_on(rec["host"], rec["worktree"], [*prs, "--json", "number", "--jq", "length"])
        if code != 0 or out.strip() != "0":
            return None
    feature = FEATURE_BRANCH.search(ticket.get("notes") or "")
    if feature and feature[1] not in ("none", branch):
        return pr_checks(rec, feature[1])
    return None


def process_alive(rec, pid):
    """True while pid runs on the worker's host, False once it is gone, None when unread."""
    code, out = run_on(rec["host"], rec["worktree"], ["ps", "-p", pid, "-o", "pid="])
    if code in (0, 1):
        return pid in out.split()
    return None


def review_ready(st, tid, rec, tickets):
    """Wake a pair's reviewer, which works ticket <coder ticket>-review, once per coder
    review request (see review_request) that is newer than the latest REVIEW VERDICT
    for its SHA; requests for one SHA since that verdict are one request. True when
    this call typed that wake."""
    if not tid.endswith("-review"):
        return False
    coder = tickets.get((rec.get("project", ""), tid.removesuffix("-review")))
    if not coder:
        return False
    requests = review_requests(coder, rec.get("since", 0), coder["agent_id"])
    if not requests:
        return False
    ready, sha = requests[-1]
    floor = max((u["id"] for u, _, s in verdicts(coder) if alike(sha, s)), default=0)
    if floor >= ready["id"]:
        return False
    ready = next(u for u, s in requests if u["id"] > floor and alike(s, sha))
    text = (
        f"dev-auto: REVIEW READY was posted on ticket {coder['task_id']}. Review it "
        "as your ticket says."
    )
    detail = f"Commit {sha}, update {ready['id']}."
    key = f"{tid}|ready|{ready['id']}"
    return emit(st, key, text, worker=rec, note_on=tid, detail=detail)


def reply_updates(ticket, blocker, skip):
    """Updates posted after the wait by someone other than the worker or dev-auto."""
    return [
        u
        for u in ticket["updates"]
        if u["id"] > blocker["id"] and u.get("agent") not in skip
    ]


def verdict_wake(st, tid, rec, ticket, below=None):
    """Wake the worker once for the newest verdict someone other than the owner posted
    after the owner's last update before id `below` (any id when None): one that beat
    the wait note counts, one it had already read does not. True when typed."""
    mine = [
        u["id"]
        for u in ticket["updates"]
        if u["agent"] == ticket["agent_id"] and (below is None or u["id"] < below)
    ]
    floor = max([rec.get("since", 0), *mine[-1:]])
    found = [u for u, _, _ in verdicts(ticket) if u["id"] > floor]
    if not found:
        return False
    authority = finish_push(ticket)
    full = None
    if authority and found[-1] is authority[0] and NO_FEATURE_BRANCH.search(ticket.get("notes") or ""):
        full = finish_commit(rec, ticket, authority[1])
    if full:
        # armed() reads this on every later wake too, while withdraw_finish keeps it: only
        # as long as this very verdict is the one finish_push names.
        rec["finish"] = {"update": authority[0]["id"], "commit": full[0], "request": full[1]}
        text = (
            f"{FINISH_WAKE} {tid}, which has no "
            "feature branch. Read it, run done-check, record the PR title and body on the "
            "ticket, then complete the ticket with finish_work outcome complete. The "
            "orchestrator pushes and opens the PR."
        )
    else:
        rec.pop("finish", None)
        text = f"dev-auto: a review verdict was posted on ticket {tid}. Read it there and continue."
    key = f"{tid}|verdict|{found[-1]['id']}"
    return emit(st, key, text, worker=rec, note_on=tid)


FINISH_WAKE = "dev-auto: REVIEW VERDICT: PUSH was posted on ticket"


# One line per SHA: the one commit it names as an object id, never a ref of that name, or
# "-" when it names none, several objects or no commit.
RESOLVE_COMMIT = (
    'for s in "$@"; do f=$(git rev-parse --disambiguate="$s" 2>/dev/null); '
    'if [ -n "$f" ] && [ "$f" = "${f%%[[:space:]]*}" ] && [ "$(git cat-file -t "$f")" = commit ]; '
    'then echo "$f"; else echo -; fi; done'
)


def finish_push(ticket):
    """(update, SHA) of the ticket's latest live verdict when it is a standing PUSH on the
    SHA of the coder's latest review request (any SHA when it has made none), else None:
    the only verdict that may tell a standalone coder to finish."""
    live = verdicts(ticket)
    if not live:
        return None
    last, _, sha = live[-1]
    if not any(u is last for u, _ in standing_pushes(ticket)):
        return None
    requests = review_requests(ticket, 0, ticket["agent_id"])
    return None if requests and not alike(requests[-1][1], sha) else (last, sha)


def finish_commit(rec, ticket, sha):
    """(full SHA, request update id or None) when the one commit a finishing PUSH names, in
    the coder's worktree, is the one the coder's latest review request, if any, names
    there; else None."""
    requests = review_requests(ticket, 0, ticket["agent_id"])
    asked = [sha, *([requests[-1][1]] if requests else [])]
    code, out = run_on(rec["host"], rec["worktree"], ["sh", "-c", RESOLVE_COMMIT, "resolve", *asked])
    found = out.split() if code == 0 else []
    if len(found) != len(asked) or not FULL_SHA.fullmatch(found[0]) or len(set(found)) != 1:
        return None
    return found[0], latest_request_id(ticket)


def latest_request_id(ticket):
    """The update id of the coder's latest review request, or None."""
    requests = review_requests(ticket, 0, ticket["agent_id"])
    return requests[-1][0]["id"] if requests else None


def withdraw_finish(st, tickets):
    """A record's finish goal stands only while finish_push names a verdict, and an
    unshown verdict wake is typed again only while its verdict is the latest live one
    and, for a finish wake, that verdict still may finish: a withdrawn PUSH, or one on
    another SHA than the coder's request, never reissues its finish instruction."""
    for tid, rec in st["workers"].items():
        ticket = tickets.get((rec.get("project", ""), tid))
        if not ticket:
            continue
        live = verdicts(ticket)
        last = live[-1][0] if live else None
        authority = finish_push(ticket)
        armed_on = rec.get("finish")
        # A finish stands on the verdict verdict_wake proved; a replacement verdict earns
        # its own proof there, and one armed before that was stored is dropped.
        finish = (
            isinstance(armed_on, dict)
            and authority
            and armed_on.get("update") == authority[0]["id"]
            # A newer review request must be proven by verdict_wake before it finishes.
            and armed_on.get("request", 0) == latest_request_id(ticket)
        )
        if armed_on and not finish:
            del rec["finish"]
            st.save()
        for unshown in list(rec.get("unshown", [])):
            kind, _, ref = unshown["key"].partition("|")[2].partition("|")
            # Stale once its verdict, an update on this ticket, is withdrawn or answered
            # by a newer one; a key naming no update here is not judged.
            ids = {str(u["id"]) for u in ticket["updates"]}
            stale = ref in ids and (not last or ref != str(last["id"]))
            if kind == "verdict" and (stale or (not finish and unshown["text"].startswith(FINISH_WAKE))):
                rec["unshown"].remove(unshown)
                if not rec["unshown"]:
                    del rec["unshown"]
                st.save()


MISSING_REVIEW_TICKET = "has no review ticket"


def review_wait_gap(rec, tid, ticket, tickets, blocker):
    """Why this review wait cannot be ended, or None when it still can.

    A verdict after the note can end it. So can an unanswered review request when a
    review ticket exists to receive it. No review ticket, or a review ticket with no
    such request, cannot be ended."""
    live = verdicts(ticket)
    if answers_wait(blocker, live):
        return None
    open_request = any(
        not any(u["id"] > posted["id"] and alike(sha, s) for u, _, s in live)
        for posted, sha in review_requests(ticket, 0, ticket.get("agent_id"))
    )
    has_reviewer = tid.endswith("-review") or tickets.get(
        (rec.get("project", ""), f"{tid}-review")
    ) is not None
    if not has_reviewer and reviewer_none(ticket.get("notes")):
        # The orchestrator reviews it: orchestrator_reviews raised the request's event.
        return None
    if not has_reviewer:
        return MISSING_REVIEW_TICKET
    if open_request:
        return None
    return "has no REVIEW READY with a SHA"


def review_wait_stuck_text(tid, blocker, gap):
    return f"STUCK {tid}: its review wait {gap}, update {blocker['id']} on the ticket"


def on_wait(st, tid, rec, blocker, ticket, tickets, ready=False):
    """At most one wake a tick: none when `ready`, the REVIEW READY wake this tick
    already typed, which also covers a ticket wait's replies."""
    target = wait_target(blocker)
    key = f"{tid}|wake|{blocker['id']}"
    tracked = True
    gap = None
    if target == "operator":
        needs = f"{tid}|needs|{blocker['id']}"
        project = rec.get("project", "")
        coder = tid.endswith("-review") and tickets.get(
            (project, tid.removesuffix("-review"))
        )
        held = coder and waiting(coder)
        if held and wait_target(held) == "operator":
            # A reviewer's copy of its coder's operator wait: the coder's NEEDS YOU covers it.
            st["sent"].setdefault(needs, int(time.time()))
            st.save()
        else:
            text = f"NEEDS YOU {tid}: its worker waits on the operator, update {blocker['id']} on the ticket"
            emit(st, needs, text)
    elif target == "review":
        if not ready:
            verdict_wake(st, tid, rec, ticket, blocker["id"])
        # No grace: a wait nothing can end is the untracked STUCK, once per note.
        gap = review_wait_gap(rec, tid, ticket, tickets, blocker)
    elif target == "ci" and key in st["sent"]:
        pass  # its wake is typed: no more CI reads, a remote one opens a terminal
    elif target == "ci":
        result = ci_result(rec, ticket, blocker)
        tracked = result is not None
        if result and not ready:
            text = f"dev-auto: CI finished with {result}. Continue ticket {tid}."
            emit(st, key, text, worker=rec, note_on=tid)
    elif target == "process":
        words = blocker["content"].removeprefix("Waiting on:").split()
        pid = words[1].rstrip(".,;") if len(words) > 1 else ""
        tracked = bool(re.fullmatch(r"[0-9]+", pid))
        alive = process_alive(rec, pid) if tracked and not ready and key not in st["sent"] else True
        # An unread probe tracks nothing this tick: the wait's STUCK, once per note, and
        # the next tick probes again.
        tracked = tracked and alive is not None
        if alive is False:
            text = f"dev-auto: process {pid} has exited. Continue ticket {tid}."
            emit(st, key, text, worker=rec, note_on=tid)
    elif target.startswith("ticket:"):
        other = tickets.get((rec.get("project", ""), target.removeprefix("ticket:")))
        tracked = other is not None
        if other:
            skip = {SUPERVISOR, ticket.get("agent_id"), rec.get("agent_id")}
            sources = [ticket] if other is ticket else [other, ticket]
            # A reviewer waiting on its coder hears only updates that name its review
            # ticket; review requests are review_ready's wake.
            coder = other is not ticket and other["task_id"] == tid.removesuffix("-review")
            named = re.compile(rf"(?<![\w-]){re.escape(tid)}(?![\w-])")
            # A ticket owner's own Waiting on note is its wait, not a reply.
            replies = [
                (u["id"], source["task_id"])
                for source in sources
                for u in reply_updates(source, blocker, skip)
                if not (
                    u["agent"] == source["agent_id"]
                    and u["content"].startswith("Waiting on:")
                )
                and not (coder and source is other and not named.search(u["content"]))
            ]
            wakes = []
            if other["status"] in DONE:
                text = f"dev-auto: ticket {other['task_id']} is {other['status']}. Continue ticket {tid}."
                # silence types the same wake to a reviewer with no live wait.
                wakes.append((f"{tid}|coder-done" if coder else key, text))
            if replies:
                newest, source = max(replies)
                text = f"dev-auto: a new update was posted on ticket {source}. Continue ticket {tid}."
                wakes.append((f"{key}|{source}|{newest}", text))
            fresh = [(k, text) for k, text in wakes if k not in st["sent"]]
            if fresh and (ready or emit(st, *fresh[0], worker=rec, note_on=tid)):
                # The one wake typed this tick covers every other pending one.
                for k, _ in fresh:
                    st["sent"].setdefault(k, int(time.time()))
                st.save()
    else:
        tracked = False
    if target == "review" and gap:
        tracked = False
    if not tracked:
        if target == "review" and gap:
            text = review_wait_stuck_text(tid, blocker, gap)
        else:
            text = (
                f"STUCK {tid}: dev-auto cannot track its Waiting on note, "
                f"update {blocker['id']} on the ticket"
            )
        emit(st, f"{tid}|wait|{blocker['id']}", text)


def reviewed_all(coder):
    """True when every review request on the coder's ticket has a later live verdict for
    its SHA: its reviewer has nothing to review and waits."""
    live = verdicts(coder)
    return bool(live) and all(
        any(v["id"] > u["id"] and alike(sha, s) for v, _, s in live)
        for u, sha in review_requests(coder, 0, coder["agent_id"])
    )


def silence(cfg, st, tid, rec, last, now, lines, tickets):
    """One nudge after silent_minutes of no output, or idle_minutes when the screen is the
    agent's own prompt with no turn running, then one STUCK as long after it. The nudge's own echo, up to the reading emit takes after the send, is not output
    from the worker; only output past it clears the nudge.

    A reviewer instead gets one wake when its coder ticket is done, and no nudge while
    its latest verdict on that ticket is newer than every review request there."""
    coder = tid.endswith("-review") and tickets.get(
        (rec.get("project", ""), tid.removesuffix("-review"))
    )
    if coder and coder["status"] in DONE:
        text = f"dev-auto: ticket {coder['task_id']} is {coder['status']}. Continue ticket {tid}."
        emit(st, f"{tid}|coder-done", text, worker=rec, note_on=tid)
        return
    idle = agent_ui(rec["agent"], lines) and not in_turn(lines)
    minutes = cfg["idle_minutes"] if idle else cfg["silent_minutes"]
    limit = minutes * 60
    nudged = rec.get("nudged_at")
    if nudged is not None and last <= rec.get("nudge_echo", nudged):
        if now - nudged >= limit:
            text = (
                f"STUCK {tid} {rec['agent_id']} on {rec['host']} {rec['terminal']}: silent "
                f"{minutes} minutes after a nudge. Its last screen is on the ticket"
            )
            screen = "\n".join(lines[-15:])
            emit(st, f"{tid}|silent|{int(nudged)}", text, note_on=tid, detail=screen)
        return
    rec.pop("nudged_at", None)
    rec.pop("nudge_echo", None)
    if coder and reviewed_all(coder):
        return
    if now - last < limit:
        return
    text = (
        f"dev-auto: no output for {minutes} minutes while ticket {tid} "
        "is open. Continue, or record a Waiting on: blocker note on it."
    )
    if emit(st, f"{tid}|nudge|{int(last)}", text, worker=rec, note_on=tid):
        rec["nudged_at"] = now
        echo = rec.get("wake", {}).get("output")
        if echo:
            rec["nudge_echo"] = echo / 1000


# Prints one verdict line, "<verdict> <branch> <HEAD> <git common dir>"; any failed
# step exits 2 instead of reading as clean or unmerged. $1 is the ticket's feature slug.
# Optional arguments are the cleared SHA, the declared feature branch, then pairs of
# configured GitHub repository and base, one pair per argument.
# A task/<feature>/<lane> branch is merged when HEAD and the remote branch, if any, are
# in feature/<feature>, origin's after a fetch or the local one, either one sufficing;
# a branch of a feature not among the given ones is unchecked. Its cleared HEAD can also
# be in the feature branch's merged PR commit after the feature ref disappears.
# Otherwise a GitHub origin's PRs decide, and gh failing there is unchecked, since git
# cannot see a squash merge;
# another remote leaves it to git: the same test against the remote's default branch.
# A detached checkout has no branch: it is merged when origin's default branch holds
# HEAD after a fetch.
VERDICTS = ("dirty", "merged", "remote-ahead", "open", "unmerged")
CHECK = (
    "b=$(git rev-parse --abbrev-ref HEAD) || exit 2; "
    "h=$(git rev-parse HEAD) || exit 2; "
    "c=$(git rev-parse --path-format=absolute --git-common-dir) || exit 2; "
    'v() { echo "$1 $b $h $c"; exit 0; }; '
    "s=$(git status --porcelain --untracked-files=all) || exit 2; "
    '[ -z "$s" ] || v dirty; '
    "git fetch -q origin 2>/dev/null; f=$?; "
    'if [ "$b" = HEAD ]; then [ "$f" = 0 ] || exit 2; '
    "t=$(git ls-remote origin HEAD) || exit 2; t=${t%%[[:space:]]*}; "
    '[ -n "$t" ] || exit 2; '
    'git merge-base --is-ancestor HEAD "$t" 2>/dev/null && v merged; v unmerged; fi; '
    'r=$(git ls-remote --heads origin "refs/heads/$b") || exit 2; r=${r%%[[:space:]]*}; '
    'case $b in task/*/*) x=${b#task/}; x=${x%%/*}; '
    'case " $1 " in *" $x "*) ;; *) exit 2;; esac; '
    't=$(git ls-remote --heads origin "refs/heads/feature/$x") || exit 2; '
    't=${t%%[[:space:]]*}; if [ -n "$t" ]; then [ "$f" = 0 ] || exit 2; fi; '
    'l=$(git rev-parse -q --verify "refs/heads/feature/$x"); a=; '
    # A branch with no commits of its own is inside origin's default branch.
    "d=$(git ls-remote origin HEAD 2>/dev/null); d=${d%%[[:space:]]*}; "
    'for o in $t $l $d; do git merge-base --is-ancestor HEAD "$o" 2>/dev/null || continue; '
    'if [ -z "$r" ] || git merge-base --is-ancestor "$r" "$o" 2>/dev/null; '
    'then v merged; fi; a=1; done; '
    'if [ -n "${2:-}" ] && [ -n "${3:-}" ] '
    '&& [ "$(git rev-parse --disambiguate="$2" 2>/dev/null)" = "$h" ]; then '
    'u=$(git remote get-url origin) || exit 2; '
    'case "$u" in https://github.com/*|git@github.com:*|ssh://git@github.com/*) '
    'repo=${u#*github.com[:/]}; repo=${repo%/}; repo=${repo%.git}; feature=$3; shift 3; '
    'for entry in "$@"; do [ "${entry% *}" = "$repo" ] || continue; '
    '[ "$f" = 0 ] || exit 2; base=${entry##* }; '
    'm=$(gh pr list --repo "github.com/$repo" --head "$feature" --base "$base" --state merged'
    ' --json mergeCommit,isCrossRepository'
    " --jq '.[] | select(.isCrossRepository == false) | .mergeCommit.oid // empty') || exit 2; "
    'for o in $m; do git merge-base --is-ancestor "$h" "$o" 2>/dev/null || continue; '
    'if [ -z "$r" ] || git merge-base --is-ancestor "$r" "$o" 2>/dev/null; '
    'then v merged; fi; a=1; done; break; done;; esac; fi; '
    '[ -z "$a" ] || v remote-ahead; v unmerged;; esac; '
    "case $(git remote get-url origin 2>/dev/null) in *github.com[:/]*) "
    'm=$(gh pr list --head "$b" --state merged --json headRefOid'
    " --jq '.[].headRefOid') || exit 2; "
    'for o in $m; do git merge-base --is-ancestor HEAD "$o" 2>/dev/null || continue; '
    'if [ -z "$r" ] || git merge-base --is-ancestor "$r" "$o" 2>/dev/null; '
    "then v merged; fi; v remote-ahead; done; "
    'p=$(gh pr list --head "$b" --state open --json number'
    " --jq '.[].number') || exit 2; "
    '[ -z "$p" ] || v open; v unmerged;; esac; '
    '[ "$f" = 0 ] || exit 2; '
    "t=$(git ls-remote origin HEAD) || exit 2; t=${t%%[[:space:]]*}; "
    '[ -n "$t" ] || exit 2; '
    'git merge-base --is-ancestor HEAD "$t" 2>/dev/null || v unmerged; '
    'if [ -z "$r" ] || git merge-base --is-ancestor "$r" "$t" 2>/dev/null; '
    "then v merged; fi; v remote-ahead"
)


def owned_now(st, tid, rec, ticket):
    """A fresh listing in which the record's own handle, or its launch ptyId, names its
    terminal, so a close can only hit dev-auto's own terminal; otherwise STUCK and None."""
    terminals = listing(rec["host"])
    row = (terminals or {}).get(rec["terminal"])
    if row:
        # Its own handle names it, whatever pty Orca now shows under it.
        return terminals
    moved = own_row({"pty": rec.get("pty")}, terminals or {}) if rec.get("pty") else None
    if moved:
        # Its launch ptyId names it under a new handle.
        rec["terminal"] = moved["handle"]
        return terminals
    text = (
        f"STUCK {tid}: ticket is {ticket['status']} but its terminal is not provably "
        "its own. Nothing closed or removed"
    )
    emit(st, f"{tid}|cleanup|owner", text, note_on=tid)
    return None


def shells_in(host, worktree, now):
    """Orca's default shell from `orca worktree create`, which cleanup may close with the
    worktree: Orca's createdAt for it is under a minute old, not in the future, and its
    only terminal is that bare shell. Its screen is kept so cleanup can tell it was never
    used. A folder's own times prove nothing: the path may have been re-created."""
    shown = orca(host, "worktree", "show", "--worktree", f"path:{worktree}")
    born = ((shown or {}).get("worktree") or {}).get("createdAt")
    if not born or not 0 <= now - born / 1000 <= SETTLED:
        return []
    rows = [
        row
        for row in (listing(host) or {}).values()
        if same_path(row.get("worktreePath"), worktree, host)
    ]
    if len(rows) != 1 or not rows[0].get("ptyId") or rows[0].get("agentIdentity"):
        return []
    handle = rows[0]["handle"]
    screen = read_screen(host, handle)
    if not bare_shell(screen):
        return []
    return [{"terminal": handle, "pty": rows[0]["ptyId"], "lines": screen["lines"]}]


def spare(st, tid, rec, ticket, terminals):
    """Plain shells with no agent in this worktree, to close with it. None, after a
    STUCK, when any agent or any other terminal is open here. Terminals in other
    folders are ignored."""
    host, worktree = rec["host"], rec["worktree"]
    shells = []
    for handle, row in terminals.items():
        if handle == rec["terminal"] or row.get("agentIdentity"):
            continue
        if not same_path(row.get("worktreePath"), worktree, host):
            continue
        if bare_shell(read_screen(host, handle)):
            shells.append(handle)
    shells.sort()
    others = sorted(
        handle
        for handle, row in terminals.items()
        if handle != rec["terminal"]
        and handle not in shells
        and same_path(row.get("worktreePath"), worktree, host)
    )
    if not others:
        return shells
    text = (
        f"STUCK {tid}: ticket is {ticket['status']} but its worktree holds other "
        f"terminals {', '.join(others)}. Not removed"
    )
    where = f"{rec['worktree']} on {rec['host']}"
    emit(st, f"{tid}|cleanup|busy", text, note_on=tid, detail=where)
    return None


def cleared_head(ticket):
    """The SHA of the ticket's latest unsuperseded verdict by someone other than its owner,
    when that verdict is PUSH; a later FIX or a superseded PUSH clears nothing."""
    verdicts = [
        u["content"].split()
        for u in ticket["updates"]
        if u["content"].startswith("REVIEW VERDICT:")
        and u["agent"] != ticket["agent_id"]
        and u.get("superseded_by") is None
    ]
    words = verdicts[-1] if verdicts else []
    if words[2:3] == ["PUSH"] and len(words) > 3 and SHA.fullmatch(words[3]):
        return words[3]
    return None


def settled(st, tid, text):
    """One line in delivered.log for a step the tick took itself; nothing is sent."""
    delivered(st, tid, text)
    print(text)


def merge_wait(cfg, st, tid, rec, ticket, coder, head, verdict, detail, now):
    """A finished ticket's unmerged head that a PUSH on its coder ticket cleared waits for
    its pull request: kept with one settled line, rechecked, and removed by the merged
    check. True while it waits; one STUCK once it has waited merge_wait_hours, an open pull
    request included. Off at 0; an open pull request starts no wait."""
    hours = cfg["merge_wait_hours"]
    sha = cleared_head(coder)
    if not hours or not sha or not head.startswith(sha):
        return False
    wait = rec.get("merge_wait")
    if wait and wait["head"] != head:
        wait = None
    if verdict == "open" and not wait:
        return False
    if not wait:
        rec["merge_wait"] = {"head": head, "at": now}
        settled(
            st,
            tid,
            f"SETTLED {tid}: ticket is {ticket['status']}; its worktree waits for its "
            f"cleared PUSH {sha} to merge, rechecked every {RECHECK // 60} minutes, "
            "and is removed then",
        )
    elif now - wait["at"] >= hours * 3600:
        text = (
            f"STUCK {tid}: ticket is {ticket['status']} but its worktree is still unmerged "
            f"{hours:g} hours after its cleared PUSH {sha}, rechecked every "
            f"{RECHECK // 60} minutes. Not removed"
        )
        emit(st, f"{tid}|cleanup|merge-wait|{head}", text, note_on=tid, detail=detail)
    return True


def worktree_gone(host, worktree):
    """The worktree is gone: its folder here, or Orca's record of it there by Orca's own
    selector_not_found, never by a lookup that timed out or could not be read."""
    if host == "local":
        return not Path(worktree).exists()
    shown = run(
        [ORCA, "worktree", "show", "--worktree", f"path:{worktree}", "--json"]
        + ["--environment", host]
    )
    try:
        error = json.loads(shown.stdout).get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    return error.get("code") == "selector_not_found"


def cleanup(cfg, st, tid, rec, ticket, terminals, now, tickets):
    host, worktree = rec["host"], rec["worktree"]
    rec["done"] = True
    row = own_row(rec, terminals)
    # Its retired entry ties revived panes by the branch its worktree shows now, on its own
    # pane or another there; supervise may never have read it.
    shown = [row] if row else [
        r for r in terminals.values() if same_path(r.get("worktreePath"), worktree, host)
    ]
    branch = next((r["branch"] for r in shown if r.get("branch")), None)
    if branch:
        rec["branch"] = branch.removeprefix("refs/heads/")
    # A terminal already gone has nothing to close; the worktree checks below still run.
    if not rec.get("closed") and row is not None:
        if owned_now(st, tid, rec, ticket) is None:
            return
        if rec["agent"] == "sdk" and not end_runner(st, tid, rec):
            return
        if close_terminal(host, rec["terminal"]) is None:
            text = (
                f"STUCK {tid}: ticket is {ticket['status']} but closing its terminal "
                f"{rec['terminal']} failed. Tried again each tick"
            )
            emit(st, f"{tid}|cleanup|close", text, note_on=tid)
            return
        # The tick's listing is shared by every record on this host: a later cleanup in
        # this tick must not count the terminal just closed as another.
        terminals.pop(rec["terminal"], None)
        rec["closed"] = True
    sharing = [
        t
        for t, r in st["workers"].items()
        if t != tid and r["host"] == host and same_path(r["worktree"], worktree, host)
    ]
    if sharing:
        # The worktree's default shells, and its ownership, go with whichever record
        # removes it.
        keeper = st["workers"][sharing[0]]
        keeper.setdefault("shells", []).extend(rec.get("shells", []))
        if rec.get("owns_worktree") is not False:
            keeper["owns_worktree"] = True
            if rec.get("owned_branch") and not keeper.get("owned_branch"):
                keeper["owned_branch"] = rec["owned_branch"]
                keeper["owned_common"] = rec.get("owned_common")
        retire(st, tid, rec, terminals, now)
        if keeper.get("owns_worktree"):
            remember_worktree(st, keeper, now)
        print(
            f"{tid}: {ticket['status']}; closed its terminal, worktree kept for {sharing[0]}"
        )
        return
    if rec.get("owns_worktree") is False:
        # Launched into a worktree it does not own: the worktree, its branch and every
        # other terminal stay.
        retire(st, tid, rec, terminals, now)
        print(f"{tid}: {ticket['status']}; closed its terminal, worktree kept")
        return
    if host == "local":
        head = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    else:
        # Orca's own record of the worktree: no probe terminal on the host.
        shown = orca(host, "worktree", "show", "--worktree", f"path:{worktree}")
        head = ((shown or {}).get("worktree") or {}).get("head") or ""
    seen = [
        ticket["status"],
        max((u["id"] for u in ticket["updates"] if u["agent"] != SUPERVISOR), default=0),
        head,
        sorted(
            [h, r.get("agentIdentity")]
            for h, r in terminals.items()
            if h != rec["terminal"] and same_path(r.get("worktreePath"), worktree, host)
        ),
    ]
    last = rec.get("checked")
    if last and last["seen"] == seen and now - last["at"] < RECHECK:
        return
    rec["checked"] = {"at": now, "seen": seen}
    if spare(st, tid, rec, ticket, terminals) is None:
        return
    feature = check_features(rec, ticket, tickets)
    coder = tickets.get((rec.get("project", ""), tid.removesuffix("-review"))) or ticket
    delivery = FEATURE_BRANCH.search(coder.get("notes") or "")
    cleared = cleared_head(coder)
    args = [feature]
    if cleared and delivery and delivery[1] != "none" and BRANCH.fullmatch(delivery[1]):
        bases = [
            f"{slug} {base}"
            for slug, repo in cfg["repos"].items()
            if (base := repo.get("base")) and BRANCH.fullmatch(base)
        ]
        if bases:
            args.extend((cleared, delivery[1], *bases))
    code, out = run_on(host, worktree, ["sh", "-c", CHECK, "check", *args])
    parts = (out.strip().splitlines() or [""])[-1].split(" ", 3)
    where = f"{worktree} on {host}"
    if code != 0 or len(parts) < 4 or parts[0] not in VERDICTS:
        # A worktree already gone holds nothing to clean.
        if worktree_gone(host, worktree):
            retire(st, tid, rec, terminals, now)
            print(f"{tid}: {ticket['status']}; its worktree {worktree} is gone, record dropped")
            return
        text = f"STUCK {tid}: ticket is {ticket['status']} but its worktree could not be checked"
        emit(st, f"{tid}|cleanup|unchecked", text, note_on=tid, detail=where)
        return
    verdict, branch, head, common = parts
    if branch != "HEAD":
        rec["branch"] = branch
    main = str(PurePosixPath(common).parent)
    detail = f"{where}, branch {branch}"
    if verdict in ("open", "unmerged") and merge_wait(
        cfg, st, tid, rec, ticket, coder, head, verdict, detail, now
    ):
        return
    if verdict == "open":
        return
    if verdict != "merged":
        text = f"STUCK {tid}: ticket is {ticket['status']} but its worktree is {verdict}. Not removed"
        emit(st, f"{tid}|cleanup|{verdict}", text, note_on=tid, detail=detail)
        return
    if not owns_checkout(rec, branch, common):
        # Its launch owned only its own branch and repository: this checkout is another's.
        own, repo = rec.get("owned_branch"), rec.get("owned_common")
        if own and repo and branch == own:
            held = f"holds the repository {common}, not its own {repo}"
        else:
            mine = own if own and repo else "branch, which its launch did not record"
            held = f"has {branch} checked out, not its own {mine}"
        text = f"STUCK {tid}: ticket is {ticket['status']} but its worktree {held}. Not removed"
        emit(st, f"{tid}|cleanup|owned", text, note_on=tid, detail=where)
        return
    if same_path(main, worktree, host):
        text = f"STUCK {tid}: ticket is {ticket['status']} but its worktree is the main checkout. Not removed"
        emit(st, f"{tid}|cleanup|main", text, note_on=tid, detail=where)
        return
    base = tid.removesuffix("-review")
    open_pr = next(
        (
            d
            for d in st.get("deliveries", {}).values()
            if base in d.get("tickets", []) and (d.get("pr") or d.get("pushed")) and not d.get("merged")
        ),
        None,
    )
    if open_pr:
        # Its pull request's check may still fail: the worktree stays until it merges.
        what = f"pull request #{open_pr['pr']}" if open_pr.get("pr") else "its pushed branch"
        print(f"{tid}: {ticket['status']} and merged locally; worktree kept until {what} merges")
        return
    # The check took seconds: prove an empty worktree again right now.
    terminals = listing(host)
    if terminals is not None and not rec.get("closed") and own_row(rec, terminals):
        # Its own terminal is listed again: it is closed through owned_now first.
        return
    shells = None if terminals is None else spare(st, tid, rec, ticket, terminals)
    if shells is None:
        return
    if any(close_terminal(host, handle) is None for handle in shells):
        print(f"{tid}: closing a shell in its worktree failed; checked again next tick")
        return
    if not removable_now(host, worktree, branch, head, common):
        print(f"{tid}: its worktree changed during its check; checked again next tick")
        return
    if orca(host, "worktree", "rm", "--worktree", f"path:{worktree}") is None:
        text = f"STUCK {tid}: orca worktree rm failed for its worktree"
        emit(st, f"{tid}|cleanup|rm", text, note_on=tid, detail=where)
        return
    # Deleted only while it still points at the checked HEAD, so a later commit survives.
    ref = ["git", "update-ref", "-d", f"refs/heads/{branch}", head]
    kept = run_on(host, main, ref)[0] != 0
    if kept:
        text = f"STUCK {tid}: its worktree was removed, but its branch moved or could not be deleted. Branch kept"
        emit(
            st, f"{tid}|cleanup|branch", text, note_on=tid, detail=f"{branch} on {host}"
        )
    retire(st, tid, rec, terminals, now)
    gone = f"; branch {branch} kept" if kept else f" and branch {branch}"
    print(f"{tid}: {ticket['status']} and merged; removed {worktree}{gone}")


# A shell this long without output, at its prompt in a recorded worktree, is closed.
IDLE_SHELL = 600


def held_handles(st, host):
    """Handles on the host that a record or the orchestrator holds."""
    records = [r for r in st["workers"].values() if r["host"] == host]
    held = {h for r in records for h in (r.get("terminal"), *r.get("handles", []))}
    pointer = st["orchestrator"]
    if pointer and pointer["host"] == host:
        held.add(pointer["terminal"])
    held.discard(None)
    return held


def idle_shells(st, host, terminals, now):
    """Close each plain shell, no agent in it, that sits at its prompt in a recorded
    worktree with no output for IDLE_SHELL seconds and that no record or the orchestrator
    holds. A dev-auto probe is never one."""
    worktrees = [r["worktree"] for r in st["workers"].values() if r["host"] == host]
    held = held_handles(st, host)
    for handle, row in sorted(terminals.items()):
        if handle in held or row.get("agentIdentity") or row.get("title") == "dev-auto probe":
            continue
        quiet = now - (row.get("lastOutputAt") or 0) / 1000
        if quiet < IDLE_SHELL:
            continue
        if not any(same_path(row.get("worktreePath"), w, host) for w in worktrees):
            continue
        if not bare_shell(read_screen(host, handle)):
            continue
        if close_terminal(host, handle) is None:
            print(f"{host}: closing idle shell {handle} failed; tried again next tick")
            continue
        terminals.pop(handle, None)
        print(f"{host}: closed idle shell {handle} in {row.get('worktreePath')}")


def names_worktree(tickets, path, branch):
    """An open ticket whose notes or title name the worktree's path or branch."""
    for ticket in tickets.values():
        if ticket["status"] in DONE:
            continue
        text = f"{ticket.get('title') or ''}\n{ticket.get('notes') or ''}"
        if path in text or (branch and branch not in ("HEAD", "") and branch in text):
            return ticket["task_id"]
    return None


def sweep_worktrees(st, tickets, terminals, now):
    """Remove a worktree dev-auto owned once no record holds it, its ticket is complete
    or closed, no open ticket names it, it holds only idle shells before and after CHECK,
    and CHECK finds the branch its launch owned clean and merged (into its feature branch,
    the repo's default branch or a merged pull request) and not the main checkout. Its
    shells close with it and its branch goes while it points at the checked head.
    Anything else, a refused shell close too, keeps it, checked again after RECHECK; a
    worktree already gone is forgotten."""
    known = st.setdefault("worktrees", {})
    extra = {}
    for key, entry in sorted(known.items()):
        host, path = entry["host"], entry["worktree"]
        if now - entry.get("checked", 0) < RECHECK:
            continue
        if host not in terminals and host not in extra:
            # A host with no record left is listed here, once a tick.
            extra[host] = listing(host)
        listed = terminals[host] if host in terminals else extra[host]
        if listed is None:
            continue
        if any(r["host"] == host and same_path(r["worktree"], path, host) for r in st["workers"].values()):
            continue
        ticket = tickets.get((entry.get("project", ""), entry.get("ticket")))
        if ticket and ticket["status"] not in DONE:
            continue
        here = [(h, r) for h, r in listed.items() if same_path(r.get("worktreePath"), path, host)]
        if any(r.get("agentIdentity") for _h, r in here):
            continue
        entry["checked"] = int(now)
        if worktree_gone(host, path):
            del known[key]
            continue
        shells = [h for h, _r in here if bare_shell(read_screen(host, h))]
        if len(shells) != len(here):
            continue
        feature = check_features(entry, ticket, tickets)
        code, out = run_on(host, path, ["sh", "-c", CHECK, "check", feature])
        parts = (out.strip().splitlines() or [""])[-1].split(" ", 3)
        if code != 0 or len(parts) < 4 or parts[0] not in VERDICTS:
            continue
        verdict, branch, head, common = parts
        main = str(PurePosixPath(common).parent)
        if verdict != "merged" or same_path(main, path, host) or names_worktree(tickets, path, branch):
            continue
        if not owns_checkout(entry, branch, common):
            continue
        # The check took seconds: only the shells proved idle before it may be here now,
        # still at their prompt, and each must close.
        fresh = listing(host)
        if fresh is None:
            continue
        now_here = [h for h, r in fresh.items() if same_path(r.get("worktreePath"), path, host)]
        if set(now_here) - set(shells) or any(fresh[h].get("agentIdentity") for h in now_here):
            continue
        if not all(bare_shell(read_screen(host, h)) for h in now_here):
            continue
        if any(close_terminal(host, h) is None for h in now_here):
            print(f"{host}: closing a shell in {path} failed; tried again later")
            continue
        for handle in now_here:
            listed.pop(handle, None)
        if not removable_now(host, path, branch, head, common):
            continue
        if orca(host, "worktree", "rm", "--worktree", f"path:{path}") is None:
            print(f"{host}: orca worktree rm failed for {path}; tried again later")
            continue
        del known[key]
        run_on(host, main, ["git", "update-ref", "-d", f"refs/heads/{branch}", head])
        print(f"{host}: removed merged worktree {path} (branch {branch}) of {entry.get('ticket')}")


# Evidence


def feature_groups(tickets):
    """Tickets grouped by the title before ` / `. A title with no feature is skipped."""
    groups = {}
    for ticket in tickets.values():
        title = ticket["title"] or ""
        if " / " not in title:
            continue
        feature = title.split(" / ", 1)[0]
        groups.setdefault(feature, []).append(ticket)
    return groups


def checkout_of(seed):
    """The main checkout for a local path, when it has a real .work directory.

    Always resolved through git's common dir, including when the seed itself
    contains .work. A linked worktree's folder is not the main checkout.
    A failed resolve is ignored: nothing is judged until a checkout is known.
    """
    path = Path(seed)
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    code, out = run_on(
        "local",
        str(path),
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    )
    if code != 0:
        return None
    common = Path(out.strip())
    if common.name != ".git":
        return None
    checkout = common.parent
    try:
        work = checkout / ".work"
        if work.is_symlink() or not work.is_dir():
            return None
    except OSError:
        return None
    return checkout


def root_commits(checkout):
    """The root commits of the checkout's HEAD; None when unread."""
    code, out = run_on("local", str(checkout), ["git", "rev-list", "--max-parents=0", "HEAD"])
    return set(out.split()) if code == 0 and out.strip() else None


def origin_url(checkout, host="local"):
    """The URL git fetches origin from, after insteadOf and with several URLs the first,
    which tells forks of one history apart; None when git cannot name it."""
    code, out = run_on(host, str(checkout), ["git", "remote", "get-url", "origin"])
    return out.strip() if code == 0 and out.strip() else None


def ledger_checkout(root):
    """The main checkout `ledger-init --checkout` recorded for this ledger, while that
    path still is a main checkout of the recorded repo (its root commit and origin's URL);
    else None, and no evidence is removed. A worker's or the orchestrator's worktree can belong to
    another repo, even one whose folder has the same name, and another repo can take the
    recorded path, so only this record names the ledger's repo."""
    try:
        recorded = (root / "supervisor" / "checkout").read_text().split("\n")
    except OSError:
        return None
    if len(recorded) < 3 or not all(recorded[:3]):
        return None
    path, first, url = recorded[:3]
    checkout = checkout_of(path)
    if checkout is None or os.path.realpath(checkout) != os.path.realpath(path):
        return None
    roots, fetches = root_commits(checkout), origin_url(checkout)
    if roots is None or fetches is None:
        print(f"STUCK evidence: {path} could not be checked as this ledger's checkout. Nothing removed")
        return None
    if first not in roots or fetches != url:
        return None
    return Path(os.path.realpath(checkout))


def evidence_place(checkout, feature_slug):
    """The evidence directory, 'missing', or 'unsafe'.

    Safe only when the resolved path is a real direct child of <checkout>/.work/evidence.
    """
    if not re.fullmatch(r"[a-z0-9._-]+", feature_slug) or feature_slug in {".", ".."}:
        return "unsafe"
    work = checkout / ".work"
    evidence = work / "evidence"
    try:
        if work.is_symlink() or evidence.is_symlink():
            return "unsafe"
        if not evidence.is_dir():
            return "missing"
        child = evidence / feature_slug
        if child.is_symlink():
            return "unsafe"
        if not child.exists():
            return "missing"
        if not child.is_dir():
            return "unsafe"
        real_evidence = Path(os.path.realpath(evidence))
        real_child = Path(os.path.realpath(child))
        if real_child.parent != real_evidence or real_child.name != feature_slug:
            return "unsafe"
    except OSError:
        return "unsafe"
    return child


def cites_evidence(text, feature_slug):
    """evidence/<slug> as a whole path component: evidence/<slug>-v2 and
    evidence/<slug>.v2 are other folders; a dot that ends a sentence is not."""
    return bool(text) and bool(
        re.search(rf"evidence/{re.escape(feature_slug)}(?![\w-]|\.[\w-])", text)
    )


def is_spec_markdown(name):
    return name.endswith(".md")


def list_spec_markdown(specs_dir):
    """Markdown paths in specs_dir, or None if the directory cannot be listed.

    iterdir raises on a permission error. Path.glob swallows that and looks empty.
    """
    try:
        return sorted(
            path for path in specs_dir.iterdir() if is_spec_markdown(path.name)
        )
    except OSError:
        return None


def evidence_cited(tickets, checkout, feature_slug):
    """True when cited, False when not, None when the check cannot run.

    A missing specs directory cites nothing. A symlinked specs directory is read
    at its target. A specs path that cannot be read does not run.
    """
    for ticket in tickets.values():
        if ticket["status"] in DONE:
            continue
        if cites_evidence(ticket.get("notes"), feature_slug) or cites_evidence(
            ticket.get("title"), feature_slug
        ):
            return True
        for update in ticket["updates"]:
            if cites_evidence(update.get("content"), feature_slug):
                return True
    specs = checkout / ".work" / "specs"
    try:
        if not specs.exists():
            if specs.is_symlink():
                return None
            return False
        specs_dir = Path(os.path.realpath(specs))
        if not specs_dir.is_dir():
            return False
        listed = list_spec_markdown(specs_dir)
        if listed is None:
            return None
        for spec in listed:
            if spec.is_symlink() or not spec.is_file():
                return True
            if cites_evidence(spec.read_text(errors="replace"), feature_slug):
                return True
    except OSError:
        return None
    return False


def evidence_ignored(checkout, place):
    """True when place is gitignored and untracked, False when it is not.

    None when git cannot answer. Tracked files are not ignored .work, even when
    a gitignore rule would otherwise match them.
    """
    code, _output = run_on(
        "local",
        str(checkout),
        ["git", "check-ignore", "-q", "--", str(place)],
    )
    if code == 1:
        return False
    if code != 0:
        return None
    code, out = run_on(
        "local", str(checkout), ["git", "ls-files", "--", str(place)]
    )
    if code != 0:
        return None
    return not out.strip()


def branch_landed(checkout, branch, repos, base=None):
    """'landed', 'unmerged', 'absent', or 'stuck'.

    The branch's tip on origin, as the fetch wrote it, and the checkout's own ref, each
    when present, must both be in origin/<base>; absent from both is 'absent'. base defaults to the repo's
    configured base. Branch and base are fetched before merge-base, so a tip that is not
    yet a local object is judged unmerged instead of failing the check. A broken local
    ref, ls-remote, rev-parse, that fetch, or any other merge-base status is stuck.
    """
    ref = f"refs/heads/{branch}"
    code, out = run_on(
        "local", str(checkout), ["git", "ls-remote", "--heads", "origin", ref]
    )
    if code != 0:
        return "stuck"
    tips = out.split()[:1]
    refs = [f"{ref}:refs/remotes/origin/{branch}"] if tips else []
    # Only stderr tells a missing ref from one git warns is broken: both exit 1.
    done = run(["git", "rev-parse", "-q", "--verify", ref], cwd=str(checkout), timeout=60)
    if done.returncode == 0 and done.stdout.split():
        tips.append(done.stdout.split()[0])
    elif done.returncode != 1 or done.stderr.strip():
        return "stuck"
    if not tips:
        return "absent"
    if base is None:
        base = "staging"
        if repos:
            code, url = run_on("local", str(checkout), ["git", "remote", "get-url", "origin"])
            if code != 0:
                return "stuck"
            found = ORIGIN_SLUG.search(url.strip())
            repo = f"{found[1]}/{found[2]}" if found else url.strip()
            base = repos.get(repo, {}).get("base", "staging")
    code, _output = run_on(
        "local",
        str(checkout),
        ["git", "fetch", "-q", "origin", *refs, f"refs/heads/{base}:refs/remotes/origin/{base}"],
    )
    if code != 0:
        return "stuck"
    if refs:
        # origin can move between ls-remote and the fetch: test what the fetch wrote.
        code, out = run_on(
            "local", str(checkout), ["git", "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}"]
        )
        if code != 0 or not out.split():
            return "stuck"
        tips[0] = out.split()[0]
    for tip in dict.fromkeys(tips):
        code, _output = run_on(
            "local",
            str(checkout),
            ["git", "merge-base", "--is-ancestor", tip, f"refs/remotes/origin/{base}"],
        )
        if code == 1:
            return "unmerged"
        if code != 0:
            return "stuck"
    return "landed"


def reviewed_sha(ticket):
    """(request SHA, verdict SHA) for the ticket's latest review request from its owner,
    when the newest live verdict after it is a standing PUSH naming that SHA; None
    otherwise. Both tokens are kept: a prefix match proves nothing until git resolves both
    to one commit (commit_landed). A PUSH of an older request, on a ticket reopened since,
    proves nothing about its last work."""
    requests = review_requests(ticket, 0, ticket["agent_id"])
    if not requests:
        return None
    asked, sha = requests[-1]
    live = [(u, kind, given) for u, kind, given in verdicts(ticket) if u["id"] > asked["id"]]
    if not live:
        return None
    verdict, kind, given = live[-1]
    standing = any(u is verdict for u, _ in standing_pushes(ticket))
    if kind != "PUSH" or not standing or not alike(sha, given):
        return None
    return sha, given


def commit_landed(checkout, shas, base):
    """'landed' when origin/<base>, fetched first, contains the one commit every SHA in
    shas names here as an object id; 'unmerged' when it does not, or when any SHA names no
    single commit or they name different ones; 'stuck' when git cannot answer."""
    fetch = ["git", "fetch", "-q", "origin", f"refs/heads/{base}:refs/remotes/origin/{base}"]
    code, _output = run_on("local", str(checkout), fetch)
    if code != 0:
        return "stuck"
    code, out = run_on("local", str(checkout), ["sh", "-c", RESOLVE_COMMIT, "resolve", *shas])
    found = out.split() if code == 0 else []
    if len(found) != len(shas) or len(set(found)) != 1 or not FULL_SHA.fullmatch(found[0]):
        return "unmerged"
    commit = found[0]
    code, _output = run_on(
        "local",
        str(checkout),
        ["git", "merge-base", "--is-ancestor", commit, f"refs/remotes/origin/{base}"],
    )
    return {0: "landed", 1: "unmerged"}.get(code, "stuck")


def evidence_landed(checkout, feature_slug, rows):
    """'landed' only when feature/<slug> is absent or in the repo's base, and each done
    standalone ticket's own `Branch:` is in its pull request's base (`pull request into
    <base>`, as delivery reads it, else the Branch line's `from` base). That branch absent
    here and on origin needs the ticket's own reviewed commit in that base, since a branch
    name can be reused; a ticket naming no base, or with no PUSH, is kept."""
    repos = load_config()["repos"]
    landed = branch_landed(checkout, f"feature/{feature_slug}", repos)
    if landed not in ("landed", "absent"):
        return landed
    for ticket in rows:
        notes = ticket.get("notes") or ""
        named = branch_fields(notes)
        if not named or not NO_FEATURE_BRANCH.search(notes):
            continue
        into = PR_INTO.search(notes)
        branch, base = named[0][1], into[1] if into else named[0][2]
        if not base:
            return "unmerged"
        landed = branch_landed(checkout, branch, repos, base)
        if landed == "absent":
            shas = reviewed_sha(ticket)
            landed = commit_landed(checkout, shas, base) if shas else "unmerged"
        if landed != "landed":
            return landed
    return "landed"


def directory_open(path):
    """True when lsof names a file, False when it names none, None when it cannot run.

    macOS lsof exits 1 both when nothing is open and when files are open. Stderr, or
    any status other than 0 or 1, means the check did not run.
    """
    done = run(["lsof", "+D", str(path)], timeout=60)
    if done.returncode not in (0, 1) or done.stderr.strip():
        return None
    return bool(done.stdout.strip())


def folder_bytes(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not Path(dirpath, name).is_symlink()
        ]
        for name in filenames:
            file = Path(dirpath) / name
            if file.is_symlink():
                continue
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return total


def remove_feature_evidence(tickets, rows, checkout, feature, feature_slug):
    """Remove one evidence folder after every gate passes. Each failure keeps it."""
    place = evidence_place(checkout, feature_slug)
    if place == "missing" or place == "unsafe":
        return
    cited = evidence_cited(tickets, checkout, feature_slug)
    if cited is None:
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    if cited:
        return
    landed = evidence_landed(checkout, feature_slug, rows)
    if landed == "unmerged":
        return
    if landed != "landed":
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    opened = directory_open(place)
    if opened is None:
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    if opened:
        return
    if evidence_place(checkout, feature_slug) != place:
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    ignored = evidence_ignored(checkout, place)
    if ignored is None:
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    if not ignored:
        return
    size = folder_bytes(place)
    try:
        shutil.rmtree(place)
    except OSError:
        print(f"STUCK {feature}: evidence check failed. Nothing removed")
        return
    print(f"{feature}: removed {place} ({size} bytes)")


def clean_evidence(root, tickets):
    """Once every ticket that shares an evidence slug is done and its branch has
    landed, remove that folder from this ledger's main checkout. Titles that slug
    together share one folder. A later tick finds nothing to remove."""
    grouped = {}
    for feature, rows in feature_groups(tickets).items():
        grouped.setdefault(slug(feature), []).append((feature, rows))
    ready = []
    for feature_slug, parts in grouped.items():
        rows = [ticket for _feature, part in parts for ticket in part]
        if rows and all(ticket["status"] in DONE for ticket in rows):
            feature = min(feature for feature, _part in parts)
            ready.append((feature, feature_slug, rows))
    checkout = ledger_checkout(root) if ready else None
    if checkout is None:
        return
    for feature, feature_slug, rows in sorted(ready, key=lambda item: item[:2]):
        remove_feature_evidence(tickets, rows, checkout, feature, feature_slug)


# Features


def features_text(tickets, repo):
    """Per feature: its spec at <repo>/.work/specs/<feature slug>.md when there is one,
    done tickets, and each open ticket with the first sentence of its brief."""
    groups = {}
    for ticket in tickets.values():
        title = ticket["title"]
        feature = title.split(" / ", 1)[0] if " / " in title else "(no feature)"
        groups.setdefault(feature, []).append(ticket)
    lines = []

    def order(item):
        return (-sum(t["status"] not in DONE for t in item[1]), item[0])

    for feature, group in sorted(groups.items(), key=order):
        done = [t["task_id"] for t in group if t["status"] in DONE]
        open_ = [t for t in group if t["status"] not in DONE]
        lines.append(f"{feature}: {len(done)} done, {len(open_)} remaining")
        spec = repo / ".work" / "specs" / f"{slug(feature)}.md"
        if spec.is_file():
            lines.append(f"  spec: {spec}")
        if done:
            lines.append(f"  done: {', '.join(done)}")
        for ticket in open_:
            if ticket["status"] == "assigned":
                who = f"{ticket['assigned_agent_id']} (not accepted)"
            else:
                who = ticket["agent_id"] or "(unassigned)"
            blocker = waiting(ticket)
            wait = f" {{{blocker['content']}}}" if blocker else ""
            lines.append(f"  {ticket['task_id']} [{ticket['status']}] {who}{wait}")
            brief = re.split(
                r"(?<=[.!?])\s|\n", (ticket["notes"] or "").strip(), maxsplit=1
            )[0]
            if brief:
                lines.append(f"    {brief}")
    return "\n".join(lines)


# Commands


def skill_names(notes):
    """Names listed after `Skills:` in the ticket notes."""
    found = []
    for line in (notes or "").splitlines():
        if not line.strip().startswith("Skills:"):
            continue
        rest = line.split("Skills:", 1)[1]
        for part in rest.split(","):
            name = part.strip().strip(".")
            if name:
                found.append(name)
    return found


TIER = re.compile(r"(?:^|\s)Tier:\s*([\w-]+)", re.MULTILINE)
# ui and easy tickets share the medium cap.
CAPPED = {"hard": "hard", "medium": "medium", "ui": "medium", "easy": "medium"}


def tier(notes):
    """The cap a ticket's `Tier:` line counts against, "hard" or "medium"; None when the
    notes carry no Tier line, or one naming no known tier."""
    found = TIER.search(notes or "")
    return CAPPED.get(found.group(1).lower()) if found else None


REVIEWER = re.compile(r"(?:^|\s)(Reviewer:[^·\n]*)", re.MULTILINE)
AGENTS = ("claude", "codex", "cursor", "grok", "sdk")


def reviewers(notes):
    """(line, kinds): a coder ticket's `Reviewer:` line and the agent kinds it names as
    words before any "never"; a model id such as grok-4.7-xhigh names no kind.
    (None, set()) without one."""
    found = REVIEWER.search(notes or "")
    if not found:
        return None, set()
    line = found.group(1).strip()
    named = re.split(r"\bnever\b", line.lower(), maxsplit=1)[0]
    return line, {kind for kind in AGENTS if re.search(rf"(?<![\w-]){kind}(?![\w-])", named)}


def over_cap(cfg, workers, tickets, ticket):
    """(why, over) when this coder ticket may not launch, else (None, False). `over` is
    true when the reason is a full cap, which --over-cap passes, and false for a ticket
    without a Tier line. Coder tickets of every agent kind whose record holds_slot count;
    review tickets, finished tickets, records lost, stopped, held or exited, and tickets
    the orchestrator runs itself, which have no live worker, do not."""
    caps = {"hard": cfg["max_hard_tickets"], "medium": cfg["max_medium_tickets"]}
    tid = ticket["task_id"]
    if all(cap is None for cap in caps.values()) or tid.endswith("-review"):
        return None, False
    mine = tier(ticket.get("notes"))
    if mine is None:
        return f"{tid} has no Tier line naming hard, medium, ui or easy", False
    cap = caps[mine]
    if cap is None:
        return None, False
    counted = []
    for other, rec in workers.items():
        held = tickets.get((rec.get("project", ""), other))
        if (
            other == tid
            or not holds_slot(rec)
            or other.endswith("-review")
            or not held
            or held["status"] in DONE
        ):
            continue
        if tier(held.get("notes")) == mine:
            counted.append(other)
    if len(counted) < cap:
        return None, False
    return f"{mine} tickets {len(counted)} of {cap} live: {', '.join(sorted(counted))}", True


LINE = r"(?:^|\s){}:\s*([^·\n]*)"


def line_of(notes, name):
    """The text after a ticket's `<name>:` line, up to the next " · " or line end; None
    without one."""
    found = re.search(LINE.format(re.escape(name)), notes or "")
    return found.group(1).strip() if found else None


def kinds_in_order(text):
    """The agent kinds a Coder or Reviewer line names before any "never", in the order
    they appear."""
    named = re.split(r"\bnever\b", (text or "").lower(), maxsplit=1)[0]
    found = [
        (m.start(), kind)
        for kind in AGENTS
        if (m := re.search(rf"(?<![\w-]){kind}(?![\w-])", named))
    ]
    return [kind for _, kind in sorted(found)]


def named_route(text, kind):
    """A named kind's optional (model, effort), including a model without effort."""
    named = re.split(r"\bnever\b", text or "", maxsplit=1, flags=re.IGNORECASE)[0]
    route = re.search(
        rf"(?<![\w-]){kind}\s*\(\s*(?:(?P<model>[\w.-]+)\s*(?:,\s*)?)?"
        r"(?:effort\s+(?P<effort>[\w-]+)\s*)?(?=[;)])",
        named,
        re.IGNORECASE,
    )
    if route is None or route["model"] == "orchestrator":
        return None, None
    return route["model"], route["effort"]


def fill_route(models, kind, text, default):
    fallback = models.get(default)
    if fallback and fallback[0] != kind:
        fallback = models.get(kind)
    model, effort = named_route(text, kind)
    if fallback:
        model = model or fallback[1]
        effort = fallback[2] if effort is None else effort
    return (kind, model, effort) if model else None


def reviewer_none(notes):
    """True when a coder ticket's Reviewer line names no kind and says none: the
    orchestrator reviews it."""
    line, kinds = reviewers(notes)
    return bool(line) and not kinds and re.search(r"\bnone\b", line.lower()) is not None


def blockers_done(ticket, tickets):
    """True when every known ticket on its `Blocked by:` line is complete or closed, read
    as roadmap.classify reads it."""
    found = re.search(r"(?i)Blocked by:\s*([^\n]*)", ticket.get("notes") or "")
    part = re.split(r"(?i)Waiting on:", found.group(1))[0].strip() if found else ""
    if part.lower().startswith("none"):
        return True
    status = {t["task_id"]: t["status"] for t in tickets.values()}
    return all(status[w] in DONE for w in re.findall(r"[\w-]+", part) if w in status)


def host_allows(ticket, host):
    """Whether a ticket's Host line lets it run on `host`: "main Mac" names this Mac,
    another host is named by its name, and no Host line allows any host."""
    text = line_of(ticket.get("notes"), "Host")
    if text is None:
        return True
    name = "main Mac" if host == "local" else host
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", text, re.IGNORECASE) is not None


def fill_pick(cfg, st, ticket, host):
    """(agent, model, effort) for a ready ticket from its Tier and Coder lines and
    [fill.models], on the first kind with a free slot on `host`; None without one."""
    models = cfg["fill"]["models"]
    notes = ticket.get("notes") or ""
    line = line_of(notes, "Coder")
    found = TIER.search(notes)
    level = found.group(1).lower() if found else None
    if level not in TIERS:
        return None
    kinds = kinds_in_order(line)
    if not kinds and level in ("hard", "ui"):
        kinds = [models[level][0]]
    for kind in kinds:
        pick = fill_route(models, kind, line, level if level in ("hard", "ui") else kind)
        if pick is None:
            continue
        agent, model, _ = pick
        if agent in ("grok", "sdk") and host != "local":
            continue
        row = slot(cfg, st["workers"], agent, model, host, memory=False, limits=st["limits"])
        if row is not None:
            return pick
    return None


def live_tickets(st, tickets):
    """Base ticket id -> its ticket, for every record not done, not lost and on a ticket
    not complete or closed; a coder and its review count once."""
    live = {}
    for tid, rec in st["workers"].items():
        held = tickets.get((rec.get("project", ""), tid))
        if rec.get("done") or rec.get("lost") or not held or held["status"] in DONE:
            continue
        base = tid.removesuffix("-review")
        live.setdefault(base, (rec["host"], tickets.get((rec.get("project", ""), base)) or held))
    return live


def fill_branches(ticket):
    """(branch, base) of a ready ticket's new worktree: task/<feature>/<lane> from
    feature/<feature>, or with `Feature branch: none` the Branch line's `x` … from
    `base`; None when neither can be read."""
    feature, _, lane = ticket["title"].partition(" / ")
    notes = ticket.get("notes") or ""
    own = line_of(notes, "Feature branch")
    if own is not None and own.lower().startswith("none"):
        found = next((m for m in branch_fields(notes) if m.group(2)), None)
        return found.groups() if found else None
    if not lane:
        return None
    return f"task/{slug(feature)}/{slug(lane)}", f"feature/{slug(feature)}"


def fill_worktree(cfg, host, ticket):
    """A new worktree for a ready ticket, created by Orca: (path, None), or (None, why)."""
    repo = cfg["hosts"].get(host, {}).get("fill_repo") or cfg["fill"].get("repo")
    if not repo:
        return None, "no [fill] repo or fill_repo for the host"
    branches = fill_branches(ticket)
    if branches is None:
        return None, "no feature or Branch line to name its branch"
    branch, base = branches
    feature, _, lane = ticket["title"].partition(" / ")
    name = f"{slug(feature)}.{slug(lane)}" if lane else slug(ticket["task_id"])
    argv = [ORCA, *(["--environment", host] if host != "local" else [])]
    argv += ["worktree", "create", "--name", name, "--repo", repo, "--base-branch", base]
    argv += ["--branch", branch, "--no-parent", "--json"]
    done = run(argv)
    try:
        data = json.loads(done.stdout)
        path = data["result"]["worktree"]["path"]
    except (ValueError, KeyError, TypeError):
        return None, f"orca worktree create failed: {(done.stdout or done.stderr).strip()[:200]}"
    return path, None


def fill_launch(cfg, st, tid, agent, model, effort, worktree, host):
    """cmd_launch inside this tick's lock: None, or why it refused."""
    args = argparse.Namespace(
        ticket=tid,
        agent=agent,
        model=model,
        effort=effort,
        worktree=worktree,
        host=host,
        account=None,
        over_cap=False,
        auto_fill=True,
    )
    told = io.StringIO()
    try:
        with redirect_stdout(told):
            cmd_launch(args, locked=st, cfg=cfg)
    except SystemExit as stop:
        if stop.code not in (None, 0):
            lines = told.getvalue().strip().splitlines()
            return stop.code if isinstance(stop.code, str) else (lines or ["exit 1"])[-1]
    finally:
        print(told.getvalue(), end="")
    return None


class FillDeferred(Exception):
    """A fill candidate changed or temporarily lost capacity before it could start."""


def fill_ready(ticket):
    return bool(
        ticket and ticket["status"] == "proposed"
        and not (ticket.get("agent_id") or ticket.get("assigned_agent_id"))
    )


def require_fill_ready(root, project, tid):
    def read(con):
        row = con.execute(
            "SELECT status, agent_id, assigned_agent_id FROM tasks WHERE project = ? AND task_id = ?",
            (project, tid),
        ).fetchone()
        return row and dict(row)

    if not fill_ready(ledger_read(root, read)):
        raise FillDeferred(f"{tid}: its ticket is no longer proposed and unowned")


def ledger_busy(tid, error):
    """A per-ticket ledger read that failed: reported, and the ticket left to the next tick."""
    print(f"{tid}: auto-fill waits for the next tick, the ledger could not be read: {error}")


def fill_hosts(cfg):
    """host -> target_tickets for each host that sets one."""
    targets = {}
    if cfg["local_target_tickets"] is not None:
        targets["local"] = cfg["local_target_tickets"]
    for host, info in cfg["hosts"].items():
        if info.get("target_tickets") is not None:
            targets[host] = info["target_tickets"]
    return targets


def fill(cfg, st, tickets, now):
    """With [fill] enabled: per host below its target, one ready ticket started on a new
    worktree, and a reviewer started for each open review request whose review ticket
    is new or still pending from fill."""
    if not cfg["fill"]["enabled"]:
        return
    failed = st.setdefault("fill_failed", {})
    for host, target in fill_hosts(cfg).items():
        live = live_tickets(st, tickets)
        here = sum(where == host for where, _ in live.values())
        if here >= target:
            continue
        refusal, _ = memory_refusal(cfg, host)
        if refusal:
            continue
        taken = [ROADMAP.owned_paths(t.get("notes") or "") for _, t in live.values()]
        for key in sorted(tickets, key=lambda k: (k[0], k[1])):
            ticket = tickets[key]
            tid = ticket["task_id"]
            if (
                not fill_ready(ticket)
                or tid.endswith("-review")
                or tid in st["workers"]
                or tid in failed
                or not blockers_done(ticket, tickets)
            ):
                continue
            notes = ticket.get("notes") or ""
            host_line = line_of(notes, "Host") or ""
            coder = line_of(notes, "Coder") or ""
            if "operator" in host_line.lower() or "claude (orchestrator)" in coder.lower():
                continue
            if not host_allows(ticket, host):
                continue
            mine = ROADMAP.owned_paths(notes)
            if any(ROADMAP.overlaps(mine, other) for other in taken):
                continue
            if over_cap(cfg, st["workers"], tickets, ticket)[0]:
                continue
            pick = fill_pick(cfg, st, ticket, host)
            if pick is None:
                continue
            kept = st.setdefault("fill_worktrees", {}).get(tid)
            if kept and kept[0] != host:
                # Its worktree waits on the host that made it.
                continue
            worktree = None
            try:
                require_fill_ready(ledger(), ticket["project"], tid)
                if kept:
                    del st["fill_worktrees"][tid]
                    worktree, why = kept[1], None
                else:
                    worktree, why = fill_worktree(cfg, host, ticket)
                if worktree:
                    why = fill_launch(cfg, st, tid, *pick, worktree, host)
            except FillDeferred:
                if worktree:
                    st["fill_worktrees"][tid] = [host, worktree]
                continue
            except sqlite3.Error as error:
                if worktree:
                    # Nothing was started; the next tick launches into this worktree.
                    st["fill_worktrees"][tid] = [host, worktree]
                ledger_busy(tid, error)
                break
            if why:
                failed[tid] = why
                emit(st, f"{tid}|fill-failed", f"STUCK {tid}: auto-fill on {host} failed: {why}")
            else:
                text = f"FILLED {tid} on {host}: {' '.join(filter(None, pick))} in {worktree}"
                emit(st, f"{tid}|filled|{int(now)}", text)
            break
    fill_reviews(cfg, st, tickets, now, failed)


def fill_reviews(cfg, st, tickets, now, failed):
    """Start an eligible reviewer in the coder's checkout. Retry fill's own review
    ticket if host capacity or the ledger prevented its launch."""
    models = cfg["fill"]["models"]
    retry = st.setdefault("fill_retry", {})
    for tid, rec in list(st["workers"].items()):
        project = rec.get("project", "")
        ticket = tickets.get((project, tid))
        if tid.endswith("-review") or rec.get("done") or not ticket or ticket["status"] in DONE:
            continue
        rid = f"{tid}-review"
        review = tickets.get((project, rid))
        if review and (
            rid not in retry or rid in st["workers"]
            or (not fill_ready(review) and not (
                review["status"] == "active" and review["agent_id"] == SUPERVISOR
                and not review.get("assigned_agent_id")
            ))
        ):
            retry.pop(rid, None)
            continue
        found = open_request(ticket)
        if not found:
            continue
        posted, sha = found
        key = f"{tid}-review|{posted['id']}"
        if key in failed:
            continue
        host = rec["host"]
        line, _ = reviewers(ticket.get("notes"))
        pick = None
        for kind in kinds_in_order(line):
            route = fill_route(models, kind, line, "reviewer" if kind == "claude" else kind)
            if route is None:
                continue
            agent, model, _ = route
            if agent in ("grok", "sdk") and host != "local":
                continue
            if slot(cfg, st["workers"], agent, model, host, memory=False, limits=st["limits"]):
                pick = route
                break
        if pick is None or memory_refusal(cfg, host)[0]:
            continue
        why = None
        try:
            if review:
                why = release_review(ledger(), project, rid)
            else:
                cmd_review_ticket(argparse.Namespace(
                    ticket=tid, worktree=rec["worktree"], host=host, reviewer_kind=pick[0],
                ))
        except SystemExit as stop:
            why = str(stop.code) if stop.code not in (None, 0) else None
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            why = str(error)
        except sqlite3.Error as error:
            retry[rid] = int(now)
            ledger_busy(rid, error)
            continue
        if not why:
            try:
                why = fill_launch(cfg, st, rid, *pick, rec["worktree"], host)
            except FillDeferred:
                retry[rid] = int(now)
                continue
            except sqlite3.Error as error:
                retry[rid] = int(now)
                ledger_busy(rid, error)
                continue
        retry.pop(rid, None)
        if why:
            failed[key] = why
            emit(st, f"{key}|fill-failed", f"STUCK {rid}: auto-fill on {host} failed: {why}")
        else:
            text = f"FILLED {rid} on {host}: {' '.join(filter(None, pick))} for {sha}"
            emit(st, f"{rid}|filled|{posted['id']}", text)


def orchestrator_reviews(st, tickets):
    """One event per open review request on a coder ticket whose Reviewer line is none:
    the orchestrator reviews it itself."""
    for tid, rec in st["workers"].items():
        ticket = tickets.get((rec.get("project", ""), tid))
        if tid.endswith("-review") or not ticket or ticket["status"] in DONE:
            continue
        if not reviewer_none(ticket.get("notes")):
            continue
        found = open_request(ticket)
        if found:
            posted, sha = found
            emit(st, f"{tid}|orch-review|{posted['id']}", f"REVIEW {tid} {sha}: orchestrator review")


def ledger_selfcheck(cfg, host):
    """The command `ledger-init` prints. `-n` closes stdin, so concord-mcp cannot sit
    on a terminal and an exit 0 is the gate's self-check rather than an empty server."""
    info = cfg["hosts"][host]
    ssh = info["ledger_ssh"]
    check = f"concord-mcp script:{info['prefix']}-selfcheck"
    return [ssh[0], "-n", "-o", "StrictHostKeyChecking=accept-new", *ssh[1:], check]


def launch_preflight(cfg, ticket, host, worktree):
    """None when the worktree exists, a remote host's ledger self-check passes and each
    Skills: name has a SKILL.md in that host's global skills folder, the worktree's
    .agents/skills or its .claude/skills. Otherwise one STUCK line naming exactly what
    failed."""
    if host == "local":
        gone = not Path(worktree).is_dir()
    else:
        shown = run(
            [ORCA, "worktree", "show", "--worktree", f"path:{worktree}", "--json"]
            + ["--environment", host]
        )
        try:
            error = json.loads(shown.stdout).get("error") or {}
        except (ValueError, AttributeError):
            error = {}
        gone = error.get("code") == "selector_not_found"
    if gone:
        # Every check below runs inside the worktree: a gone one fails them all.
        return f"STUCK {ticket['task_id']}: worktree missing: {worktree} on {host}"
    gaps = []
    if host != "local":
        check = ledger_selfcheck(cfg, host)
        code, _output = run_on(host, worktree, check)
        if code:
            gaps.append(shlex.join(check))
    missing, safe = [], []
    for name in skill_names(ticket.get("notes")):
        if re.fullmatch(r"[A-Za-z0-9._-]+", name):
            safe.append(name)
        else:
            missing.append(name)
    folder = "~/.agents/skills"
    if safe:
        script = (
            'dir="${DEV_AUTO_SKILLS_DIR:-$HOME/.agents/skills}"; missing=; '
            'for name in "$@"; do '
            'for d in "$dir" .agents/skills .claude/skills; do '
            '[ -f "$d/$name/SKILL.md" ] && continue 2; '
            "done; "
            'missing="$missing $name"; '
            "done; "
            '[ -z "$missing" ] || { echo "$missing"; echo "$dir"; exit 1; }'
        )
        code, output = run_on(host, worktree, ["sh", "-c", script, "skills", *safe])
        if code:
            names, _, used = output.partition("\n")
            missing.extend(names.split() or safe)
            folder = used.strip() or folder
    if missing:
        tree = worktree.rstrip("/")
        places = f"{folder}, {tree}/.agents/skills, {tree}/.claude/skills"
        gaps.append(f"missing SKILL.md: {', '.join(missing)} (looked in {places})")
    if not gaps:
        return None
    return "STUCK " + ticket["task_id"] + ": " + "; ".join(gaps)


# Updates

DAY = 86400
# Seconds a launch waits for a host's Orca to report the new version after its restart.
ORCA_RESTART = 90
ORCA_RELEASES = "https://github.com/stablyai/orca/releases/latest/download"
NPM = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
VERSIONS = "__DA_V__"
# The WSL AppImage install, as the orchestrator did it by hand: download, check the sha512
# in latest-linux.yml, extract, swap, restart. The restart runs outside orca-serve.service
# so this probe returns first; KillMode=process keeps the host's terminals through it.
ORCA_APPIMAGE = "; ".join(
    [
        "exec </dev/null 2>&1",
        'd="$HOME/.local/opt/orca"',
        '[ -d "$d/squashfs-root" ] || { echo "no AppImage Orca in $d"; exit 1; }',
        "set -e",
        f"u={ORCA_RELEASES}",
        "s=$(curl -fsSL \"$u/latest-linux.yml\" | sed -n 's/^sha512: *//p')",
        '[ -n "$s" ] || { echo "latest-linux.yml has no sha512"; exit 1; }',
        't=$(mktemp -d "$d/new.XXXXXX")',
        "trap 'rm -rf \"$t\"' EXIT",
        'curl -fsSL -o "$t/orca.AppImage" "$u/orca-linux.AppImage"',
        'h=$(openssl dgst -sha512 -binary "$t/orca.AppImage" | openssl base64 -A)',
        '[ "$h" = "$s" ] || { echo "orca-linux.AppImage sha512 does not match latest-linux.yml"; exit 1; }',
        'chmod +x "$t/orca.AppImage"',
        '(cd "$t" && ./orca.AppImage --appimage-extract >/dev/null)',
        'rm -rf "$d/squashfs-root.old"',
        'mv "$d/squashfs-root" "$d/squashfs-root.old"',
        'mv "$t/squashfs-root" "$d/squashfs-root"',
        ("systemd-run --user --on-active=3 systemctl --user restart orca-serve.service >/dev/null"
        ' || { echo "systemd-run could not schedule the orca-serve restart"; exit 1; }'),
    ]
)


def update_script(agent):
    """The CLI's version before and after its update. An npm global updates through npm,
    with sudo -n when its prefix is not writable (WSL); any other install through the
    tool's own command, which keeps the running binary in place."""
    binary = {"cursor": "cursor-agent"}.get(agent, agent)
    command = f"{binary} update"
    package = NPM.get(agent)
    if package:
        install = f"npm install -g {package}@latest"
        command = (
            f"if npm ls -g --depth=0 {package} >/dev/null 2>&1; then "
            f'if [ -w "$(npm root -g)" ]; then {install}; else sudo -n {install}; fi; '
            f"else {command}; fi"
        )
    version = f"$({binary} --version 2>/dev/null | head -n 1)"
    return (
        f'exec </dev/null 2>&1; a="{version}"; {command}; c=$?; '
        f'echo "{VERSIONS}$a|{version}"; exit $c'
    )


def orca_running(host):
    """The version that host's Orca runtime runs, and how it installs updates."""
    runtime = (orca(host, "status") or {}).get("runtime") or {}
    support = runtime.get("remoteUpdateSupport") or {}
    return runtime.get("appVersion"), support.get("installMode")


def orca_latest():
    done = run(["curl", "-fsSL", f"{ORCA_RELEASES}/latest-linux.yml"], timeout=60)
    found = re.search(r"^version: *(\S+)", done.stdout, re.MULTILINE)
    return found.group(1) if found else None


def update_cli(host, worktree, tool):
    """(old, new) versions, or the error the update ended on."""
    script = update_script(tool)
    code, output = run_on(host, worktree, ["sh", "-c", script], timeout=300)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    said = [line for line in lines if not line.startswith(VERSIONS)]
    if code:
        return said[-1][:200] if said else f"exit {code}"
    marks = [line for line in lines if line.startswith(VERSIONS)] or [VERSIONS]
    before, _, after = marks[-1].removeprefix(VERSIONS).partition("|")
    return before, after


def update_orca(host, worktree):
    """(old, new) versions, the error, or None to try again later: the runtime restarts
    only with no agent on the host. Orca desktop installs its update when it restarts,
    which dev-auto never does, so a desktop behind the latest release is an error."""
    running, mode = orca_running(host)
    if not running:
        return "cannot read the running version"
    if mode != "interactive":
        # A busy host waits before the release is even fetched, so each start costs two
        # reads until the host is free and the day's check runs.
        terminals = listing(host)

        def gone(handle, row):
            # Orca keeps no start time; as in live_sessions, the last output stands in.
            out = row.get("lastOutputAt")
            screen = out is not None and read_screen(host, handle)
            return bool(screen) and exited({"launched_at": out / 1000}, row, screen, time.time())

        if terminals is None or not all(gone(h, r) for h, r in terminals.items()):
            return None
    latest = orca_latest()
    if not latest:
        return "cannot read the latest release"
    if running == latest:
        return running, latest
    if mode == "interactive":
        return f"Orca desktop runs {running}, latest is {latest}; it installs that on restart"
    code, output = run_on(host, worktree, ["sh", "-c", ORCA_APPIMAGE], timeout=900)
    said = [line.strip() for line in output.splitlines() if line.strip()]
    if code:
        return said[-1][:200] if said else f"exit {code}"
    deadline = time.time() + ORCA_RESTART
    time.sleep(min(5, ORCA_RESTART))
    now_running = orca_running(host)[0]
    while now_running != latest and time.time() < deadline:
        time.sleep(2)
        now_running = orca_running(host)[0]
    if now_running != latest:
        return f"runs {now_running or 'nothing'} after the restart, latest is {latest}"
    return running, latest


def fresh_tools(st, ticket, host, worktree, agent, now):
    """Update the agent's CLI and the host's Orca, each at most once a day per host. A
    change is noted on the ticket; a failure is one STUCK there and never stops the start."""
    checked = st.setdefault("updates", {})
    # The Agent SDK runs the CLI bundled in its package.
    for tool in ("orca",) if agent == "sdk" else (agent, "orca"):
        key = f"{host}|{tool}"
        if now - checked.get(key, 0) < DAY:
            continue
        if tool == "orca":
            outcome = update_orca(host, worktree)
        else:
            outcome = update_cli(host, worktree, tool)
        if outcome is None:
            continue
        checked[key] = now
        st.save()
        if isinstance(outcome, str):
            text = f"STUCK {ticket}: {tool} update on {host} failed: {outcome}"
            print(text)
            note(ticket, text)
        elif outcome[0] != outcome[1]:
            old, new = outcome[0] or "unknown", outcome[1]
            note(ticket, f"updated {tool} on {host}: {old} -> {new}")


def cmd_slot(args):
    why = []
    with state(ledger()) as st:
        row = slot(
            load_config(),
            st["workers"],
            args.agent,
            args.model,
            args.host,
            limits=st["limits"],
            account=args.account,
            why=why,
        )
    if row is None:
        print("no capacity")
    else:
        print(f"{row['id']}\t{row['label']}\t{row['home']}")
    for reason in why:
        print(reason, file=sys.stderr)
    if row is None:
        sys.exit(1)


def ledger_owner(root, project, task):
    """(owner, version, places) of a ticket, read from the store: inspect_work answers
    with a text summary. places are the worktree and cwd the owner registered with."""

    def read(con):
        owner, version = con.execute(
            "SELECT agent_id, version FROM tasks WHERE project = ? AND task_id = ?",
            (project, task),
        ).fetchone() or (None, None)
        where = con.execute(
            "SELECT worktree, cwd FROM agents WHERE agent_id = ?", (owner,)
        ).fetchone()
        return owner, version, {p for p in where or () if p}

    return ledger_read(root, read)


def live_sessions(st, tid, kind, places):
    """Terminals at these (host, worktree) places that may hold an agent of this kind:
    listed as one, or listed as none without proof that none runs. Those another record
    or the orchestrator holds are left out. None names a host whose list failed."""
    found = []
    pointer = st["orchestrator"]
    for host in sorted({h for h, _ in places}):
        terminals = listing(host)
        if terminals is None:
            return None, host
        held = {r.get("terminal") for t, r in st["workers"].items() if t != tid and r["host"] == host}
        if pointer and pointer["host"] == host:
            held.add(pointer["terminal"])
        for handle, row in terminals.items():
            here = any(same_path(row.get("worktreePath"), p, host) for h, p in places if h == host)
            if handle in held or not here:
                continue
            ident = row.get("agentIdentity")
            # Orca keeps no start time. A launch echoes its command, so the last output is no
            # earlier than the launch; with none listed, nothing proves the agent gone.
            out = row.get("lastOutputAt")
            gone = (
                not ident
                and out is not None
                and exited({"launched_at": out / 1000}, row, read_screen(host, handle), time.time())
            )
            if ident == kind or not (ident or gone):
                found.append(handle)
    return found, None


def review_placement(ticket, coder):
    if coder:
        return coder["host"], coder["worktree"]
    published = re.search(
        r"^You are the duo reviewer: .* in the coder's worktree on (.+?) \((.+)\)\.$",
        ticket.get("notes") or "",
        re.MULTILINE,
    )
    if published:
        host, worktree = published.groups()
        return ("local" if host == "this Mac" else host), worktree
    return None, None


def check_review_placement(ticket, coder_id, coder, host, worktree):
    placed, expected = review_placement(ticket, coder)
    rid = ticket["task_id"]
    if placed and placed != host:
        sys.exit(
            f"{rid} refused: its coder {coder_id} runs on {placed}, not "
            f"{host}; a coder and its reviewer run on the same host"
        )
    if expected:
        if host == "local":
            expected = str(Path(expected).expanduser())
        if not same_path(expected, worktree, host):
            sys.exit(f"{rid} refused: its coder {coder_id}'s worktree is {expected} on {host}")


def launch_route(args, ticket, review_line):
    """Honor explicit route fields unless this is an operator override or restart proposal."""
    review = args.ticket.endswith("-review")
    field = "Reviewer" if review else "Coder"
    line = review_line if review else line_of(ticket.get("notes"), field)
    kinds = kinds_in_order(line)
    kind = args.agent
    if kind == "sdk" and not review and "claude" in kinds and "sdk" not in kinds:
        kind = "claude"
    model, effort = named_route(line, kind)
    override = getattr(args, "override_route", False) or getattr(args, "proposal", False)
    if not override:
        if kinds and kind not in kinds:
            sys.exit(f"{args.ticket} refused: its {field} line does not name {args.agent}: {line}")
        for key, named in (("model", model), ("effort", effort)):
            supplied = getattr(args, key)
            if named and supplied and named != supplied:
                sys.exit(f"{args.ticket} refused: its {field} line names {key} {named}, not {supplied}")
    args.model = args.model or model
    args.effort = args.effort or effort
    if not args.model:
        sys.exit(f"{args.ticket}: supply --model when its {field} line names none")


def cmd_launch(args, locked=None, cfg=None):
    """`locked`: the state of a tick that already holds the lock, and its `cfg`."""
    cfg, root = cfg or load_config(), ledger()
    if args.agent == "grok" and args.host != "local":
        # shortcut: Grok's concord server is configured on this Mac only; register one on a host to run Grok there.
        sys.exit("grok workers run on this Mac only")
    if args.agent == "sdk" and args.host != "local":
        # The ssh gate starts no sdk kind, and the spend file sits beside the ledger.
        sys.exit("sdk workers run on the ledger's host only")
    if not SAFE.fullmatch(args.ticket):
        sys.exit(f"ticket id {args.ticket!r} has characters dev-auto will not type")
    tickets = read_tickets(root)
    found = [t for (_, task_id), t in tickets.items() if task_id == args.ticket]
    if not found:
        sys.exit(f"ticket {args.ticket} is not in the ledger")
    if len(found) > 1:
        projects = ", ".join(repr(t["project"]) for t in found)
        sys.exit(f"ticket {args.ticket} is in several projects: {projects}")
    (ticket,) = found
    auto_fill = getattr(args, "auto_fill", False)
    if auto_fill and not fill_ready(ticket):
        raise FillDeferred(f"{args.ticket}: its ticket is no longer proposed and unowned")
    if getattr(args, "proposal", False) and ticket["status"] in DONE:
        sys.exit(f"{args.ticket} is {ticket['status']}; its restart proposal launches nothing")
    coder, line = None, None
    if args.ticket.endswith("-review"):
        coder = args.ticket.removesuffix("-review")
        held = tickets.get((ticket["project"], coder))
        line, kinds = reviewers(held and held.get("notes"))
        if not line:
            sys.exit(f"{args.ticket} refused: {coder} has no Reviewer line")
        if args.agent not in kinds:
            named = " or ".join(sorted(kinds)) or "no agent kind"
            sys.exit(f"{args.ticket} refused: {coder}'s Reviewer line names {named}, not {args.agent}: {line}")
    launch_route(args, ticket, line)
    if args.host != "local" and args.host not in cfg["hosts"]:
        sys.exit(f"host {args.host} has no [hosts.{args.host}] entry in accounts.toml")
    refusal, memory_notes = memory_refusal(cfg, args.host)
    if refusal:
        if auto_fill and coder:
            raise FillDeferred(refusal)
        sys.exit(refusal)
    for line in memory_notes:
        print(line)
    worktree = args.worktree
    if args.host == "local":
        worktree = str(Path(worktree).expanduser().resolve())
    with nullcontext(locked) if locked is not None else state(root) as st:
        if coder:
            current = st["workers"].get(coder)
            if current and current.get("project", "") != ticket["project"]:
                current = None
            check_review_placement(ticket, coder, current, args.host, worktree)
        old = st["workers"].get(args.ticket)
        if auto_fill and old:
            raise FillDeferred(f"{args.ticket}: its worker is already recorded")
        shell = None
        if old:
            terminals = listing(old["host"])
            if terminals is None:
                sys.exit(f"{old['host']}: terminal list failed; {args.ticket} is kept")
            shell = own_row(old, terminals)
            unheld = unproved_terminal(st, args.ticket, old, terminals)
            if unheld:
                sys.exit(no_terminal_refusal(args.ticket, unheld))
            other = None if shell else possible_session(st, args.ticket, old, terminals, time.time())
            live = shell or other
            if live and live.get("agentIdentity"):
                sys.exit(f"{args.ticket} already runs in {live['handle']}")
            if other:
                sys.exit(
                    f"{args.ticket}: {other['handle']} in its worktree may still hold its agent; "
                    "close it first to replace the worker"
                )
            screen = shell and read_screen(old["host"], shell["handle"])
            if shell and not exited(old, shell, screen, time.time()):
                sys.exit(
                    f"{args.ticket}: {shell['handle']} may still hold its agent; "
                    "close it first to replace the worker"
                )
        if (not old or old["host"] != args.host) and listing(args.host) is None:
            # Nothing is saved yet: a host that cannot list its terminals is never asked to
            # create one, so a failed create is never the only evidence of what Orca did.
            sys.exit(f"{args.host}: terminal list failed; {args.ticket} is not launched")
        newest = (u["id"] for t in tickets.values() for u in t["updates"])
        slug = lane_slug(ticket)
        # A replaced worker of the same kind and host keeps its id, so the owner stays.
        same = old and (old["agent"], old["host"]) == (args.agent, args.host)
        rec = {
            "typed_texts": [],
            "ticket": args.ticket,
            "agent": args.agent,
            "model": args.model,
            "effort": args.effort,
            "worktree": worktree,
            "host": args.host,
            "project": ticket["project"],
            "agent_id": old["agent_id"]
            if same
            else ticket_agent_id(cfg, args.agent, args.host, args.ticket),
            "title": f"{slug} {args.agent}",
            # Updates up to here were seen before this ticket's first launch.
            "since": old.get("since", 0) if old else max(newest, default=0),
        }
        capped, over = over_cap(cfg, st["workers"], tickets, ticket)
        if capped and not (over and args.over_cap):
            hint = "; --over-cap launches it on the operator's word" if over else ""
            sys.exit(f"{args.ticket} refused: {capped}{hint}")
        why = []
        row = slot(
            cfg,
            st["workers"],
            args.agent,
            args.model,
            args.host,
            memory=False,
            ignore=args.ticket,
            limits=st["limits"],
            account=args.account,
            why=why,
        )
        if row is None:
            if auto_fill and coder:
                raise FillDeferred(f"{args.ticket}: no reviewer capacity")
            print("no capacity")
            for reason in why:
                print(reason, file=sys.stderr)
            sys.exit(1)
        stuck = launch_preflight(cfg, ticket, args.host, worktree)
        if stuck:
            print(stuck)
            note(args.ticket, stuck)
            sys.exit(1)
        owner, version, where = None, None, set()
        legacy_slug = id_slug(ticket)
        if ticket["status"] not in DONE:
            try:
                owner, version, where = ledger_owner(root, ticket["project"], args.ticket)
            except sqlite3.Error as error:
                if locked is not None:
                    raise  # auto-fill leaves the ticket to the next tick
                sys.exit(f"{args.ticket}: its owner in the ledger could not be read: {error}")
            except OSError as error:
                sys.exit(f"{args.ticket}: its owner in the ledger could not be read: {error}")
            if auto_fill and owner:
                raise FillDeferred(f"{args.ticket}: its ticket now has an owner")
            if not old and owner == agent_id(cfg, args.agent, args.host, legacy_slug):
                # The ledger still holds this worker's claim after its record was dropped.
                rec["agent_id"] = owner
        for other, held in st["workers"].items():
            if other != args.ticket and held["agent_id"] == rec["agent_id"]:
                sys.exit(f"agent id {rec['agent_id']} is already ticket {other}'s worker")
        if owner and owner != rec["agent_id"]:
            # Release only this ticket's own previous worker, including legacy IDs.
            own = {
                (known, host)
                for kind in AGENTS
                for host in ("local", *cfg["hosts"])
                for known in (
                    ticket_agent_id(cfg, kind, host, args.ticket),
                    agent_id(cfg, kind, host, legacy_slug),
                )
            }
            if old:
                own.add((old["agent_id"], old["host"]))
            hosts = {host for known, host in own if known == owner}
            if hosts:
                # Its session may run where it registered, here, or in the stored record's place.
                places = {(args.host, worktree)} | {(h, p) for h in hosts for p in where}
                if old:
                    places.add((old["host"], old["worktree"]))
                refusal = f"STUCK {args.ticket} {rec['agent_id']}: the earlier owner {owner}"
                kept = None
                if where or (old and owner == old["agent_id"]):
                    live, down = live_sessions(st, args.ticket, owner.split(":")[0], places)
                    if down:
                        kept = f"could not be checked: {down}'s terminal list failed"
                    elif live:
                        kept = f"may still run in {', '.join(live)}"
                else:
                    kept = "has no worktree in the ledger"
                if kept:
                    refusal += f" {kept}; its claim is kept"
                else:
                    release = {
                        "task_id": args.ticket,
                        "action": "release",
                        # At the version read now, so a claim taken since is never undone.
                        "expected_version": version,
                        "reason": f"relaunched on {args.host} as {rec['agent_id']}",
                    }
                    try:
                        concord_call("transfer_work", release, agent=owner)
                        refusal = None
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        refusal += f"'s claim was not released: {error}"
                if refusal:
                    note(args.ticket, refusal)
                    sys.exit(refusal)
        if shell:
            # Its agent provably exited to this shell, which dev-auto opened.
            close_terminal(old["host"], shell["handle"])
        branch = worktree_branch(args.host, worktree)
        if branch is not None:
            # A detached HEAD is no branch, whatever a brief names.
            rec["owns_worktree"] = branch != "HEAD" and branch in own_branches(ticket, tickets)
            if rec["owns_worktree"]:
                # Cleanup removes the worktree and branch only while this branch of this
                # repository is out.
                rec["owned_branch"] = branch
                rec["owned_common"] = worktree_common(args.host, worktree)
        else:
            # Unread, it is the same folder's recorded answer, else not owned: never removed.
            same_tree = (
                old
                and old["host"] == args.host
                and same_path(old["worktree"], worktree, args.host)
            )
            rec["owns_worktree"] = bool(same_tree and old.get("owns_worktree"))
            if rec["owns_worktree"] and old.get("owned_branch"):
                rec["owned_branch"] = old["owned_branch"]
                rec["owned_common"] = old.get("owned_common")
        here = [
            r
            for r in st["workers"].values()
            if r["host"] == args.host and same_path(r["worktree"], worktree, args.host)
        ]
        if here:
            # Not its first launch: the shells seen then stay with the record that saw them.
            rec["shells"] = old.get("shells", []) if old in here else []
        else:
            rec["shells"] = shells_in(args.host, worktree, time.time())
        if auto_fill:
            require_fill_ready(root, ticket["project"], args.ticket)
        st["workers"][args.ticket] = rec
        if rec["owns_worktree"] and not auto_fill:
            remember_worktree(st, rec, time.time())
        try:
            started = start(
                cfg, root, st, rec, row, brief(rec, ticket), time.time(), auto_fill=auto_fill,
            )
        except (FillDeferred, sqlite3.Error):
            if auto_fill:
                del st["workers"][args.ticket]
                st.save()
            raise
        if started is None:
            sys.exit(1)
        if not started:
            if old:
                st["workers"][args.ticket] = old
            else:
                del st["workers"][args.ticket]
            sys.exit(1)
    if capped:
        note(args.ticket, f"launched over the cap on the operator's word: {capped}")
    print(f"{args.ticket}: {rec['agent_id']} on {row['label']} in {rec['terminal']}")


def usage_reset(st, account_id, now):
    """The operator's word that an account's usage was reset: its readings until now no
    longer count, and until it is seen_full again every wait on it ends on a tick."""
    st["usage_resets"][account_id] = now
    st["limits"].pop(account_id, None)


def cmd_usage_reset(args):
    cfg = load_config()
    row = next((r for r in cfg["account"] if args.account in (r["id"], r["label"])), None)
    if row is None:
        sys.exit(f"no account {args.account} in accounts.toml")
    with state(ledger()) as st:
        usage_reset(st, row["id"], time.time())
    print(f"{row['label']}: usage counts as reset until a reading or limit screen newer than now shows it full")


def cmd_keep(args):
    cfg = load_config()
    with state(ledger()) as st:
        rec = st["workers"].get(args.ticket)
        if not rec or rec["agent"] != "claude" or missing(rec):
            sys.exit(f"{args.ticket}: no Claude worker record to keep")
        st.setdefault("keep", {})[rec["account"]] = args.ticket
    row = account_row(cfg, rec["account"])
    label = row["label"] if row else rec["account"]
    print(f"{args.ticket}: kept running on {label} when it reaches handoff_percent")


def cmd_proposals(args):
    """List restart proposals, accept some through launch, or drop some."""
    cfg, root = load_config(), ledger()
    with state(root) as st:
        props = st.setdefault("proposals", {})
        if args.action == "list":
            print(proposals_text(st), end="")
            return
        chosen = sorted(props) if args.all and args.action == "accept" else args.tickets
        if not chosen:
            sys.exit(f"proposals {args.action}: name a ticket{' or --all' if args.action == 'accept' else ''}")
        unknown = [tid for tid in chosen if tid not in props]
        if unknown:
            sys.exit(f"no restart proposal for {', '.join(unknown)}")
        if args.action == "drop":
            for tid in chosen:
                end_proposal(st, tid, "dropped", "by the orchestrator")
                print(f"{tid}: proposal dropped")
            return
        # Selected, checked, launched and ended under this one lock, so no drop, tick or
        # newer proposal for the ticket acts in between.
        failed = []
        for tid in dict.fromkeys(chosen):
            p = props[tid]
            rec = st["workers"].get(tid)
            launch = dict(p["launch"])
            for key in ("account", "model", "effort", "host"):
                if getattr(args, key):
                    launch[key] = getattr(args, key)
            how = "accepted" if launch == p["launch"] else "changed"
            ticket = read_tickets(root).get((p.get("project", ""), tid))
            row = next(
                (
                    r
                    for r in cfg["account"]
                    if r["agent"] == "claude"
                    and launch["account"] in (r["id"], r["label"])
                    and r["host"] == launch["host"]
                ),
                None,
            )
            if ticket is None or ticket["status"] in DONE:
                why = "its ticket is done or gone"
            elif not rec or not rec.get("stopped"):
                why = "its worker is no longer the stopped one this proposal restarts"
            elif row is None:
                why = f"no Claude account {launch['account']} on {launch['host']}"
            else:
                why = launch_refusal(cfg, st, tid, launch["model"], row, time.time())
            if why:
                print(f"{tid}: not launched: {why}")
                failed.append(tid)
                continue
            detail = f"launched {launch['model']} on {launch['account']} {launch['host']}"
            # Saved with the launch's record: a crash after it logs this end, not a drop.
            p["accepting"] = {"how": how, "detail": detail}
            try:
                cmd_launch(argparse.Namespace(ticket=tid, over_cap=False, proposal=True, **launch), locked=st, cfg=cfg)
            except SystemExit as error:
                print(f"{tid}: not launched: {error.code}")
                failed.append(tid)
                p.pop("accepting", None)
                continue
            end_proposal(st, tid, how, detail)
    if failed:
        sys.exit(1)


def cmd_tick(args):
    cfg, root = load_config(), ledger()
    with state(root) as st:
        try:
            tickets = read_tickets(root)
        except sqlite3.Error as error:
            sys.exit(f"tick skipped: the ledger {root} could not be read ({error}); every worker is left to the next tick")
        tick(cfg, root, st, tickets, time.time())


def cmd_event_done(args):
    """Remove the named lines from events.md, read again under the state lock, so a
    line a tick appended since the orchestrator read the file stays."""
    wanted = list(dict.fromkeys(l if l.startswith("- ") else f"- {l}" for l in (x.strip() for x in args.lines)))
    with state(ledger()) as st:
        path = events_file()
        try:
            lines = path.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            lines = []
        missing = [l for l in wanted if l not in lines]
        if missing:
            sys.exit(f"not in {path}, nothing removed: {'; '.join(missing)}")
        for line in wanted:
            lines.remove(line)
        # The outbox entries are marked first: a kill before the file is replaced leaves
        # the lines to remove again, never an acknowledged one for the next tick to append.
        for entry in st.get("outbox", {}).values():
            if entry["line"] in wanted:
                entry["acked"] = True
        st.save()
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(f"{l}\n" for l in lines))
        tmp.replace(path)
    print(f"{len(wanted)} removed from {path}")


def cmd_set_orchestrator(args):
    with state(ledger()) as st:
        st["orchestrator"] = {
            "terminal": args.terminal,
            "host": args.host,
            "account": args.account,
            "session": args.session,
        }
        if args.new_cycle:
            st["relay"] = {"used": False}
            st["stopped"] = False
        relay = "used" if st["relay"]["used"] else "unused"
    print(f"orchestrator {args.terminal} on {args.host}; relay {relay}")


def cmd_features(args):
    repo = Path(args.repo).expanduser().resolve()
    print(features_text(read_tickets(ledger()), repo))


def cmd_ledger_init(args):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.repo_name) or args.repo_name in (
        ".",
        "..",
    ):
        sys.exit("REPO_NAME must be a plain folder name")
    checkout = None
    if args.checkout:
        given = os.path.realpath(Path(args.checkout).expanduser())
        checkout = checkout_of(given)
        if checkout is None or os.path.realpath(checkout) != given:
            sys.exit("--checkout must be the repo's main checkout, with a .work folder")
        roots, url = root_commits(checkout), origin_url(checkout)
        if not roots or not url:
            sys.exit("--checkout must have a commit and an origin: they name the repo")
    cfg = load_config()
    prefixes = set()
    for name, host in cfg["hosts"].items():
        if not host.get("address"):
            continue
        prefix = host.get("prefix")
        if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,46}", prefix):
            sys.exit(
                f"[hosts.{name}] invalid prefix: use 1 to 47 lowercase letters, digits, "
                "dots, underscores or hyphens, starting with a letter or digit"
            )
        if any(
            (prefix + "-").startswith(other + "-") or (other + "-").startswith(prefix + "-")
            for other in prefixes
        ):
            sys.exit(f"[hosts.{name}] overlapping prefix: {prefix}")
        prefixes.add(prefix)
    root = Path.home() / ".concord-ledger" / args.repo_name
    (root / "supervisor").mkdir(parents=True, exist_ok=True)
    if checkout is not None:
        (root / "supervisor" / "checkout").write_text(f"{given}\n{min(roots)}\n{url}\n")
        print(f"evidence is cleaned in {given}")
    env = {**os.environ, **QUIET, "CONCORD_REPO_ROOT": str(root)}
    done = run(
        ["concord", "setup", "--no-global", "--no-mcp", "--no-adapters"],
        cwd=root,
        env=env,
    )
    if done.returncode:
        sys.exit(f"concord setup failed:\n{done.stdout}{done.stderr}")
    gate = root / "concord-ssh-gate.sh"
    shutil.copyfile(HERE / "concord-ssh-gate.sh", gate)
    gate.chmod(0o755)
    print(f"export CONCORD_REPO_ROOT={shlex.quote(str(root))}")
    for name, host in cfg["hosts"].items():
        if not host.get("address"):
            print(f"[hosts.{name}] has no address: add it, then run ledger-init again")
            continue
        command = shlex.join([str(gate), host["prefix"]])
        command = command.replace("\\", "\\\\").replace('"', '\\"')
        entry = f'from="{host["address"]}",restrict,command="{command}"'
        print(f"{name}: authorized_keys entry for its ledger key: {entry} <public key>")
        once = ledger_selfcheck(cfg, name)
        print(
            f"{name}: run once there to trust this Mac's host key: {shlex.join(once)}"
        )


REVIEW_NOTES = """Outcome: the coder of {tid} ({lane}) gets a REVIEW VERDICT on each local SHA it asks you to clear, so only cleared work leaves its worktree.
Coder's ticket: {tid}. Read its notes and every update, including the orchestrator's notes; its outcome, checks, do-not-touch list and acceptance are what you review against.
You are the duo reviewer: {who} in the coder's worktree on {where} ({worktree}).
Check independently: every acceptance item on {tid} against the exact SHA; only its owned paths changed, or those the orchestrator granted on the ticket; no existing assertion, coverage floor or time limit weakened. Re-run the checks its Checks line names yourself, serially when the 1-minute load is under 6. Record `REVIEW VERDICT: PUSH <sha>` or `REVIEW VERDICT: FIX <sha>` with numbered findings on {tid}.
Do not touch: you edit, commit and push nothing; never type into the live tick loop, the ledger, supervisor/state.json or another worker; never sign in, never open the live profile or the operator's store; no headed browser.
Stop when: a PUSH verdict for the coder's final SHA is recorded on {tid} and the coder's ticket is complete.
Skills: {skills}
Coder: {who} · Reviewer: none · Tier: {level}
Blocked by: none"""


def cmd_review_ticket(args):
    root = ledger()
    tickets = read_tickets(root)
    tid, rid = args.ticket, f"{args.ticket}-review"
    found = [t for (_, task_id), t in tickets.items() if task_id == tid]
    if not found or tid.endswith("-review"):
        sys.exit(f"{tid} is not a coder ticket in the ledger")
    if len(found) > 1:
        projects = ", ".join(repr(t["project"]) for t in found)
        sys.exit(f"ticket {tid} is in several projects: {projects}")
    (coder,) = found
    if any(task_id == rid for _, task_id in tickets):
        sys.exit(f"{rid} is already in the ledger")
    notes = coder.get("notes") or ""
    line, kinds = reviewers(notes)
    if not kinds:
        sys.exit(f"{tid} has no Reviewer line naming an agent kind")
    found = TIER.search(notes)
    level = found.group(1).lower() if found else "medium"
    extra = {"hard": ", interrogate", "ui": ", ui-review"}.get(level, "")
    title = f"{coder['title']}-review"
    text = REVIEW_NOTES.format(
        tid=tid,
        lane=coder["title"].split(" / ", 1)[-1],
        who=line.removeprefix("Reviewer:").strip(),
        where="this Mac" if args.host == "local" else args.host,
        worktree=args.worktree,
        skills=f"duo, review{extra}",
        level=level,
    )
    kind = getattr(args, "reviewer_kind", None) or kinds_in_order(line)[0]
    if kind not in kinds:
        sys.exit(f"{tid}'s Reviewer line no longer names {kind}")
    claim = {"task_id": rid, "title": title, "kind": kind, "notes": text, "summary": title}
    concord_call("start_work", claim)
    why = release_review(root, coder["project"], rid, started=True)
    if why:
        sys.exit(why)
    print(f"{rid}: published unowned, {title}")


def release_review(root, project, rid, started=False):
    """Release the review ticket dev-auto started, at the store's version: start_work
    answers with text. None once it is unowned; else why not. One not just started may
    already be unowned."""
    owner, version, _ = ledger_owner(root, project, rid)
    if owner is None and not started:
        return None
    if owner != SUPERVISOR:
        return f"{rid}: started, but the store shows owner {owner}; release it by hand"
    concord_call("transfer_work", {"task_id": rid, "action": "release", "expected_version": version})
    return None


def cmd_resume(args):
    """Resume a Claude record's own session on its account and host, closing its terminal
    first when listed: after a host restart lost it, or to restart its Claude Code. A
    record stopped for a restart proposal is refused, and so, before anything is closed,
    is an account or host slot would not give this ticket now."""
    cfg, root = load_config(), ledger()
    def pinned(rec):
        return rec and {key: rec.get(key) for key in ("host", "account", "terminal", "launched_at")}

    with state(root) as st:
        before = pinned(st["workers"].get(args.ticket))
    # Read before the lock, as launch does: a refused host is read again a minute later.
    memory = memory_refusal(cfg, before["host"])[0] if before else None
    with state(root) as st:
        rec = st["workers"].get(args.ticket)
        if not rec:
            sys.exit(f"{args.ticket} has no record")
        if pinned(rec) != before:
            sys.exit(f"{args.ticket}: its record changed while its host was read; run resume again")
        if rec["agent"] != "claude" or not rec.get("session"):
            sys.exit(f"{args.ticket}: only a Claude record with a session resumes; use launch")
        row = account(cfg, rec.get("account"), rec["host"])
        if row is None:
            sys.exit(f"{args.ticket}: no account row for {rec.get('account')} on {rec['host']}")
        if rec.get("stopped"):
            sys.exit(f"{args.ticket}: stopped for a restart proposal; accept that proposal instead")
        if defer_stale_claude(
            st, rec, f"{args.ticket}|resume|{rec['session']}", "Resume the Claude worker."
        ):
            sys.exit(f"{args.ticket}: Claude input age requires a fresh session")
        refused = [memory] if memory else []
        picks = {"ignore": args.ticket, "limits": st["limits"], "account": row["id"], "why": refused}
        if memory or slot(cfg, st["workers"], "claude", rec["model"], rec["host"], memory=False, **picks) is None:
            given = "; ".join(refused) or f"{row['label']} is disabled"
            sys.exit(f"{args.ticket}: {row['label']} on {rec['host']} takes no worker now ({given}); {args.ticket} is kept")
        terminals = listing(rec["host"])
        if terminals is None:
            sys.exit(f"{rec['host']}: terminal list failed; {args.ticket} is kept")
        shell = own_row(rec, terminals)
        unheld = unproved_terminal(st, args.ticket, rec, terminals)
        if unheld:
            sys.exit(no_terminal_refusal(args.ticket, unheld))
        other = None if shell else possible_session(st, args.ticket, rec, terminals, time.time())
        if other:
            sys.exit(
                f"{args.ticket}: {other['handle']} in its worktree may still hold its session; "
                "close it first to resume it"
            )
        if shell:
            screen = read_screen(rec["host"], shell["handle"])
            if screen is None or in_turn(screen["lines"]):
                sys.exit(f"{args.ticket}: {shell['handle']} is unread or mid-turn; resume it at its prompt")
            closed = close_terminal(rec["host"], shell["handle"])
            after = listing(rec["host"])
            if closed is None or after is None or own_row(rec, after):
                sys.exit(f"{args.ticket}: {shell['handle']} may still be open; {args.ticket} is kept")
        why = "was closed to restart it" if shell else "is gone"
        text = (
            f"dev-auto: this session's terminal {why}; this resumes the same session, "
            f"nothing else changed. Continue ticket {args.ticket}."
        )
        text = armed(rec, goal_line(args.ticket), text)
        if not start(cfg, root, st, rec, row, text, time.time(), resume=rec["session"]):
            sys.exit(1)
        rec.pop("lost", None)
    print(f"{args.ticket}: resumed on {row['label']} {rec['host']} in {rec['terminal']}")


def cmd_run(args):
    code, out = run_on(args.host, args.worktree, ["sh", "-c", args.command], timeout=args.timeout)
    print(out, end="" if out.endswith("\n") or not out else "\n")
    sys.exit(code)


def cmd_screen(args):
    with state(ledger()) as st:
        rec = st["workers"].get(args.ticket)
    if not rec or not rec.get("terminal"):
        sys.exit(f"{args.ticket} has no record with a terminal")
    screen = read_screen(rec["host"], rec["terminal"])
    if screen is None:
        sys.exit(f"{args.ticket}: {rec['terminal']} on {rec['host']} could not be read")
    print(f"{args.ticket} {rec['agent']} on {rec['host']} in {rec['terminal']}: {screen['status']}")
    print("\n".join(screen["lines"][-args.lines :]))


# Each first-parent commit the branch has over origin: a 2-parent merge whose second parent
# is a cleared SHA, each resolved to one commit here, and whose tree is the clean merge of
# its two parents. Then a fast-forward. The origin tip is the commit the fetch returned, so
# a narrow fetch refspec cannot leave it stale. A branch origin lacks is read from origin's
# $2, the base, and with no base names origin's URL and exits 3. One line, as UNPUSHED: a
# remote host types the command into its shell.
VERIFY_MERGES = (
    'b=$1; nb=$2; shift 2; git ls-remote -q --exit-code origin "refs/heads/$b" >/dev/null; r=$?; '
    'if [ $r = 2 ] && [ -z "$nb" ]; then echo "$b: not on origin, no base for $(git remote get-url origin)"; exit 3; fi; '
    'if [ $r = 2 ]; then t=$nb; elif [ $r = 0 ]; then t=$b; else echo "$b: fetch from origin failed"; exit 1; fi; '
    'git fetch -q origin "refs/heads/$t" || { echo "$t: fetch from origin failed"; exit 1; }; '
    'o=$(git rev-parse -q --verify "FETCH_HEAD^{commit}") || { echo "$b: fetch of origin $t gave no commit"; exit 1; }; '
    'l=$(git rev-parse -q --verify "refs/heads/$b") || { echo "$b: no local branch"; exit 1; }; '
    'ms=$(git rev-list --first-parent --reverse "$o..$l") || { echo "$b: its commits could not be listed"; exit 1; }; '
    # A SHA is read as an object id, never a ref of that name, and must name one commit.
    'cs=; for c in "$@"; do f=$(git rev-parse --disambiguate="$c" 2>/dev/null); '
    '[ -n "$f" ] && [ "$f" = "${f%%[[:space:]]*}" ] && [ "$(git cat-file -t "$f")" = commit ] && cs="$cs $f"; done; '
    "ok=yes; "
    "for m in $ms; do "
    'p1=$(git rev-parse -q --verify "$m^1") || p1=none; p2=$(git rev-parse -q --verify "$m^2") || p2=none; '
    'hit=no; for c in $cs; do [ "$p2" = "$c" ] && hit=yes; done; '
    'tree=$(git rev-parse -q --verify "$m^{tree}") || tree=; '
    'if [ "$p2" = none ]; then how=not-a-merge; '
    'elif git rev-parse -q --verify "$m^3" >/dev/null; then how=EXTRA-PARENT; '
    'elif w=$(git merge-tree --write-tree "$p1" "$p2" 2>/dev/null) && [ -n "$tree" ] && [ "$w" = "$tree" ]; then how=merge-eq; '
    "else how=DIFF; fi; "
    'echo "$(git rev-parse --short=9 "$m") p2=$(echo "$p2" | cut -c1-9) cleared=$hit $how"; '
    '[ $hit$how = yesmerge-eq ] || ok=no; done; '
    'if [ $r = 2 ]; then echo "$b is new on origin, read from origin $nb"; '
    'elif git merge-base --is-ancestor "$o" "$l"; then echo "$b fast-forwards origin"; '
    'else echo "$b BEHIND-ORIGIN"; ok=no; fi; '
    'if [ $ok = yes ]; then echo "$b VERIFIED"; exit 0; fi; echo "$b FAILED"; exit 1'
)


def cmd_verify_merges(args):
    cleared = sorted(
        sha
        for ticket in read_tickets(ledger()).values()
        if (found := FEATURE_BRANCH.search(ticket.get("notes") or "")) and found[1] == args.branch
        for _, sha in standing_pushes(ticket)
    )

    def verify(base):
        argv = ["sh", "-c", VERIFY_MERGES, "verify-merges", args.branch, base, *cleared]
        return run_on(args.host, args.worktree, argv, timeout=args.timeout)

    code, out = verify("")
    if code == 3:
        # First publication: compare with the repo's configured base.
        slug = ORIGIN_SLUG.search(out.strip().rsplit(" ", 1)[-1])
        repo = load_config().get("repos", {}).get(f"{slug[1]}/{slug[2]}" if slug else "", {})
        code, out = verify(repo["base"]) if repo.get("base") else (1, out)
    print(out, end="" if out.endswith("\n") or not out else "\n")
    sys.exit(code)


def main(argv=None):
    # Closing the loop's terminal sends SIGHUP; exiting through SystemExit still saves state.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(1))
    parser = argparse.ArgumentParser(
        prog="dev_auto.py",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    agents = ("claude", "codex", "cursor", "grok", "sdk")

    sub = commands.add_parser("slot")
    sub.add_argument("--agent", required=True, choices=agents)
    sub.add_argument("--model", required=True)
    sub.add_argument("--host", default="local")
    sub.add_argument("--account", help="account id or label; no other row is used")
    sub.set_defaults(func=cmd_slot)

    sub = commands.add_parser("launch")
    sub.add_argument("ticket")
    sub.add_argument("--agent", required=True, choices=agents)
    sub.add_argument("--model", help="defaults to the model named on the ticket")
    sub.add_argument("--effort")
    sub.add_argument("--worktree", required=True)
    sub.add_argument("--host", default="local")
    sub.add_argument("--account", help="account id or label; no other row is used")
    sub.add_argument(
        "--override-route",
        action="store_true",
        help="override the named model or effort, or coder kind, on the operator's word",
    )
    sub.add_argument(
        "--over-cap",
        action="store_true",
        help="launch past max_hard_tickets or max_medium_tickets on the operator's word",
    )
    sub.set_defaults(func=cmd_launch)

    commands.add_parser("tick").set_defaults(func=cmd_tick)

    sub = commands.add_parser("event-done", help="remove handled lines from events.md")
    sub.add_argument("lines", nargs="+", help="each line as events.md shows it")
    sub.set_defaults(func=cmd_event_done)

    sub = commands.add_parser(
        "keep", help="keep this Claude worker running when its account reaches handoff_percent"
    )
    sub.add_argument("ticket")
    sub.set_defaults(func=cmd_keep)

    sub = commands.add_parser("proposals", help="list, accept or drop restart proposals")
    sub.add_argument("action", nargs="?", default="list", choices=("list", "accept", "drop"))
    sub.add_argument("tickets", nargs="*")
    sub.add_argument("--all", action="store_true", help="accept every proposal")
    sub.add_argument("--account", help="launch on this account id or label instead")
    sub.add_argument("--model", help="launch this model instead")
    sub.add_argument("--effort", help="launch at this effort instead")
    sub.add_argument("--host", help="launch on this host instead")
    sub.set_defaults(func=cmd_proposals)

    sub = commands.add_parser(
        "usage-reset", help="record the operator's usage reset of an account"
    )
    sub.add_argument("account", help="account id or label")
    sub.set_defaults(func=cmd_usage_reset)

    sub = commands.add_parser("set-orchestrator")
    sub.add_argument("terminal")
    sub.add_argument("--host", default="local")
    sub.add_argument("--account", help="account id, for the relay-once usage rule")
    sub.add_argument(
        "--new-cycle", action="store_true", help="clear the relay and stop"
    )
    # Run from the orchestrator's own Claude Code, whose Bash carries its session id.
    sub.add_argument(
        "--session",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
        help="its Claude session id, whose transcript gives its context",
    )
    sub.set_defaults(func=cmd_set_orchestrator)

    sub = commands.add_parser("features")
    sub.add_argument("--repo", default=".", help="the repo whose .work/specs to show")
    sub.set_defaults(func=cmd_features)

    sub = commands.add_parser("ledger-init")
    sub.add_argument("repo_name")
    sub.add_argument("--checkout", help="the repo's main checkout, where evidence is cleaned")
    sub.set_defaults(func=cmd_ledger_init)

    sub = commands.add_parser("review-ticket", help="publish <ticket>-review unowned")
    sub.add_argument("ticket")
    sub.add_argument("--worktree", required=True, help="the coder's worktree")
    sub.add_argument("--host", default="local")
    sub.set_defaults(func=cmd_review_ticket)

    sub = commands.add_parser("resume", help="resume a Claude record's own session")
    sub.add_argument("ticket")
    sub.set_defaults(func=cmd_resume)

    sub = commands.add_parser("run", help="run a shell command in a worktree on a host")
    sub.add_argument("host")
    sub.add_argument("worktree")
    sub.add_argument("command")
    sub.add_argument("--timeout", type=int, default=90)
    sub.set_defaults(func=cmd_run)

    sub = commands.add_parser("screen", help="a worker's last screen lines")
    sub.add_argument("ticket")
    sub.add_argument("--lines", type=int, default=25)
    sub.set_defaults(func=cmd_screen)

    sub = commands.add_parser("verify-merges", help="check a feature branch before its push")
    sub.add_argument("branch")
    sub.add_argument("--worktree", required=True, help="a checkout that has the branch")
    sub.add_argument("--host", default="local")
    sub.add_argument("--timeout", type=int, default=120)
    sub.set_defaults(func=cmd_verify_merges)

    parser.epilog = "\n\n".join(
        commands.choices[name].format_help().rstrip() for name in ("slot", "launch")
    )
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
