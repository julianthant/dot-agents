import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tick

L = tick.limited
assert L(["⏺ working", "⎿  You've hit your 5-hour limit · resets 5pm"]) == "⎿  You've hit your 5-hour limit · resets 5pm"
assert L(["■ You’ve hit your usage limit. Try again in 3 days"]).startswith("■ You’ve hit")
assert L(["Weekly usage limit reached."]) == "Weekly usage limit reached."
# Output after the limit line makes it history; quoted limit text is not a limit screen.
assert L(["You've hit your limit", "⏺ Back at work"]) is None
assert L(["- the worker said you've hit your limit"]) is None
assert L([]) is None

show = lambda stage, last: {"projection": {"stage": {"dispatch": stage}}, "terminal": {"lastOutputAt": last}}
now = 10_000
assert tick.silent(show("dispatched", (now - 31 * 60) * 1000), now)
assert not tick.silent(show("dispatched", (now - 29 * 60) * 1000), now)
assert not tick.silent(show("completed", 0), now)
assert not tick.silent(show("dispatched", None), now)

# orca() drops the pane variables a launchd job never has.
with patch.dict(tick.os.environ, {"ORCA_TERMINAL_HANDLE": "term_x", "ORCA_CLI_COMMAND": "orca"}), \
        patch.object(tick.subprocess, "run") as run:
    run.return_value.stdout, run.return_value.returncode = '{"ok": true, "result": {}}', 0
    assert tick.orca("status") == {"ok": True, "result": {}}
    env = run.call_args.kwargs["env"]
    assert "ORCA_TERMINAL_HANDLE" not in env and env["ORCA_CLI_COMMAND"] == "orca"
    assert run.call_args.args[0] == ["orca", "status", "--json"]

with patch.object(tick, "command", return_value='{"ok": true, "result": {}}') as cli:
    tick.runtime("account", "select", "--agent", "codex", "--account", "seat")
    cli.assert_called_once_with(tick.ORCA, "account", "select", "--agent", "codex", "--account", "seat", "--json")
    tick.runtime("account", "list")
    cli.assert_called_with(tick.ORCA, "account", "list", "--json")
    tick.runtime("terminal", "list")
    cli.assert_called_with(tick.ORCA, "terminal", "list", "--environment", "local", "--json")

# orca(): a nonzero exit, invalid JSON or a timeout is a failed receipt, raised unless ok=False.
with patch.object(tick.subprocess, "run") as run:
    run.return_value.stdout, run.return_value.returncode, run.return_value.stderr = '{"ok": true}', 1, ""
    assert tick.orca("x", ok=False)["ok"] is False
    run.return_value.stdout = "not json"
    assert "invalid JSON" in tick.orca("x", ok=False)["error"]
    run.side_effect = tick.subprocess.TimeoutExpired("orca", 900)
    assert tick.orca("x", ok=False) == {"ok": False, "error": "timed out"}
    try:
        tick.orca("x")
        raise AssertionError("orca should raise")
    except tick.OrcaError as e:
        assert "timed out" in str(e)


def fake(replies):
    """An orca() stand-in: replies maps the first two args to a response; calls are recorded."""
    calls = []

    def run(*args, ok=True):
        calls.append(args[:2])
        r = replies.get(args[:2], {"ok": True, "result": {}})
        if ok and not r.get("ok"):
            raise tick.OrcaError(str(r.get("error")))
        return r
    return run, calls


REL = ("orchestration", "worker-release")
CLOSE = ("terminal", "close")
RM = ("worktree", "rm")
st = {"worktree": "path:/scratch", "path": "/scratch", "branch": "scratch/x", "git": "/repo/.git",
      "anchor": "term_a", "started": True, "dispatch": "ctx_1"}
good = {REL: {"ok": True, "result": {"state": "released"}},
        CLOSE: {"ok": True, "result": {"close": {"ptyKilled": True}}},
        RM: {"ok": True, "result": {"removed": True}}}


def clean(replies, state=st, left=()):
    run, calls = fake({**good, **replies})
    with patch.object(tick, "orca", run), patch.object(tick, "leftovers", return_value=list(left)), \
            redirect_stdout(io.StringIO()):
        s = dict(state)
        return tick.cleanup(s), calls, s


KEPT = "kept scratch worktree path:/scratch"
errs, calls, s = clean({})
assert errs == [] and RM in calls and "anchor" not in s

# Each unconfirmed step keeps the checkout: no rm call, the reason, then KEPT.
for bad in ({REL: {"ok": True, "result": {"state": "retained"}}},
            {REL: {"ok": False, "error": {"code": "release_unknown"}}},
            {REL: {"ok": False, "error": "exit 1", "result": {"state": "released"}}},
            {REL: {"ok": False, "error": "timed out"}},
            {CLOSE: {"ok": True, "result": {"close": {"ptyKilled": False}}}},
            {CLOSE: {"ok": False, "error": "invalid JSON, exit 1: "}}):
    errs, calls, s = clean(bad)
    assert RM not in calls and errs[-1] == KEPT and len(errs) == 2, (bad, errs)
# An unconfirmed close keeps the anchor handle for the next attempt.
assert clean({CLOSE: {"ok": False, "error": "timed out"}})[2]["anchor"] == "term_a"
# A start with no Dispatch, or a follow-up whose receipt never resolved: nothing is released or removed.
errs, calls, _ = clean({}, {**st, "dispatch": None})
assert REL not in calls and RM not in calls and "may be live" in errs[0]
errs, calls, _ = clean({}, {**st, "followup": True})
assert RM not in calls and "follow-up" in errs[0] and errs[-1] == KEPT

# No start attempted: nothing to release, the checkout goes.
errs, calls, _ = clean({}, {k: st[k] for k in ("worktree", "path", "branch", "git")})
assert errs == [] and calls == [RM]

# rm that fails, or succeeds while git still shows the checkout or branch, is not clean.
for bad in ({RM: {"ok": False, "error": "busy"}}, {RM: {"ok": True, "result": {"removed": False}}},
            {RM: {"ok": False, "error": "timed out"}}):
    assert "worktree rm not confirmed" in clean(bad)[0][0]
assert clean({}, left=["branch scratch/x"])[0] == ["still present after rm: branch scratch/x"]

# finish: a cleanup failure exits 1; a proof failure is re-raised unchanged, even over a cleanup crash.
run, _ = fake({**good, REL: {"ok": True, "result": {"state": "retained"}}})
with patch.object(tick, "orca", run), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
    try:
        tick.finish(dict(st))
        raise AssertionError("finish should exit")
    except SystemExit as e:
        assert e.code == 1
    boom = tick.OrcaError("worker-start failed")
    for cleaner in (tick.cleanup, lambda s: 1 / 0):
        with patch.object(tick, "cleanup", cleaner):
            try:
                tick.finish(dict(st), boom)
                raise AssertionError("finish should raise")
            except tick.OrcaError as e:
                assert e is boom
assert KEPT in err.getvalue() and "cleanup crashed: ZeroDivisionError" in err.getvalue()

# completion: only a succeeded worker_done for the expected Dispatch of this Run.
done = lambda d, o: {"id": "m", "payload": {"dispatchId": d, "outcome": o}}
show = {("orchestration", "worker-show"): {"ok": True, "result": {"runId": "run_1"}}}
run, _ = fake(show)
with patch.object(tick, "orca", run):
    assert tick.completion(done("ctx_1", "succeeded"), "run_1", "ctx_1")
    for m, r in ((done("ctx_1", "failed"), "run_1"), (done("ctx_2", "succeeded"), "run_1"),
                 (done("ctx_1", "succeeded"), "run_2"), ({"id": "m"}, "run_1")):
        try:
            tick.completion(m, r, "ctx_1")
            raise AssertionError(f"accepted {m} for {r}")
        except tick.OrcaError as e:
            assert "rejected" in str(e)

# orca(): only a literal JSON true is success.
with patch.object(tick.subprocess, "run") as run:
    run.return_value.returncode, run.return_value.stderr = 0, ""
    for v in ('"false"', '"true"', "1", "{}"):
        run.return_value.stdout = '{"ok": %s}' % v
        try:
            tick.orca("x")
            raise AssertionError(f"accepted ok={v}")
        except tick.OrcaError:
            pass

# Non-boolean release, close and removal fields keep the checkout or fail the removal.
for v in ("true", "false", 1):
    for bad in ({REL: {"ok": v, "result": {"state": "released"}}},
                {CLOSE: {"ok": v, "result": {"close": {"ptyKilled": True}}}},
                {CLOSE: {"ok": True, "result": {"close": {"ptyKilled": v}}}}):
        errs, calls, _ = clean(bad)
        assert RM not in calls and errs[-1] == KEPT, (bad, errs)
    for bad in ({RM: {"ok": v, "result": {"removed": True}}}, {RM: {"ok": True, "result": {"removed": v}}}):
        assert "worktree rm not confirmed" in clean(bad)[0][0], bad

# leftovers() against a real repo: present, then absent; a failed or unreadable git raises.
with TemporaryDirectory() as tmp:
    repo, wt = f"{tmp}/repo", f"{tmp}/wt"
    g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", repo], check=True)
    g("commit", "-q", "--allow-empty", "-m", "init")
    g("worktree", "add", "-q", "-b", "scratch/x", wt)
    real = {"path": wt, "branch": "scratch/x", "git": f"{repo}/.git"}
    assert tick.leftovers(real) == [f"worktree {wt}", f"directory {wt}", "branch scratch/x"]
    assert tick.leftovers({**real, "branch": "scratch/x-other"})[-1] == f"directory {wt}"
    g("worktree", "remove", wt)
    assert tick.leftovers(real) == ["branch scratch/x"]
    g("branch", "-q", "-D", "scratch/x")
    assert tick.leftovers(real) == []
    # A broken ref is not absence: for-each-ref skips it, show-ref --exists fails on it.
    os.makedirs(f"{repo}/.git/refs/heads/scratch", exist_ok=True)
    with open(f"{repo}/.git/refs/heads/scratch/x", "w") as f:
        f.write("garbage\n")
    try:
        tick.leftovers(real)
        raise AssertionError("broken ref read as absent")
    except tick.OrcaError as e:
        assert "show-ref refs/heads/scratch/x exit 1" in str(e), e
    os.remove(f"{repo}/.git/refs/heads/scratch/x")
    try:
        tick.leftovers({**real, "git": f"{tmp}/missing"})
        raise AssertionError("missing git dir passed")
    except tick.OrcaError as e:
        assert "exit 128" in str(e), e
    with patch.object(tick, "git", return_value=["garbage"]):
        try:
            tick.leftovers(real)
            raise AssertionError("unreadable output passed")
        except tick.OrcaError as e:
            assert "unreadable" in str(e)
    # Through cleanup: a git that cannot answer is not clean.
    run, _ = fake(good)
    with patch.object(tick, "orca", run), redirect_stdout(io.StringIO()):
        errs = tick.cleanup({**st, **real, "git": f"{tmp}/missing"})
    assert errs[0].startswith("absence not verified: git worktree list exit 128"), errs


# The dry-run plan reserves both paths and account capacity in priority order.
from unittest import TestCase
check = TestCase()
base = dict(status="Todo", kind="Task", later=False, assigned=False, blocked=False, busy=False,
            repo="hr-relay", branch="task/a/lane", paths=["src/a"], route=("codex", "gpt-6.2-sol", "medium", None),
            score=0, item="card", title="a / lane")
tickets = [dict(base, number=1, score=3), dict(base, number=2, paths=["src/a/file.ts"]),
           dict(base, number=3, paths=["src/b"]), dict(base, number=4, blocked=True, score=9),
           dict(base, number=5, status="In Progress", paths=["src/c"]),
           dict(base, number=6, paths=["src/c/sub"]), dict(base, number=7, later=True),
           dict(base, number=8, assigned=True, paths=["src/d"]), dict(base, number=9, kind="Feature")]
for t in tickets:
    t["branch"] += str(t["number"])

account = dict(id="seat", label="example")
def seat(route, held):
    return (account, []) if len(held) < 1 else (None, ["capacity"])
with redirect_stdout(io.StringIO()) as output:
    plan = list(tick.fill_plan(tickets, seat))
check.assertEqual([t["number"] for t, a in plan], [1])
check.assertIn("capacity", output.getvalue())
body = """## Owned paths

src/backend/logs/ (writers), tests/backend/logs*.ts

## Route

- Coder: claude (claude-sonnet-5-5, effort high) with Opus advisor · Reviewer: codex (gpt-6.2-sol, effort medium)
"""
check.assertEqual(tick.coder_route(body), ("claude", "claude-sonnet-5-5", "high", "opus"))
# The card label cannot show +advisor for Codex: rejected since 25ea75d.
assert tick.coder_route("Coder: codex / gpt-6.2-sol / high + opus advisor") is None
check.assertEqual(tick.owned_paths(body), ["src/backend/logs/", "tests/backend/logs*.ts"])
check.assertEqual(tick.owned_paths("Owned paths: selected service code and fixtures."), ["*"])

# A response lost after dispatch prevents a second launch, even after reopening the state.
import sqlite3
from pathlib import Path
with TemporaryDirectory() as tmp:
    dbpath = Path(tmp) / "state"
    with sqlite3.connect(dbpath) as db:
        tick.reserve(db, 17, {"task": "fixture"})
    with sqlite3.connect(dbpath) as db:
        with check.assertRaisesRegex(tick.OrcaError, "17"):
            tick.reserve(db, 17, {"task": "fixture"})

# Relevant fields from an Orca worker-start --full receipt, with identifiers redacted.
def full_start(dispatch, terminal):
    return {"state": "ready", "dispatchId": dispatch, "stage": "input_accepted", "turnStart": "observed",
            "effects": [{"kind": "worktree", "action": "reused", "id": "tree"},
                        {"kind": "setup", "action": "not_applicable", "state": "not_applicable"},
                        {"kind": "terminal", "role": "agent", "action": "created", "id": terminal, "surface": "visible"},
                        {"kind": "dispatch_input", "role": "agent", "id": terminal, "state": "accepted"}]}


# Exercise launch through the real advisor file, journal, hold and Project-edit boundary.
from types import SimpleNamespace
import json
with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
    root = Path(tmp)
    card = []
    starts = []
    ticket = dict(base, number=51, sha="sha", base="origin/main", route=("claude", "claude-sonnet-5-5", "medium", "opus"))
    args = SimpleNamespace(anchor=None, run=None)
    def cli(*args):
        if args[0] == "git":
            return "sha" if "rev-parse" in args else ticket["branch"]
        check.assertEqual(args[:3], ("gh", "project", "item-edit"))
        card.append(args[-1])
        return ""
    def local(*args):
        verb = args[:2]
        if verb == ("orchestration", "task-create"):
            check.assertTrue(args[args.index("--spec") + 1].startswith("/goal Implement julianthant/hr-relay#51"))
            return {"task": {"id": "fixture-task"}}
        if verb == ("orchestration", "dispatch-show"):
            return {"dispatch": {"id": "fixture-dispatch"}}
        if verb == ("orchestration", "run-current"):
            return {"runId": "fixture"}
        if verb == ("worktree", "create"):
            return {"worktree": {"path": str(root), "branch": "refs/heads/" + ticket["branch"], "head": "sha", "id": "wt"}}
        if verb == ("account", "select"):
            return {}
        if verb == ("account", "list"):
            return {"claude": {"activeAccountId": "seat"}}
        if verb == ("orchestration", "worker-start"):
            starts.append(args)
            check.assertIn("--task", args)
            check.assertEqual(json.loads((root / ".claude/settings.local.json").read_text()), {"advisorModel": "opus"})
            check.assertNotIn("--from", args)
            check.assertIn("--full", args)
            return full_start("fixture-dispatch", "fixture-terminal")
        check.assertEqual(verb, ("orchestration", "worker-show"))
        return {"dispatch": {"id": "fixture-dispatch", "assigneeHandle": "fixture-terminal", "runId": "fixture",
                             "taskId": "fixture-task"}, "worker": {"state": "ready", "worktreeId": "wt"}}

    with patch.object(tick, "project_fields", return_value=("project", [("s","progress"),("c","coding"),("a",None)], ("c","needs-you"))), \
         patch.object(tick, "runtime", local), patch.object(tick, "command", cli), \
         patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE":"fixture"}), redirect_stdout(io.StringIO()):
        tick.launch(db, root, ticket, account, args, root / "holds.json")
        with check.assertRaisesRegex(tick.OrcaError, "51"):
            tick.launch(db, root, ticket, account, args, root / "holds.json")
    check.assertEqual(len(starts), 1)
    check.assertEqual(json.loads((root / "holds.json").read_text())["fixture-terminal"]["account"], "seat")
    check.assertEqual(card, ["progress", "coding", "coder claude-sonnet-5-5 medium+advisor @example"])

# Preparation can fail without reserving an issue. A lost start response is recovered by
# its durable Task, including after reopening SQLite with a parked Codex menu.
with TemporaryDirectory() as tmp:
    root = Path(tmp)
    ledger = root / "starts"
    holds = root / "holds.json"
    args = SimpleNamespace(anchor=None, run=None, dry_run=False, repo_path=root, holds=holds, issue=None)
    codex_ticket = dict(ticket, route=base["route"])
    screen = ["Switch model?"]
    operations = []
    preparation_failed = True
    visible = False
    refused = False
    cancels = []
    cancel_fails = False
    def recovery_runtime(*argv):
        verb = argv[:2]
        operations.append(verb)
        if verb == ("account", "list"):
            return {"codex": {"activeAccountId": "other" if preparation_failed else "seat"}}
        if verb == ("orchestration", "task-update"):
            cancels.append(argv)
            if cancel_fails:
                raise tick.OrcaError("cancel refused")
            return {}
        if verb == ("orchestration", "worker-start"):
            import tomllib
            with (root / ".codex/config.toml").open("rb") as cfg:
                check.assertEqual(tomllib.load(cfg), {"notice": {"hide_rate_limit_model_nudge": True}})
            if refused:
                reply = {"ok": False, "error": {"message": "refused", "data": {"effectsApplied": False}}}
                with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(argv, 1, json.dumps(reply), "")):
                    return real_command("orca", *argv)
            raise subprocess.TimeoutExpired("worker-start", 1)
        if verb == ("orchestration", "dispatch-show") and not visible:
            return {"dispatch": None}
        if verb == ("orchestration", "worker-show"):
            return dict(local(*argv), observation=None, terminal={"handle": "fixture-terminal"})
        if verb == ("orchestration", "send"):
            check.assertEqual(argv[argv.index("--to")+1], "run:fixture")
            check.assertIn(screen[0], argv[argv.index("--body")+1])
            return {"messageId": "notice"}
        if verb == ("terminal", "read"):
            return {"terminal": {"tail": screen}}
        return local(*argv)

    recovery_edits = []
    real_command = tick.command
    def recovery_command(*argv):
        if argv[:3] == ("gh", "project", "item-edit"):
            recovery_edits.append(argv)
            return ""
        if "--git-path" in argv:
            return str(root / "info/exclude")
        if "get-url" in argv:
            return "git@github.com:julianthant/hr-relay.git"
        if "for-each-ref" in argv or "ls-remote" in argv:
            return ""
        return cli(*argv)

    import pick
    with (patch.object(tick, "project_fields", return_value=("project", [("field", "coding")], ("field","needs-you"))),
          patch.object(tick, "runtime", recovery_runtime), patch.object(tick, "command", recovery_command),
          patch.object(tick, "tickets", return_value=[]), patch.object(tick, "inventory", return_value=set()),
          patch.object(pick.account_usage, "load_config", return_value={}),
          patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "fixture"}), redirect_stdout(io.StringIO()) as output):
        with sqlite3.connect(ledger) as db:
            with check.assertRaisesRegex(tick.OrcaError, f"selected account changed.*worktree {root}"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            check.assertEqual(cancels, [("orchestration", "task-update", "--id", "fixture-task",
                                         "--status", "failed", "--result", "cancelled")])
            cancel_fails = True
            with check.assertRaisesRegex(tick.OrcaError, f"uncancelled Task fixture-task.*worktree {root}"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            cancel_fails = False
            preparation_failed = False
            refused = True
            with check.assertRaisesRegex(tick.OrcaError, "no start effects"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            check.assertEqual(db.execute("SELECT count(*) FROM starts").fetchone(), (0,))
            operations.clear()
            refused = False
            with check.assertRaisesRegex(tick.OrcaError, "fixture-task"):
                tick.launch(db, root, codex_ticket, account, args, holds)
        with sqlite3.connect(ledger) as db:
            tick.fill_tick(args, db)
            check.assertIn("no Dispatch", output.getvalue())
            visible = True
            tick.fill_tick(args, db)
            check.assertIn("waiting for operator input", output.getvalue())
            check.assertIn('"prompt": "Switch model?"', output.getvalue())
            check.assertEqual(json.loads(holds.read_text())["fixture-terminal"]["account"], "seat")
            tick.fill_tick(args, db)
            check.assertEqual(operations.count(("orchestration", "send")), 1)
            screen[:] = ["Hooks need review before continuing"]
            tick.fill_tick(args, db)
            check.assertIn('"prompt": "Hooks need review before continuing"', output.getvalue())
            check.assertEqual([edit[-1] for edit in recovery_edits], ["needs-you", "needs-you"])
            screen.clear()
            tick.fill_tick(args, db)
            check.assertEqual(db.execute("SELECT receipt FROM starts").fetchone(), ("done",))
            check.assertEqual([edit[-1] for edit in recovery_edits], ["needs-you", "needs-you", "coding"])
    check.assertEqual(operations.count(("orchestration", "worker-start")), 1)
    check.assertNotIn(("terminal", "send"), operations)
    check.assertNotIn(("orchestration", "approve"), operations)

# The fleet's actual model menu has no question mark. Quoted prose is not a prompt.
menu = ["Approaching rate limits", "› 1. Switch to gpt-6-luna", "  2. Keep current model"]
check.assertEqual(tick.codex_prompt(["unrelated transcript", *menu, "unrelated footer"]), "\n".join(menu))
check.assertIsNone(tick.codex_prompt(['I did not see the "Hooks need review" screen.']))

# An in-pane worker cannot select somebody else's anchor even with a valid-looking Run.
with sqlite3.connect(":memory:") as db, patch.object(tick, "project_fields", return_value=(None, [], ("c","needs-you"))), \
     patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE":"fixture"}), patch.object(tick, "runtime") as call:
    with check.assertRaisesRegex(tick.OrcaError, "pane-less"):
        tick.launch(db, None, ticket, account, SimpleNamespace(anchor="external", run="run"), None)
    call.assert_not_called()


# Dry-run reads the existing account ledger without pruning it or starting work.
import pick
from contextlib import ExitStack
with TemporaryDirectory() as tmp, ExitStack() as stack:
    root = Path(tmp)
    holds = root / "holds.json"
    holds.write_text('{"dead": {"host": "local"}}')
    args = SimpleNamespace(repo_path=root, dry_run=True, issue=None, holds=holds)
    stack.enter_context(patch.object(tick, "tickets", return_value=[dict(ticket)]))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(tick, "command", side_effect=["git@github.com:julianthant/hr-relay.git", "origin/main",
                                                              "abcdef refs/heads/main"]))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    launch = stack.enter_context(patch.object(tick, "launch"))
    with redirect_stdout(io.StringIO()) as output:
        with patch.object(Path, "home", return_value=root):
            tick.fill(args)
    check.assertFalse((root / ".local").exists())
    check.assertIn('would start', output.getvalue())
    check.assertIn('"issue": 51', output.getvalue())
    check.assertEqual(holds.read_text(), '{"dead": {"host": "local"}}')
    launch.assert_not_called()


# Only the chosen upstream base is fetched. A concurrent remote branch blocks the start.
with TemporaryDirectory() as temp, ExitStack() as stack:
    root = Path(temp)
    args = SimpleNamespace(repo_path=root, dry_run=False, issue=51, holds=root / "holds.json")
    calls = []
    raced = False
    def git(*argv):
        calls.append(argv)
        if "--git-path" in argv:
            return str(root / "info/exclude")
        if "get-url" in argv:
            return "git@github.com:julianthant/hr-relay.git"
        if "for-each-ref" in argv:
            return ""
        if "ls-remote" in argv:
            return "abc refs/heads/main" + ("\ndef refs/heads/task/a/lane" if raced and argv[-1] != "origin" else "")
        if "fetch" in argv:
            check.assertEqual(argv[3:], ("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main"))
            return ""
        check.assertEqual(argv[3:], ("rev-parse", "--verify", "origin/main"))
        return "abc"

    stack.enter_context(patch.object(tick, "command", git))
    stack.enter_context(patch.object(tick, "tickets", side_effect=lambda: [dict(ticket)]))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    start = stack.enter_context(patch.object(tick, "launch"))
    stack.enter_context(redirect_stdout(io.StringIO()))
    tick.fill_tick(args, None)
    check.assertEqual(start.call_args.args[2]["sha"], "abc")
    check.assertEqual([c[3] for c in calls][-3:], ["fetch", "ls-remote", "rev-parse"])
    raced = True
    with check.assertRaisesRegex(tick.OrcaError, "base changed or task branch"):
        tick.fill_tick(args, None)
    start.assert_called_once()


# An unresolved attempt still owns its paths and seat; an unrelated ticket can start.
with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
    root = Path(tmp)
    stuck = dict(ticket, number=40, branch="task/a/stuck", paths=["src/occupied"])
    overlap_ticket = dict(ticket, number=41, branch="task/a/overlap", paths=["src/occupied/file"])
    tick.reserve(db, 40, dict(task="stuck", run="different", route=base["route"], account="held"))
    args = SimpleNamespace(repo_path=root, dry_run=False, issue=None, holds=root / "holds.json")
    stack.enter_context(patch.object(tick, "command", git))
    raced = False
    stack.enter_context(patch.object(tick, "tickets", side_effect=lambda: [dict(t) for t in (stuck, overlap_ticket, ticket)]))
    stack.enter_context(patch.object(tick, "supervisor", return_value=([], "own")))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    picker = stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    start = stack.enter_context(patch.object(tick, "launch"))
    output = stack.enter_context(redirect_stdout(io.StringIO()))
    tick.fill_tick(args, db)
    check.assertIn("reconcile from its owning Run", output.getvalue())
    check.assertIn("overlaps #40", output.getvalue())
    check.assertEqual(start.call_args.args[2]["number"], ticket["number"])
    start.assert_called_once()
    check.assertEqual(picker.call_args.kwargs["holds"][0]["account"], "held")
    check.assertEqual(db.execute("SELECT issue FROM starts").fetchall(), [(40,)])

# Release is its own operation and only clears the inspected pending issue.
with TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)), patch.object(tick, "fill_tick") as fill:
    state = Path(tmp) / ".local/state/dev-auto"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "starts.sqlite") as db:
        tick.reserve(db, 40, {"task": "inspected"})
        tick.reserve(db, 41, {"task": "other"})
        db.execute("INSERT INTO starts VALUES(42,'done')")
    with redirect_stdout(io.StringIO()):
        tick.main(["fill", "--release", "40"])
        tick.main(["fill", "--release", "42"])
    fill.assert_not_called()
    with sqlite3.connect(state / "starts.sqlite") as db:
        check.assertEqual(db.execute("SELECT issue FROM starts ORDER BY issue").fetchall(), [(41,), (42,)])

check.assertEqual(tick.owned_paths("Owned paths: Edit `scripts/dev.py` and `docs/how.md`, plus config/app.json."),
                  ["scripts/dev.py", "docs/how.md", "config/app.json"])
check.assertEqual(tick.owned_paths("Owned paths: src, docs, scripts/setup.py."), ["src", "docs", "scripts/setup.py"])
check.assertIn(".github/workflows", tick.owned_paths("Owned paths: Development/checks workflows, CI plan and focused tests."))

with TemporaryDirectory() as tmp:
    import tomllib
    cfg = Path(tmp) / ".codex/config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[profile]\nname="unchanged"\n[notice] # existing notices\nhide_rate_limit_model_nudge = false\n')
    tick.configure_codex_notice(Path(tmp))
    check.assertEqual(tomllib.loads(cfg.read_text()), {"profile": {"name": "unchanged"},
                                                       "notice": {"hide_rate_limit_model_nudge": True}})

    cfg.write_text('notice = { hide_other_banner = true }\n')
    tick.configure_codex_notice(Path(tmp))
    check.assertEqual(tomllib.loads(cfg.read_text())["notice"], {"hide_other_banner": True,
                                                                 "hide_rate_limit_model_nudge": True})

# A Codex worktree keeps its .codex/ settings out of git status, so a coder cannot commit them.
with TemporaryDirectory() as tmp:
    repo, wt = Path(tmp) / "repo", Path(tmp) / "wt"
    run = lambda *argv: subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout
    run("init", "-q", "-b", "main", str(repo))
    run("-C", str(repo), "-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "-q", "--allow-empty", "-m", "base")
    run("-C", str(repo), "worktree", "add", "-q", "-b", "task/x", str(wt))
    for _ in range(2):
        tick.worker_settings(wt, ("codex", "gpt-6.2-sol", "high", None))
    assert (wt / ".codex/config.toml").exists()
    check.assertEqual(run("-C", str(wt), "status", "--porcelain", "--untracked-files=all"), "")
    exclude = Path(run("-C", str(wt), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    check.assertEqual(exclude.read_text().splitlines().count(".codex/"), 1)
    # The exclude is the clone's shared one: .codex/ stays out of the main checkout and every other worktree too.
    other = Path(tmp) / "other"
    run("-C", str(repo), "worktree", "add", "-q", "-b", "task/y", str(other))
    for checkout in (repo, other):
        (checkout / ".codex").mkdir(exist_ok=True)
        (checkout / ".codex/config.toml").write_text("x = 1\n")
        check.assertEqual(run("-C", str(checkout), "status", "--porcelain", "--untracked-files=all"), "")

with patch.object(tick, "command", side_effect=[json.dumps({"id": "project"}), json.dumps({"fields": [
        {"name": "Status", "id": "status", "options": [{"name": "In Progress", "id": "progress"}]},
        {"name": "Agent state", "id": "stage", "options": [{"name": "Coding", "id": "coding"},
                                                           {"name": "Needs you", "id": "waiting"}]},
        {"name": "Agent", "id": "agent"}]})]):
    check.assertEqual(tick.project_fields()[2], ("stage", "waiting"))

# Git owns the fixture worktree and refs; only the Orca transport is simulated.
for interrupted in ("clean", "committed", "untracked", "preexisting"):
    with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
        repo = Path(tmp) / "repo"
        checkout = Path(tmp) / "worker"
        def git_at(path, *argv):
            return subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True, text=True).stdout.strip()
        repo.mkdir()
        git_at(repo, "init", "-b", "main")
        git_at(repo, "config", "user.name", "Fixture")
        git_at(repo, "config", "user.email", "fixture@example.invalid")
        git_at(repo, "commit", "--allow-empty", "-m", "base")
        base_sha = git_at(repo, "rev-parse", "HEAD")
        test_ticket = dict(ticket, base="main", sha=base_sha, route=base["route"])
        branch = test_ticket["branch"]
        if interrupted == "preexisting":
            git_at(repo, "branch", branch, base_sha)
        def worktree_runtime(*argv):
            verb = argv[:2]
            if verb == ("worktree", "create"):
                if interrupted == "preexisting":
                    git_at(repo, "worktree", "add", str(checkout), branch)
                else:
                    git_at(repo, "worktree", "add", "-b", branch, str(checkout), base_sha)
                return {"worktree": {"id": "fixture-tree", "head": base_sha, "path": str(checkout),
                                     "branch": "refs/heads/" + branch}}
            if verb == ("worktree", "rm"):
                check.assertNotIn("--force", argv)
                git_at(repo, "worktree", "remove", str(checkout))
                return {"removed": True}
            if verb == ("account", "list"):
                return {"codex": {"activeAccountId": "coord"}}
            if verb == ("account", "select"):
                if interrupted == "committed":
                    git_at(checkout, "commit", "--allow-empty", "-m", "someone's new commit")
                if interrupted == "untracked":
                    (checkout / "notes").write_text("keep my working copy")
                raise tick.OrcaError("account selection failed")
            raise AssertionError(argv)

        with (patch.object(tick, "runtime", worktree_runtime),
              patch.object(tick, "project_fields", return_value=("p", [], ("s","needs-you"))),
              patch.object(tick, "supervisor", return_value=([], "run")),
              redirect_stdout(io.StringIO()), check.assertRaisesRegex(tick.OrcaError, "account selection failed")):
            tick.launch(db, repo, test_ticket, account, args, repo / "holds.json")
        check.assertEqual(checkout.exists(), interrupted != "clean")
        check.assertEqual(bool(git_at(repo, "branch", "--list", branch)), interrupted != "clean")
        if interrupted == "committed":
            check.assertNotEqual(git_at(checkout, "rev-parse", "HEAD"), base_sha)
        if interrupted == "untracked":
            check.assertEqual((checkout / "notes").read_text(), "keep my working copy")

# Legacy titles provide a branch when the body omits it; explicit branches win.
empty = {"nodes": [], "totalCount": 0}
def issue_row(number, title, body=""):
    return dict(number=number, title=title, body=body, issueType={"name": "Task"}, milestone=None,
                projectItems=empty, assignees=empty, subIssues=empty, closedByPullRequestsReferences=empty,
                labels=empty, blockedBy=empty, blocking=empty)

rows = [issue_row(473, "dashboard-frontend-speed / clock-tick-in-leaves: render labels"),
        issue_row(497, "driver-reads / CRM SSO route recovery: recover sign-in"),
        issue_row(506, "tests-reliability / timing-guesses: condition waits", "Branch: task/tests/explicit")]
with patch.object(tick, "command", return_value=json.dumps([{"data": {"repository": {"issues": {"nodes": rows}}}}])):
    parsed = tick.tickets()
check.assertEqual([row["branch"] for row in parsed], ["task/dashboard-frontend-speed/clock-tick-in-leaves",
                  "task/driver-reads/crm-sso-route-recovery", "task/tests/explicit"])

# Orca identifies agent terminals separately from plain shells and links recovery worktrees to issues.
items = [dict(base, number=row["number"], branch=row["branch"], paths=[str(row["number"])]) for row in parsed]
items.append(dict(base, number=484, branch="task/speed/read", paths=["disjoint"]))
trees = [{"id": "agent", "branch": "refs/heads/" + items[0]["branch"]},
         {"id": "recovery", "branch": "refs/heads/task/driver-reads/crm-sso-recovery", "linkedIssue": 497},
         {"id": "shell", "branch": "refs/heads/" + items[2]["branch"]},
         {"id": "other", "branch": "refs/heads/task/closed/lane"}]
terms = [{"worktreeId": t["id"], "handle": t["id"], **({"agentIdentity": "codex"} if t["id"] != "shell" else {})}
         for t in trees]
with patch.object(tick, "runtime", side_effect=[{"worktrees": trees, "truncated": False},
                                               {"terminals": terms, "truncated": False}]), redirect_stdout(io.StringIO()) as out:
    tick.inventory(Path("/fixture"), items, "hr-relay")
    plan = list(tick.fill_plan(items, lambda *_: (account, []), 484))
check.assertEqual([(t.get("occupied", False), t["busy"]) for t in items],
                  [(True, True), (True, True), (True, False), (False, False)])
check.assertIn("task/closed/lane", out.getvalue())
check.assertEqual([t["number"] for t, _ in plan], [484])

# A task report needs the current remote branch SHA and its own successful task workflow.
review_ticket = {"number": 41, "branch": "task/ui/header", "repo": "hr-relay"}
sha = "a" * 40
def green_check(head, conclusion, body=sha):
    with patch.object(tick, "command", side_effect=[
        head, json.dumps([{"headSha": head, "headBranch": "task/ui/header", "status": "completed",
                              "conclusion": conclusion}])]):
        return tick.green_report(review_ticket, body)
assert green_check(sha, "success") == sha
assert green_check(sha, "failure") is None
assert green_check("b" * 40, "success") is None

# Redacted inbox --full records retain JSON-string payloads even after the owner reads them.
def report(dispatch, body, kind="worker_done", msg_id="report"):
    return {"id": msg_id, "type": kind, "read": 1, "body": body, "subject": "worker report", "sequence": 1,
                "payload": json.dumps({"taskId": "task", "dispatchId": dispatch, "outcome": "succeeded"})}

recorded = report("coder", f"I pushed {sha} with host-local account selection.")
assert tick.review_mail([recorded], "coder") == [recorded]
assert not tick.review_mail([report("coder", sha, "question"), recorded], "other")
assert not tick.review_mail([report("coder", sha, "question")], "coder")

assert tick.review_route("Coder: codex / gpt-6.2-sol / medium. Reviewer: none (UI, simple; green task CI clears it). Tier: UI, simple.") is None
check.assertEqual(tick.review_route("Reviewer: Claude Sonnet 5.5 medium with Opus advisor"),
                  ("claude", "claude-sonnet-5-5", "medium", "opus"))
with check.assertRaises(tick.OrcaError):
    tick.review_route("Reviewer: none (the orchestrator reads the diff) · Tier: easy")

# Drive independent ticks through a reviewer report, restart and duplicate delivery.
def review_fixture():
    ticket = dict(review_ticket, body="Reviewer: claude / claude-sonnet-5-5 / medium", item="card",
                  base="origin/development", route=("codex", "gpt-6.2-sol", "medium", None))
    state = {"coder": "coder", "expected": "coder", "path": "/fixture", "model": "gpt-6.2-sol",
                 "agent": "coder gpt-6.2-sol medium @seat", "history": [], "stage": "Coding"}
    return ticket, state

ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=sha), \
        patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
        patch.object(tick, "worker_settings"), patch.object(tick, "record_hold") as hold, \
        patch.object(tick, "review_card") as card, patch.object(tick, "runtime") as cli:
    cli.side_effect = [{}, {"claude": {"activeAccountId": "seat"}}, full_start("reader", "pane")]
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert "--full" in cli.call_args.args and hold.call_args.args[0]["terminal"] == "pane"
    assert card.call_args.args[1] == "Reviewing" and hold.call_count == 1
    spec = cli.call_args.args[cli.call_args.args.index("--spec") + 1]
    assert spec.splitlines()[1] == "Role: follow ~/.agents/roles/reviewer.md."
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert cli.call_count == 3
    verdict = report("reader", "REVIEW VERDICT: PUSH " + sha + "\nReviewed the diff.", msg_id="v")
    cli.side_effect = [{"delivery": "reopened", "followUpOf": "coder", "dispatchId": "landing"}]
    tick.review_step(db, "run", [], ticket, state, [recorded, verdict])
    assert card.call_args.args[1] == "Merging"
    assert "auto-merge" in cli.call_args.args[-1] and "Closes #41" in cli.call_args.args[-1]

# Every task PR lands on the repository's trunk, whatever base the ticket recorded (feature branches are gone).
for repo, trunk in (("hr-relay", "development"), ("dot-agents", "main")):
    ticket, state = review_fixture()
    ticket.update(base="origin/feature/dashboard-state-tests", repo=repo, body="Reviewer: none. Tier: UI, simple.")
    with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "review_card"), \
            patch.object(tick, "runtime", return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "land"}) as cli:
        tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [recorded])
    landing = cli.call_args.args[-1]
    assert f"merge origin/{trunk}," in landing and f"open or update the PR into {trunk}," in landing, landing

# A no-review route can land only on green. A red report receives a new coder dispatch.
ticket, state = review_fixture()
ticket["body"] = "Reviewer: none (green CI clears it). Tier: UI, simple."
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", side_effect=[None, sha]), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=[{"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"},
                                                 {"delivery": "follow_up", "followUpOf": "fix", "dispatchId": "land"}]) as cli:
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_args.args[1:] == ("Fixing", state["agent"])
    tick.review_step(db, "run", [], ticket, state, [report("fix", sha, msg_id="green")])
    assert card.call_args.args[1:] == ("Merging", state["agent"])
    assert all(call.args[1] == "send" for call in cli.call_args_list)

# A Codex reviewer start gives the coordinator bot its Codex account back once the start is confirmed.
ticket, state = review_fixture()
ticket["body"] = "Reviewer: codex / gpt-6.2-astra / medium"
with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "worker_settings"), \
        patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
        patch.object(tick, "record_hold"), patch.object(tick, "review_card"), \
        patch.object(pick, "coordinator", return_value=None), patch.object(pick.account_usage, "load_config", return_value={}), \
        patch.object(tick, "runtime") as cli:
    cli.side_effect = [{"codex": {"activeAccountId": "coord"}}, {}, {"codex": {"activeAccountId": "seat"}},
                       full_start("reader", "pane"), {}, {"codex": {"activeAccountId": "coord"}}]
    tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [recorded])
    assert [c.args[-1] for c in cli.call_args_list if c.args[:2] == ("account", "select")] == ["seat", "coord"]

# The coordinator moves to pick.coordinator's choice when its own account is past handoff; a race is reported.
for fresh, listed in ((None, "coord"), ({"id": "fresh"}, "fresh"), (None, "other")):
    with patch.object(pick, "coordinator", return_value=fresh) as choose, \
            patch.object(pick.account_usage, "load_config", return_value={}), \
            patch.object(tick, "runtime", side_effect=[{}, {"codex": {"activeAccountId": listed}}]) as cli:
        if listed == "other":
            with check.assertRaisesRegex(tick.OrcaError, "coordinator account"):
                tick.restore_coordinator("coord")
        else:
            tick.restore_coordinator("coord")
        assert choose.call_args.args[3] == "coord"
        assert cli.call_args_list[0].args == ("account", "select", "--agent", "codex", "--account",
                                              (fresh or {}).get("id") or "coord")
tick.restore_coordinator(None)  # a Claude start never touches the Codex account

# A coder report refreshes the card's seat from the reporting terminal's hold.
ticket, state = review_fixture()
state["coder_terminal"] = "pane-c"
with TemporaryDirectory() as tmp, patch.object(pick, "HOLDS", Path(tmp) / "holds.json"), \
        patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"}):
    pick.HOLDS.write_text(json.dumps({"pane-c": {"label": "fresh", "host": "local"}}))
    tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [recorded])
    assert card.call_args.args[1:] == ("Fixing", "coder gpt-6.2-sol medium @fresh")

# A manual retry on a new pane, with review state already saved, is recognized by its retry lineage and
# refreshes the card from its own hold; another Task's report on a third pane is ignored.
ticket, state = review_fixture()
state.update(coder_terminal="pane-old")
retried = dict(report("retry", "red at the old head", msg_id="retry-report"), from_handle="pane-new", sequence=3)
stranger = dict(report("other", "unrelated", msg_id="other-report"), from_handle="pane-x", sequence=2,
                payload=json.dumps({"taskId": "other-task", "dispatchId": "other", "outcome": "succeeded"}))
def lineage(*args):
    if args[:2] == ("orchestration", "worker-show"):
        return {"dispatch": {"id": args[3], "runId": "run", "taskId": "task",
                             "retryOfDispatchId": {"retry": "coder"}.get(args[3]), "followUpOf": None}}
    assert args[:2] == ("orchestration", "send") and "dispatch:retry" in args, args
    return {"delivery": "follow_up", "followUpOf": "retry", "dispatchId": "fix"}
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE review_mail (run TEXT, id TEXT, PRIMARY KEY(run,id))")
db.execute("INSERT INTO review_mail VALUES ('run','report')")
with TemporaryDirectory() as tmp, patch.object(pick, "HOLDS", Path(tmp) / "holds.json"), \
        patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=lineage) as cli:
    pick.HOLDS.write_text(json.dumps({"pane-old": {"label": "old", "host": "local"},
                                      "pane-new": {"label": "fresh", "host": "local"}}))
    tick.review_step(db, "run", [], ticket, state, [recorded, stranger, retried])
    assert card.call_args.args[1:] == ("Fixing", "coder gpt-6.2-sol medium @fresh")
    saved = tick.review_state(db, "run", 41)
    assert (saved["coder"], saved["coder_terminal"]) == ("fix", "pane-new")
    assert not any("other" in call.args for call in cli.call_args_list)

# A reviewer's failed worker_done needs the operator, unless watch is restarting it from a handoff.
for handoff in (False, True):
    ticket, state = review_fixture()
    state.update(stage="Reviewing", expected="reader", sha=sha, reviewer={"dispatch": "reader", "terminal": "pane-r"})
    failed = dict(report("reader", "Stopping: cannot read the diff.", msg_id="f1"),
                  payload=json.dumps({"taskId": "task", "dispatchId": "reader", "outcome": "failed"}))
    db = sqlite3.connect(":memory:")
    if handoff:
        db.execute("CREATE TABLE watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
        db.execute("INSERT INTO watch VALUES ('run','pane-r',?)", (json.dumps({"handoff": 1}),))
    with patch.object(tick, "review_card") as card, patch.object(tick, "runtime") as cli:
        for _ in range(2):
            tick.review_step(db, "run", [], ticket, state, [failed])
        cli.assert_not_called()
        if handoff:
            card.assert_not_called()
        else:
            assert card.call_count == 1
            assert card.call_args.args[1:] == ("Needs you", "coordinator · reviewer failed f1")

# Third FIX sends all findings to the owning Run, once, and retires the ticket.
ticket, state = review_fixture()
for i in range(3):
    db = sqlite3.connect(":memory:")
    state.update(stage="Reviewing", expected="reader", sha=sha, history=["round one", "round two"][:i])
    with patch.object(tick, "review_card") as card, patch.object(tick, "runtime",
            return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"}) as cli:
        verdict = report("reader", "REVIEW VERDICT: FIX " + sha + "\nmissing a check")
        tick.review_step(db, "run", [], ticket, state, [verdict])
        if i < 2:
            assert card.call_args.args[1] == "Fixing" and "dispatch:coder" in cli.call_args.args
        else:
            assert card.call_args.args[1] == "Needs you" and "run:run" in cli.call_args.args
            # Orca refuses --type escalation from the Run's own pane (sender_not_assignee).
            sent = cli.call_args.args
            assert sent[:2] == ("orchestration", "send") and sent[sent.index("--type") + 1] == "status"
            assert all(text in cli.call_args.args[-1] for text in ["round one", "round two", "missing a check"])
        tick.review_step(db, "run", [], ticket, state, [verdict])
        assert cli.call_count == 1

# A PUSH whose head moved or whose CI is not green goes back to the coder, saying why.
ticket, state = review_fixture()
state.update(stage="Reviewing", expected="reader", sha=sha)
with patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"}) as cli:
    tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [report("reader", "REVIEW VERDICT: PUSH " + sha)])
    assert card.call_args.args[1] == "Fixing" and "dispatch:coder" in cli.call_args.args
    assert (f"The reviewer's PUSH of {sha} was voided: the branch head moved or its task CI is not green.\n"
            + tick.GREEN_INSTRUCTIONS) in cli.call_args.args[-1]

ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=None), patch.object(tick, "runtime",
        side_effect=tick.OrcaError("lost receipt")) as cli:
    with check.assertRaisesRegex(tick.OrcaError, "lost receipt"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    with check.assertRaisesRegex(tick.OrcaError, "inspect uncertain"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    assert cli.call_count == 1

# Load the coordinator's handled mail through the review command, using fill's task title.
args = SimpleNamespace(issue=41, coder=None, run=None, anchor=None)
ticket, state = review_fixture()
ticket.update(agent=state["agent"], stage="Coding")
with patch.object(tick, "tickets", return_value=[ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
        patch.object(tick, "command", return_value="https://github.com/julianthant/hr-relay.git\n"), \
        patch.object(tick, "review_step") as step, patch.object(tick, "runtime") as cli:
    cli.side_effect = [{"messages": [recorded]}, {"tasks": [{"title": "#41 coder", "id": "task"}]},
                       {"dispatch": {"id": "coder"}}, {"dispatch": {"id": "coder", "runId": "run", "taskId": "task"},
                           "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {"model": "gpt-6.2-sol"}}}}},
                       {"worktree": {"id": "tree", "path": "/fixture", "branch": "refs/heads/task/ui/header"}}]
    tick.review_tick(args, sqlite3.connect(":memory:"))
    assert step.call_args.args[-1] == [recorded] and step.call_args.args[-2]["expected"] == "coder"
    assert cli.call_args_list[0].args == ("orchestration", "inbox", "--terminal", "run:run", "--limit", "10000", "--full")

# An explicit coder that reports to another Run fails loudly instead of exiting quietly.
args = SimpleNamespace(issue=41, coder="ctx_elsewhere", run=None, anchor=None)
with patch.object(tick, "tickets", return_value=[ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
        patch.object(tick, "review_step") as step, patch.object(tick, "runtime") as cli:
    cli.side_effect = [{"messages": [recorded]}, {"tasks": []},
                       {"dispatch": {"id": "ctx_elsewhere", "runId": "run_other", "taskId": "task"}}]
    with check.assertRaisesRegex(tick.OrcaError, "ctx_elsewhere belongs to Run run_other, not the bound Run run"):
        tick.review_tick(args, sqlite3.connect(":memory:"))
    step.assert_not_called()

# A partial Project write resumes on restart without a second follow-up.
ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card",
        side_effect=[tick.OrcaError("Project unavailable"), None]) as card, patch.object(tick, "runtime",
        return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"}) as cli:
    with check.assertRaisesRegex(tick.OrcaError, "Project unavailable"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_count == 2 and cli.call_count == 1

# Only the multi-step UI advisor pairing may reuse the coder model.
ticket, state = review_fixture()
advisor = ("claude", "claude-sonnet-5-5", "medium", "opus")
ticket.update(route=advisor, body="Tier: UI, multi-step\nReviewer: Claude Sonnet 5.5 medium with Opus advisor")
state["model"] = advisor[1]
for tier, accepted in [("backend, hard", False), ("UI, multi-step", True)]:
    trial = dict(ticket, body=ticket["body"].replace("UI, multi-step", tier))
    with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "review_account",
            return_value={"id": "seat", "label": "review"}), patch.object(tick, "worker_settings"), patch.object(
            tick, "record_hold"), patch.object(tick, "review_card"), patch.object(tick, "runtime",
            side_effect=[{}, {"claude": {"activeAccountId": "seat"}}, full_start("reader", "pane")]):
        db = sqlite3.connect(":memory:")
        if accepted:
            tick.review_step(db, "run", [], trial, state, [recorded])
            assert tick.review_state(db, "run", 41)["expected"] == "reader"
        else:
            with check.assertRaisesRegex(tick.OrcaError, "different model"):
                tick.review_step(db, "run", [], trial, state, [recorded])

# Recover a pre-upgrade reservation whose start succeeded, then retire that reviewer on FIX.
import pick
ticket, state = review_fixture()
ticket.update(stage="Reviewing", agent="coder · reviewer claude-sonnet-5-5 medium @review")
state.update(pending=recorded["id"], sha=sha)
spec = (f"/goal Review {sha} and return its verdict.\nRole: follow ~/.agents/roles/reviewer.md.\n"
        f"Issue julianthant/hr-relay#41. Branch task/ui/header, base origin/development.\nReview the issue's checks at {sha}.")
task = {"id": "task", "run_id": "run", "task_title": "#41 reviewer", "spec": spec, "created_at": "2026-11-22 22:36:09"}
show = {"dispatch": {"id": "reader", "runId": "run", "taskId": "task", "assigneeHandle": "pane"},
        "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {
            "agent": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}}}}}
db = sqlite3.connect(":memory:")
tick.review_state(db, "run", 41)
tick.save_review(db, "run", 41, state)
db.execute("CREATE TABLE review_mail (run TEXT, id TEXT, PRIMARY KEY(run,id))")
db.execute("INSERT INTO review_mail VALUES ('run','report')")
with TemporaryDirectory() as tmp, patch.object(pick, "HOLDS", Path(tmp) / "holds.json"), patch.object(tick, "review_card") as card:
    pick.HOLDS.write_text(json.dumps({"pane": {"account": "seat", "label": "review", "model": "claude-sonnet-5-5",
                                              "agent": "claude", "effort": "medium", "host": "local", "at": 1}}))
    with patch.object(tick, "runtime", return_value={"tasks": [task, dict(task, id="other")]}) as cli:
        with check.assertRaisesRegex(tick.OrcaError, "inspect uncertain"):
            tick.review_step(db, "run", [], ticket, state, [dict(recorded, created_at="2026-11-22T22:35:40Z")])
        assert cli.call_count == 1 and tick.review_state(db, "run", 41)["pending"] == recorded["id"]
    with patch.object(tick, "runtime", side_effect=[{"tasks": [task]}, {"dispatch": {"id": "reader"}}, show,
                                                 {"worktree": {"id": "tree", "path": "/fixture"}}]) as cli:
        tick.review_step(db, "run", [], ticket, state, [dict(recorded, created_at="2026-11-22T22:35:40Z")])
        assert all(call.args[1] not in ("worker-start", "send") for call in cli.call_args_list)
        assert tick.review_state(db, "run", 41)["expected"] == "reader"
    fix = report("reader", f"REVIEW VERDICT: FIX {sha}\nAdd a regression test.", msg_id="fix")
    with patch.object(tick, "runtime", side_effect=[{"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "coder2"},
                                                 {"state": "released"}]) as cli:
        tick.review_step(db, "run", [], ticket, state, [fix])
        tick.review_step(db, "run", [], ticket, state, [fix])
        assert cli.call_args.args == ("orchestration", "worker-release", "--dispatch", "reader")
        assert cli.call_count == 2 and "pane" not in json.loads(pick.HOLDS.read_text())

# Malformed verdicts leave a single actionable Project update with a mailbox reference.
ticket, state = review_fixture()
state.update(stage="Reviewing", expected="reader", sha=sha)
db = sqlite3.connect(":memory:")
with patch.object(tick, "review_card") as card, patch.object(tick, "runtime") as cli:
    invalid = report("reader", "PUSH", msg_id="bad-verdict")
    for _ in range(2):
        tick.review_step(db, "run", [], ticket, state, [invalid])
    assert card.call_count == 1 and card.call_args.args[1] == "Needs you"
    assert "bad-verdict" in card.call_args.args[2]
    cli.assert_not_called()

# Recorded Run history: a manual coder follow-up intervened before the tick's send.
root, manual, delivered = "first-coder", "manual-fix", "return-green"
verdict = report("reader", f"REVIEW VERDICT: PUSH {sha}", msg_id="push-verdict")
verdict["created_at"] = "2026-11-22T22:37:23Z"
follow_task = {"id": "green-task", "run_id": "run", "task_title": "#41: Fixing",
               "created_at": "2026-11-22 22:42:30", "spec": "/goal Return a green task SHA.\n"
               "Role: follow ~/.agents/roles/coder.md.\nIssue julianthant/hr-relay#41. Branch task/ui/header, "
               "base origin/development.\nReport the pushed branch head as a full SHA after its task CI is green."}
def ancestry(*args):
    if args[1] == "worker-show":
        dispatch = args[3]
        return {"dispatch": {"id": dispatch, "runId": "run", "taskId": "green-task" if dispatch == delivered else "manual",
                             "followUpOf": {manual: root, delivered: manual}.get(dispatch)}}
    if args[1] == "task-list":
        return {"tasks": [follow_task]}
    if args[1] == "dispatch-show":
        return {"dispatch": {"id": delivered}}
    raise AssertionError(args)

with patch.object(tick, "runtime", side_effect=ancestry):
    receipt = {"delivery": "follow_up", "followUpOf": manual, "dispatchId": delivered}
    assert tick.followup_dispatch(receipt, root, "run") == delivered
    with check.assertRaises(tick.OrcaError):
        tick.followup_dispatch(receipt, "unrelated", "run")

for release_first in (False, True):
    ticket, state = review_fixture()
    state.update(stage="Reviewing", coder=root, expected="reader", sha=sha, pending=verdict["id"])
    with TemporaryDirectory() as tmp, patch.object(tick.Path, "home", return_value=Path(tmp)), patch.object(
            tick, "supervisor", return_value=([], "run")), patch.object(tick, "runtime", side_effect=ancestry) as cli:
        directory = Path(tmp) / ".local/state/dev-auto"
        directory.mkdir(parents=True)
        with sqlite3.connect(directory / "starts.sqlite") as db:
            tick.review_state(db, "run", 41)
            tick.save_review(db, "run", 41, state)
            tick.save_review(db, "another-run", 41, state)
            db.execute("CREATE TABLE review_mail (run TEXT, id TEXT, PRIMARY KEY(run,id))")
            db.execute("INSERT INTO review_mail VALUES ('run','push-verdict')")
            db.commit()
            if release_first:
                tick.main(["review", "--release", "41"])
                assert "pending" not in tick.review_state(db, "run", 41)
                assert tick.review_state(db, "another-run", 41)["pending"] == verdict["id"]
                cli.assert_not_called()
            with patch.object(tick, "review_card") as card:
                tick.review_step(db, "run", [], ticket, state, [verdict])
                assert tick.review_state(db, "run", 41)["expected"] == delivered
                assert card.call_args.args[1] == "Fixing"
            green = report(delivered, "I verified the pushed head is " + sha, msg_id="green-task-result")
            with patch.object(tick, "green_report", return_value=sha) as verify, patch.object(tick, "review_account",
                    side_effect=tick.OrcaError("seat unavailable")):
                with check.assertRaisesRegex(tick.OrcaError, "seat unavailable"):
                    tick.review_step(db, "run", [], ticket, state, [verdict, green])
                verify.assert_called_once()
            assert not any(c.args[1] in ("worker-start", "send") for c in cli.call_args_list)

# The newest manual follow-up can report before the tick reads the older green report.
ticket, state = review_fixture()
old = dict(report(root, "older head"), from_handle="coder-pane")
new = dict(report(manual, sha, msg_id="manual-result"), from_handle="coder-pane", sequence=2)
state.update(coder=root, expected=root, coder_terminal="coder-pane")
with patch.object(tick, "runtime", side_effect=ancestry), patch.object(tick, "green_report", return_value=sha) as verify, \
        patch.object(tick, "review_account", side_effect=tick.OrcaError("no reviewer seat")):
    with check.assertRaisesRegex(tick.OrcaError, "no reviewer seat"):
        tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [new, old])
    assert sha in verify.call_args.args[1]

def close_fixture(tmp, *, merged=True, dirty=False, refused=False, release_uncertain=False, remote_race=False,
                  release_unknown=False, ticks=1):
    root = Path(tmp)
    path = str(root / "task-tree")
    branch = "task/dev-auto/example"
    ref = "refs/heads/" + branch
    worktree = "repo::" + path
    holds = root / "holds.json"
    handles = {"term_coder", "term_reviewer"}
    holds.write_text(json.dumps({handle: {"host": "local"} for handle in (*handles, "term_other")}))
    args = SimpleNamespace(repo_path=root, issue=41, holds=holds)
    card = dict(id="card", agent={"text": "coder"}, stage={"name": "Merging"},
                project={"number": 5, "owner": {"login": "julianthant"}})
    pr = dict(merged=True, headRefName=branch, headRefOid=sha, isCrossRepository=False,
              repository={"nameWithOwner": "julianthant/dot-agents"})
    issue = dict(number=41, title="dev-auto / example", body=f"Branch: dot-agents `{branch}`",
                 projectItems={"totalCount": 1, "nodes": [card]},
                 closedByPullRequestsReferences={"totalCount": int(merged), "nodes": [pr] if merged else []})
    # Projected fields from the #551 worker-list and worker-show receipts.
    workers = [dict(dispatchId=dispatch, runId="run", agentTerminalHandle=terminal if owns else None,
                    dispatchStatus="completed", projection={"workspace": {"id": worktree}, "host": {"kind": "local"}})
               for dispatch, terminal, owns in (("ctx_merge", "term_coder", True),
                                                ("ctx_review", "term_reviewer", True),
                                                ("ctx_initial", "term_coder", False))]
    events = []
    tree = dict(id=worktree, path=path, branch=ref, isMainWorktree=False)
    present = True
    local_ref = sha
    remote_present = remote_race

    def cli(*args):
        nonlocal local_ref, remote_present
        events.append(args)
        if args[:2] == ("gh", "api"):
            return json.dumps([{"data": {"repository": {"issues": {"nodes": [issue]}}}}])
        if args[0] == "gh":
            assert args[1:3] == ("project", "item-edit")
            return ""
        if args[3:5] == ("remote", "get-url"):
            return "https://github.com/julianthant/dot-agents.git\n"
        if args[3:5] == ("worktree", "list"):
            return f"worktree {root}\0HEAD {sha}\0branch refs/heads/main\0\0" + (
                f"worktree {path}\0HEAD {sha}\0branch {ref}\0\0" if present else "")
        if args[3] == "status":
            return "?? notes.txt\n" if dirty else ""
        if args[3] == "ls-remote":
            return f"{sha}\t{ref}\n" if remote_present else ""
        if args[3] == "push":
            assert remote_race and not present
            assert args[4:] == (f"--force-with-lease={ref}:{sha}", "origin", ":" + ref)
            remote_present = False  # GitHub deletes the branch between lookup and deletion.
            raise tick.OrcaError("remote ref does not exist")
        if args[3] == "for-each-ref":
            return f"{local_ref} {ref}\n" if local_ref else ""
        if args[3] == "update-ref":
            assert args[4:] == ("-d", ref, sha)
            local_ref = ""
            return ""
        raise AssertionError(args)

    def orca(*args):
        nonlocal present
        events.append(args)
        if args[:2] == ("worktree", "list"):
            return {"truncated": False, "worktrees": [tree] if present else []}
        if args[:2] == ("orchestration", "worker-list"):
            if "--cursor" in args:
                return {"workers": workers[2:], "page": {"hasMore": False, "nextCursor": None}}
            return {"workers": workers[:2], "page": {"hasMore": True, "nextCursor": "recorded-cursor"}}
        if args[:2] == ("orchestration", "worker-show"):
            return {"dispatch": {"id": "ctx_initial", "runId": "run", "assigneeHandle": "term_coder"},
                    "worker": {"dispatchId": "ctx_initial", "worktreeId": worktree, "effects": [
                        {"kind": "terminal", "role": "agent", "action": "reused", "id": "term_coder"}]}}
        if args[:2] == ("orchestration", "worker-release"):
            if release_uncertain and args[3] == "ctx_review":
                return {"state": "release_pending"}
            if release_unknown and args[3] == "ctx_review":
                # After a restart the recorded handle no longer resolves; the CLI exits 1 for release_unknown.
                raise tick.OrcaError('orca orchestration failed: {"ok": true, "result": {"state": "release_unknown"}}')
            return {"ctx_merge": {"state": "released", "reason": "closed_agent_terminal"},
                    "ctx_review": {"state": "already_released"},
                    "ctx_initial": {"state": "retained", "reason": "ownership_transferred"}}[args[3]]
        if args[:2] == ("terminal", "list"):
            return {"truncated": False, "terminals": []}
        if args[:2] == ("worktree", "rm"):
            assert args == ("worktree", "rm", "--worktree", "id:" + worktree)
            if refused:
                raise tick.OrcaError("untracked leftovers")
            present = False
            return {"removed": True}
        if args[:2] == ("orchestration", "send"):
            return {}
        raise AssertionError(args)

    with sqlite3.connect(":memory:") as db, patch.object(pick, "HOLDS", holds), \
            patch.object(tick, "supervisor", return_value=([], "run")), \
            patch.object(tick, "project_fields", return_value=("project", [("status", "progress"),
                         ("stage", "coding"), ("agent", None)], ("stage", "needs"))), \
            patch.object(tick, "runtime", side_effect=orca), patch.object(tick, "command", side_effect=cli), \
            redirect_stdout(io.StringIO()) as output:
        for _ in range(ticks):
            tick.close_tick(args, db)
    assert [event[3] for event in events if event[:2] == REL] == ["ctx_merge", "ctx_review", "ctx_initial"] * ticks
    expected_holds = {"term_other", "term_reviewer"} if release_uncertain else {"term_other"}
    assert set(json.loads(holds.read_text())) == expected_holds
    edits = [event[-3:] for event in events if event[:3] == ("gh", "project", "item-edit")]
    if merged and not dirty and not refused and not release_uncertain:
        assert not present and not local_ref
        assert edits == [("--field-id", "agent", "--clear"), ("--field-id", "stage", "--clear")]
        assert any(event[0] == "git" and event[3] == "push" for event in events) == remote_race
    else:
        assert present and local_ref == sha
        assert edits == [("stage", "--single-select-option-id", "needs")]
        assert "Needs you" in output.getvalue()
        sends = [event for event in events if event[:2] == ("orchestration", "send")]
        assert len(sends) == 1 and all("--type" in e and e[e.index("--type") + 1] == "status"
                             and e[e.index("--to") + 1] == "run:run" for e in sends), sends
        assert any(event[:2] == RM for event in events) == refused


for scenario in ({}, {"merged": False}, {"dirty": True}, {"refused": True},
                 {"release_uncertain": True}, {"remote_race": True}, {"release_unknown": True},
                 {"release_uncertain": True, "ticks": 2}):
    with TemporaryDirectory() as tmp:
        close_fixture(tmp, **scenario)


# Screens recorded from live workers. Codex 2026-11-22 at and near its limit; Claude from the retired dev_auto.py tests.
CODEX_AT_LIMIT = [
    "■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit",
    "  https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 6:49 PM.",
    "",
    "  Approaching rate limits",
    "› 1. Switch to gpt-6-luna   Uses fewer credits for upcoming turns",
    "  2. Keep current model",
    "  3. Keep current model (never show again)   Hide future rate limit reminders about switching models",
]
CODEX_NEAR_LIMIT = [
    "• Ran python3 test_tick.py",
    "⚠ Heads up, you have less than 5% of your 5h limit left.",
    "› Ask Codex to do anything",
    "  gpt-6.2-astra medium · ~/work/repo",
    "  ⚠ 5h limit: only 4% left · resets at 5:57 PM",
]
CODEX_SWITCHED = ["• Working (12s • esc to interrupt)", "› Ask Codex to do anything",
                  "  GPT-6-Luna medium · ~/work/repo"]
# Claude Code 2.1.330's wording, read from its installed binary: "You've hit your session limit" (5-hour) and
# "Usage limit reached · set to continue automatically ..."; its goal status line stays under the limit line.
CLAUDE_CURRENT = ["⏺ Running the tests.", "  ⎿  You've hit your session limit · resets 5am (America/Los_Angeles)",
                  "⏺ Goal paused · usage limit reached · resets 5am"]
assert tick.limited(CLAUDE_CURRENT) == CLAUDE_CURRENT[1].strip()
assert tick.limited(["Usage limit reached · set to continue automatically at 5am"])
assert tick.limited([*CLAUDE_CURRENT, "⏺ Back at work."]) is None
CLAUDE_AT_LIMIT = ["⏺ Running the tests.", "  ⎿  You've hit your 5-hour limit · resets 5am (America/Los_Angeles)",
                   "Usage limit reached · continuing automatically at 5am · esc to cancel"]
CLAUDE_WORKING = ["⏺ Reading tick.py", "✶ Prestidigitating… (5m 45s · ↓ 2.8k tokens)", "❯",
                  "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"]
assert tick.screen_limit(CODEX_AT_LIMIT, {"agent": "codex", "model": "gpt-6.2-astra"}).startswith("■ You've hit")
assert tick.screen_limit(CODEX_AT_LIMIT[3:], {"agent": "codex", "model": "gpt-6.2-astra"}) == "Approaching rate limits"
assert tick.screen_limit(CODEX_SWITCHED, {"agent": "codex", "model": "gpt-6.2-astra"}) == "model switched to GPT-6-Luna medium"
assert tick.screen_limit(CODEX_NEAR_LIMIT, {"agent": "codex", "model": "gpt-6.2-astra"}) is None
assert tick.screen_limit(CLAUDE_AT_LIMIT, {"agent": "claude", "model": "claude-opus-5-5"}).startswith("Usage limit reached")
assert tick.screen_limit(CLAUDE_AT_LIMIT[:2], {"agent": "claude", "model": "claude-opus-5-5"}).startswith("⎿  You've hit")
assert tick.screen_limit(CLAUDE_WORKING, {"agent": "claude", "model": "claude-opus-5-5"}) is None
assert [l for l in CODEX_NEAR_LIMIT if tick.NEAR_LIMIT.match(l)] == CODEX_NEAR_LIMIT[1:2] + CODEX_NEAR_LIMIT[-1:]
assert tick.card_agent("coder gpt-6.2-astra medium @jzaw", False, "x2") == "coder gpt-6.2-astra medium @x2"
assert tick.card_agent("coder m e @a · reviewer m e @b", True, "c") == "coder m e @a · reviewer m e @c"


class Fleet:
    """One coder in the coordinating Run, shaped like the recorded #553 worker-list and worker-show receipts."""

    def __init__(self, tmp, agent="codex", model="gpt-6.2-astra", screen=CODEX_NEAR_LIMIT):
        self.tmp = Path(tmp)
        self.holds = self.tmp / "holds.json"
        self.holds.write_text(json.dumps({"term_coder": {"account": "jzaw", "label": "jzaw", "host": "local"}}))
        self.agent, self.model, self.screen = agent, model, screen
        self.dispatch, self.terminal, self.status, self.state = "ctx_coder", "term_coder", "dispatched", "ready"
        self.liveness, self.failure, self.wait = "live", None, None
        self.seats = ["jzaw", "x2"]  # the limited account is first in accounts.toml order
        self.superseded = False
        self.start_error = None
        self.lose_start = False  # Orca starts the retry, but the response is lost
        self.note_error = None
        self.active, self.fresh = "coord", None  # the coordinator bot's Codex account; pick.coordinator's choice
        self.events, self.cards, self.now = [], [], 1_795_389_500
        self.ticket = dict(number=553, item="card", agent=f"coder {model} medium @jzaw", stage="Coding")
        self.title = "#553 coder"
        self.notes = []
        self.tickets_error = None
        self.db = sqlite3.connect(":memory:")

    def orca(self, *args):
        self.events.append(args)
        verb = args[:2]
        if verb == ("orchestration", "task-list"):
            return {"tasks": [{"id": "task_coder", "run_id": "run", "task_title": self.title,
                               "spec": "/goal Implement julianthant/hr-relay#553 and report a green SHA."}]}
        if verb == ("orchestration", "worker-list"):
            row = dict(dispatchId=self.dispatch, taskId="task_coder", runId="run", workerState=self.state,
                       dispatchStatus=self.status, agentTerminalHandle=self.terminal,
                       resource={"ownerDispatchId": self.dispatch},
                       projection={"host": {"kind": "local"}, "liveness": {"verdict": self.liveness}})
            # An older attempt on the same pane is history.
            old = dict(row, dispatchId="ctx_older", dispatchStatus="failed", resource={"ownerDispatchId": "ctx_older"})
            # A follow-up's predecessor can be listed while the pane belongs to the newer Dispatch.
            before = [dict(row, dispatchId="ctx_followed", resource={"ownerDispatchId": self.dispatch})] if self.superseded else []
            # The Task's earlier attempt on its own, older pane is history too.
            retried = dict(row, dispatchId="ctx_first", dispatchStatus="failed", workerState="failed",
                           agentTerminalHandle="term_first", resource={"ownerDispatchId": "ctx_first"})
            # After a retry, the replaced attempt stays listed behind it.
            replaced = [dict(row, dispatchId="ctx_coder", dispatchStatus="failed", workerState="stopped",
                             agentTerminalHandle="term_coder", resource={"ownerDispatchId": "ctx_coder"})
                        ] if self.dispatch == "ctx_restart" else []
            return {"workers": [*before, row, old, retried, *replaced], "page": {"hasMore": False, "nextCursor": None}}
        if verb == ("orchestration", "dispatch-show"):
            # Recorded shape: only the newest Dispatch's id and status.
            return {"dispatch": {"id": self.dispatch, "status": self.status, "taskId": None, "assigneeHandle": None}}
        if verb == ("orchestration", "worker-show"):
            assert args[3] == self.dispatch, args
            retry = "ctx_coder" if self.dispatch == "ctx_restart" else None
            return {"dispatch": {"id": self.dispatch, "runId": "run", "taskId": "task_coder", "status": self.status,
                                 "assigneeHandle": self.terminal, "lastFailure": self.failure,
                                 "retryOfDispatchId": retry, "followUpOf": None},
                    "worker": {"state": self.state, "worktreeId": "repo::/wt", "dispatchId": self.dispatch,
                               "effects": [{"kind": "terminal", "role": "agent", "action": "created", "id": self.terminal}],
                               "startOptions": {"launch": {"effective": {"agent": self.agent, "model": self.model,
                                                                         "effort": "medium"}}}},
                    "projection": {"liveness": {"verdict": self.liveness}},
                    "terminal": {"handle": self.terminal, "agentWait": self.wait}, "observation": {"agentWait": self.wait}}
        if verb == ("terminal", "read"):
            return {"terminal": {"handle": self.terminal, "tail": self.screen, "source": "screen"}}
        if verb == ("orchestration", "worker-stop"):
            self.state, self.status, self.failure = "stopped", "failed", "stopped"
            return {"dispatchId": self.dispatch, "processAction": "closed_agent_terminal"}
        if verb == ("account", "select"):
            self.active = args[-1]
            return {}
        if verb == ("account", "list"):
            return {self.agent: {"activeAccountId": self.active}}
        if verb == ("orchestration", "worker-start"):
            if self.start_error:
                raise self.start_error
            # The same Task retries: no second same-titled Task for review discovery to refuse.
            assert args[args.index("--task") + 1] == "task_coder" and args[args.index("--retry-of") + 1] == self.dispatch
            assert "--spec" not in args and "--task-title" not in args and self.status == "failed", args
            assert args[args.index("--worktree") + 1] == "id:repo::/wt"
            self.dispatch, self.terminal = "ctx_restart", "term_restart"
            self.status, self.state, self.liveness, self.failure = "dispatched", "ready", "live", None
            self.screen = CLAUDE_WORKING
            if self.lose_start:
                raise tick.OrcaError("timed out")
            return full_start("ctx_restart", "term_restart")
        if verb == ("orchestration", "send"):
            if args[args.index("--to") + 1] == "dispatch:ctx_restart":
                if self.note_error:
                    raise self.note_error
                self.notes.append(args[args.index("--body") + 1])
            return {"delivery": "queued"}
        raise AssertionError(args)

    def tick(self, minutes=0):
        self.now += minutes * 60
        self.events.clear()
        seat = lambda cfg, *route, now=None: (
            (next(({"id": s, "label": s} for s in self.seats if s in [r["id"] for r in cfg["account"]]), None), ["full"]))
        cfg = {"account": [{"id": "jzaw"}, *({"id": s} for s in self.seats)]}
        record = lambda ticket, stage, agent: (self.cards.append((stage, agent)), ticket.update(agent=agent))
        out = io.StringIO()
        with patch.object(tick, "supervisor", return_value=([], "run")), patch.object(tick, "runtime", self.orca), \
                patch.object(tick, "tickets", side_effect=self.tickets_error, return_value=[self.ticket]), patch.object(tick, "review_card", record), \
                patch.object(pick, "main", return_value=0), patch.object(pick, "due", return_value=[]), \
                patch.object(pick, "pick", seat), patch.object(pick.account_usage, "load_config", return_value=cfg), \
                patch.object(pick, "coordinator", return_value=self.fresh and {"id": self.fresh}), \
                patch.object(tick.time, "time", return_value=self.now), redirect_stdout(out):
            try:
                tick.watch_tick(SimpleNamespace(holds=self.holds), self.db)
            finally:
                self.lines = out.getvalue().splitlines()
        assert len(self.lines) == 1 and self.lines[0].startswith("watch"), self.lines
        return [e[:2] for e in self.events]

    def sent(self):
        return [(e[e.index("--to") + 1], e[e.index("--subject") + 1]) for e in self.events
                if e[:2] == ("orchestration", "send") and e[e.index("--to") + 1] != "dispatch:ctx_restart"]


# Near its limit: one handoff request, Handed off on the card, then the same Task retries on another seat.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder",
                                        "agent": "coder gpt-6.2-astra medium @x1"})
    f.tick()
    assert f.sent() == [("dispatch:ctx_coder", "#553: hand off now")] and f.cards == [("Handed off", f.ticket["agent"])]
    assert "handoff" in f.lines[0]
    f.holds.write_text("{}")  # a pruned hold: the remembered account is still excluded
    f.tick(1)
    assert f.sent() == [] and len(f.cards) == 1  # asked once
    f.status, f.state = "failed", "failed"
    # A recorded failed worker_done receipt: the checkpoint is in reportPath, the body is generic.
    f.failure = json.dumps({"provenance": "worker_report", "outcome": "failed", "subject": "handed off",
                            "body": "Stopping at the usage limit.", "reportPath": "/wt/.handoff-553.md"})
    calls = f.tick(1)
    assert ("orchestration", "worker-start") in calls and ("orchestration", "worker-stop") not in calls
    assert len(f.notes) == 1 and "/wt/.handoff-553.md" in f.notes[0] and "Stopping at the usage limit." in f.notes[0]
    selects = [e[-1] for e in f.events if e[:2] == ("account", "select")]
    assert selects == ["x2", "coord"] and f.active == "coord"  # the coordinator gets its account back
    assert f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")
    holds = json.loads(f.holds.read_text())
    assert "term_coder" not in holds and holds["term_restart"]["account"] == "x2"
    review = tick.review_state(f.db, "run", 553)
    assert (review["coder"], review["coder_terminal"]) == ("ctx_restart", "term_restart")
    assert review["agent"] == "coder gpt-6.2-astra medium @x2"  # review publishes keep the new seat
    # Review discovery still finds exactly one "#553 coder" Task, whose newest Dispatch is the retry.
    assert [t["id"] for t in f.orca("orchestration", "task-list")["tasks"] if t["task_title"] == "#553 coder"] == ["task_coder"]

# At the limit, on a model-switch menu, or after Codex switched models: stop it, then retry on another account.
for agent, model, screen in (("codex", "gpt-6.2-astra", CODEX_AT_LIMIT), ("codex", "gpt-6.2-astra", CODEX_SWITCHED),
                             ("claude", "claude-opus-5-5", CLAUDE_AT_LIMIT), ("claude", "claude-opus-5-5", CLAUDE_CURRENT)):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, agent, model, screen)
        calls = f.tick()
        assert calls.index(("orchestration", "worker-stop")) < calls.index(("orchestration", "worker-start")), calls
        assert ("account", "select", "--agent", agent, "--account", "x2") in f.events
        assert [c[0] for c in f.cards] == ["Handed off", "Coding"]
        assert not any(e[:2] == ("terminal", "send") for e in f.events)  # never types into the pane

# No hold ever recorded: the exhausted account cannot be excluded, so nothing is stopped and the Run decides.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.holds.write_text("{}")
    calls = f.tick()
    assert ("orchestration", "worker-stop") not in calls and ("orchestration", "worker-start") not in calls
    assert f.sent() == [("run:run", "#553: limit account unknown")] and f.cards[-1][0] == "Needs you"
    f.tick(1)
    assert f.sent() == []

# No seat: the card stays Handed off, the worker is left as is, and each tick retries.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.seats = ["jzaw"]
    assert ("orchestration", "worker-start") not in f.tick() and f.cards == [("Handed off", f.ticket["agent"])]
    assert "waiting for seat" in f.lines[0] and ("orchestration", "worker-stop") not in [e[:2] for e in f.events]
    f.seats = ["jzaw", "x2"]
    calls = f.tick(5)
    assert ("orchestration", "worker-stop") in calls and ("orchestration", "worker-start") in calls
    assert f.cards[-1][0] == "Coding"

# A lost worker-start response is never replayed: the coordinator inspects it.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.superseded = True
    f.start_error = tick.OrcaError("timed out")
    try:
        f.tick()
        raise AssertionError("watch should report the failed start")
    except tick.OrcaError:
        assert "timed out" in f.lines[0]
    f.start_error = None
    calls = f.tick(1)
    assert ("orchestration", "worker-start") not in calls and f.sent() == [("run:run", "#553: restart unconfirmed")]

# A lost start response whose retry Orca did start is adopted on the next tick, never replayed.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    f.lose_start = True
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert "timed out" in f.lines[0] and f.dispatch == "ctx_restart"
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick(1)
    assert [e[:2] for e in f.events].count(("orchestration", "worker-start")) == 0 and f.notes == []
    holds = json.loads(f.holds.read_text())
    assert "term_coder" not in holds and holds["term_restart"]["account"] == "x2"
    assert tick.review_state(f.db, "run", 553)["coder"] == "ctx_restart"
    assert f.cards[-1][0] == "Handed off" and f.sent() == []  # pending until the note is confirmed
    assert json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])["card"]["stage"] == "Coding"
    f.note_error = None
    calls = f.tick(1)
    assert ("orchestration", "worker-start") not in calls and len(f.notes) == 1 and "ctx_coder" in f.notes[0]
    assert f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")
    f.tick(1)
    assert len(f.notes) == 1 and f.cards[-1][0] == "Coding"


# The replacement settles while its resume note cannot be sent: the note and card target outlive the worker scan.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert f.dispatch == "ctx_restart" and f.cards[-1][0] == "Handed off"
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"
    with check.assertRaises(tick.OrcaError):
        f.tick(1)
    row = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])
    assert row["note"]["to"] == "ctx_restart" and row["card"]["stage"] == "Coding"
    f.note_error = None
    f.tick(1)
    assert len(f.notes) == 1 and f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")
    f.tick(1)
    assert not f.db.execute("SELECT * FROM watch").fetchall() and len(f.notes) == 1

# A replacement that reports while its resume note is undelivered waits; review acts once delivery is confirmed.
ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
pending_note = {"note": {"to": "coder", "subject": "#41: resume", "body": "read the handoff"},
                "card": {"number": 41, "stage": "Coding", "agent": state["agent"]}}
db.execute("INSERT INTO watch VALUES ('run','pane-new',?)", (json.dumps(pending_note),))
with patch.object(tick, "green_report", return_value=None) as verify, patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", return_value={"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "fix"}) as cli:
    tick.review_step(db, "run", [], ticket, state, [recorded])
    verify.assert_not_called()
    card.assert_not_called()
    cli.assert_not_called()
    pending_note.pop("note")  # delivered; the card target is still being restored
    db.execute("UPDATE watch SET state=?", (json.dumps(pending_note),))
    tick.review_step(db, "run", [], ticket, state, [recorded])
    card.assert_not_called()
    db.execute("UPDATE watch SET state='{}'")
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_args.args[1] == "Fixing" and cli.call_count == 1


class FailingWatchInsert:
    """A connection that dies writing the successor's watch row once, inside adopt's transaction."""
    def __init__(self, db):
        self.db, self.armed = db, True

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *exc):
        return self.db.__exit__(*exc)

    def execute(self, sql, params=()):
        if self.armed and sql.startswith("INSERT OR REPLACE INTO watch") and params[1] == "term_restart":
            self.armed = False
            raise sqlite3.OperationalError("disk I/O error")
        return self.db.execute(sql, params)


# Interrupted inside the adoption transaction: the predecessor row and review state survive, the next tick adopts once.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    real, f.db = f.db, FailingWatchInsert(f.db)
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert "disk I/O error" in f.lines[0] and f.notes == [] and f.cards[-1][0] == "Handed off"
    rows = dict(real.execute("SELECT terminal,state FROM watch"))
    assert list(rows) == ["term_coder"] and json.loads(rows["term_coder"])["pending"]["starting"]
    assert tick.review_state(real, "run", 553)["coder"] == "ctx_coder"
    f.db = real
    calls = f.tick(1)
    assert ("orchestration", "worker-start") not in calls and len(f.notes) == 1
    assert list(dict(real.execute("SELECT terminal,state FROM watch"))) == ["term_restart"]
    assert tick.review_state(real, "run", 553)["coder"] == "ctx_restart"
    assert f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")

# A crash relaunches once, on any seat, in the worker's own phase; a second crash goes to the Run as Needs you.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.seats = ["jzaw"]
    f.liveness, f.ticket["stage"] = "exited", "Fixing"
    assert ("orchestration", "worker-start") in f.tick() and f.cards == [("Fixing", "coder gpt-6.2-astra medium @jzaw")]
    f.liveness = "exited"
    calls = f.tick(1)
    assert ("orchestration", "worker-start") not in calls and f.sent() == [("run:run", "#553: worker crashed again")]
    assert f.cards[-1][0] == "Needs you"
    f.tick(1)
    assert f.sent() == []

# A reviewer Orca already settled as failed, before any watch row: relaunched once, card kept at Reviewing.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.title, f.ticket["stage"], f.ticket["agent"] = "#553 reviewer", "Reviewing", "coder m e @a · reviewer m e @jzaw"
    f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", "agent process exited"
    assert ("orchestration", "worker-start") in f.tick()
    assert f.cards == [("Reviewing", "coder m e @a · reviewer m e @jzaw")]
    f.tick(1)
    assert ("orchestration", "worker-start") not in [e[:2] for e in f.events]
# The same failed receipt for a ticket no longer in work is history.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.ticket["stage"] = "Merging"
    f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", "agent process exited"
    assert f.tick() == [("orchestration", "task-list"), ("orchestration", "worker-list"),
                        ("orchestration", "worker-show")] and f.lines == ['watch        "no action"']
# A Merging coder that failed is still current when the review state names it: relaunched.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.ticket["stage"] = "Merging"
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", "agent process exited"
    assert ("orchestration", "worker-start") in f.tick()
    assert tick.review_state(f.db, "run", 553)["coder"] == "ctx_restart"

# An unchanged screen for 20 minutes: nudge once, then report; a changing screen is never silent.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.tick()
    f.tick(19)
    assert f.sent() == []
    f.tick(1)
    assert f.sent() == [("dispatch:ctx_coder", "#553: still working?")]
    f.tick(19)
    assert f.sent() == []
    f.tick(1)
    assert f.sent() == [("run:run", "#553: silent worker")] and f.cards == []
    f.tick(30)
    assert f.sent() == []
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    for minute in range(5):
        f.screen = [*CLAUDE_WORKING[:1], f"✶ Prestidigitating… ({minute * 10}m · ↓ 2.8k tokens)"]
        f.tick(10)
    assert f.sent() == []

# Only a limit dialog showing now counts: the same menu above newer healthy output is history.
CODEX_HEALTHY = ["• Kept the current model; running the tests.", "› Ask Codex to do anything",
                 "  gpt-6.2-astra medium · ~/work/repo"]
assert tick.screen_limit([*CODEX_AT_LIMIT, *CODEX_HEALTHY], {"agent": "codex", "model": "gpt-6.2-astra"}) is None
assert tick.screen_limit([*CODEX_AT_LIMIT[3:], ""], {"agent": "codex", "model": "gpt-6.2-astra"}) == "Approaching rate limits"
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=[*CODEX_AT_LIMIT, *CODEX_HEALTHY])
    calls = f.tick()
    assert ("orchestration", "worker-stop") not in calls and f.cards == [] and f.sent() == []

# A limit restart parked for a seat rechecks the live screen: a recovered worker is not stopped and gets its card back.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.seats = ["jzaw"]
    f.tick()
    assert f.cards == [("Handed off", f.ticket["agent"])]
    f.seats, f.screen = ["jzaw", "x2"], CODEX_HEALTHY
    calls = f.tick(5)
    assert ("orchestration", "worker-stop") not in calls and ("orchestration", "worker-start") not in calls
    assert f.cards[-1][0] == "Coding" and "recovered" in f.lines[0]
    f.screen = CODEX_AT_LIMIT  # a later limit is handled afresh
    calls = f.tick(5)
    assert calls.index(("orchestration", "worker-stop")) < calls.index(("orchestration", "worker-start"))

# A cleared prompt gives the card back, so review_tick's discovery reads the coder's successful report.
def discovered(ticket):
    found = []
    def orca(*args):
        if args[1] == "inbox":
            return {"messages": [report("ctx_coder", "green at " + sha)]}
        if args[1] == "task-list":
            return {"tasks": [{"id": "task_coder", "title": "#553 coder"}]}
        if args[1] == "dispatch-show":
            return {"dispatch": {"id": "ctx_coder"}}
        if args[1] == "worker-show":
            return {"dispatch": {"id": "ctx_coder", "runId": "run", "taskId": "task_coder", "assigneeHandle": "term_coder"},
                    "worker": {"worktreeId": "repo::/wt", "startOptions": {"launch": {"effective": {"model": "gpt-6.2-astra"}}}}}
        if args[:2] == ("worktree", "show"):
            return {"worktree": {"id": "repo::/wt", "path": "/wt", "branch": "refs/heads/task/dev-auto/watch-workers"}}
        raise AssertionError(args)
    with patch.object(tick, "supervisor", return_value=([], "run")), patch.object(tick, "runtime", orca), \
            patch.object(tick, "tickets", return_value=[ticket]), \
            patch.object(tick, "command", return_value="https://github.com/julianthant/dot-agents.git"), \
            patch.object(tick, "review_step", lambda *a: found.append(a[3]["number"])):
        tick.review_tick(SimpleNamespace(issue=None, coder=None), sqlite3.connect(":memory:"))
    return found

with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.ticket.update(branch="task/dev-auto/watch-workers", repo="dot-agents")
    f.wait = {"kind": "permission", "approvalId": "appr_1"}
    f.tick()
    assert f.ticket["stage"] == "Needs you" and discovered(f.ticket) == []
    f.wait = None
    f.tick(1)
    assert f.cards[-1] == ("Coding", f.ticket["agent"]) and "prompt cleared" in f.lines[0]
    assert discovered(f.ticket) == [553]
    f.tick(1)
    assert len(f.cards) == 2  # restored once

# The coder answers its prompt and reports success before the next tick: the card is restored, then the row goes.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.ticket.update(branch="task/dev-auto/watch-workers", repo="dot-agents")
    f.wait = {"kind": "permission", "approvalId": "appr_1"}
    f.tick()
    assert f.ticket["stage"] == "Needs you"
    f.wait, f.status, f.state, f.liveness = None, "completed", "succeeded", "exited"
    f.tick(1)
    assert f.cards[-1] == ("Coding", f.ticket["agent"]) and "prompt cleared" in f.lines[0]
    assert not f.db.execute("SELECT * FROM watch").fetchall() and discovered(f.ticket) == [553]

# A permission prompt is forwarded to the Run once and the card goes to Needs you; nothing is typed or approved.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.wait = {"kind": "permission", "approvalId": "appr_1"}
    f.tick()
    assert f.sent() == [("run:run", "#553: permission prompt")] and f.cards[-1][0] == "Needs you"
    escalation = next(e for e in f.events if e[:2] == ("orchestration", "send"))
    assert escalation[escalation.index("--type") + 1] == "status"
    assert not any(e[1] in ("approve", "deny") for e in f.events)
    f.tick(1)
    assert f.sent() == []

# An operator stop, or a finished worker, ends the watch; neither is relaunched.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.tick()
    f.status, f.state, f.failure, f.liveness = "failed", "stopped", "stopped", "exited"
    assert ("orchestration", "worker-start") not in f.tick(1) and f.sent() == []
    assert not f.db.execute("SELECT * FROM watch").fetchall()

# A Project lookup failure after the coordinator check still logs one structured watch line, then fails the tick.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp)
    f.tickets_error = tick.OrcaError("gh: project unavailable")
    try:
        f.tick()
        raise AssertionError("watch should fail")
    except tick.OrcaError:
        pass
    assert json.loads(f.lines[0].split(None, 1)[1]) == [{"tick": "gh: project unavailable"}]

print("ok")

# A concurrent tick: while another process holds fill.lock, fill skips and release refuses, through the real CLI.
with TemporaryDirectory() as home:
    env = dict(os.environ, HOME=home)
    lock = Path(home) / ".local/state/dev-auto/fill.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys\nf = open(sys.argv[1], 'a+')\n"
                               "fcntl.flock(f, fcntl.LOCK_EX)\nprint('locked', flush=True)\nsys.stdin.read()", str(lock)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        script = str(Path(tick.__file__).resolve())
        skipped = subprocess.run([sys.executable, script, "fill", "--repo-path", home, "--issue", "1"],
                                 env=env, capture_output=True, text=True, timeout=60)
        assert skipped.returncode == 0 and "another tick is active" in skipped.stdout, skipped
        refused = subprocess.run([sys.executable, script, "fill", "--release", "1"],
                                 env=env, capture_output=True, text=True, timeout=60)
        assert refused.returncode == 1 and "another tick is active" in refused.stderr, refused
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
