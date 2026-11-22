import io
import os
import subprocess
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
            return {"dispatchId": "fixture-dispatch", "state": "ready"}
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
    def recovery_runtime(*argv):
        verb = argv[:2]
        operations.append(verb)
        if verb == ("account", "list"):
            return {"codex": {"activeAccountId": "other" if preparation_failed else "seat"}}
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
            with check.assertRaisesRegex(tick.OrcaError, "selected account changed"):
                tick.launch(db, root, codex_ticket, account, args, holds)
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

print("ok")
