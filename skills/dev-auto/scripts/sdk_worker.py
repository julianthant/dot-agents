#!/usr/bin/env python3
"""One Claude Agent SDK session for a dev-auto worker, billed to an Anthropic API key.

dev-auto starts it in the ticket's Orca terminal, in the worktree:

  uv run --no-project --with claude-agent-sdk sdk_worker.py --ticket T --account A --model M
      [--effort E] --key-file F --ticket-usd N --monthly-usd N --spend FILE --mcp JSON TEXT

TEXT is the first message. After each turn it prints IDLE and reads the next typed line
as the next message, so dev-auto wakes it as it wakes a Claude session. The key is read
from the key file into the SDK's env for the CLI it starts, never printed or passed on a
command line. Spend takes a reservation before the first request, records readings that
only grow, and releases only on proof; the row codes in comments (T1, R5, C6, H3 ...) are
those of the SPEND MODEL on ticket dev-auto-fixes-24.
"""

import argparse
import asyncio
import codecs
import fcntl
import json
import math
import os
import re
import signal
import stat
import sys
import time
from pathlib import Path

# Fixed rows dev-auto reads off the screen, whole: the runner's own rows start with "· ",
# its assistant text with "⏺ " or "  ". Each fits a 40-column terminal unwrapped.
BUSY = "· sdk worker: working"
IDLE = "· sdk worker: idle"
BUDGET = "· sdk worker: budget cap reached"
FAILED = "· sdk worker: API turn failed, exited"
UNPRICED = "· sdk worker: unpriced usage, exited"
NOT_API = "· sdk worker: not billed to the API key"
NO_SCRUB = "· sdk worker: key scrub not in force"
NO_KEY = "· sdk worker: key file missing or empty"
NO_SPEND = "· sdk worker: spend file unreadable"
RUNNING = "· sdk worker: ticket already running"
# The rows the runner exits on; dev-auto raises one STUCK on each and never relaunches.
STOPS = (BUDGET, FAILED, UNPRICED, NOT_API, NO_SCRUB, NO_KEY, NO_SPEND, RUNNING)
# Commands that restart the CLI's totals; the runner never forwards them (R8).
RESETS = ("/clear", "/reset", "/new")
# Seconds between readings while a turn is open.
READ_EVERY = 60
HANDOFF = (
    "dev-auto: this ticket has used 80 percent of its API budget. Finish the step you "
    "are on, write your handoff to the ticket, then end your turn."
)
PROMPT = "› "
# The share of ticket_usd left when the handoff message is sent.
HANDOFF_SHARE = 0.2
# Background work could still spend after a result (O3): no background tasks, and every
# MCP call runs in the foreground, so no MCP task can finish and start the model while the
# runner is idle. A scrubbed subprocess env keeps the key out of Bash, hooks and MCP
# servers (O4).
GUARDS = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_AUTO_BACKGROUND_TASKS": "",
    "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND": "1",
    "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
}
# The scrub holds the CLI in default permission mode, where only allowed tools run without
# a prompt and a headless session refuses the rest; no allow rule matches every tool.
TOOLS = [
    "Agent",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Read",
    "Skill",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
    "mcp__concord__*",
]
# Credentials Claude Code ranks above ANTHROPIC_API_KEY; empty reads as unset.
OUTRANK = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


# Claude Code places the cache breakpoints itself; the SDK offers no option for them. Wakes
# land minutes to hours apart, past the 5-minute default, so both the main loop and
# subagents ask for the 1-hour TTL, which a hit refreshes at no cost; nothing may turn
# caching off. FORCE_PROMPT_CACHING_5M outranks the TTL variables, so it is emptied.
CACHE = {
    "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h",
    "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h",
    "ENABLE_PROMPT_CACHING_1H": "1",
    "FORCE_PROMPT_CACHING_5M": "",
    **dict.fromkeys(
        (
            "DISABLE_PROMPT_CACHING",
            "DISABLE_PROMPT_CACHING_HAIKU",
            "DISABLE_PROMPT_CACHING_SONNET",
            "DISABLE_PROMPT_CACHING_OPUS",
            "DISABLE_PROMPT_CACHING_FABLE",
        ),
        "",
    ),
}
# USD per million tokens: input, 5-minute cache write, 1-hour cache write, cache read,
# output, for every Claude 5 model on platform.claude.com/docs/en/about-claude/pricing,
# read 2026-11-13.
# A model not listed has no price here and stops the runner. Haiku 5.5 is listed at its
# rates for prompts up to 100k tokens; a request whose prompt (input, cache writes and
# reads) is over that pays HAIKU_LONG on all of its tokens.
PRICES = {
    "claude-fable-5-2": (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-mythos-5-2": (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-fable-5-1": (10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-mythos-5-1": (10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-fable-5": (10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-mythos-5": (10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-sonnet-5-5": (2.0, 2.50, 4.0, 0.10, 10.0),
    "claude-sonnet-5": (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
}
HAIKU = "claude-haiku-5-5"
HAIKU_LONG = (0.50, 0.625, 1.0, 0.05, 2.50)
HAIKU_LONG_PROMPT = 100_000
# inference_geo "us" costs 1.1 times on every token category, the long-prompt rates
# included; "global", the default, costs 1. Any other value has no price here.
GEO = {None: 1.0, "global": 1.0, "us": 1.1}
GEO_MOST = max(GEO.values())
# speed and service_tier at standard pricing; the runner keeps fast mode off.
STANDARD = (None, "standard")
# Web search: 10 USD per 1000 searches, on top of the tokens its results add; web fetch
# costs only its tokens. Any other server tool has no price here.
SEARCH_USD = 0.01
SERVER_TOOLS = ("web_search_requests", "web_fetch_requests")
# Per family: input, cache writes, cache reads, output, searches; model_usage's names.
TOTALS = (
    "inputTokens",
    "cacheCreationInputTokens",
    "cacheReadInputTokens",
    "outputTokens",
    "webSearchRequests",
)


def family(model):
    """The table's id for a model string, without a date suffix; None when unlisted."""
    name = re.sub(r"\[.*\]$", "", model or "")
    base = re.sub(r"-\d{8}$", "", name)
    return name if name in PRICES else base if base in PRICES else None


def counts(usage):
    """A request's API usage as (input, 5m writes, 1h writes, reads, output, searches);
    writes the usage does not split count as 1-hour ones, the TTL the runner asks for."""
    wrote = float(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation") or {}
    short = float(split.get("ephemeral_5m_input_tokens") or 0)
    long = float(split.get("ephemeral_1h_input_tokens") or 0) if split else wrote
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return (
        float(usage.get("input_tokens") or 0),
        short,
        long,
        float(usage.get("cache_read_input_tokens") or 0),
        float(usage.get("output_tokens") or 0),
        float(searches),
    )


def request_cost(model, usage):
    """(USD, None) for one request, or (0, what has no price here)."""
    name = family(model)
    if name is None:
        return 0.0, f"model {model}"
    for field in ("speed", "service_tier"):
        if usage.get(field) not in STANDARD:
            return 0.0, f"{field} {usage.get(field)}"
    geo = usage.get("inference_geo")
    if geo not in GEO:
        return 0.0, f"inference_geo {geo}"
    for tool, used in (usage.get("server_tool_use") or {}).items():
        if used and tool not in SERVER_TOOLS:
            return 0.0, f"server_tool_use {tool}"
    n = counts(usage)
    price = HAIKU_LONG if name == HAIKU and sum(n[:4]) > HAIKU_LONG_PROMPT else PRICES[name]
    return sum(c * p for c, p in zip(n, price)) / 1e6 * GEO[geo] + n[5] * SEARCH_USD, None


def requests(projects, key, sessions):
    """({message id: (model, usage)}, problems) from the transcripts Claude Code keeps
    for these sessions and their subagents. A response streams several lines under one
    id; its last line carries the final usage. A session with no readable transcript is
    a problem: its requests' prices cannot be known."""
    found, problems = {}, []
    folder = Path(projects) / key
    for session in sessions:
        main = folder / f"{session}.jsonl"
        paths = [main, *sorted((folder / session / "subagents").rglob("agent-*.jsonl"))]
        for path in paths:
            try:
                lines = path.read_text().splitlines()
            except FileNotFoundError:
                if path == main:
                    problems.append("no transcript")
                continue
            except (OSError, UnicodeDecodeError):
                problems.append("transcript unreadable")
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message")
                if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("id"):
                    found[message["id"]] = (message.get("model") or "", message.get("usage") or {})
    return found, problems


def session_cost(model_usage, found):
    """(USD, USD of it at upper bounds, what has no price) for the process so far, from
    the API's usage, never the CLI's estimate. Each transcript request is priced on its
    own. What model_usage counts beyond them, such as helper requests, has no known
    prompt or region, so it pays the highest rates that could apply: Haiku's long-prompt
    rates and the US multiplier."""
    total, bounded, unpriced, seen = 0.0, 0.0, [], {}
    for model, usage in found.values():
        n = counts(usage)
        if not any(n):
            continue
        cost, why = request_cost(model, usage)
        if why:
            unpriced.append(why)
            continue
        total += cost
        have = seen.setdefault(family(model), [0.0] * 5)
        for i, value in enumerate((n[0], n[1] + n[2], n[3], n[4], n[5])):
            have[i] += value
    for name, used in model_usage.items():
        model = family(used.get("canonicalModel") or name)
        have = seen.setdefault(model or name, [0.0] * 5)
        rest = [max(0.0, float(used.get(k) or 0) - have[i]) for i, k in enumerate(TOTALS)]
        if not any(rest):
            continue
        if model is None:
            unpriced.append(f"model {name}")
            continue
        price = HAIKU_LONG if model == HAIKU else PRICES[model]
        tokens = rest[0] * price[0] + rest[1] * price[2] + rest[2] * price[3] + rest[3] * price[4]
        cost = tokens / 1e6 * GEO_MOST + rest[4] * SEARCH_USD
        total += cost
        bounded += cost
    return total, bounded, list(dict.fromkeys(unpriced))


def trusted(model_usage, found):
    """True when a result's model_usage can be the session's whole-tree total: it counts
    some tokens and, for every model and category, searches included, no fewer than its
    transcript requests hold. A crashed session's last result may carry it zeroed."""
    if not any(float(used.get(k) or 0) for used in model_usage.values() for k in TOTALS[:4]):
        return False
    have = {}
    for model, usage in found.values():
        n = counts(usage)
        sums = have.setdefault(family(model) or model, [0.0] * 5)
        for i, value in enumerate((n[0], n[1] + n[2], n[3], n[4], n[5])):
            sums[i] += value
    for name, used in model_usage.items():
        sums = have.pop(family(used.get("canonicalModel") or name) or name, [0.0] * 5)
        if any(float(used.get(k) or 0) < sums[i] for i, k in enumerate(TOTALS)):
            return False
    return not any(any(sums) for sums in have.values())


class Refused(Exception):
    """The session's init message shows it would run wrong: its stop row is the argument."""


class SpendFailed(Exception):
    """The spend file could not be read or written mid-run (C7)."""


def month_key(now):
    return time.strftime("%Y-%m", time.gmtime(now))


def read_spend(path):
    """{month: {"usd": n, "tickets": {id: n}}, "reserved": {ticket: reservation}}; {} when
    missing, ValueError when unreadable. A reservation is {"pid": int or None, "usd":
    allowance, "spent": recorded, "exact": recorded at exact tariffs, "months": {month:
    recorded there}, "open": a turn may have spent without a complete reading, "why":
    [codes]}."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ValueError(str(error)) from error
    if not isinstance(data, dict) or not all(isinstance(m, dict) for m in data.values()):
        raise ValueError("not a month table")
    return data


def months(data):
    return [m for key, m in data.items() if key != "reserved"]


def ticket_spent(data, ticket):
    return sum(float((m.get("tickets") or {}).get(ticket, 0)) for m in months(data))


def alive(pid):
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def counted(r):
    """A reservation that still takes headroom: its runner lives (C1, C3), or a turn was
    open when it went (a hold, C5, H1). A dead runner's closed one is spent (C4)."""
    return alive(r.get("pid")) or bool(r.get("open"))


def unrecorded(data, but=None, account=None):
    """The unspent part of counted reservations, including unassigned legacy holds."""
    return sum(
        max(0.0, float(r["usd"]) - float(r["spent"]))
        for ticket, r in (data.get("reserved") or {}).items()
        if ticket != but and (account is None or r.get("account") in (None, account)) and counted(r)
    )


def account_month_spent(data, now, account, but=None):
    row = data.get(month_key(now), {})
    spent = float(row.get("usd", 0))
    if account is not None:
        accounts = row.get("accounts") or {}
        # Legacy charges have no account ID and count against every row.
        legacy = max(0.0, round(spent - sum(map(float, accounts.values())), 6))
        spent = legacy + float(accounts.get(account, 0))
    held = unrecorded(data, but, account)
    return round(spent + held, 6)


def add_charge(row, ticket, account, amount):
    row["usd"] = round(float(row.get("usd", 0)) + amount, 6)
    tickets = row.setdefault("tickets", {})
    tickets[ticket] = round(float(tickets.get(ticket, 0)) + amount, 6)
    if account is not None:
        accounts = row.setdefault("accounts", {})
        accounts[account] = round(float(accounts.get(account, 0)) + amount, 6)


def update_spend(path, change):
    """change(data) on the spend file under its lock; the file is written when it changed."""
    path = Path(path)
    with open(path.with_name(path.name + ".lock"), "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        data = read_spend(path)
        was = json.dumps(data, sort_keys=True)
        value = change(data)
        if json.dumps(data, sort_keys=True) != was:
            fresh = path.with_name(path.name + ".tmp")
            fresh.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.replace(fresh, path)
        return value


def reserve(args, now):
    """(ticket's spend so far, allowance, refusal). The allowance is what the ticket's cap
    and the month's cap leave, less every other counted reservation's unrecorded part
    (C1); a positive one is reserved for this process before its first request. A
    ticket with a live runner is refused "running", one with a hold "held"; neither is
    ever overwritten (C2). Dead runners' closed reservations are dropped (C4)."""

    def change(data):
        reserved = data.setdefault("reserved", {})
        for ticket, r in list(reserved.items()):
            if not counted(r):
                del reserved[ticket]
        before = ticket_spent(data, args.ticket)
        mine = reserved.get(args.ticket)
        if mine:
            return before, 0.0, "running" if alive(mine.get("pid")) else "held"
        month = account_month_spent(data, now, args.account, args.ticket)
        allowance = min(args.ticket_usd - before, args.monthly_usd - month)
        if allowance > 0:
            reserved[args.ticket] = {
                "pid": os.getpid(),
                "usd": round(allowance, 6),
                "spent": 0,
                "exact": 0,
                "months": {},
                "open": False,
                "why": [],
            }
            if args.account is not None:
                reserved[args.ticket]["account"] = args.account
        if not reserved:
            del data["reserved"]
        return before, allowance, None

    return update_spend(args.spend, change)


def mine(data, args):
    r = (data.get("reserved") or {}).get(args.ticket) or {}
    return r if r.get("pid") == os.getpid() else None


def write(args, change):
    try:
        return update_spend(args.spend, change)
    except (OSError, ValueError) as error:
        raise SpendFailed from error


def open_turn(args, now):
    """Before a query: the turn may spend, and the month it starts in is touched (H4)."""

    def change(data):
        r = mine(data, args)
        if r:
            r["open"] = True
            r.setdefault("months", {}).setdefault(month_key(now), 0)

    write(args, change)


def record(args, new, exact, now, close):
    """Add a reading's growth to its UTC month, the ticket and the reservation; close the
    reservation in the same write when the reading is complete."""

    def change(data):
        r = mine(data, args)
        month = month_key(now)
        if new > 0:
            add_charge(data.setdefault(month, {}), args.ticket, args.account, new)
        if r:
            spent = r.setdefault("months", {})
            spent[month] = round(float(spent.get(month, 0)) + new, 6)
            r["spent"] = round(float(r["spent"]) + new, 6)
            r["exact"] = round(max(float(r.get("exact", 0)), exact), 6)
            if close:
                r["open"] = False

    write(args, change)


def finish(args, why, proven):
    """On the way out: the reservation is released only on proof, a closed reservation
    whose last reading was complete with nothing found since, or a session that sent no
    request. Anything else stays as an open hold with its codes, and only settle drops it
    (shape 2)."""

    def change(data):
        r = mine(data, args)
        if not r:
            return
        if proven and not r.get("open") and not why:
            del data["reserved"][args.ticket]
        else:
            r.update(open=True, pid=None, why=list(dict.fromkeys([*r.get("why", []), *why])))
        if not data["reserved"]:
            del data["reserved"]

    try:
        update_spend(args.spend, change)
    except (OSError, ValueError):
        pass


def billed_amount(raw):
    usd = float(raw)
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("billed amounts must be finite and nonnegative")
    return usd


def settle(argv):
    """sdk_worker.py settle --ticket T --spend FILE (--usd N | --month YYYY-MM=N ...):
    record what the Console billed a held ticket's stopped session and drop its hold.
    Each figure replaces the share the hold recorded in that month (H3); a hold that
    touched several months needs one --month figure for each (H4). A live reservation
    or none is refused (H2), and so are figures below the hold's exact share (H5)."""
    parser = argparse.ArgumentParser(prog="sdk_worker.py settle")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--spend", required=True)
    parser.add_argument("--usd")
    parser.add_argument("--month", action="append", default=[], help="YYYY-MM=USD")
    args = parser.parse_args(argv)
    given = {}
    try:
        if args.usd is not None:
            args.usd = billed_amount(args.usd)
        for pair in args.month:
            month, _, usd = pair.partition("=")
            if not re.fullmatch(r"\d{4}-\d\d", month) or not usd:
                parser.error(f"--month takes YYYY-MM=USD, not {pair!r}")
            given[month] = billed_amount(usd)
    except ValueError as error:
        parser.error(str(error))

    def change(data):
        held = (data.get("reserved") or {}).get(args.ticket)
        if not held or alive(held.get("pid")) or not held.get("open"):
            return f"no held spend for {args.ticket}"
        spent = held.get("months") or {}
        touched = sorted(spent)
        figures = dict(given)
        if args.usd is not None and len(touched) == 1 and not given:
            figures = {touched[0]: args.usd}
        if not touched or sorted(figures) != touched:
            return (
                f"held spend for {args.ticket} spans {', '.join(touched) or 'no month'}; "
                "give --month YYYY-MM=USD for each"
            )
        if sum(figures.values()) < float(held.get("exact", 0)) - 1e-9:
            return (
                f"settle for {args.ticket} is below its exact share "
                f"{float(held.get('exact', 0)):.4f} USD; the Console is behind or the figure is wrong"
            )
        for month, usd in figures.items():
            extra = usd - float(spent.get(month, 0))
            add_charge(data.setdefault(month, {}), args.ticket, held.get("account"), extra)
        del data["reserved"][args.ticket]
        if not data["reserved"]:
            del data["reserved"]
        return None

    refused = update_spend(args.spend, change)
    if refused:
        print(f"· sdk worker: {refused}")
        return 1
    total = sum(given.values()) if given else args.usd
    print(f"· sdk worker: settled {args.ticket} at {total:g} USD")
    return 0


def tokens_in(usage):
    return sum(int(v or 0) for k, v in (usage or {}).items() if k.endswith("_tokens"))


class Worker:
    def __init__(self, sdk, args, key, out):
        self.sdk, self.args, self.key, self.out = sdk, args, key, out
        self.before = 0.0
        self.loaded = False
        # The sessions this process saw, whose transcripts price its requests.
        self.sessions = []
        # The last whole model_usage; the highest total and exact total read; this
        # process's recorded spend; whether anything showed API usage (the evidence rule).
        self.model_usage = {}
        self.total = 0.0
        self.exact = 0.0
        self.spent = 0.0
        self.evidence = False
        self.bounded = 0.0
        # Set by a reading inside a turn that interrupted it: ("unpriced", codes) or
        # ("budget",).
        self.stopping = None
        # Whether the last reading at a result was complete with nothing found since: the
        # only proof that releases the reservation (shape 2). The watcher's failure, and
        # this turn's recorded spend when it opened.
        self.proven = False
        self.watch_error = None
        self.turn_from = 0.0

    def say(self, line=""):
        self.out.write(line + "\n")
        self.out.flush()

    def options(self, budget, resume=None):
        server = json.loads(self.args.mcp)
        given = {
            "cwd": os.getcwd(),
            "model": self.args.model,
            "allowed_tools": TOOLS,
            "setting_sources": ["user", "project"],
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            # Concord only: no settings or plugin server can run tasks the runner cannot see.
            "mcp_servers": {"concord": {"type": "stdio", **server}},
            "strict_mcp_config": True,
            "env": {
                **dict.fromkeys(OUTRANK, ""),
                **CACHE,
                **GUARDS,
                # Fast mode bills twice the rates; the runner keeps it off.
                "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
                "ANTHROPIC_API_KEY": self.key,
            },
            "max_budget_usd": round(budget, 6),
            # Background tasks are off, but a Monitor could still wake the model while
            # the runner is idle, when no reading runs (O3).
            "disallowed_tools": ["Monitor"],
        }
        if self.args.effort:
            given["effort"] = self.args.effort
        if resume:
            given["resume"] = resume
        return self.sdk.ClaudeAgentOptions(**given)

    def reading(self, result=None, at_exit=False):
        """Price the process so far, record what grew past the highest total, and return
        (complete, codes). At a result, or on the way out, codes are every reason the
        reading is not complete; between results only T3's."""
        if result is not None:
            if result.session_id and result.session_id not in self.sessions:
                self.sessions.append(result.session_id)
            if tokens_in(result.usage) or float(result.total_cost_usd or 0) > 0:
                self.evidence = True
        config = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        key = self.sdk.project_key_for_directory(os.getcwd())
        found, problems = requests(Path(config) / "projects", key, self.sessions)
        if any(any(counts(u)) for _, u in found.values()):
            self.evidence = True
        given = (result.model_usage or {}) if result is not None else {}
        if any(float(u.get(k) or 0) for u in given.values() for k in (*TOTALS[:4], "costUSD")):
            self.evidence = True
        whole = result is not None and trusted(given, found)
        if whole:
            self.model_usage = given
        total, bounded, codes = session_cost(self.model_usage, found)
        exact = total - bounded
        if result is not None or at_exit:
            judged = [p for p in problems if p != "no transcript" or self.evidence]
            if result is not None and not whole and self.evidence:
                judged.append("model_usage missing or zeroed")
            if at_exit and self.evidence and not trusted(self.model_usage, found):
                # Late transcript lines past the last whole aggregate, or usage with none
                # (R2 on the way out).
                judged.append("model_usage missing or zeroed")
            if (
                result is not None
                and not self.evidence
                and result.subtype == "error_during_execution"
            ):
                judged.append("model_usage missing or zeroed")
            codes = list(dict.fromkeys([*judged, *codes]))
        complete = result is not None and not codes
        new = max(0.0, total - self.total)
        # Written first: a failed write must leave the next reading to record it (C7).
        record(self.args, new, max(self.exact, exact), time.time(), close=complete)
        self.total = max(self.total, total)
        self.exact = max(self.exact, exact)
        self.spent += new
        self.bounded = bounded
        if result is not None:
            self.proven = complete
        elif codes:
            self.proven = False
        return complete, codes

    async def watch(self, client, stage, turn):
        """A reading every READ_EVERY seconds while a turn runs: it records, and stops the
        turn on unpriced usage (T3) or at a stop point measured on recorded spend (C6).
        When the reading or the interrupt fails, it cancels the turn, so no request
        starts after a stop that did not land, and the turn re-raises the failure."""
        try:
            while True:
                await asyncio.sleep(READ_EVERY)
                _complete, codes = self.reading()
                if codes:
                    self.stopping = ("unpriced", codes)
                elif self.spent >= stage:
                    self.stopping = ("budget",)
                if self.stopping:
                    await client.interrupt()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the turn re-raises it
            self.watch_error = error
            turn.cancel()

    async def turn(self, client, text, stage):
        """Send one message and stream the turn; its ResultMessage, or None."""
        sdk = self.sdk
        self.say(BUSY)
        open_turn(self.args, time.time())
        self.proven, self.turn_from = False, self.spent
        await client.query(text)
        result = None
        self.messages, self.error, self.stopping = 0, None, None
        watch = asyncio.create_task(self.watch(client, stage, asyncio.current_task()))
        try:
            async for message in client.receive_response():
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    data = message.data or {}
                    if data.get("session_id") and data["session_id"] not in self.sessions:
                        self.sessions.append(data["session_id"])
                    if data.get("apiKeySource") != "ANTHROPIC_API_KEY":
                        await client.interrupt()
                        raise Refused(NOT_API)
                    if data.get("permissionMode") != "default":
                        # The scrub forces default mode; any other mode means it is off.
                        await client.interrupt()
                        raise Refused(NO_SCRUB)
                    if not self.loaded:
                        self.loaded = True
                        servers = {m.get("name"): m.get("status") for m in data.get("mcp_servers") or []}
                        self.say(
                            f"· sdk worker: billed to ANTHROPIC_API_KEY, concord "
                            f"{servers.get('concord', 'missing')}, {len(data.get('skills') or [])} skills"
                        )
                elif isinstance(message, sdk.AssistantMessage):
                    self.messages += 1
                    self.error = message.error or self.error
                    if tokens_in(message.usage):
                        self.evidence = True
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock) and block.text.strip():
                            first, *rest = block.text.strip().splitlines()
                            self.say(f"⏺ {first}")
                            for line in rest:
                                self.say(f"  {line}")
                        elif isinstance(block, sdk.ToolUseBlock):
                            self.say(f"⏺ {block.name}")
                    self.say(BUSY)
                elif isinstance(message, sdk.ResultMessage):
                    result = message
        except asyncio.CancelledError:
            if self.watch_error:
                raise self.watch_error from None
            raise
        finally:
            watch.cancel()
        if self.watch_error:
            raise self.watch_error
        return result

    def report(self, result):
        """The turn's cache row, from its API usage, and its cost row."""
        usage = result.usage or {}
        read, wrote, fresh, out = (
            int(usage.get(k) or 0)
            for k in (
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "input_tokens",
                "output_tokens",
            )
        )
        seen = read + wrote + fresh
        self.say(
            f"· sdk worker: cache {read / seen if seen else 0:.0%} hit, {read} read, "
            f"{wrote} written, {fresh} uncached, {out} out"
        )
        self.say(
            f"· sdk worker: {result.subtype}, {self.spent - self.turn_from:.2f} USD this turn, "
            f"{self.before + self.spent:.2f} of {self.args.ticket_usd:g} USD on "
            f"{self.args.ticket}"
            + (f", {self.bounded:.4f} USD at upper bounds" if self.bounded > 0 else "")
        )

    def failure(self, result):
        """One row of a failed result's codes and counts, never its text."""
        what = "no API usage" if not self.evidence else "turn failed"
        self.say(
            f"· sdk worker: {what}: {result.subtype}, is_error {result.is_error}, "
            f"api_error_status {result.api_error_status}, error {self.error}, "
            f"terminal_reason {result.terminal_reason}, {self.messages} messages, "
            f"{result.num_turns} turns, {result.duration_api_ms} ms API"
        )


async def input_lines(stdin):
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError):
        fd = None
    mode = os.fstat(fd).st_mode if fd is not None else 0
    if fd is None or not (os.isatty(fd) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        for line in iter(stdin.readline, ""):
            yield line
        return

    loop = asyncio.get_running_loop()
    decode = codecs.getincrementaldecoder(stdin.encoding or "utf-8")(stdin.errors or "strict")
    pending = ""
    while True:
        ready = asyncio.Event()
        loop.add_reader(fd, ready.set)
        try:
            await ready.wait()
        finally:
            loop.remove_reader(fd)
        chunk = os.read(fd, 8192)
        pending += decode.decode(chunk, final=not chunk)
        *lines, pending = pending.split("\n")
        for line in lines:
            yield line
        if not chunk:
            if pending:
                yield pending
            return


async def messages(stdin):
    async for line in input_lines(stdin):
        text = line.rstrip("\r\n").rsplit("\x15", 1)[-1].strip()
        if text:
            yield text


async def run(args, sdk, stdin, out):
    worker = Worker(sdk, args, "", out)
    if family(args.model) is None:
        worker.say(f"· sdk worker: unpriced usage: model {args.model}")
        worker.say(UNPRICED)
        return 1
    try:
        key = Path(args.key_file).expanduser().read_text().strip()
    except OSError:
        key = ""
    if not key:
        worker.say(NO_KEY)
        return 1
    worker.key = key
    try:
        worker.before, stop, refusal = reserve(args, time.time())
    except (ValueError, TypeError, AttributeError, KeyError, OSError):
        worker.say(NO_SPEND)
        return 1
    if refusal == "running":
        worker.say(f"· sdk worker: ticket {args.ticket} has a live runner")
        worker.say(RUNNING)
        return 1
    if refusal == "held":
        worker.say("· sdk worker: unpriced usage: spend held from an earlier run")
        worker.say(UNPRICED)
        return 1
    if stop <= 0:
        worker.say(BUDGET)
        return 0
    why, code = [], None
    try:
        code = await session(worker, args, sdk, stdin, out, stop, why)
    except SpendFailed:
        # Nothing more can be written; the file stays as last written, a hold if open (C7).
        worker.say(NO_SPEND)
        return 1
    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - every such ending is R5
        # The session ended with no result: the stream, the SDK, the connection or a
        # signal (R5). What the exit reading finds is recorded and the rest is held.
        why.append("no result")
        worker.proven = False
        worker.say("· sdk worker: unpriced usage: no result")
        worker.say(UNPRICED)
        code = 1
    try:
        _complete, codes = worker.reading(at_exit=True)
    except SpendFailed:
        worker.say(NO_SPEND)
        return 1
    except Exception:  # noqa: BLE001 - the reservation then stays open
        codes = ["exit reading failed"]
    if "no result" in why and not worker.model_usage:
        # A session with no result has no aggregate to recheck; R5 already holds it.
        codes = [c for c in codes if c != "model_usage missing or zeroed"]
    codes = [c for c in codes if c not in why]
    if codes:
        # Found on the way out, after the last result: a stop of its own (T3, X1, X2).
        worker.say(f"· sdk worker: unpriced usage: {', '.join(codes)}")
        worker.say(UNPRICED)
        why.extend(codes)
        code = 1
    finish(args, why, worker.proven)
    return code


async def session(worker, args, sdk, stdin, out, stop, why):
    handoff_at = (1 - HANDOFF_SHARE) * args.ticket_usd - worker.before
    text = args.text
    handoff_pending = 0 < handoff_at < stop
    if handoff_at <= 0:
        text = f"{text} {HANDOFF}"
    try:
        client = sdk.ClaudeSDKClient(options=worker.options(handoff_at if handoff_pending else stop))
        await client.connect()
    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - any start failure
        # Before the first request nothing was spent, so the reservation goes (row 44).
        finish(args, [], True)
        worker.say(f"· sdk worker: session failed to start: {type(error).__name__}")
        worker.say(FAILED)
        return 1
    incoming = messages(stdin)
    try:
        while True:
            try:
                result = await worker.turn(client, text, handoff_at if handoff_pending else stop)
            except Refused as refused:
                # Stopped at the init message. A session billed elsewhere spends nothing
                # on the key, so with no evidence of usage its reservation closes; any
                # other refusal leaves the reading on the way out to record and hold.
                billed_elsewhere = refused.args[0] == NOT_API and not worker.evidence
                if billed_elsewhere:
                    record(args, 0.0, worker.exact, time.time(), close=True)
                else:
                    why.append(refused.args[0])
                worker.proven = billed_elsewhere
                worker.say(refused.args[0])
                return 1
            if result is None:
                raise RuntimeError("the turn ended with no result")
            _complete, codes = worker.reading(result)
            worker.report(result)
            if worker.stopping and worker.stopping[0] == "unpriced":
                codes = list(dict.fromkeys([*worker.stopping[1], *codes]))
            if codes:
                why.extend(codes)
                worker.say(f"· sdk worker: unpriced usage: {', '.join(codes)}")
                worker.say(UNPRICED)
                return 1
            capped = result.subtype == "error_max_budget_usd" or worker.stopping == ("budget",)
            if not capped and (result.is_error or not worker.evidence):
                worker.failure(result)
                worker.say(FAILED)
                return 1
            if worker.spent >= stop or (not handoff_pending and capped):
                worker.say(BUDGET)
                return 0
            if handoff_pending and (capped or worker.spent >= handoff_at):
                # The rest of the cap goes to a resumed session, whose budget counts only
                # its own spend; its first message is the handoff.
                handoff_pending = False
                await client.disconnect()
                resumed = worker.options(stop - worker.spent, resume=result.session_id)
                client = sdk.ClaudeSDKClient(options=resumed)
                await client.connect()
                text = HANDOFF
                continue
            worker.say(IDLE)
            while True:
                out.write(PROMPT)
                out.flush()
                text = await anext(incoming, None)
                if text is None:
                    return 0
                command = text.split()[0]
                if command not in RESETS:
                    break
                # They restart the CLI's totals, which only R2 may treat as a reset (R8).
                worker.say(f"· sdk worker: ignored {command}")
    finally:
        await incoming.aclose()
        await client.disconnect()


def parse(argv):
    parser = argparse.ArgumentParser(prog="sdk_worker.py", description=__doc__.splitlines()[0])
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--account", help="the configured SDK account ID")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ticket-usd", type=float, required=True)
    parser.add_argument("--monthly-usd", type=float, required=True)
    parser.add_argument("--spend", required=True)
    parser.add_argument("--mcp", required=True, help="the concord server as JSON")
    parser.add_argument("text")
    return parser.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["settle"]:
        return settle(argv[1:])
    args = parse(argv)
    import claude_agent_sdk

    async def guarded():
        # A closed terminal or a kill signal cancels the turn, which is R5's exit path.
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        return await run(args, claude_agent_sdk, sys.stdin, sys.stdout)

    return asyncio.run(guarded())


if __name__ == "__main__":
    sys.exit(main())
