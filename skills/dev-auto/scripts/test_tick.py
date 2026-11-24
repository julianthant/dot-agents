import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import tick

_snapshot_dir = TemporaryDirectory()  # never read or write the live issue snapshot
tick.SNAPSHOT = tick.Path(_snapshot_dir.name) / "issues.json"
tick.PROJECT_CACHE = tick.Path(_snapshot_dir.name) / "project.json"

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
    assert run.call_args.args[0] == [tick.ORCA, "status", "--json"]

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


# The dry-run plan reserves account capacity in priority order.
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
plan = list(tick.fill_plan(tickets, seat))
check.assertEqual([t["number"] for t, a, _ in plan if a], [1])
check.assertEqual({t["number"]: why for t, _, why in plan}[2], "no seat")
body = """## Owned paths

src/backend/logs/ (writers), tests/backend/logs*.ts

## Route

- Coder: claude (claude-sonnet-5-5, effort high) with Opus advisor · Reviewer: codex (gpt-6.2-sol, effort medium)
"""
check.assertEqual(tick.coder_route(body), ("claude", "claude-sonnet-5-5", "high", "opus"))
# The card label cannot show +advisor for Codex: rejected since 25ea75d.
assert tick.coder_route("Coder: codex / gpt-6.2-sol / high + opus advisor") is None
assert tick.coder_route("Coder: grok / grok-4.7 / xhigh") == ("grok", "grok-4.7", "xhigh", None)
assert tick.coder_route("Coder: cursor / grok-4.7-high / high") == ("cursor", "grok-4.7-high", "high", None)
assert tick.coder_route("Coder: cursor / grok-4.7-high / xhigh") is None
coding_body = "Coder: cursor / grok-4.7-high / high\nReviewer: claude / claude-sonnet-5-5 / medium"
cursor_ticket = dict(base, number=25, body=coding_body, route=tick.coder_route(coding_body))
assert next(tick.fill_plan([cursor_ticket], seat))[1] == account
assert next(tick.fill_plan([dict(cursor_ticket, body=coding_body.replace("claude-sonnet", "claude-opus"))],
                           seat))[2] == "Grok coding needs a Sonnet reviewer"
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

receipt = dict(full_start("grok-dispatch", "grok-pane"), state="outcome_unknown", turnStart="unobserved",
               stage="turn_start_unobserved")
with patch.object(tick.subprocess, "run", return_value=subprocess.CompletedProcess(
        [], 1, json.dumps({"ok": True, "result": receipt}), "")):
    assert tick.runtime("orchestration", "worker-start", "--agent", "grok", "--full") == receipt


# Exercise launch through the real advisor file, journal, hold and Project-edit boundary.
from types import SimpleNamespace
with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
    root = Path(tmp)
    card = []
    starts = []
    comments = []
    ticket = dict(base, number=51, sha="sha", base="origin/main", route=("claude", "claude-sonnet-5-5", "medium", "opus"))
    args = SimpleNamespace(anchor=None, run=None)
    def cli(*args):
        if args[0] == "git":
            return "sha" if "rev-parse" in args else ticket["branch"]
        if args[:2] == ("gh", "api"):
            check.assertIn("/issues/51/comments", args[3])
            return "1\n" if comments else ""
        if args[:3] == ("gh", "issue", "comment"):
            check.assertTrue(starts)
            comments.append(args[-1])
            raise tick.OrcaError("comment receipt lost")
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
        with check.assertRaisesRegex(tick.OrcaError, "comment receipt lost"):
            tick.launch(db, root, dict(ticket, overlap_note="Overlaps #40 on `src/a`."), account, args, root / "holds.json")
        rec = json.loads(db.execute("SELECT receipt FROM starts WHERE issue=51").fetchone()[0])
        tick.reconcile_start(db, 51, rec, [])
        with check.assertRaisesRegex(tick.OrcaError, "51"):
            tick.launch(db, root, ticket, account, args, root / "holds.json")
    check.assertEqual(len(starts), 1)
    check.assertEqual(len(comments), 1)
    check.assertIn("Overlaps #40 on `src/a`.", comments[0])
    check.assertEqual(json.loads((root / "holds.json").read_text())["fixture-terminal"]["account"], "seat")
    review = tick.review_state(db, "fixture", 51)
    assert (review["coder"], review["expected"], review["coder_terminal"]) == (
        "fixture-dispatch", "fixture-dispatch", "fixture-terminal")
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
    active, selects = ["coord"], []  # the coordinator bot's Codex account
    restore_fails = [False]
    task_fails = [False]
    def recovery_runtime(*argv):
        verb = argv[:2]
        operations.append(verb)
        if verb == ("orchestration", "task-create") and task_fails[0]:
            task_fails[0] = False
            raise tick.OrcaError("task store down")
        if verb == ("account", "select"):
            if restore_fails[0] and argv[-1] == "coord":
                restore_fails[0] = False
                raise tick.OrcaError("account switch refused")
            selects.append(argv[-1])
            if not preparation_failed:  # a racing selection holds the account while preparation fails
                active[0] = argv[-1]
            return {}
        if verb == ("account", "list"):
            return {"codex": {"activeAccountId": active[0]}}
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
    edit_fails = [False]
    real_command = tick.command
    def recovery_command(*argv):
        if argv[:3] == ("gh", "project", "item-edit"):
            if edit_fails[0]:
                edit_fails[0] = False
                raise tick.OrcaError("Project unavailable")
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
          patch.object(pick.account_usage, "load_config", return_value={}), patch.object(pick, "coordinator", return_value=None),
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
            # A failed preparation hands the coordinator its account back.
            check.assertEqual(selects, ["seat", "coord", "seat", "coord"])
            preparation_failed = False
            refused = True
            with check.assertRaisesRegex(tick.OrcaError, "no start effects"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            check.assertEqual(db.execute("SELECT count(*) FROM starts").fetchone(), (0,))
            check.assertEqual((selects[-2:], active), (["seat", "coord"], ["coord"]))
            # The restore fails once after a refused start: the next fill restores it without a Dispatch lookup.
            restore_fails[0] = True
            with check.assertRaisesRegex(tick.OrcaError, "account switch refused"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            check.assertTrue(json.loads(db.execute("SELECT receipt FROM starts").fetchone()[0])["not_applied"])
            operations.clear()
            tick.fill_tick(args, db)
            check.assertEqual(db.execute("SELECT count(*) FROM starts").fetchone(), (0,))
            check.assertEqual((selects[-1], active), ("coord", ["coord"]))
            check.assertNotIn(("orchestration", "dispatch-show"), operations)
            # Task creation and the restore both fail: the restore was owed before the selection, so the next fill
            # gives the coordinator its account back without a Task or Dispatch.
            task_fails[0] = restore_fails[0] = True
            with check.assertRaisesRegex(tick.OrcaError, "task store down; restore the coordinator's Codex account coord"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            check.assertEqual(active, ["seat"])
            check.assertEqual(json.loads(db.execute("SELECT receipt FROM starts").fetchone()[0]),
                              {"not_applied": True, "coordinator": "coord"})
            tick.fill_tick(args, db)
            check.assertEqual(db.execute("SELECT count(*) FROM starts").fetchone(), (0,))
            check.assertEqual((selects[-1], active), ("coord", ["coord"]))
            operations.clear()
            refused = False
            with check.assertRaisesRegex(tick.OrcaError, "fixture-task"):
                tick.launch(db, root, codex_ticket, account, args, holds)
            # Lost reply, Dispatch not yet visible: the worker's selection stays until its attempt is verified.
            check.assertEqual(active, ["seat"])
            check.assertEqual(json.loads(db.execute("SELECT receipt FROM starts").fetchone()[0])["coordinator"], "coord")
        with sqlite3.connect(ledger) as db:
            tick.fill_tick(args, db)
            check.assertIn("no Dispatch", output.getvalue())
            check.assertEqual(active, ["seat"])
            visible = True
            edit_fails[0] = True  # the Project write fails; the restoration does not depend on it
            tick.fill_tick(args, db)
            check.assertIn("Project unavailable", output.getvalue())
            check.assertEqual(active, ["coord"])
            check.assertNotIn("coordinator", json.loads(db.execute("SELECT receipt FROM starts").fetchone()[0]))
            restored = len(selects)
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
            check.assertEqual(len(selects), restored)  # restored once
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
    stack.enter_context(patch.object(tick, "tickets", return_value=[dict(ticket)]))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(tick, "command", side_effect=["git@github.com:julianthant/hr-relay.git", "origin/main",
                                                              "abcdef refs/heads/main"]))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    launch = stack.enter_context(patch.object(tick, "launch"))
    with redirect_stdout(io.StringIO()) as output:
        with patch.object(Path, "home", return_value=root):
            check.assertEqual(tick.main(["fill", "--repo-path", str(root), "--dry-run", "--holds", str(holds)]), 0)
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
    stack.enter_context(patch.object(tick, "tickets", side_effect=lambda fresh=False: [dict(ticket)]))
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

    # A sleeping WSL host at discovery or at prelaunch refresh only removes overflow.
    raced = False
    host = "Juzzark-WSL"
    for failure in ("checkout", "inventory", "refresh"):
        start.reset_mock()
        scans = []
        def scan(path, items, repo, emit, target, counts):
            if target == host:
                scans.append(target)
                if failure == "inventory" or len(scans) == 2:
                    raise TimeoutError("inventory unavailable")
            return set()
        with (patch.object(pick.account_usage, "load_config", return_value={"hosts": {host: {"target_tickets": 1}}}),
              patch.object(pick.account_usage, "host_checkout", return_value="/remote/relay",
                           side_effect=OSError("checkout unavailable") if failure == "checkout" else None),
              patch.object(tick, "inventory", side_effect=scan), redirect_stdout(io.StringIO()) as output):
            tick.fill_tick(args, None)
        start.assert_called_once()
        assert start.call_args.args[1] == root.resolve() and start.call_args.args[3] == account
        assert host in output.getvalue() and "unavailable" in output.getvalue()


# An unresolved attempt still holds its seat. A Bug can start alongside live work on overlapping paths.
with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
    root = Path(tmp)
    stuck = dict(ticket, number=40, branch="task/a/stuck", paths=["src/occupied", "src/unrelated"])
    overlap_ticket = dict(ticket, number=41, branch="task/a/overlap", kind="Bug", paths=["src/occupied/file"])
    tick.reserve(db, 40, dict(task="stuck", run="different", route=base["route"], account="held"))
    args = SimpleNamespace(repo_path=root, dry_run=False, issue=None, holds=root / "holds.json")
    stack.enter_context(patch.object(tick, "command", git))
    raced = False
    stack.enter_context(patch.object(tick, "tickets", side_effect=lambda fresh=False: [dict(t) for t in (stuck, overlap_ticket, ticket)]))
    stack.enter_context(patch.object(tick, "supervisor", return_value=([], "own")))
    def live_inventory(path, issues, repo, emit, host, counts):
        next(t for t in issues if t["number"] == 40)["live"] = True
        return set()
    stack.enter_context(patch.object(tick, "inventory", side_effect=live_inventory))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    picker = stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    start = stack.enter_context(patch.object(tick, "launch"))
    output = stack.enter_context(redirect_stdout(io.StringIO()))
    tick.fill_tick(args, db)
    check.assertIn("reconcile from its owning Run", output.getvalue())
    starts = [call.args[2] for call in start.call_args_list]
    check.assertEqual([t["number"] for t in starts], [41, ticket["number"]])
    check.assertIn("#40", starts[0]["overlap_note"])
    check.assertIn("src/occupied", starts[0]["overlap_note"])
    check.assertNotIn("src/unrelated", starts[0]["overlap_note"])
    check.assertEqual(picker.call_args.kwargs["holds"][0]["account"], "held")
    check.assertEqual(db.execute("SELECT issue FROM starts").fetchall(), [(40,)])

# A Codex start whose reply was lost still owes the coordinator's account: no other Codex selection in the
# same pass can record the worker's seat as the coordinator's. A Claude start does not touch that account.
with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
    root = Path(tmp)
    first = dict(ticket, number=60, branch="task/a/first", paths=["src/first"], route=base["route"])
    second = dict(ticket, number=61, branch="task/a/second", paths=["src/second"], route=base["route"])
    claude = dict(ticket, number=62, branch="task/a/claude", paths=["src/claude"])
    args = SimpleNamespace(repo_path=root, dry_run=False, issue=None, holds=root / "holds.json")
    def lost(db, repo, t, acct, args, holds):
        if t["number"] == 60:
            tick.reserve(db, 60, {"task": "t60", "run": "own", "coordinator": "coord"})
            raise tick.OrcaError("timed out")
    stack.enter_context(patch.object(tick, "command", git))
    stack.enter_context(patch.object(tick, "tickets", side_effect=lambda fresh=False: [dict(t) for t in (first, second, claude)]))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    stack.enter_context(patch.object(pick, "pick", return_value=(account, [])))
    start = stack.enter_context(patch.object(tick, "launch", side_effect=lost))
    output = stack.enter_context(redirect_stdout(io.StringIO()))
    tick.fill_tick(args, db)
    check.assertEqual([c.args[2]["number"] for c in start.call_args_list], [60, 62])
    check.assertIn("coordinator restore still owed by ['starts:60']", output.getvalue())

# A full start receipt confirms the launch: the coordinator is restored even when dispatch-show is unavailable.
with sqlite3.connect(":memory:") as db, patch.object(tick, "restore_coordinator") as restore, \
        patch.object(tick, "runtime", side_effect=tick.OrcaError("Orca busy")):
    rec = {"task": "t", "run": "run", "dispatch": "d", "terminal": "term", "coordinator": "coord"}
    tick.reserve(db, 63, rec)
    with check.assertRaisesRegex(tick.OrcaError, "Orca busy"):
        tick.reconcile_start(db, 63, dict(rec), [])
    restore.assert_called_once_with("coord")
    check.assertNotIn("coordinator", json.loads(db.execute("SELECT receipt FROM starts").fetchone()[0]))

# Release is its own operation and only clears the inspected pending issue, giving the coordinator its account back.
with TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)), patch.object(tick, "fill_tick") as fill, \
        patch.object(tick, "restore_coordinator") as restore:
    state = Path(tmp) / ".local/state/dev-auto"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "starts.sqlite") as db:
        tick.reserve(db, 40, {"task": "inspected", "coordinator": "coord"})
        tick.reserve(db, 41, {"task": "other"})
        db.execute("INSERT INTO starts VALUES(42,'done')")
    with redirect_stdout(io.StringIO()):
        tick.main(["fill", "--release", "40"])
        tick.main(["fill", "--release", "42"])
    fill.assert_not_called()
    restore.assert_called_once_with("coord")
    with sqlite3.connect(state / "starts.sqlite") as db:
        check.assertEqual(db.execute("SELECT issue FROM starts ORDER BY issue").fetchall(), [(41,), (42,)])

check.assertEqual(tick.owned_paths("Owned paths: Edit `scripts/dev.py` and `docs/how.md`, plus config/app.json."),
                  ["scripts/dev.py", "docs/how.md", "config/app.json"])
check.assertEqual(tick.owned_paths("Owned paths: src, docs, scripts/setup.py."), ["src", "docs", "scripts/setup.py"])
check.assertIn(".github/workflows", tick.owned_paths("Owned paths: Development/checks workflows, CI plan and focused tests."))
# Bug 7: prose "test"/"tests" is no path, or every ticket overlaps the whole tests tree.
check.assertEqual(tick.owned_paths("Owned paths: `src/a.ts` and its tests; focused test updates."), ["src/a.ts"])
check.assertEqual(tick.owned_paths("Owned paths: src/a.ts, tests"), ["src/a.ts", "tests"])  # a listed path still counts

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
def issue_page(nodes, more=False):
    return json.dumps({"data": {"repository": {"issues": {"nodes": nodes,
                                                          "pageInfo": {"hasNextPage": more, "endCursor": "c"}}}}})


with patch.object(tick, "command", side_effect=lambda *a: issue_page(rows if "states=OPEN" in a else [])):
    parsed = tick.tickets(fresh=True)
check.assertEqual([row["branch"] for row in parsed], ["task/dashboard-frontend-speed/clock-tick-in-leaves",
                  "task/driver-reads/crm-sso-route-recovery", "task/tests/explicit"])

research_body = ("Branch: none (read-only).\nReviewer: none (the orchestrator checks the report)\n"
                 "Coder: grok / grok-4.7 / xhigh\nOwned paths: none.\n")
research_ticket, = tick.tickets(rows=[issue_row(762, "dependencies / package review: research", research_body)])
assert (research_ticket["branch"], research_ticket["base"], research_ticket["paths"]) == (None, "origin/development", [])

# Bug 8: one issue read a minute across processes; closed issues are read incrementally between full reads.
with TemporaryDirectory() as tmp:
    calls, closed_rows = [], [dict(issue_row(n, f"c / {n}"), updatedAt=f"2026-11-23T10:0{n % 10}:00Z") for n in (1, 2)]

    def gh(*a):
        calls.append(("OPEN" if "states=OPEN" in a else "CLOSED", next((x for x in a if x.startswith("since=")), None)))
        if "states=OPEN" in a:
            return issue_page(rows[:2] if "endCursor=c" in a else rows[2:], more="endCursor=c" not in a)
        return issue_page(closed_rows if not calls[-1][1] else [dict(closed_rows[0], title="edited"), dict(rows[0], updatedAt="2026-11-23T11:00:00Z")])

    clock = [1_795_400_000]
    with patch.object(tick, "SNAPSHOT", Path(tmp) / "issues.json"), patch.object(tick, "command", gh), \
            patch.object(tick.time, "time", lambda: clock[0]):
        check.assertEqual(len(tick.tickets()), 3)
        check.assertEqual(calls, [("OPEN", None), ("OPEN", None), ("CLOSED", None)])  # two open pages, one full closed read
        tick.tickets(), tick.snapshot()  # another verb, or the roadmap, within the minute
        check.assertEqual(len(calls), 3)
        clock[0] += 61
        snap = tick.snapshot()
        check.assertEqual(calls[3:], [("OPEN", None), ("OPEN", None), ("CLOSED", "since=2026-11-23T02:11:20Z")])
        # An edited closed issue is replaced; one still open is never listed as closed.
        check.assertEqual([(i["number"], i["title"]) for i in snap["closed"]], [(2, "c / 2"), (1, "edited")])
        tick.stale_snapshot()  # a Project write: the next read refetches open issues and still reads closed ones since
        tick.tickets()
        check.assertEqual(calls[6:], [("OPEN", None), ("OPEN", None), ("CLOSED", "since=2026-11-23T02:12:21Z")])
        with patch.object(tick, "SNAPSHOT", Path(tmp) / "missing" / "issues.json"):
            tick.stale_snapshot()  # no snapshot yet (nor its directory): nothing to mark
        tick.tickets(fresh=True)  # fill's refresh before side effects never uses a cached read
        check.assertEqual(len(calls), 12)
        clock[0] += 600
        tick.snapshot()
        check.assertEqual(calls[-1], ("CLOSED", None))  # ten minutes on: a full closed read again

# Bug 8: the roadmap moved here renders a recorded snapshot exactly as the live script did for the same GitHub
# replies (ROADMAP_BEFORE is that script's output), while reading issues from the shared snapshot.
import roadmap

def rm_card(status, agent, stage, area, project=5):
    return {"id": "item", "project": {"number": project, "owner": {"login": "julianthant"}},
            "status": status and {"name": status}, "agent": agent and {"text": agent},
            "stage": stage and {"name": stage}, "area": area and {"name": area}}
def rm_issue(n, state, updated, title, reason=None, cards=(), labels=(), blocked=(), parent=None, kids=0, done=0, ms=None, kind="Task"):
    conn = lambda xs: {"totalCount": len(xs), "nodes": list(xs)}
    return {"number": n, "title": title, "body": "Owned paths: `a`", "state": state, "stateReason": reason, "updatedAt": updated,
            "issueType": kind and {"name": kind}, "milestone": ms and {"title": ms}, "parent": parent and {"number": parent},
            "subIssuesSummary": {"total": kids, "completed": done} if kids else None,
            "assignees": conn([]), "subIssues": conn([{}] * kids), "closedByPullRequestsReferences": conn([]),
            "labels": conn([{"name": l} for l in labels]), "blockedBy": conn([{"number": b, "state": s} for b, s in blocked]),
            "blocking": conn([]), "projectItems": conn(list(cards))}
RM_OPEN = [rm_issue(10, "OPEN", "2026-11-23T09:00:00Z", "area / older", cards=[rm_card("Todo", None, "Queued", "Tick")], ms="Staging cut"),
        rm_issue(11, "OPEN", "2026-11-23T10:00:00Z", "area / newest", cards=[rm_card(None, None, None, None, 7), rm_card("In Progress", "coder m e @a", "Coding", "Roadmap")],
              labels=["later", "bug"], blocked=[(10, "OPEN"), (3, "CLOSED")], parent=1),
        rm_issue(12, "OPEN", "2026-11-23T09:30:00Z", "feature", kind="Feature", kids=3, done=1)]
RM_CLOSED = [rm_issue(3, "CLOSED", "2026-11-22T08:00:00Z", "done one", reason="COMPLETED", cards=[rm_card("Done", None, None, "Tick")]),
          rm_issue(4, "CLOSED", "2026-11-22T07:00:00Z", "dropped", reason="NOT_PLANNED", kind=None)]
RM_PR = lambda n, state: {"number": n, "title": f"pr {n}", "headRefName": f"task/a/{n}", "autoMergeRequest": None if n % 2 else {},
                          "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}, "isDraft": False,
                          "closingIssuesReferences": {"nodes": [{"number": 11, "repository": {"nameWithOwner": "julianthant/hr-relay"}}]},
                          "updatedAt": "2026-11-23T09:00:00Z"}
def rm_answer(args):
    """The recorded gh reply for one command line, for both the old and the moved script."""
    a = " ".join(args)
    if args[:2] == ["project", "view"]:
        return {"id": "proj"}
    if args[:2] == ["project", "field-list"]:
        return {"fields": [{"name": "Status", "options": [{"name": "Todo"}]}, {"name": "Area", "options": [{"name": "Tick"}, {"name": "Roadmap"}]}]}
    if args[:2] == ["api", "graphql"]:
        return {"data": {"relay": {"dev": {"nodes": [RM_PR(21, "PENDING"), RM_PR(22, "FAILURE")]},
                                   "staging": {"nodes": [RM_PR(30, "SUCCESS")]}}, "agents": {"task": {"nodes": []}}}}
    if "releases?per_page=1" in a:
        return [{"tag_name": "v1.2.0", "published_at": "2026-11-20T12:00:00Z"}]
    if "search/issues" in args:
        assert "q=repo:julianthant/hr-relay is:pr is:merged base:development merged:>2026-11-20" in args, args
        return {"total_count": 7, "items": [{}]}
    if a.startswith("api repos/julianthant/hr-relay/compare/"):
        return {"ahead_by": 4 if "staging..." in a else 9}
    if a == "api repos/julianthant/hr-relay/commits/main":
        return {"commit": {"committer": {"date": "2026-11-19T00:00:00Z"}}}
    if a.endswith("matching-refs/heads/cut/"):
        return [{"ref": "refs/heads/cut/2026-11-21"}]
    if "actions/workflows/" in a:
        return {"workflow_runs": [{"event": "schedule", "head_branch": "development", "conclusion": "success",
                                   "status": "completed", "created_at": "2026-11-23T06:00:00Z",
                                   "head_sha": "abc", "html_url": "u"}]}
    if "milestones" in a:
        return [{"title": "Staging cut"}, {"title": "Later"}]
    raise AssertionError(args)

ROADMAP_BEFORE = json.loads(
    "{\"areas\":[\"Tick\",\"Roadmap\"],\"milestones\":[\"Staging cut\",\"Later\"],\"issues\":[{\"n\":11,\"t\":\"area / newest\",\"type\":"
    "\"Task\",\"open\":true,\"done\":false,\"ms\":null,\"later\":true,\"parent\":1,\"kids\":0,\"kidsDone\":0,\"blockers\":[10],\"deps\""
    ":[10,3],\"who\":\"coder m e @a\",\"updated\":\"2026-11-23T10:00:00Z\",\"status\":\"In Progress\",\"agent\":\"Coding\",\"area\":\""
    "Roadmap\"},{\"n\":12,\"t\":\"feature\",\"type\":\"Feature\",\"open\":true,\"done\":false,\"ms\":null,\"later\":false,\"parent\":nul"
    "l,\"kids\":3,\"kidsDone\":1,\"blockers\":[],\"deps\":[],\"who\":null,\"updated\":\"2026-11-23T09:30:00Z\",\"status\":null,\"age"
    "nt\":null,\"area\":null},{\"n\":10,\"t\":\"area / older\",\"type\":\"Task\",\"open\":true,\"done\":false,\"ms\":\"Staging cut\",\"la"
    "ter\":false,\"parent\":null,\"kids\":0,\"kidsDone\":0,\"blockers\":[],\"deps\":[],\"who\":null,\"updated\":\"2026-11-23T09:00:"
    "00Z\",\"status\":\"Todo\",\"agent\":\"Queued\",\"area\":\"Tick\"},{\"n\":3,\"t\":\"done one\",\"type\":\"Task\",\"open\":false,\"done\":t"
    "rue,\"ms\":null,\"later\":false,\"parent\":null,\"kids\":0,\"kidsDone\":0,\"blockers\":[],\"deps\":[],\"who\":null,\"updated\":\""
    "2026-11-22T08:00:00Z\",\"status\":\"Done\",\"agent\":null,\"area\":\"Tick\"},{\"n\":4,\"t\":\"dropped\",\"type\":null,\"open\":fals"
    "e,\"done\":false,\"ms\":null,\"later\":false,\"parent\":null,\"kids\":0,\"kidsDone\":0,\"blockers\":[],\"deps\":[],\"who\":null,"
    "\"updated\":\"2026-11-22T07:00:00Z\",\"status\":null,\"agent\":null,\"area\":null}],\"pipeline\":{\"devPrs\":[{\"n\":21,\"t\":\"p"
    "r 21\",\"head\":\"task/a/21\",\"auto\":false,\"draft\":false,\"checks\":\"running\",\"issues\":[11],\"updated\":\"2026-11-23T09:"
    "00:00Z\"},{\"n\":22,\"t\":\"pr 22\",\"head\":\"task/a/22\",\"auto\":true,\"draft\":false,\"checks\":\"failing\",\"issues\":[11],\"up"
    "dated\":\"2026-11-23T09:00:00Z\"}],\"mergedSinceRelease\":7,\"devAheadOfStaging\":4,\"devAheadOfMain\":9,\"stagingPr\":[{"
    "\"n\":30,\"head\":\"task/a/30\",\"checks\":\"green\"}],\"release\":{\"tagName\":\"v1.2.0\",\"publishedAt\":\"2026-11-20T12:00:00Z"
    "\"},\"cuts\":[\"cut/2026-11-21\"],\"fullRun\":{\"conclusion\":\"success\",\"status\":\"completed\",\"createdAt\":\"2026-11-23T06"
    ":00:00Z\",\"headSha\":\"abc\",\"url\":\"u\"}}}")

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    with (patch.object(roadmap, "STATE", root / "roadmap.json"),
          patch.object(tick, "SNAPSHOT", root / "issues.json"),
          patch.object(tick, "PROJECT_CACHE", root / "project.json"),
          patch.object(tick, "issue_nodes", side_effect=lambda state, *_: RM_OPEN if state == "OPEN" else RM_CLOSED),
          patch.object(tick, "command", lambda *a: json.dumps(rm_answer(list(a[1:])))),
          patch.object(roadmap, "gh", lambda *a: rm_answer(list(a))),
          patch.object(roadmap, "poll", lambda url, _: (200, None, json.dumps(rm_answer(["api", url])))),
          patch.object(roadmap, "branch_heads", return_value={}),
          patch.object(roadmap, "readiness", return_value={})):
        built = roadmap.render(full=True)
        check.assertEqual(roadmap.render()["pipeline"], built["pipeline"])

built.pop("generated")
check.assertIsNotNone(built.pop("githubFetchedAt"))
for card in built["issues"]:
    live = card.pop("now")
    if card["n"] == 11:
        check.assertEqual((live["stage"], live["pr"]["n"]), (3, 21))
    else:
        check.assertIsNone(live)
    if card["open"] and card["type"] == "Task":
        check.assertFalse(card.pop("ready"))
        check.assertEqual(card.pop("holdReason"), "readiness unavailable")
check.assertEqual(built["pipeline"].pop("agentPrs"), [])
built["pipeline"]["stagingPr"] = [{k: p[k] for k in ("n", "head", "checks")} for p in built["pipeline"]["stagingPr"]]
check.assertEqual(built, {**ROADMAP_BEFORE, "tick": None})

# Bug 8: field-list costs about 100 points, so the Project's ids are cached and refetched only on a miss or a failed edit.
with TemporaryDirectory() as tmp:
    reads = []
    fields = {"fields": [{"name": "Agent", "id": "f_agent"},
                         {"name": "Agent state", "id": "f_stage", "options": [{"name": "Coding", "id": "o_coding"}]}]}

    def gh(*a):
        if a[1:3] == ("project", "field-list"):
            reads.append(a)
            return json.dumps(fields)
        if a[1:3] == ("project", "view"):
            return json.dumps({"id": "proj"})
        if a[1:3] == ("project", "item-edit"):
            return "{}"
        raise AssertionError(a)

    with patch.object(tick, "PROJECT_CACHE", Path(tmp) / "project.json"), patch.object(tick, "command", gh):
        card = {"item": "card"}
        tick.review_card(card, "Coding", "coder")
        tick.review_card(card, "Coding", "coder")
        check.assertEqual(len(reads), 1)  # the second card edit reads no ids
        fields["fields"][1]["options"].append({"name": "Fixing", "id": "o_fixing"})
        tick.review_card(card, "Fixing", "coder")  # an option added since the cache: one refetch
        check.assertEqual(len(reads), 2)
    # A failed edit drops the cache through command(), so the next edit refetches.
    cache = Path(tmp) / "project.json"
    with patch.object(tick, "PROJECT_CACHE", cache), patch.object(tick.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 1, "", "Could not resolve to a node")):
        with check.assertRaises(tick.OrcaError):
            tick.command("gh", "project", "item-edit", "--id", "card")
    check.assertFalse(cache.exists())

# The local roadmap survives an unavailable GitHub API and preserves the cached Area order.
with TemporaryDirectory() as tmp:
    root = Path(tmp)
    cache, snap = root / "project.json", root / "issues.json"
    snap.write_text(json.dumps({"open": RM_OPEN, "closed": RM_CLOSED, "fetched": 0}))
    with (patch.object(roadmap, "STATE", root / "roadmap.json"),
          patch.object(tick, "PROJECT_CACHE", cache), patch.object(tick, "SNAPSHOT", snap),
          patch.object(tick, "command", side_effect=AssertionError("local render called GitHub")),
          patch.object(roadmap, "gh", side_effect=AssertionError("local render called GitHub"))):
        check.assertEqual(roadmap.render()["areas"], [])
        cache.write_text(json.dumps({"id": "proj", "fields": {"Area": {"options": [{"name": "Driver"}]}}}))
        data = roadmap.render()
        check.assertEqual(data["areas"], ["Driver"])
        check.assertTrue(cache.exists())
        check.assertEqual([card["n"] for card in data["issues"]], [11, 12, 10, 3, 4])

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
check.assertEqual([t["number"] for t, a, _ in plan if a], [484])

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
mailbox = tick.ReviewMailbox(sqlite3.connect(":memory:"), "run", [report("other", sha, "question"), recorded])
assert mailbox.successes == {"coder": [recorded]}

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


def followup_worker(dispatch, terminal, *, task="task", previous="coder"):
    return {"dispatch": {"id": dispatch, "runId": "run", "taskId": task, "assigneeHandle": terminal,
                         "followUpOf": previous}}


def followup_replies(dispatch, terminal="coder-pane", *, previous="coder", delivery="follow_up"):
    return [{"delivery": delivery, "followUpOf": previous, "dispatchId": dispatch},
            followup_worker(dispatch, terminal, previous=previous)]


# Successful mail and lost-receipt recovery both record the merger before publishing its card.
for lost_receipt in (False, True):
    with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
        ticket, state = review_fixture()
        state.update(stage="Reviewing", sha=sha, expected="reader", coder_terminal="old-pane")
        verdict = dict(report("reader", "REVIEW VERDICT: PUSH " + sha), created_at="2026-11-23T08:00:00Z")
        holds = Path(tmp) / "holds.json"
        original = dict(account="coder-seat", label="coder-seat", host="local", agent="codex",
                        model="gpt-6.2-sol", effort="medium", at=123)
        holds.write_text(json.dumps({"old-pane": original}))
        tasks = []

        def reply(*args):
            match args[:2]:
                case "orchestration", "send":
                    assert "dispatch:coder" in args and "#41: Merging" in args
                    tasks.append({"id": "task", "run_id": "run", "created_at": "2026-11-23 08:00:01",
                                  "task_title": "#41: Merging", "spec": args[args.index("--body") + 1]})
                    if lost_receipt:
                        raise tick.OrcaError("lost merger receipt")
                    return {"delivery": "reopened", "followUpOf": "coder", "dispatchId": "merger"}
                case "orchestration", "task-list":
                    return {"tasks": tasks}
                case "orchestration", "dispatch-show":
                    return {"dispatch": {"id": "merger"}}
                case "orchestration", "worker-show":
                    assert args[3] == "merger"
                    return followup_worker("merger", "merge-pane")
            raise AssertionError(args)

        def merging_card(*args):
            assert args[1] == "Merging"
            saved = tick.review_state(db, "run", 41)
            assert (saved["coder"], saved["expected"], saved["coder_terminal"]) == ("merger", "merger", "merge-pane")
            assert json.loads(holds.read_text())["merge-pane"] == original

        with patch.object(pick, "HOLDS", holds), patch.object(tick, "green_report", return_value=sha), \
                patch.object(tick, "review_account", side_effect=AssertionError("mail follow-up picked a new seat")), \
                patch.object(tick, "runtime", side_effect=reply), patch.object(tick, "review_card", side_effect=merging_card):
            if lost_receipt:
                with check.assertRaisesRegex(tick.OrcaError, "lost merger receipt"):
                    tick.review_step(db, "run", [], ticket, state, [verdict])
                holds.write_text("{}")  # the old pane closed and its hold was pruned before recovery
            tick.review_step(db, "run", [], ticket, state, [verdict])
        assert len(tasks) == 1


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
    cli.side_effect = followup_replies("landing", delivery="reopened")
    tick.review_step(db, "run", [], ticket, state, [recorded, verdict])
    assert card.call_args.args[1] == "Merging"
    spec = cli.call_args_list[-2].args[-1]
    assert "auto-merge" in spec and "Closes #41" in spec

# Every task PR lands on the repository's trunk, whatever base the ticket recorded (feature branches are gone).
for repo, trunk in (("hr-relay", "development"), ("dot-agents", "main")):
    ticket, state = review_fixture()
    ticket.update(base="origin/feature/dashboard-state-tests", repo=repo, body="Reviewer: none. Tier: UI, simple.")
    with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "review_card"), \
            patch.object(tick, "runtime", side_effect=followup_replies("land")) as cli:
        tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [recorded])
    landing = cli.call_args_list[0].args[-1]
    assert f"merge origin/{trunk}," in landing and f"open or update the PR into {trunk}," in landing, landing

# A no-review route can land only on green. A red report receives a new coder dispatch.
ticket, state = review_fixture()
ticket["body"] = "Reviewer: none (green CI clears it). Tier: UI, simple."
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", side_effect=[None, sha]), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=[*followup_replies("fix"), *followup_replies("land", previous="fix")]) as cli:
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_args.args[1:] == ("Fixing", state["agent"])
    tick.review_step(db, "run", [], ticket, state, [report("fix", sha, msg_id="green")])
    assert card.call_args.args[1:] == ("Merging", state["agent"])
    assert [call.args[1] for call in cli.call_args_list] == ["send", "worker-show"] * 2

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

# A watch restart, or a reviewer of the same issue in another Run, still owes the coordinator's account:
# the Codex reviewer waits, and the report is read again later.
for table, owner, key in (("watch", "pane", "watch:run:pane"), ("reviews", 41, "reviews:other:41")):
    ticket, state = review_fixture()
    ticket["body"] = "Reviewer: codex / gpt-6.2-astra / medium"
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
    tick.review_state(db, "run", 41)
    db.execute(f"INSERT INTO {table} VALUES (?,?,?)",
               ("run" if table == "watch" else "other", owner, json.dumps({"coordinator": "coord"})))
    with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "review_account") as seat_pick, \
            patch.object(tick, "review_card") as card, patch.object(tick, "runtime") as cli, redirect_stdout(io.StringIO()) as out:
        tick.review_step(db, "run", [], ticket, state, [recorded])
        cli.assert_not_called()
        seat_pick.assert_not_called()
        card.assert_not_called()
        assert f"coordinator restore still owed by ['{key}']" in out.getvalue(), out.getvalue()
        assert not db.execute("SELECT * FROM review_mail").fetchall()

# The coordinator gets its account back even when the reviewer's card write fails, or when a lost start is adopted.
for lose_start in (False, True):
    ticket, state = review_fixture()
    ticket["body"] = "Reviewer: codex / gpt-6.2-astra / medium"
    reviewer_task = {"id": "rtask", "run_id": "run", "task_title": "#41 reviewer report",
                     "spec": tick.reviewer_spec(ticket, sha, False), "created_at": "2026-11-23 08:00:00"}
    active, selects = ["coord"], []
    def codex_review(*args):
        verb = args[:2]
        if verb == ("account", "select"):
            selects.append(args[-1])
            active[0] = args[-1]
            return {}
        if verb == ("account", "list"):
            return {"codex": {"activeAccountId": active[0]}}
        if verb == ("orchestration", "worker-start"):
            if lose_start:
                raise tick.OrcaError("timed out")
            return full_start("reader", "pane")
        if verb == ("orchestration", "task-list"):
            return {"tasks": [reviewer_task]}
        if verb == ("orchestration", "dispatch-show"):
            return {"dispatch": {"id": "reader"}}
        if verb == ("orchestration", "worker-show"):
            return {"dispatch": {"id": "reader", "runId": "run", "taskId": "rtask", "assigneeHandle": "pane"},
                    "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {
                        "agent": "codex", "model": "gpt-6.2-astra", "effort": "medium"}}}}}
        if verb == ("worktree", "show"):
            return {"worktree": {"id": "tree", "path": "/fixture"}}
        raise AssertionError(args)
    db = sqlite3.connect(":memory:")
    mail = [dict(recorded, created_at="2026-11-23T07:59:00Z")]
    with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "worker_settings"), \
            patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
            patch.object(tick, "record_hold"), patch.object(pick, "coordinator", return_value=None), \
            patch.object(pick.account_usage, "load_config", return_value={}), \
            patch.object(tick, "review_card", side_effect=[tick.OrcaError("Project unavailable"), None]) as card, \
            patch.object(tick, "runtime", side_effect=codex_review):
        with check.assertRaisesRegex(tick.OrcaError, "timed out" if lose_start else "Project unavailable"):
            tick.review_step(db, "run", [], ticket, state, mail)
        if lose_start:
            assert selects == ["seat"] and tick.review_state(db, "run", 41)["coordinator"] == "coord"
            with check.assertRaisesRegex(tick.OrcaError, "Project unavailable"):
                tick.review_step(db, "run", [], ticket, state, mail)  # adopts the started reviewer
        assert selects == ["seat", "coord"] and active == ["coord"]
        assert "coordinator" not in tick.review_state(db, "run", 41)
        tick.review_step(db, "run", [], ticket, state, mail)
        assert card.call_args.args[1] == "Reviewing" and selects == ["seat", "coord"]

# Orca confirms a reviewer start had no effects: the coordinator is restored and the report is read again.
ticket, state = review_fixture()
ticket["body"] = "Reviewer: codex / gpt-6.2-astra / medium"
active, selects, refuse = ["coord"], [], [True]
def refused_review(*args):
    if args[:2] == ("account", "select"):
        selects.append(args[-1])
        active[0] = args[-1]
        return {}
    if args[:2] == ("account", "list"):
        return {"codex": {"activeAccountId": active[0]}}
    assert args[:2] == ("orchestration", "worker-start"), args
    if refuse[0]:
        raise tick.StartNotApplied("refused")
    return full_start("reader", "pane")
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "worker_settings"), \
        patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
        patch.object(tick, "record_hold"), patch.object(pick, "coordinator", return_value=None), \
        patch.object(pick.account_usage, "load_config", return_value={}), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=refused_review):
    with check.assertRaisesRegex(tick.OrcaError, "no reviewer start effects"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    saved = tick.review_state(db, "run", 41)
    assert selects == ["seat", "coord"] and active == ["coord"] and "pending" not in saved and "coordinator" not in saved
    card.assert_not_called()
    refuse[0] = False
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_args.args[1] == "Reviewing" and selects == ["seat", "coord", "seat", "coord"]

# The reviewer seat's selection cannot be verified: the owed coordinator account is restored before the next try.
ticket, state = review_fixture()
ticket["body"] = "Reviewer: codex / gpt-6.2-astra / medium"
active, selects, raced = ["coord"], [], [True]
def racing_review(*args):
    if args[:2] == ("account", "select"):
        selects.append(args[-1])
        active[0] = args[-1]
        return {}
    if args[:2] == ("account", "list"):
        if raced[0] and active[0] == "seat":
            raced[0] = False
            return {"codex": {"activeAccountId": "other"}}
        return {"codex": {"activeAccountId": active[0]}}
    assert args[:2] == ("orchestration", "worker-start"), args
    return full_start("reader", "pane")
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=sha), patch.object(tick, "worker_settings"), \
        patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
        patch.object(tick, "record_hold"), patch.object(pick, "coordinator", return_value=None), \
        patch.object(pick.account_usage, "load_config", return_value={}), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=racing_review):
    with check.assertRaisesRegex(tick.OrcaError, "reviewer account selection changed"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    assert tick.review_state(db, "run", 41)["coordinator"] == "coord"
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert selects == ["seat", "coord", "seat", "coord"] and active == ["coord"]
    assert card.call_args.args[1] == "Reviewing"

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
        patch.object(tick, "runtime", side_effect=followup_replies("fix", "pane-c")):
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
        if args[3] == "fix":
            return followup_worker("fix", "pane-fix", previous="retry")
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
    pick.HOLDS.write_text(json.dumps({"pane-old": {"account": "old", "label": "old", "host": "local"},
                                      "pane-new": {"account": "fresh", "label": "fresh", "host": "local"}}))
    tick.review_step(db, "run", [], ticket, state, [recorded, stranger, retried])
    assert card.call_args.args[1:] == ("Fixing", "coder gpt-6.2-sol medium @fresh")
    saved = tick.review_state(db, "run", 41)
    assert (saved["coder"], saved["expected"], saved["coder_terminal"]) == ("fix", "fix", "pane-fix")
    assert json.loads(pick.HOLDS.read_text())["pane-fix"]["account"] == "fresh"
    assert not any("other" in call.args for call in cli.call_args_list)

# A reviewer's failed worker_done needs the operator, unless watch is restarting it from a handoff.
for handoff in ({}, {"handoff": 1}, {"pending": {"why": "handed off with a checkpoint", "crash": False}},
                {"pending": {"why": "handed off at a usage limit", "crash": False}}):
    ticket, state = review_fixture()
    state.update(stage="Reviewing", expected="reader", sha=sha, reviewer={"dispatch": "reader", "terminal": "pane-r"})
    failed = dict(report("reader", "Stopping: cannot read the diff.", msg_id="f1"),
                  payload=json.dumps({"taskId": "task", "dispatchId": "reader", "outcome": "failed"}))
    db = sqlite3.connect(":memory:")
    if handoff:
        db.execute("CREATE TABLE watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
        db.execute("INSERT INTO watch VALUES ('run','pane-r',?)", (json.dumps(handoff),))
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
    with patch.object(tick, "review_card") as card, patch.object(
            tick, "runtime", side_effect=followup_replies("fix") if i < 2 else [{}]) as cli:
        verdict = report("reader", "REVIEW VERDICT: FIX " + sha + "\nmissing a check")
        tick.review_step(db, "run", [], ticket, state, [verdict])
        if i < 2:
            assert card.call_args.args[1] == "Fixing" and "dispatch:coder" in cli.call_args_list[0].args
        else:
            assert card.call_args.args[1] == "Needs you" and "run:run" in cli.call_args.args
            # Orca refuses --type escalation from the Run's own pane (sender_not_assignee).
            sent = cli.call_args.args
            assert sent[:2] == ("orchestration", "send") and sent[sent.index("--type") + 1] == "status"
            assert all(text in cli.call_args.args[-1] for text in ["round one", "round two", "missing a check"])
        tick.review_step(db, "run", [], ticket, state, [verdict])
        assert cli.call_count == (2 if i < 2 else 1)

# A PUSH whose head moved or whose CI is not green goes back to the coder, saying why.
ticket, state = review_fixture()
state.update(stage="Reviewing", expected="reader", sha=sha)
with patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=followup_replies("fix")) as cli:
    tick.review_step(sqlite3.connect(":memory:"), "run", [], ticket, state, [report("reader", "REVIEW VERDICT: PUSH " + sha)])
    sent = cli.call_args_list[0].args
    assert card.call_args.args[1] == "Fixing" and "dispatch:coder" in sent
    assert (f"The reviewer's PUSH of {sha} was voided: the branch head moved or its task CI is not green.\n"
            + tick.GREEN_INSTRUCTIONS) in sent[-1]

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
    assert step.call_args.args[-1].successes["coder"] == [recorded] and step.call_args.args[-2]["expected"] == "coder"
    assert cli.call_args_list[0].args == ("orchestration", "inbox", "--terminal", "run:run", "--limit", "10000", "--full")

# Bug 3 (#488, #517): restarts left two "#41 coder" Tasks; the one whose attempt reported is reviewed.
with patch.object(tick, "tickets", return_value=[ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
        patch.object(tick, "command", return_value="https://github.com/julianthant/hr-relay.git\n"), \
        patch.object(tick, "review_step") as step, patch.object(tick, "runtime") as cli:
    cli.side_effect = [{"messages": [recorded]}, {"tasks": [{"title": "#41 coder", "id": "task_old"}, {"title": "#41 coder", "id": "task"}]},
                       {"dispatch": {"id": "old"}}, {"dispatch": {"id": "coder"}},
                       {"dispatch": {"id": "coder", "runId": "run", "taskId": "task"},
                           "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {"model": "gpt-6.2-sol"}}}}},
                       {"worktree": {"id": "tree", "path": "/fixture", "branch": "refs/heads/task/ui/header"}}]
    tick.review_tick(args, sqlite3.connect(":memory:"))
    assert step.call_args.args[-2]["expected"] == "coder"

# An uncertain reservation is isolated to its ticket.
with sqlite3.connect(":memory:") as db:
    ticket, state = review_fixture()
    other = dict(ticket, number=42)
    tick.review_state(db, "run", 41)
    tick.save_review(db, "run", 41, dict(state, host="local", pending="missing", sha=sha))
    tick.save_review(db, "run", 42, dict(state, host="local", coder="other-coder", expected="other-coder"))
    other_report = report("other-coder", "red at the old head", msg_id="other-report")
    cli = [{"messages": [other_report]}, {"tasks": []}, *followup_replies("fix", previous="other-coder")]
    with patch.object(tick, "supervisor", return_value=([], "run")), \
            patch.object(tick, "tickets", return_value=[ticket, other]), \
            patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card"), \
            patch.object(tick, "runtime", side_effect=cli), patch.object(tick, "log") as log:
        tick.review_tick(SimpleNamespace(issue=None, coder=None), db)
    assert tick.review_state(db, "run", 41)["pending"] == "missing"
    assert tick.review_state(db, "run", 42)["stage"] == "Fixing"
    log.assert_any_call("Needs you", {"issue": 41, "reason": "inspect uncertain Orca action for message missing before continuing"})

# A manual follow-up may create both a new Task and a new pane, including while review has a reservation.
for pending in (False, True):
    ticket, state = review_fixture()
    state.update(coder_terminal="old-pane")
    original = dict(recorded, created_at="2026-11-23T08:00:00Z")
    manual = dict(report("manual", "red at the old head", msg_id="manual-report"), sequence=3, subject="#41 follow-up",
                  from_handle="manual-pane",
                  payload={"taskId": "manual-task", "dispatchId": "manual", "outcome": "succeeded"})

    def manual_lineage(*args):
        if args[:2] == ("orchestration", "send"):
            assert "dispatch:manual" in args
            return {"delivery": "follow_up", "followUpOf": "manual", "dispatchId": "fix"}
        if args[:2] == ("orchestration", "task-list"):
            return {"tasks": []}  # no reviewer was created for the superseded report
        if args[3] == "coder":
            return followup_worker("coder", "old-pane", previous=None)
        if args[3] == "manual":
            return followup_worker("manual", "manual-pane", task="manual-task")
        assert args[3] == "fix"
        return followup_worker("fix", "fix-pane", previous="manual")

    with sqlite3.connect(":memory:") as db, patch.object(tick, "green_report", return_value=None), \
            patch.object(tick, "review_card"), \
            patch.object(tick, "runtime", side_effect=manual_lineage):
        if pending:
            tick.review_state(db, "run", 41)
            tick.save_review(db, "run", 41, dict(state, pending=original["id"], sha=sha))
        tick.review_step(db, "run", [], ticket, state, [original, manual])
        assert tick.review_state(db, "run", 41)["coder"] == "fix"

# An explicit coder that reports to another Run fails loudly instead of exiting quietly.
args = SimpleNamespace(issue=41, coder="ctx_elsewhere", run=None, anchor=None)
with patch.object(tick, "tickets", return_value=[ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
        patch.object(tick, "review_step") as step, patch.object(tick, "runtime") as cli:
    cli.side_effect = [{"messages": [recorded]}, {"tasks": []},
                       {"dispatch": {"id": "ctx_elsewhere", "runId": "run_other", "taskId": "task"}}]
    with check.assertRaisesRegex(tick.OrcaError, "ctx_elsewhere belongs to Run run_other, not the bound Run run"):
        tick.review_tick(args, sqlite3.connect(":memory:"))
    step.assert_not_called()
    # Saved review state does not hide the mismatch.
    saved_db = sqlite3.connect(":memory:")
    tick.review_state(saved_db, "run", 41)
    tick.save_review(saved_db, "run", 41, dict(state))
    cli.side_effect = [{"messages": [recorded]}, {"tasks": []},
                       {"dispatch": {"id": "ctx_elsewhere", "runId": "run_other", "taskId": "task"}}]
    with check.assertRaisesRegex(tick.OrcaError, "ctx_elsewhere belongs to Run run_other"):
        tick.review_tick(args, saved_db)
    step.assert_not_called()

# A partial Project write resumes on restart without a second follow-up.
ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
with patch.object(tick, "green_report", return_value=None), patch.object(tick, "review_card",
        side_effect=[tick.OrcaError("Project unavailable"), None]) as card, patch.object(tick, "runtime",
        side_effect=followup_replies("fix")) as cli:
    with check.assertRaisesRegex(tick.OrcaError, "Project unavailable"):
        tick.review_step(db, "run", [], ticket, state, [recorded])
    tick.review_step(db, "run", [], ticket, state, [recorded])
    assert card.call_count == 2 and [call.args[1] for call in cli.call_args_list] == ["send", "worker-show"]

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

# Close and fill must see a worker started after watch loaded the Run's inventory.
ticket, state = review_fixture()
workers = tick.WorkerInventory("run")
reader = {"dispatchId": "reader"}
with sqlite3.connect(":memory:") as db, patch.object(tick, "green_report", return_value=sha), \
        patch.object(tick, "review_account", return_value={"id": "seat", "label": "review"}), \
        patch.object(tick, "held_label", return_value="coder"), patch.object(tick, "worker_settings"), \
        patch.object(tick, "record_hold"), patch.object(tick, "review_card"), patch.object(tick, "runtime", side_effect=[
            {"workers": [], "page": {"hasMore": False}}, {}, {"claude": {"activeAccountId": "seat"}},
            full_start("reader", "pane"), {"workers": [reader], "page": {"hasMore": False}},
        ]):
    assert workers.rows == ()
    tick.review_step(db, "run", [], ticket, state, [recorded], inventory=workers)
    assert workers.rows == (reader,)

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
    with patch.object(tick, "runtime", side_effect=[*followup_replies("coder2"), {"state": "released"}]) as cli:
        tick.review_step(db, "run", [], ticket, state, [fix])
        tick.review_step(db, "run", [], ticket, state, [fix])
        assert cli.call_args.args == ("orchestration", "worker-release", "--dispatch", "reader")
        assert cli.call_count == 3 and "pane" not in json.loads(pick.HOLDS.read_text())

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
                             "assigneeHandle": "delivered-pane" if dispatch == delivered else "manual-pane",
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

# A Run's unrelated successes cannot delay Fixing or recovery of an uncertain reviewer start.
with sqlite3.connect(":memory:") as db:
    ticket, state = review_fixture()
    ticket["stage"] = "Fixing"
    state.update(stage="Fixing", host="local", coder_terminal="coder-pane")
    pending = dict(report("pending-coder", sha, msg_id="pending"), sequence=0, created_at="2026-11-22T22:35:40Z")
    other = dict(ticket, number=42)
    tick.review_state(db, "run", 41)
    tick.save_review(db, "run", 41, state)
    tick.save_review(db, "run", 42, dict(state, pending="pending", sha=sha, path="/other",
                                        coder="pending-coder", expected="pending-coder", coder_terminal="other-pane"))
    chained = dict(report("retry", sha, msg_id="chained"), from_handle="fresh-pane", sequence=2)
    messages = [pending, recorded, chained, *[
        dict(report(f"history-{i}", "unrelated", msg_id=f"old-{i}"), from_handle=f"pane-{i}", sequence=i + 3,
             payload={"dispatchId": f"history-{i}", "taskId": f"old-task-{i}", "outcome": "succeeded"})
        for i in range(700)
    ]]

    def report_worker(dispatch, task, workspace, terminal, **ancestry):
        row = {"dispatchId": dispatch, "taskId": task, "runId": "run", "agentTerminalHandle": terminal,
               "terminalState": "released", "dispatchStatus": "completed",
               "projection": {"workspace": {"id": workspace}}}
        show = {"dispatch": {"id": dispatch, "runId": "run", "taskId": task, "status": "completed", **ancestry},
                "projection": {"resource": {"releaseState": "released"}}}
        return row, show

    history = [
        report_worker("coder", "task", "repo::/fixture", "coder-pane"),
        report_worker("pending-coder", "pending-task", "repo::/other", "other-pane"),
        report_worker("manual", "manual-task", "repo::/fixture", "manual-pane", followUpOf="coder"),
        report_worker("retry", "manual-task", "repo::/fixture", "fresh-pane", retryOfDispatchId="manual"),
        *(report_worker(f"history-{i}", f"old-task-{i}", f"repo::/old-{i}", f"pane-{i}") for i in range(700)),
    ]
    workers = [row for row, _ in history]
    details = {row["dispatchId"]: show for row, show in history}
    calls = []

    def review_runtime(*args):
        calls.append(args)
        match args[1]:
            case "inbox":
                return {"messages": messages}
            case "task-list":
                return {"tasks": []}
            case "worker-list":
                return {"workers": workers, "page": {"hasMore": False}}
            case "worker-show":
                return details[args[3]]
        raise AssertionError(args)

    with patch.object(tick, "runtime", review_runtime), patch.object(tick, "supervisor", return_value=([], "run")), \
            patch.object(tick, "tickets", return_value=[other, ticket]), patch.object(tick, "held_label", return_value="coder"), \
            patch.object(tick, "green_report", return_value=sha) as verify, patch.object(tick, "log") as captured, \
            patch.object(tick, "review_account", side_effect=tick.UncertainReview("simulated unavailable seat")):
        for expected_reads in ({"retry", "manual"}, set()):
            calls.clear()
            verify.reset_mock()
            tick.review_tick(SimpleNamespace(issue=None, coder=None), db)
            verify.assert_called_once_with(ticket, chained["subject"] + "\n" + chained["body"])
            assert [args[1] for args in calls].count("inbox") == 1
            shows = [args[3] for args in calls if args[1] == "worker-show"]
            assert len(shows) <= 30, f"700 unrelated successes produced {len(shows)} worker-show calls"
            assert set(shows) == expected_reads, shows
        assert any(args.args[1]["issue"] == 42 and "inspect uncertain Orca action" in args.args[1]["reason"]
                   for args in captured.call_args_list)
        assert tick.review_state(db, "run", 42)["pending"] == "pending"

def close_fixture(tmp, *, merged=True, dirty=False, refused=False, release_uncertain=False, remote_race=False,
                  released_projection=False, release_race=False,
                  release_unknown=False, ticks=1, card_fails=False, send_lost=False, rewritten=False, pre_upgrade=None,
                  rollback=False, remote_worker=False):
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
    workers = [dict(dispatchId=dispatch, runId="run", taskId=dispatch + "-task", agentTerminalHandle=terminal if owns else None,
                    dispatchStatus="completed", projection={"workspace": {"id": worktree}, "host": {"kind": "local"}})
               for dispatch, terminal, owns in (("ctx_merge", "term_coder", True),
                                                ("ctx_review", "term_reviewer", True),
                                                ("ctx_initial", "term_coder", False))]
    if released_projection:
        workers[1]["terminalState"] = "released"
    if remote_worker:
        workers.insert(1, {
            "agentTerminalHandle": "term_wsl",
            "dispatchId": "ctx_wsl",
            "dispatchStatus": "dispatched",
            "resource": None,
            "runId": "run",
            "server": {"environmentId": "env_wsl", "name": "Juzzark-WSL", "asOf": "2026-11-23T18:00:00Z"},
            "taskId": "task_wsl",
            "terminalState": "active",
            "workerState": "ready",
        })
    events = []
    tree = dict(id=worktree, path=path, branch=ref, isMainWorktree=False)
    present = True
    mailbox = []
    applied = {}  # Orca's recorded send results by request id
    local_ref = sha
    remote_present = remote_race
    pending = "held\nOriginal body:\nkept" if pre_upgrade == "original" else None  # a reason with the wrapper's line

    def cli(*args):
        nonlocal local_ref, remote_present
        events.append(args)
        if args[:2] == ("gh", "api"):
            return json.dumps([{"data": {"repository": {"issues": {"nodes": [issue]}}}}])
        if args[0] == "gh":
            assert args[1:3] == ("project", "item-edit")
            if card_fails and sum(e[:3] == ("gh", "project", "item-edit") for e in events) == 1:
                raise tick.OrcaError("Project unavailable")
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

    def rejected(subject, body):
        # The shape recorded from a rejected escalation on 2026-11-23.
        return {"type": "status", "subject": "Rejected escalation: " + subject,
                "body": "Orca rejected this escalation: No active Dispatch belongs to this message sender.\n\n"
                        "Original body:\n" + body}

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
            worker = next(w for w in workers if w["dispatchId"] == args[3])
            terminal = worker["agentTerminalHandle"] or "term_coder"
            status = "dispatched" if release_race and args[3] == "ctx_review" else worker["dispatchStatus"]
            return {"dispatch": {"id": args[3], "runId": "run", "taskId": worker["taskId"],
                                 "assigneeHandle": terminal, "status": status},
                    "worker": {"dispatchId": args[3], "worktreeId": worktree, "effects": [
                        {"kind": "terminal", "role": "agent", "action": "reused", "id": terminal}]}}
        if args[:2] == ("orchestration", "worker-release"):
            if release_uncertain and args[3] == "ctx_review":
                return {"state": "release_pending", "reason": pending}
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
            request = args[args.index("--retry-request") + 1] if "--retry-request" in args else None
            if request in applied:
                return applied[request]  # Orca replays an applied request
            subject, body = args[args.index("--subject") + 1], args[args.index("--body") + 1]
            mailbox.append(rejected(subject, body) if rewritten else
                           {"type": args[args.index("--type") + 1], "subject": subject, "body": body})
            if request:
                applied[request] = {}
            if send_lost and len(applied) == 1:
                raise tick.OrcaError("timed out")  # Orca accepted it; the reply is lost
            return {}
        if args[:2] == ("orchestration", "inbox"):
            if pre_upgrade == "unlisted":
                return {"messages": [], "truncated": True}  # the notice is past the listing's end
            return {"messages": mailbox}
        raise AssertionError(args)

    actor = ["--from", "term_anchor", "--run", "run"] if ticks > 1 else []
    with sqlite3.connect(":memory:") as db, patch.object(pick, "HOLDS", holds), \
            patch.object(tick, "supervisor", return_value=(actor, "run")), \
            patch.object(tick, "project_fields", return_value=("project", [("status", "progress"),
                         ("stage", "coding"), ("agent", None)], ("stage", "needs"))), \
            patch.object(tick, "runtime", side_effect=orca), patch.object(tick, "command", side_effect=cli), \
            redirect_stdout(io.StringIO()) as output:
        if pre_upgrade:
            # A notice recorded before requests were saved, in the table's pre-upgrade shape.
            reason = f"ctx_review: release unconfirmed (release_pending, {pending})"
            db.execute("CREATE TABLE close_notice (run TEXT, issue INTEGER, reason TEXT, sent INTEGER, carded INTEGER,"
                       " PRIMARY KEY(run,issue))")
            db.execute("INSERT INTO close_notice VALUES ('run', 41, ?, 0, 0)", (reason,))
            db.commit()
            # Another issue's notice, or a status whose subject only ends in this one's, may carry this reason.
            legacy = {"foreign": "#141", "foreign_wrapped": "#141", "prefixed": "Fwd: #41"}.get(pre_upgrade, "#41")
            legacy += ": Needs you after close"
            if pre_upgrade in ("rewritten", "foreign_wrapped"):
                mailbox.append(rejected(legacy, reason))
            elif pre_upgrade != "unsent":
                # An older blocked state's notice carries that state's error.
                mailbox.append({"type": "status", "subject": legacy,
                                "body": "worktree contains local changes or untracked files" if pre_upgrade == "other"
                                else reason})
        unconfirmed = 0
        for _ in range(ticks):
            try:
                tick.close_tick(args, db)
            except tick.OrcaError as e:
                unconfirmed += "incomplete Run mailbox" in str(e)
                # A later tick retries only the unconfirmed step.
                assert card_fails or send_lost or pre_upgrade == "unlisted"
        if rollback:
            # A tick from before request ids records a later state, then stops after its send.
            old = "worktree contains local changes or untracked files"
            db.execute("INSERT OR REPLACE INTO close_notice VALUES (?,?,?,0,0)", ("run", 41, old))
            assert db.execute("SELECT reason,sent,carded FROM close_notice WHERE run=? AND issue=?",
                              ("run", 41)).fetchone() == (old, 0, 0)
            mailbox.append({"type": "status", "subject": "#41: Needs you after close", "body": old})
            release_uncertain = False  # the next tick blocks on that state
            tick.close_tick(args, db)
        notice = db.execute("SELECT sent,carded,request FROM close_notice LEFT JOIN close_request USING (run,issue,reason)"
                            " WHERE run='run' AND issue=41").fetchone()
    expected_releases = ["ctx_merge", "ctx_initial"] if released_projection or release_race else [
        "ctx_merge", "ctx_review", "ctx_initial"]
    assert [event[3] for event in events if event[:2] == REL] == expected_releases * (ticks + rollback)
    if released_projection:
        assert not any(e[:4] == ("orchestration", "worker-show", "--dispatch", "ctx_review") for e in events)
    expected_holds = {"term_other", "term_reviewer"} if release_uncertain or release_race else {"term_other"}
    assert set(json.loads(holds.read_text())) == expected_holds
    edits = [event[-3:] for event in events if event[:3] == ("gh", "project", "item-edit")]
    if merged and not dirty and not refused and not release_uncertain and not release_race:
        assert not present and not local_ref
        assert edits == [("--field-id", "agent", "--clear"), ("--field-id", "stage", "--clear")]
        assert any(event[0] == "git" and event[3] == "push" for event in events) == remote_race
    else:
        assert present and local_ref == sha
        assert edits == [("stage", "--single-select-option-id", "needs")] * (2 if card_fails or rollback else 1)
        notices = [line for line in output.getvalue().splitlines() if line.startswith("Needs you")]
        assert len(notices) == (0 if pre_upgrade else 1), notices
        if notices:
            assert "tick.py close --repo-path" in notices[0] and "--issue 41" in notices[0], notices
            if actor:
                assert "--anchor term_anchor --run run" in notices[0], notices
                assert "plain shell outside Orca" in notices[0], notices
        # One notice in the Run's mailbox, pre-upgrade or new, besides an older state's notice.
        assert len(mailbox) == (2 if rollback or pre_upgrade in ("other", "foreign", "foreign_wrapped", "prefixed")
                                else 1), mailbox
        sends = [event for event in events if event[:2] == ("orchestration", "send")]
        assert all("--type" in e and e[e.index("--type") + 1] == "status"
                   and e[e.index("--to") + 1] == "run:run" for e in sends), sends
        assert all("--retry-request" in e for e in sends), sends
        assert len({e[e.index("--retry-request") + 1] for e in sends}) <= 1, sends
        if rollback:
            assert len(sends) == 1 and notice == (1, 1, None), notice  # the old tick's notice was found, not resent
        elif pre_upgrade == "unlisted":
            assert not sends and notice == (0, 1, None) and unconfirmed == ticks, notice  # pending kept
        elif pre_upgrade in ("exact", "rewritten", "original"):
            assert not sends and notice[0] == 1, notice
        elif pre_upgrade in ("unsent", "other", "foreign", "foreign_wrapped", "prefixed"):
            assert len(sends) == 1 and notice[0] == 1 and notice[2], notice
        else:
            assert len(sends) == (2 if send_lost else 1), sends  # a lost reply's retry replays the same request
            assert notice[0] == 1 and notice[2], notice
        assert any(event[:2] == RM for event in events) == refused


for scenario in ({}, {"remote_worker": True}, {"merged": False}, {"dirty": True}, {"refused": True},
                 {"released_projection": True}, {"release_race": True},
                 {"release_uncertain": True}, {"remote_race": True}, {"release_unknown": True},
                 {"release_uncertain": True, "ticks": 2}, {"release_uncertain": True, "ticks": 3, "card_fails": True},
                 {"release_uncertain": True, "ticks": 3, "send_lost": True},
                 {"release_uncertain": True, "ticks": 3, "send_lost": True, "rewritten": True},
                 {"release_uncertain": True, "dirty": True, "ticks": 2, "rollback": True},
                 *({"release_uncertain": True, "ticks": 3, "pre_upgrade": pre_upgrade}
                   for pre_upgrade in ("exact", "rewritten", "unsent", "unlisted", "other", "foreign", "foreign_wrapped",
                                       "prefixed", "original"))):
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
assert tick.screen_limit(CODEX_SWITCHED, {"agent": "codex", "model": "gpt-6.2-astra"}) is None
assert tick.screen_limit(CODEX_SWITCHED, {"agent": "codex", "model": None}) is None
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
        self.terminal_state = None
        self.liveness, self.failure, self.wait = "live", None, None
        self.seats = ["jzaw", "x2"]  # the limited account is first in accounts.toml order
        self.superseded = False
        self.task_status = "dispatched"
        self.follow_row = False
        self.replacement = None
        self.transferred = False  # the follow-up owns the current attempt's pane, which lists no older attempt
        # The follow-up's own worker in its fresh pane, listed once it exists.
        self.follow_status, self.follow_liveness = "succeeded", "exited"
        self.effort = "medium" if model else None  # a follow-up Dispatch records no model or effort
        self.follow_of = None  # the Dispatch this one follows up
        self.orig_title = "#553 reviewer"  # the Task of the Dispatch it follows up
        self.orig_parent = None  # the Dispatch that original itself follows up, on a "#553 reviewer" Task
        self.bare = False  # the Task's newest attempt has no terminal or resource record
        self.started_retry = None  # whether the restart passed --retry-of
        self.start_error = None
        self.lose_start = False  # Orca starts the retry, but the response is lost
        self.note_error = None
        self.select_error = None  # selecting the coordinator's account fails
        self.stop_lost = False  # worker-stop applies, but its reply is lost
        self.active, self.fresh = "coord", None  # the coordinator bot's Codex account; pick.coordinator's choice
        self.events, self.cards, self.now = [], [], 1_795_389_500
        self.head = "a" * 40
        self.next_start = ("ctx_restart", "term_restart")
        self.ticket = dict(number=553, item="card", agent=f"coder {model} medium @jzaw", stage="Coding")
        self.title = "#553 coder"
        self.spec = "/goal Implement julianthant/hr-relay#553 and report a green SHA."
        self.worktree = "repo::/wt"
        self.notes = []
        self.followups = []  # Tasks Orca created for mail to a settled Dispatch
        self.note_lost = False  # the send to the replacement applies, but its reply is lost
        self.follow_retried = False  # Orca retried the follow-up Task; its newest attempt has no followUpOf
        self.follow_terminal = "term_follow"  # the fresh pane Orca starts a follow-up in; None before its worker record
        self.tickets_error = None
        self.receipts = {}  # Orca's recorded receipts by request id
        self.db = sqlite3.connect(":memory:")

    def orca(self, *args):
        self.events.append(args)
        verb = args[:2]
        if verb == ("orchestration", "task-list"):
            return {"tasks": [{"id": "task_coder", "run_id": "run", "task_title": self.title, "status": self.task_status,
                               "created_at": "2026-11-24 19:00:00", "spec": self.spec},
                              *([dict(id="task_replacement", run_id="run", task_title=self.replacement["title"],
                                      spec=self.replacement["spec"],
                                      created_at="2026-11-24 19:01:00" if self.replacement["newer"] else "2026-11-24 18:59:00")]
                                if self.replacement and self.replacement["task_listed"] else []),
                              *self.followups,
                              *([{"id": "task_orig", "run_id": "run", "task_title": self.orig_title}]
                                if self.follow_of or (self.replacement or {}).get("follow_of") else []),
                              *([{"id": "task_root", "run_id": "run", "task_title": "#553 reviewer"}] if self.orig_parent else [])]}
        if verb == ("orchestration", "worker-list"):
            row = dict(dispatchId=self.dispatch, taskId="task_coder", runId="run", workerState=self.state,
                       dispatchStatus=self.status, agentTerminalHandle=self.terminal, terminalState=self.terminal_state,
                       resource={"ownerDispatchId": "ctx_follow" if self.transferred else self.dispatch},
                       projection={"host": {"kind": "local"}, "workspace": {"id": self.worktree},
                                   "liveness": {"verdict": self.liveness}})
            manual, remote = [], []
            if self.replacement:
                manual = [dict(row, dispatchId="ctx_manual", taskId="task_replacement", agentTerminalHandle="term_manual",
                               dispatchStatus=self.replacement["status"], workerState="ready",
                               resource={"ownerDispatchId": "ctx_manual"},
                               projection=dict(row["projection"], workspace={"id": self.replacement["worktree"]},
                                               liveness={"verdict": self.replacement["liveness"]}))]
                if self.replacement["remote"]:
                    if "--include-remote" in args:
                        remote = [dict(manual[0], server={"name": "peer"}, projection=None)]
                    manual = []
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
            # A settled follow-up Task's newer Dispatch on the same pane.
            follow = [dict(row, dispatchId="ctx_follow", taskId="task_follow", dispatchStatus="succeeded",
                           workerState="succeeded", resource={"ownerDispatchId": "ctx_follow"})] if self.follow_row else []
            fresh = [dict(row, dispatchId="ctx_follow", taskId="task_follow", agentTerminalHandle=self.follow_terminal,
                          dispatchStatus=self.follow_status, workerState="ready" if self.follow_status == "dispatched" else "succeeded",
                          resource={"ownerDispatchId": "ctx_follow"},
                          projection={"host": {"kind": "local"}, "liveness": {"verdict": self.follow_liveness}})
                     ] if self.followups and self.follow_terminal and self.follow_terminal != self.terminal else []
            olds = [] if self.transferred else [old]
            if self.bare:
                newest = dict(row, dispatchId="ctx_newest", dispatchStatus="succeeded", workerState="succeeded",
                              agentTerminalHandle=None, resource=None)
                return {"workers": [newest, retried], "page": {"hasMore": False, "nextCursor": None}}
            manual_is_newer = self.replacement and self.replacement["newer"]
            peers = [*manual, row] if manual_is_newer else [row, *manual]
            return {"workers": [*fresh, *follow, *before, *peers, *olds, retried, *replaced, *remote],
                    "page": {"hasMore": False, "nextCursor": None}}
        if verb == ("worktree", "show"):
            assert args[2:] == ("--worktree", "id:repo::/wt"), args
            return {"worktree": {"id": "repo::/wt", "path": str(self.tmp), "branch": "task/test/work"}}
        if verb == ("orchestration", "dispatch-show") and args[3] == "task_follow":
            current = "ctx_follow_retry" if self.follow_retried else "ctx_follow"
            return {"dispatch": {"id": current, "status": "dispatched", "taskId": None, "assigneeHandle": None}}
        if verb == ("orchestration", "worker-show") and args[3] == "ctx_follow_retry":
            return {"dispatch": {"id": "ctx_follow_retry", "runId": "run", "taskId": "task_follow",
                                 "retryOfDispatchId": "ctx_follow", "followUpOf": None},
                    **self.follow_worker("ctx_follow_retry")}
        if verb == ("orchestration", "worker-show") and args[3] == "ctx_follow":
            return {"dispatch": {"id": "ctx_follow", "runId": "run", "taskId": "task_follow", "followUpOf": "ctx_restart",
                                 "status": self.follow_status, "assigneeHandle": self.follow_terminal, "lastFailure": None},
                    "projection": {"liveness": {"verdict": self.follow_liveness}},
                    **self.follow_worker("ctx_follow")}
        if verb == ("orchestration", "dispatch-show"):
            # Recorded shape: only the newest Dispatch's id and status.
            return {"dispatch": {"id": self.dispatch, "status": self.status, "taskId": None, "assigneeHandle": None}}
        if verb == ("orchestration", "worker-show") and args[3] == "ctx_root":
            return {"dispatch": {"id": "ctx_root", "taskId": "task_root", "followUpOf": None}}
        if verb == ("orchestration", "worker-show") and args[3] == "ctx_orig":
            return {"dispatch": {"id": "ctx_orig", "taskId": "task_orig", "followUpOf": self.orig_parent}, "worker": {"startOptions": {"launch": {
                "effective": {"agent": "codex", "model": "gpt-6.2-sol", "effort": "xhigh"}}}}}
        if verb == ("orchestration", "worker-show") and args[3] == "ctx_manual":
            return {"dispatch": {"id": "ctx_manual", "runId": "run", "taskId": "task_replacement",
                                 "status": self.replacement["status"], "followUpOf": self.replacement["follow_of"]},
                    "worker": {"state": "ready", "worktreeId": self.replacement["worktree"],
                               "startOptions": {"launch": {"effective": {}}}},
                    "projection": {"liveness": {"verdict": self.replacement["liveness"]}}}
        if verb == ("orchestration", "worker-show"):
            assert args[3] == self.dispatch, args
            retry = "ctx_coder" if self.dispatch == "ctx_restart" and self.started_retry is not False else None
            return {"dispatch": {"id": self.dispatch, "runId": "run", "taskId": "task_coder", "status": self.status,
                                 "assigneeHandle": self.terminal, "lastFailure": self.failure,
                                 "retryOfDispatchId": retry, "followUpOf": self.follow_of},
                    "worker": {"state": self.state, "worktreeId": self.worktree, "dispatchId": self.dispatch,
                               "effects": [{"kind": "terminal", "role": "agent", "action": "created", "id": self.terminal}],
                               "startOptions": {"launch": {"effective": {"agent": self.agent, "model": self.model,
                                                                         "effort": self.effort}}}},
                    "projection": {"liveness": {"verdict": self.liveness},
                                   "resource": {"releaseState": self.terminal_state}},
                    "terminal": {"handle": self.terminal, "agentWait": self.wait}, "observation": {"agentWait": self.wait}}
        if verb == ("terminal", "read"):
            if args[3] == "term_manual":
                return {"terminal": {"handle": "term_manual", "tail": self.replacement["screen"], "source": "screen"}}
            return {"terminal": {"handle": self.terminal, "tail": self.screen, "source": "screen"}}
        if verb == ("orchestration", "worker-stop") and args[3] == "ctx_follow":
            self.follow_status = "failed"
            return {"dispatchId": "ctx_follow", "processAction": "closed_agent_terminal"}
        if verb == ("orchestration", "worker-stop"):
            self.state, self.status, self.failure = "stopped", "failed", "stopped"
            if self.stop_lost:
                self.stop_lost = False
                raise tick.OrcaError("timed out")
            return {"dispatchId": self.dispatch, "processAction": "closed_agent_terminal"}
        if verb == ("account", "select"):
            if self.select_error and args[-1] == "coord":
                raise self.select_error
            self.active = args[-1]
            return {}
        if verb == ("account", "list"):
            return {self.agent: {"activeAccountId": self.active}}
        if verb == ("orchestration", "worker-start"):
            if self.start_error:
                raise self.start_error
            if args[args.index("--task") + 1] == "task_follow":
                # The follow-up Task retries in its own pane's worktree.
                assert args[args.index("--retry-of") + 1] == "ctx_follow" and self.follow_status == "failed", args
                self.follow_status, self.followups = "retried", []
                return full_start("ctx_follow2", "term_follow2")
            # The same Task retries: no second same-titled Task for review discovery to refuse.
            assert args[args.index("--task") + 1] == "task_coder"
            # Orca refuses --retry-of for a ready Task (task_not_startable).
            assert ("--retry-of" not in args) if self.task_status == "ready" else args[args.index("--retry-of") + 1] == self.dispatch
            self.started_retry = "--retry-of" in args
            assert "--spec" not in args and "--task-title" not in args and self.status == "failed", args
            assert args[args.index("--worktree") + 1] == "id:repo::/wt"
            self.dispatch, self.terminal = self.next_start
            self.status, self.state, self.liveness, self.failure = "dispatched", "ready", "live", None
            self.screen = CLAUDE_WORKING
            if self.lose_start:
                raise tick.OrcaError("timed out")
            return full_start(self.dispatch, self.terminal)
        if verb == ("orchestration", "send"):
            if args[args.index("--to") + 1] == "dispatch:ctx_restart":
                if self.note_error:
                    raise self.note_error
                request = args[args.index("--retry-request") + 1] if "--retry-request" in args else None
                if request in self.receipts:
                    return self.receipts[request]  # Orca replays an applied request
                self.notes.append(args[args.index("--body") + 1])
                receipt = {"delivery": "queued"}
                if self.status != "dispatched":
                    # Mail to a settled Dispatch starts a follow-up Task and Dispatch.
                    self.followups.append({"id": "task_follow", "run_id": "run", "spec": args[args.index("--body") + 1],
                                           "task_title": args[args.index("--subject") + 1]})
                    receipt = {"delivery": "follow_up", "followUpOf": "ctx_restart", "dispatchId": "ctx_follow"}
                if request:
                    self.receipts[request] = receipt
                if self.note_lost:
                    self.note_lost = False
                    raise tick.OrcaError("timed out")
                return receipt
            return {"delivery": "queued"}
        raise AssertionError(args)

    def add_replacement(self, title="#553 coder", *, worktree="repo::/wt", live=True, newer=True, spec="", remote=False,
                        screen=CLAUDE_WORKING, follow_of=None, task_listed=True):
        self.replacement = dict(title=title, worktree=worktree, newer=newer, spec=spec, remote=remote,
                                status="dispatched" if live else "completed", liveness="live" if live else "exited",
                                screen=screen, follow_of=follow_of, task_listed=task_listed)

    def checkpoint_failure(self):
        self.status, self.state, self.liveness = "failed", "failed", "exited"
        self.failure = json.dumps({"provenance": "worker_report", "outcome": "failed", "reportPath": "/wt/.handoff-553.md"})

    def follow_worker(self, dispatch):
        """Orca's worker record for a follow-up, omitted while it has none."""
        return {"worker": {"dispatchId": dispatch, "state": "ready", "worktreeId": "repo::/wt",
                           "startOptions": {"launch": {"effective": {"agent": self.agent, "model": None, "effort": None}}},
                           "effects": [{"kind": "terminal", "role": "agent", "action": "created", "id": self.follow_terminal}]}
                } if self.follow_terminal else {}

    def git(self, *args):
        assert args == ("git", "-C", str(self.tmp), "rev-parse", "--verify", "refs/heads/task/test/work"), args
        return self.head + "\n"

    def tick(self, minutes=0):
        self.now += minutes * 60
        self.events.clear()
        seat = lambda cfg, *route, now=None: (
            (next(({"id": s, "label": s} for s in self.seats if s in [r["id"] for r in cfg["account"]]), None), ["full"]))
        cfg = {"account": [{"id": "jzaw"}, *({"id": s} for s in self.seats)]}
        record = lambda ticket, stage, agent: (self.cards.append((stage, agent)), ticket.update(agent=agent))
        out = io.StringIO()
        with patch.object(tick, "supervisor", return_value=([], "run")), patch.object(tick, "runtime", self.orca), \
                patch.object(tick, "command", self.git), \
                patch.object(tick, "tickets", side_effect=self.tickets_error, return_value=[self.ticket]), patch.object(tick, "review_card", record), \
                patch.object(pick, "main", return_value=0) as self.pick_main, patch.object(pick, "due", return_value=[]), \
                patch.object(pick, "record_usage_handoff") as self.usage_handoff, \
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


# Watch rotates a limited merger whose new pane inherited the coder's account from a PUSH follow-up.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    ticket, state = review_fixture()
    route = ("codex", f.model, "medium", None)
    ticket.update(f.ticket, route=route)
    state.update(stage="Reviewing", sha=sha, expected="reader", coder_terminal="term_coder")
    verdict = report("reader", "REVIEW VERDICT: PUSH " + sha)
    with patch.object(pick, "HOLDS", f.holds), patch.object(tick, "green_report", return_value=sha), \
            patch.object(tick, "review_card"), patch.object(
                tick, "runtime", side_effect=followup_replies("merger", "pane-merger")) as cli:
        tick.review_step(f.db, "run", [], ticket, state, [verdict])
    assert [call.args[1] for call in cli.call_args_list] == ["send", "worker-show"]
    assert json.loads(f.holds.read_text())["pane-merger"]["account"] == "jzaw"
    f.dispatch, f.terminal = "merger", "pane-merger"
    f.model = f.effort = None  # Orca does not record a model or effort on the mail follow-up.
    f.follow_of, f.orig_title = "ctx_orig", "#553 coder"
    f.title = "#553: Merging"
    f.ticket.update(stage="Merging", route=route)
    calls = f.tick()
    assert calls.index(("orchestration", "worker-stop")) < calls.index(("orchestration", "worker-start"))
    assert ("account", "select", "--agent", "codex", "--account", "x2") in f.events
    holds = json.loads(f.holds.read_text())
    assert "pane-merger" not in holds and holds["term_restart"]["account"] == "x2"
    review = tick.review_state(f.db, "run", 553)
    assert (review["coder"], review["expected"], review["coder_terminal"]) == ("ctx_restart", "ctx_restart", "term_restart")
    assert f.cards[-1] == ("Merging", "coder gpt-6.2-astra medium @x2")

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
    assert f.usage_handoff.call_args.args[1:4] == ("local", "jzaw", f.now)
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

# A context handoff can continue in a fresh session even when its old account is the only usable seat.
for role, stage in (("coder", "Fixing"), ("reviewer", "Reviewing")):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CLAUDE_WORKING)
        f.title, f.ticket["stage"] = f"#553 {role}", stage
        f.seats = ["jzaw"]
        f.status, f.state, f.liveness = "failed", "failed", "exited"
        f.failure = json.dumps({"provenance": "worker_report", "outcome": "failed",
                                "body": "Context limit reached.", "reportPath": "/wt/.handoff-553.md"})
        calls = f.tick()
        assert ("orchestration", "worker-start") in calls and ("orchestration", "worker-stop") not in calls
        start = next(e for e in f.events if e[:2] == ("orchestration", "worker-start"))
        assert start[start.index("--agent") + 1:start.index("--full")] == (
            "codex", "--model", "gpt-6.2-astra", "--effort", "medium")
        assert "/wt/.handoff-553.md" in f.notes[0] and f.cards[-1][0] == stage
        assert ("account", "select", "--agent", "codex", "--account", "jzaw") in f.events
        f.usage_handoff.assert_not_called()
        assert ("orchestration", "worker-start") not in f.tick(1) and len(f.notes) == 1, f.lines

# A hand-started replacement makes the old checkpoint history across branch heads and inventory changes.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.tick()
    f.checkpoint_failure()
    f.add_replacement()
    assert ("orchestration", "worker-start") not in f.tick(1), f.lines
    f.head = "b" * 40
    assert ("orchestration", "worker-start") not in f.tick(1), f.lines
    f.replacement = None
    assert ("orchestration", "worker-start") not in f.tick(1), f.lines

# A retired Dispatch can still be live, but cannot retire the worker that replaced it.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.add_replacement(newer=False, screen=CODEX_AT_LIMIT)
    start = ("orchestration", "worker-start")
    assert start not in f.tick() and '"dispatch": "ctx_manual", "replaced by": "ctx_coder"' in f.lines[0], f.lines
    f.screen = CODEX_AT_LIMIT
    assert f.tick(1).count(start) == 1 and f.dispatch == "ctx_restart", f.lines
    assert start not in f.tick(1), f.lines

# A newer unclassified Dispatch makes an unsaved checkpoint history on upgrade.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.checkpoint_failure()
    f.add_replacement("Continue work", live=False)
    assert ("orchestration", "worker-start") not in f.tick(1), f.lines
    f.replacement = None
    assert ("orchestration", "worker-start") not in f.tick(1), f.lines

# A reviewer can share the worktree without blocking coder recovery, even after an upgrade.
for watched in (False, True):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CLAUDE_WORKING)
        if watched:
            f.tick()
        f.checkpoint_failure()
        f.add_replacement("#553 reviewer")
        assert f.tick(1).count(("orchestration", "worker-start")) == 1, f.lines

# An unwatched coder's checkpoint still restarts beside a live re-review.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.checkpoint_failure()
    f.add_replacement("#553 re-review at abc")
    assert f.tick().count(("orchestration", "worker-start")) == 1, f.lines

# The same holds when a reviewer follows up under a neutral title.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.checkpoint_failure()
    f.add_replacement("#553: resume", follow_of="ctx_orig")
    assert f.tick().count(("orchestration", "worker-start")) == 1, f.lines

# A live re-review supersedes a re-review at a limit.
for title in ("#553 re-review", "#553 review"):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CODEX_AT_LIMIT)
        f.title, f.spec = title, ""
        f.add_replacement("#553 re-review 2")
        calls = f.tick()
        assert '"dispatch": "ctx_coder", "replaced by": "ctx_manual"' in f.lines[0], f.lines
        assert ("orchestration", "worker-start") not in calls and ("orchestration", "worker-stop") not in calls

# Detached research stays out of watch even when neither Task has a role or a research card.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.title, f.spec = "#553: research notes", ""
    f.worktree = f"repo::{tick.research_root() / 'issue-553-abcd'}"
    f.checkpoint_failure()
    f.add_replacement("#553: continue research", worktree=f.worktree)
    calls = f.tick()
    assert "replaced by" not in f.lines[0], f.lines
    assert ("orchestration", "worker-start") not in calls and ("terminal", "read") not in calls

# An older live owner of the worktree blocks a usage-limit restart in the same role.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.add_replacement("#554 coder", newer=False)
    calls = f.tick()
    assert ("orchestration", "worker-start") not in calls and ("orchestration", "worker-stop") not in calls, f.lines
    assert "replaced by" in f.lines[0] and not f.cards, (f.lines, f.cards)

# Only the newer remote Task blocks this crash retry, regardless of where its Dispatch is listed.
for newer in (True, False):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CLAUDE_WORKING)
        f.status, f.state, f.liveness = "failed", "failed", "exited"
        f.add_replacement("Continue checkpoint", worktree="repo::/elsewhere", live=False, remote=True, newer=newer,
                          spec="/goal Implement julianthant/hr-relay#553 from the checkpoint.")
        calls = f.tick()
        assert calls.count(("orchestration", "worker-start")) == (0 if newer else 1), f.lines
        assert ("orchestration", "worker-stop") not in calls and ("replaced by" in f.lines[0]) == newer, f.lines

# Paired rows without Tasks supersede local coders only when the review row names that Dispatch.
for recorded_coder in ("ctx_manual", "ctx_unrelated"):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CLAUDE_WORKING)
        f.checkpoint_failure()
        f.add_replacement(worktree="repo::/elsewhere", remote=True, task_listed=False)
        tick.review_state(f.db, "run", 553)
        tick.save_review(f.db, "run", 553, {"coder": recorded_coder})
        calls = f.tick()
        replaced = recorded_coder == "ctx_manual"
        assert calls.count(("orchestration", "worker-start")) == (0 if replaced else 1), f.lines
        assert ('"dispatch": "ctx_coder", "replaced by": "ctx_manual"' in f.lines[0]) == replaced, f.lines

# A second checkpoint at the same branch head needs the operator, once.
for progressed in (False, True):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CLAUDE_WORKING)
        failure = json.dumps({"provenance": "worker_report", "outcome": "failed",
                              "reportPath": "/wt/.handoff-553.md"})
        start = ("orchestration", "worker-start")
        f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", failure
        assert f.tick().count(start) == 1
        if progressed:
            f.head = "b" * 40
            f.next_start = ("ctx_restart2", "term_restart2")
            f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", failure
            assert f.tick(3).count(start) == 1 and f.ticket["stage"] == "Coding"
        f.status, f.state, f.liveness, f.failure = "failed", "failed", "exited", failure
        assert start not in f.tick(3)
        assert f.ticket["stage"] == "Needs you" and f.sent() == [("run:run", "#553: checkpoint restart limit")]
        assert start not in f.tick(3) and not f.sent()

# A worker can hit a usage limit and hand off between watch passes.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.status, f.state, f.liveness = "failed", "failed", "exited"
    f.failure = json.dumps({"provenance": "worker_report", "outcome": "failed", "subject": "Usage limit",
                            "reportPath": "/wt/.handoff-553.md"})
    assert ("orchestration", "worker-start") in f.tick()
    assert ("account", "select", "--agent", "codex", "--account", "x2") in f.events
    assert f.usage_handoff.call_args.args[1:4] == ("local", "jzaw", f.now)

# At the limit or on a model-switch menu: stop it, then retry on another account.
for agent, model, screen in (("codex", "gpt-6.2-astra", CODEX_AT_LIMIT),
                             ("claude", "claude-opus-5-5", CLAUDE_AT_LIMIT), ("claude", "claude-opus-5-5", CLAUDE_CURRENT)):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, agent, model, screen)
        calls = f.tick()
        assert calls.index(("orchestration", "worker-stop")) < calls.index(("orchestration", "worker-start")), calls
        assert ("account", "select", "--agent", agent, "--account", "x2") in f.events
        assert [c[0] for c in f.cards] == ["Handed off", "Coding"]
        assert not any(e[:2] == ("terminal", "send") for e in f.events)  # never types into the pane

# Bug 4 (#517, #485): a Codex status line naming another model is a healthy worker, not a limit.
for model in ("gpt-6.2-astra", None):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, "codex", model, CODEX_SWITCHED)
        calls = f.tick()
        assert ("orchestration", "worker-stop") not in calls and not f.sent() and f.cards == [], (calls, f.lines)

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

# A checkpoint queued for a seat is discarded when a replacement appears before the seat frees.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.checkpoint_failure()
    f.seats = []
    assert ("orchestration", "worker-start") not in f.tick() and "waiting for seat" in f.lines[0]
    f.add_replacement()
    f.seats = ["jzaw"]
    calls = f.tick(5)
    assert ("orchestration", "worker-start") not in calls and ("account", "select") not in calls, f.lines
    f.replacement = None
    assert ("orchestration", "worker-start") not in f.tick(5), f.lines

# The review row leaves a restart parked while the operator manages the ticket.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.seats = ["jzaw"]
    assert ("orchestration", "worker-start") not in f.tick() and "waiting for seat" in f.lines[0]
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"stage": "Needs you"})
    f.seats.append("x2")
    assert ("orchestration", "worker-start") not in f.tick(5) and not f.sent(), f.lines
    tick.save_review(f.db, "run", 553, {"stage": "Fixing"})
    assert f.tick(5).count(("orchestration", "worker-start")) == 1, f.lines

# A hand-managed ticket still gets live-worker notifications.
for screen, wait, minutes, expected in (
        (CLAUDE_WORKING, {"kind": "permission", "approvalId": "appr_1"}, 0, ("run:run", "#553: permission prompt")),
        (CODEX_NEAR_LIMIT, None, 0, ("dispatch:ctx_coder", "#553: hand off now")),
        (CLAUDE_WORKING, None, 20, ("dispatch:ctx_coder", "#553: still working?"))):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=screen)
        f.wait = wait
        tick.review_state(f.db, "run", 553)
        tick.save_review(f.db, "run", 553, {"stage": "Needs you"})
        calls = f.tick()
        if minutes:
            calls += f.tick(minutes)
        assert f.sent() == [expected] and ("orchestration", "worker-start") not in calls, f.lines

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
# The note then starts a follow-up Dispatch, which review expects from the same transaction that clears the note.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder",
                                        "agent": "coder gpt-6.2-astra medium @x1"})
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert f.dispatch == "ctx_restart" and f.cards[-1][0] == "Handed off"
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"
    with check.assertRaises(tick.OrcaError):
        f.tick(1)
    row = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])
    assert row["note"]["to"] == "ctx_restart" and row["card"]["stage"] == "Coding"
    assert tick.resume_pending(f.db, "run", 553, {"ctx_restart"})  # review waits on the note
    f.note_error = None
    f.tick(1)
    assert len(f.notes) == 1 and f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")
    review = tick.review_state(f.db, "run", 553)
    assert (review["coder"], review["expected"]) == ("ctx_follow", "ctx_follow") and "followed up" in f.lines[0]
    assert review["coder_terminal"] == "term_follow"
    f.tick(1)
    assert not f.db.execute("SELECT * FROM watch").fetchall() and len(f.notes) == 1

# #588 item C: a follow-up in a fresh pane takes the account hold and the watch row with it, so its own usage limit
# restarts it on another account instead of escalating "limit account unknown".
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.ticket["route"] = ("codex", "gpt-6.2-astra", "medium", None)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    f.status, f.state, f.liveness, f.note_error = "completed", "succeeded", "exited", None
    f.follow_status, f.follow_liveness = "dispatched", "live"
    f.tick(1)
    holds = json.loads(f.holds.read_text())
    assert "term_restart" not in holds and holds["term_follow"]["account"] == "x2", holds
    rows = dict(f.db.execute("SELECT terminal,state FROM watch"))
    assert list(rows) == ["term_follow"] and json.loads(rows["term_follow"])["account"] == "x2", rows
    f.screen = CODEX_AT_LIMIT  # the follow-up hits its own limit
    f.tick(1)
    start = next(e for e in f.events if e[:2] == ("orchestration", "worker-start"))
    assert start[start.index("--task") + 1] == "task_follow" and ("account", "select", "--agent", "codex", "--account", "jzaw") in f.events
    assert not [s for s in f.sent() if "limit account unknown" in s[1]] and f.cards[-1][0] != "Needs you", (f.sent(), f.cards)
    assert json.loads(f.holds.read_text())["term_follow2"]["account"] == "jzaw"

# A reviewer's settled replacement gets its note as a follow-up in a fresh pane. Review takes the pane with the
# Dispatch, so a handoff there is not read as a review failure.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.title = "#553 review"
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_c", "expected": "ctx_coder", "coder_terminal": "term_c",
                                        "reviewer": {"dispatch": "ctx_coder", "terminal": "term_coder",
                                                     "holds": str(f.holds)}})
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"
    f.note_error, f.follow_terminal = None, None
    with check.assertRaisesRegex(tick.OrcaError, "watch error"):
        f.tick(1)
    assert json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])["note"]
    review = tick.review_state(f.db, "run", 553)
    assert review["expected"] == "ctx_restart" and review["reviewer"]["terminal"] == "term_restart"
    f.follow_terminal = "term_follow"
    f.tick(1)
    assert len(f.notes) == 1 and len(f.followups) == 1
    review = tick.review_state(f.db, "run", 553)
    assert review["expected"] == "ctx_follow" and review["reviewer"]["dispatch"] == "ctx_follow"
    assert review["reviewer"]["terminal"] == "term_follow"
    f.db.execute("INSERT OR REPLACE INTO watch VALUES ('run','term_follow',?)", (json.dumps({"handoff": 1}),))
    done = {"type": "worker_done", "id": "m1", "sequence": 9,
            "payload": json.dumps({"dispatchId": "ctx_follow", "outcome": "failed"})}
    assert tick.failed_reviews(f.db, "run", tick.review_state(f.db, "run", 553),
                               tick.ReviewMailbox(f.db, "run", [done])) == []

# The settled-worker send applies but its reply is lost: the next tick adopts the follow-up Orca started instead of
# sending again, even after Orca retried that Task. With no review state yet, the follow-up is recorded for review's
# first discovery.
for retried, current in ((False, "ctx_follow"), (True, "ctx_follow_retry")):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CODEX_AT_LIMIT)
        f.follow_retried = retried
        f.note_error = tick.OrcaError("mailbox busy")
        with check.assertRaises(tick.OrcaError):
            f.tick()
        f.status, f.state, f.liveness = "completed", "succeeded", "exited"
        f.note_error, f.note_lost = None, True
        with check.assertRaisesRegex(tick.OrcaError, "watch error"):
            f.tick(1)
        assert len(f.followups) == 1 and json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])["note"]["sending"]
        f.tick(1)
        assert len(f.followups) == 1 and len(f.notes) == 1 and "followed up" in f.lines[0]
        assert f.db.execute("SELECT run,issue,dispatch FROM followups").fetchall() == [("run", 553, current)]
        assert f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")

# #588 item B: a pre-upgrade row (no sending mark, no request id) whose follow-up-starting send was applied is
# reconciled against that follow-up instead of starting a second one.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    f.note_error = tick.OrcaError("mailbox busy")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"
    f.note_error, f.note_lost = None, True
    with check.assertRaisesRegex(tick.OrcaError, "watch error"):
        f.tick(1)
    row = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])
    for key in ("sending", "request"):
        row["note"].pop(key, None)
    with f.db:
        f.db.execute("UPDATE watch SET state=? WHERE terminal='term_restart'", (json.dumps(row),))
    f.tick(1)
    assert len(f.followups) == 1 and len(f.notes) == 1, (f.followups, f.lines)
    assert tick.review_state(f.db, "run", 553)["expected"] == "ctx_follow"

# Orca queues the note for the live replacement, the reply is lost, and the replacement settles before the next
# tick: the retry replays the first send under its saved request instead of starting a follow-up.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.review_state(f.db, "run", 553)
    tick.save_review(f.db, "run", 553, {"coder": "ctx_coder", "expected": "ctx_coder", "coder_terminal": "term_coder"})
    f.note_lost = True
    with check.assertRaisesRegex(tick.OrcaError, "watch error"):
        f.tick()
    assert len(f.notes) == 1 and f.dispatch == "ctx_restart" and f.cards[-1][0] == "Handed off"
    assert tick.resume_pending(f.db, "run", 553, {"ctx_restart"})
    request = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])["note"].get("request")
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"
    f.tick(1)
    assert len(f.notes) == 1 and not f.followups
    assert "followed up" not in f.lines[0]
    review = tick.review_state(f.db, "run", 553)
    assert (review["coder"], review["expected"]) == ("ctx_restart", "ctx_restart")
    assert f.cards[-1] == ("Coding", "coder gpt-6.2-astra medium @x2")
    sends = [e for e in f.events if e[:2] == ("orchestration", "send") and "dispatch:ctx_restart" in e]
    assert [e[e.index("--retry-request") + 1] for e in sends] == [request]
    f.tick(1)
    assert len(f.notes) == 1 and not f.db.execute("SELECT * FROM watch").fetchall()

# Review's first discovery reads the recorded follow-up's report, not the predecessor's, and follows a retry of
# the follow-up Task to its current attempt.
for current in ("follow", "follow_retry"):
    args = SimpleNamespace(issue=41, coder=None, run=None, anchor=None)
    ticket, state = review_fixture()
    ticket.update(agent=state["agent"], stage="Coding")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE followups (run TEXT, issue INTEGER, dispatch TEXT, PRIMARY KEY(run,issue))")
    db.execute("INSERT INTO followups VALUES ('run', 41, 'follow')")
    followed_report = report(current, f"I pushed {sha}.", msg_id="follow-report")
    attempts = {"coder": {"id": "coder", "runId": "run", "taskId": "task"},
                "follow": {"id": "follow", "runId": "run", "taskId": "task_follow", "followUpOf": "coder"},
                "follow_retry": {"id": "follow_retry", "runId": "run", "taskId": "task_follow", "retryOfDispatchId": "follow"}}
    def discovery(*argv, current=current, attempts=attempts, followed_report=followed_report):
        if argv[:2] == ("orchestration", "inbox"):
            return {"messages": [followed_report]}
        if argv[:2] == ("orchestration", "task-list"):
            return {"tasks": [{"title": "#41 coder", "id": "task"}]}
        if argv[:2] == ("orchestration", "dispatch-show"):
            return {"dispatch": {"id": {"task": "coder", "task_follow": current}[argv[3]]}}
        if argv[:2] == ("orchestration", "worker-show"):
            return {"dispatch": attempts[argv[3]],
                    "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {"model": "gpt-6.2-sol"}}}}}
        assert argv[:2] == ("worktree", "show"), argv
        return {"worktree": {"id": "tree", "path": "/fixture", "branch": "refs/heads/task/ui/header"}}
    with patch.object(tick, "tickets", return_value=[ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
            patch.object(tick, "command", return_value="https://github.com/julianthant/hr-relay.git\n"), \
            patch.object(tick, "review_step") as step, patch.object(tick, "runtime", discovery):
        tick.review_tick(args, db)
        assert step.call_args.args[-2]["expected"] == current and step.call_args.args[-1].successes[current] == [followed_report]

# Bug 3: an older coder Task's success report does not outrank the follow-up's root, which has no report; a
# Task with no Dispatch is skipped, and the next issue still runs.
args = SimpleNamespace(issue=None, coder=None, run=None, anchor=None)
ticket, state = review_fixture()
ticket.update(agent=state["agent"], stage="Coding")
empty = dict(ticket, number=40)
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE followups (run TEXT, issue INTEGER, dispatch TEXT, PRIMARY KEY(run,issue))")
db.execute("INSERT INTO followups VALUES ('run', 41, 'follow')")
reports = [report("old", f"I pushed {sha}.", msg_id="old-report"), report("follow", f"I pushed {sha}.", msg_id="follow-report")]
attempts = {"old": {"id": "old", "runId": "run", "taskId": "task_old"},
            "coder": {"id": "coder", "runId": "run", "taskId": "task"},
            "follow": {"id": "follow", "runId": "run", "taskId": "task_follow", "followUpOf": "coder"}}
def discovery(*argv):
    if argv[:2] == ("orchestration", "inbox"):
        return {"messages": reports}
    if argv[:2] == ("orchestration", "task-list"):
        return {"tasks": [{"title": "#40 coder", "id": "task_none"}, {"title": "#41 coder", "id": "task_old"},
                          {"title": "#41 coder", "id": "task"}, {"title": "#41 coder", "id": "task_unstarted"}]}
    if argv[:2] == ("orchestration", "dispatch-show"):
        return {"dispatch": {"task_old": {"id": "old"}, "task": {"id": "coder"}, "task_follow": {"id": "follow"}}.get(argv[3])}
    if argv[:2] == ("orchestration", "worker-show"):
        return {"dispatch": attempts[argv[3]],
                "worker": {"worktreeId": "tree", "startOptions": {"launch": {"effective": {"model": "gpt-6.2-sol"}}}}}
    assert argv[:2] == ("worktree", "show"), argv
    return {"worktree": {"id": "tree", "path": "/fixture", "branch": "refs/heads/task/ui/header"}}
with patch.object(tick, "tickets", return_value=[empty, ticket]), patch.object(tick, "supervisor", return_value=([], "run")), \
        patch.object(tick, "command", return_value="https://github.com/julianthant/hr-relay.git\n"), \
        patch.object(tick, "review_step") as step, patch.object(tick, "runtime", discovery):
    tick.review_tick(args, db)
    assert step.call_count == 1 and step.call_args.args[-2]["expected"] == "follow"

# A replacement that reports while its resume note is undelivered waits; review acts once delivery is confirmed.
ticket, state = review_fixture()
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
pending_note = {"note": {"to": "coder", "subject": "#41: resume", "body": "read the handoff"},
                "card": {"number": 41, "stage": "Coding", "agent": state["agent"]}}
db.execute("INSERT INTO watch VALUES ('run','pane-new',?)", (json.dumps(pending_note),))
with patch.object(tick, "green_report", return_value=None) as verify, patch.object(tick, "review_card") as card, \
        patch.object(tick, "runtime", side_effect=followup_replies("fix")) as cli:
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
    assert card.call_args.args[1] == "Fixing" and [call.args[1] for call in cli.call_args_list] == ["send", "worker-show"]


# A failed coordinator restore after a restart is retried from the watch row; the resume note does not wait for it.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.select_error = tick.OrcaError("account switch refused")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert f.active == "x2" and len(f.notes) == 1 and f.cards[-1][0] == "Coding"
    assert json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])["coordinator"] == "coord"
    f.status, f.state, f.liveness = "completed", "succeeded", "exited"  # the replacement settles first
    with check.assertRaises(tick.OrcaError):
        f.tick(1)
    f.select_error = None
    f.tick(1)
    assert f.active == "coord" and len(f.notes) == 1
    f.tick(1)
    assert not f.db.execute("SELECT * FROM watch").fetchall()


# Orca confirms a restart had no effects: the coordinator's account is restored, or kept owed, and the restart retried.
for restore_fails in (False, True):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, screen=CODEX_AT_LIMIT)
        f.start_error = tick.StartNotApplied("refused")
        f.select_error = tick.OrcaError("account switch refused") if restore_fails else None
        with check.assertRaises(tick.OrcaError):
            f.tick()
        row = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_coder"])
        assert not row["pending"].get("starting") and f.notes == []
        assert (f.active, row.get("coordinator")) == (("x2", "coord") if restore_fails else ("coord", None))
        f.start_error = f.select_error = None
        calls = f.tick(1)
        assert ("orchestration", "worker-start") in calls and f.active == "coord" and len(f.notes) == 1

# The worker-stop reply is lost after the worker seat is selected: the next tick restores the coordinator first.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.stop_lost = True
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert f.active == "x2" and json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_coder"])["coordinator"] == "coord"
    calls = f.tick(1)
    selects = [e[-1] for e in f.events if e[:2] == ("account", "select")]
    assert selects == ["coord", "x2", "coord"] and f.active == "coord", selects
    assert ("orchestration", "worker-start") in calls and len(f.notes) == 1

# A fill start still owes the coordinator's account: the restart waits on its seat instead of recording that seat.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    tick.reserve(f.db, 70, {"not_applied": True, "coordinator": "coord"})
    calls = f.tick()
    assert ("account", "select") not in calls and ("orchestration", "worker-start") not in calls
    assert "waiting for coordinator restore" in f.lines[0] and "starts:70" in f.lines[0]
    f.db.execute("DELETE FROM starts")
    calls = f.tick(1)
    assert ("orchestration", "worker-start") in calls and f.active == "coord"

# A replacement crashes before its resume note and account restore land: its successor inherits both.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    f.note_error = tick.OrcaError("mailbox busy")
    f.select_error = tick.OrcaError("account switch refused")
    with check.assertRaises(tick.OrcaError):
        f.tick()
    assert f.active == "x2" and f.cards[-1][0] == "Handed off"
    f.liveness = "exited"  # the replacement crashes
    with check.assertRaises(tick.OrcaError):
        f.tick(1)
    row = json.loads(dict(f.db.execute("SELECT terminal,state FROM watch"))["term_restart"])
    assert row["relaunched"] and row["coordinator"] == "coord" and row["card"]["stage"] == "Coding"
    assert "Its own resume note, never delivered" in row["note"]["body"]
    f.note_error = f.select_error = None
    f.tick(1)
    assert f.active == "coord" and len(f.notes) == 1 and "never delivered" in f.notes[0]
    assert f.cards[-1][0] == "Coding"


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

# #588 item A: no coordinator rotation while a restore is owed; the active Codex account is a worker's seat.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    tick.reserve(f.db, 60, {"task": "t60", "run": "run", "coordinator": "coord"})
    f.active = "x2"
    f.tick()
    f.pick_main.assert_not_called()
    assert "rotation deferred" in f.lines[0]
    with f.db:
        f.db.execute("UPDATE starts SET receipt=? WHERE issue=60", (json.dumps({"task": "t60", "run": "run"}),))
    f.tick(1)
    f.pick_main.assert_called_once_with(["coordinator"])

# Bug 5: the retry shares its pane with a newer follow-up; the Task's earlier, failed attempts stay history.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.dispatch, f.terminal, f.status, f.state, f.follow_row = "ctx_restart", "term_restart", "succeeded", "succeeded", True
    calls = f.tick()
    assert not [e for e in f.events if e[:2] in (("orchestration", "worker-show"), ("orchestration", "worker-start"))], f.events
# Bug 5: ownership of the newest attempt's pane moved to the follow-up; the Task's older failed attempt stays history.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.dispatch, f.terminal, f.status, f.state, f.follow_row = "ctx_restart", "term_restart", "succeeded", "succeeded", True
    f.transferred = True
    f.tick()
    assert not [e for e in f.events if e[:2] in (("orchestration", "worker-show"), ("orchestration", "worker-start"))], f.events
# Bug 5: a completed newest attempt with no terminal or resource record still makes the older failed one history.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.bare = True
    f.tick()
    assert not [e for e in f.events if e[:2] in (("orchestration", "worker-show"), ("orchestration", "worker-start"))], f.events

# Bug 6: a follow-up Dispatch records no model; its restart takes the Coder line, or a reviewer's original Dispatch.
# Orca titles a resume Task "#N: resume", so the original Dispatch's Task names the role.
# A second resume follows a resume Task: the reviewer Task its ancestry reaches keeps the parent's verified route.
for title, follow_of, orig, parent, want in (("#553 coder", None, None, None, ("gpt-6.2-astra", "medium")),
                                             ("#553 reviewer", "ctx_orig", "#553 reviewer", None, ("gpt-6.2-sol", "xhigh")),
                                             ("#553: resume", "ctx_orig", "#553 reviewer m1", None, ("gpt-6.2-sol", "xhigh")),
                                             ("#553: resume", "ctx_orig", "#553 coder", None, ("gpt-6.2-astra", "medium")),
                                             ("#553: resume", "ctx_orig", "#553: resume", "ctx_root", ("gpt-6.2-sol", "xhigh")),
                                             ("#553: resume", "ctx_orig", "#553: resume", None, ("gpt-6.2-sol", "xhigh"))):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, "codex", None, CODEX_AT_LIMIT)
        f.title, f.follow_of, f.orig_title, f.orig_parent = title, follow_of, orig, parent
        f.ticket["route"] = ("codex", "gpt-6.2-astra", "medium", None)
        f.tick()
        start = next(e for e in f.events if e[:2] == ("orchestration", "worker-start"))
        assert (start[start.index("--model") + 1], start[start.index("--effort") + 1]) == want, start

# The original's cached route and its parent's cached role survive release, while a changed status forces a new read.
for status, expected_route in (("completed", ("gpt-6.2-sol", "xhigh")), ("failed", ("gpt-6.2-astra", "high"))):
    with TemporaryDirectory() as tmp:
        f = Fleet(tmp, "codex", None, CODEX_AT_LIMIT)
        f.title, f.follow_of, f.orig_title, f.orig_parent = "#553: resume", "ctx_orig", "#553: resume", "ctx_root"
        f.ticket["route"] = ("codex", "gpt-6.2-astra", "medium", None)
        underlying = f.orca
        originals = [
            {"dispatchId": "ctx_orig", "taskId": "task_orig", "runId": "run", "agentTerminalHandle": None,
             "terminalState": "released", "dispatchStatus": status, "projection": {"host": {"kind": "local"}}},
            {"dispatchId": "ctx_root", "taskId": "task_root", "runId": "run", "agentTerminalHandle": None,
             "terminalState": "released", "dispatchStatus": "completed", "projection": {"host": {"kind": "local"}}},
        ]

        def released_fact(row, state, route=()):
            dispatch = {"id": row["dispatchId"], "taskId": row["taskId"], "runId": "run", "status": state}
            if row["dispatchId"] == "ctx_orig":
                dispatch["followUpOf"] = "ctx_root"
            return {"dispatch": dispatch, "projection": {"resource": {"releaseState": "released"}},
                    "worker": {"startOptions": {"launch": {"effective":
                        dict(zip(("agent", "model", "effort"), route))}}}}

        history = tick.ReleasedDispatchHistory(f.db, "run")
        for row in originals:
            completed = dict(row, dispatchStatus="completed")
            history.remember(completed, released_fact(completed, "completed", ("codex", "gpt-6.2-sol", "xhigh")))

        def history_runtime(*args):
            if args[:2] == ("orchestration", "worker-list"):
                out = underlying(*args)
                return {**out, "workers": [*out["workers"], *originals]}
            if args[:2] == ("orchestration", "worker-show") and args[3] == "ctx_orig":
                f.events.append(args)
                return released_fact(originals[0], status, ("codex", "gpt-6.2-astra", "high"))
            return underlying(*args)

        with patch.object(f, "orca", history_runtime):
            f.tick()
        reads = [e[3] for e in f.events if e[:2] == ("orchestration", "worker-show")]
        assert "ctx_root" not in reads and reads.count("ctx_orig") == int(status == "failed"), reads
        start = next(e for e in f.events if e[:2] == ("orchestration", "worker-start"))
        assert (start[start.index("--model") + 1], start[start.index("--effort") + 1]) == expected_route, start

# Bug 1: after an Orca restart the crashed worker's Task is ready again; it starts without --retry-of and is adopted.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.task_status, f.status, f.state, f.liveness, f.failure = "ready", "failed", "failed", "exited", "agent process exited"
    assert ("orchestration", "worker-start") in f.tick() and f.started_retry is False
    assert json.loads(f.holds.read_text())["term_restart"]["account"] == "jzaw"
    row = json.loads(f.db.execute("SELECT state FROM watch WHERE terminal='term_restart'").fetchone()[0])
    assert row["relaunched"] and "pending" not in row, row
# A lost start response: the next tick adopts the Task's new attempt, which has no retry lineage.
with TemporaryDirectory() as tmp:
    g = Fleet(tmp, screen=CLAUDE_WORKING)
    g.task_status, g.status, g.state, g.liveness, g.failure = "ready", "failed", "failed", "exited", "agent process exited"
    g.lose_start = True
    with check.assertRaises(tick.OrcaError):
        g.tick()
    calls = g.tick(1)
    assert ("orchestration", "worker-start") not in calls and "term_restart" in json.loads(g.holds.read_text()), (calls, g.lines)

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
    assert f.tick() == [("orchestration", "task-list"), ("orchestration", "worker-list")]
    assert f.lines == ['watch        "no action"']

# Hundreds of distinct historical Tasks must not delay watching the current worker.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    history = [{
        "dispatchId": f"history-{i}", "taskId": f"old-task-{i}", "runId": "run",
        "workerState": "failed", "dispatchStatus": "failed", "terminalState": "released",
        "agentTerminalHandle": f"old-pane-{i}", "projection": {"host": {"kind": "local"}},
    } for i in range(700)]
    underlying = f.orca

    def long_run(*args):
        if args[:2] == ("orchestration", "worker-list") and "--cursor" in args:
            f.events.append(args)
            start = int(args[-1])
            end = start + 100
            return {"workers": history[start:end],
                    "page": {"hasMore": end < len(history), "nextCursor": str(end)}}
        if args[:2] == ("orchestration", "worker-list"):
            return {**underlying(*args), "page": {"hasMore": True, "nextCursor": "0"}}
        if args[:2] == ("orchestration", "worker-show") and args[3].startswith("history-"):
            f.events.append(args)
            return {"dispatch": {"id": args[3], "runId": "run"}}
        return underlying(*args)

    with patch.object(f, "orca", long_run):
        calls = f.tick()
    assert ("terminal", "read") in calls
    shows = calls.count(("orchestration", "worker-show"))
    assert shows <= 30, f"700 settled Dispatches produced {shows} worker-show calls"

# A settled released report is read once across passes even while its card still says Coding.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CLAUDE_WORKING)
    f.status, f.state, f.terminal_state = "failed", "failed", "released"
    f.failure = json.dumps({"provenance": "worker_report", "outcome": "failed"})
    assert f.tick().count(("orchestration", "worker-show")) == 1
    assert ("orchestration", "worker-show") not in f.tick(1)

# A worker changed after the parallel observation. Its old limit screen cannot authorize a restart.
with TemporaryDirectory() as tmp:
    f = Fleet(tmp, screen=CODEX_AT_LIMIT)
    underlying = f.orca

    def settle_before_restart(*args):
        if args[:2] == ("orchestration", "worker-show") and args in f.events:
            f.status, f.state = "completed", "succeeded"
        return underlying(*args)

    with patch.object(f, "orca", settle_before_restart), check.assertRaises(tick.OrcaError):
        f.tick()
    assert "Dispatch identity changed before restart" in f.lines[0]
    assert not any(e[:2] in (("orchestration", "worker-stop"), ("orchestration", "worker-start")) for e in f.events)

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
            patch.object(tick, "review_step", lambda *a, **_: found.append(a[3]["number"])):
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

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    args = SimpleNamespace(anchor="term_anchor", run="run_live", repo_paths=[root / "hr-relay", root / "dot-agents"])
    with sqlite3.connect(root / "starts.sqlite") as db:
        tick.reserve(db, 555, {"run": "run_live", "dispatch": "ctx_existing"})

    calls = []

    def phase(name):
        def run(options, db):
            assert (options.anchor, options.run) == ("term_anchor", "run_live")
            assert json.loads(db.execute("SELECT receipt FROM starts WHERE issue=555").fetchone()[0]) == {
                "run": "run_live", "dispatch": "ctx_existing"}
            assert list(tick.pass_workers(options.run, options.workers)) == [{"dispatchId": "ctx_existing"}]
            calls.append((name, options.repo_path))
            if name == "review":
                raise tick.OrcaError("private CLI error")
        return run

    with patch.object(tick, "SNAPSHOT", root / "issues.json"), \
            patch.object(tick, "supervisor", return_value=(["--from", args.anchor, "--run", args.run], args.run)) as bind, \
            patch.object(tick, "fleet_workers", return_value=iter([{"dispatchId": "ctx_existing"}])) as roster, \
            patch.object(tick, "watch_tick", phase("watch")), patch.object(tick, "review_tick", phase("review")), \
            patch.object(tick, "close_tick", phase("close")), patch.object(tick, "fill_tick", phase("fill")), \
            patch.object(tick, "full_run_tick", phase_full := Mock()), \
            redirect_stdout(io.StringIO()) as output:
        assert tick.scheduled_tick(args) == 1
        bind.assert_called_once()
        roster.assert_called_once_with(args.run)
        phase_full.assert_called_once_with()
        status = json.loads((root / "tick.json").read_text())
        assert status["lastRun"] and status["errors"] == [{"phase": "review", "error": "OrcaError"}], status
        assert "private CLI error" in output.getvalue()
        assert calls == [("watch", None), ("review", None), ("close", args.repo_paths[0]),
                         ("close", args.repo_paths[1]), ("fill", args.repo_paths[0]), ("fill", args.repo_paths[1])]
        assert [line.split(None, 2)[2] for line in output.getvalue().splitlines() if line.startswith("phase start")] == [
            '"adopt"', '"state"', '"watch"', '"review"', '"close"', '"close"', '"fill"', '"fill"', '"full-run"']
        calls.clear()
        bind.side_effect = tick.OrcaError("wrong Run")
        other_run = [{"truncated": False, "terminals": [{"handle": args.anchor}]},
                     {"run": {"id": args.run, "coordinator_handle": None}}]
        with patch.dict(tick.os.environ, {}, clear=True), patch.object(tick, "runtime", side_effect=other_run):
            assert tick.scheduled_tick(args) == 1
        assert calls == []
        assert json.loads((root / "tick.json").read_text())["errors"] == [{"phase": "adopt", "error": "OrcaError"}]

    import fcntl
    with (root / "fill.lock").open("a+") as lock, patch.object(tick, "SNAPSHOT", root / "issues.json"), \
            patch.object(tick, "supervisor") as bind, redirect_stdout(io.StringIO()):
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = (root / "tick.json").read_text()
        assert tick.scheduled_tick(args) == 0
        bind.assert_not_called()
        assert (root / "tick.json").read_text() == before

    import install_tick
    for repo in args.repo_paths:
        repo.mkdir()
    accounts = root / "accounts.toml"
    with patch.object(tick, "SNAPSHOT", root / "issues.json"), patch.object(tick, "ORCA", "orca"), \
            patch.object(install_tick.account_usage, "ACCOUNTS", accounts), \
            patch.object(install_tick.shutil, "which", side_effect=lambda tool: f"/cli/{tool}"):
        agent = install_tick.launch_agent(args)
    assert agent["StartInterval"] == 60 and agent["RunAtLoad"] is True
    assert agent["ProgramArguments"] == [sys.executable, str(tick.HERE / "tick.py"), "scheduled",
                                         "--anchor", args.anchor, "--run", args.run,
                                         "--repo-path", str(args.repo_paths[0].resolve()),
                                         "--repo-path", str(args.repo_paths[1].resolve())]
    assert agent["EnvironmentVariables"]["ORCA_CLI_COMMAND"] == "/cli/orca"
    assert agent["EnvironmentVariables"]["DEV_AUTO_ACCOUNTS"] == str(accounts)

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

# A dry run spends the remaining local ticket first and spills the next one to
# the remote host. At the remote target another ticket must remain queued.
with TemporaryDirectory() as tmp, ExitStack() as stack:
    root = Path(tmp)
    args = SimpleNamespace(repo_path=root, dry_run=True, issue=None, holds=root / "holds.json")
    work = [dict(base, base="origin/main", number=n, branch=f"task/test/{n}", paths=[str(n)]) for n in range(1, 4)]
    stack.enter_context(patch.object(tick, "tickets", return_value=work))
    stack.enter_context(patch.object(tick, "command", side_effect=["git@github.com:julianthant/hr-relay.git", "",
                                                                   "abc refs/heads/main"]))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={
        "local_target_tickets": 1, "hosts": {"Juzzark-WSL": {"target_tickets": 1}}}))
    stack.enter_context(patch.object(pick.account_usage, "host_checkout", return_value="/remote/relay"))
    stack.enter_context(patch.object(tick, "inventory", return_value=set()))
    stack.enter_context(patch.object(pick, "pick", side_effect=lambda cfg, a, m, e, host, now, holds:
                                    (dict(account, host=host), [])))
    with redirect_stdout(io.StringIO()) as result:
        tick.fill_tick(args, None)
    starts = [line for line in result.getvalue().splitlines() if "would start" in line]
    assert len(starts) == 2 and '"host": "local"' in starts[0] and '"host": "Juzzark-WSL"' in starts[1]

# Fill recovers a remote worker before its Dispatch is visible to dispatch-show.
def remote_start(root, stack, db, has_receipt, repo="hr-relay"):
    host = "Juzzark-WSL"
    dot = repo == "dot-agents"
    checkout = Path("/remote") / repo
    work = dict(base, repo=repo, number=99, sha="abc", base="origin/main")
    wt = {"id": "wsl-tree", "branch": work["branch"], "head": "abc", "path": "/remote/worktree"}
    dispatch = {"id": "ctx", "taskId": "task", "runId": "run"}
    mirror = {"dispatch": dispatch, "worker": {"dispatchId": "ctx", "state": "ready", "stage": "server_record",
                                               "agentTerminalHandle": None, "worktreeId": None}, "terminal": None}
    server = {"environmentId": "env_wsl", "name": host, "asOf": "2026-11-23T18:00:00Z"}
    listed = False
    execution_worktree = "other-tree" if has_receipt and not dot else wt["id"]
    starts = []

    def home_rpc(*args):
        if args[:2] == ("orchestration", "worker-start"):
            starts.append(args)
            assert args[args.index("--on") + 1] == host
            assert args[args.index("--worktree") + 1] == "path:/remote/worktree"
            return full_start("ctx", "wsl-term") if has_receipt else {"dispatchId": "ctx"}
        if args[:2] == ("orchestration", "worker-list"):
            assert "--include-remote" in args and args[args.index("--run") + 1] == "run"
            if listed and "--cursor" not in args:
                return {"workers": [{"dispatchId": "other", "taskId": "other", "server": server}],
                        "page": {"hasMore": True, "nextCursor": "next"}}
            if listed:
                assert args[args.index("--cursor") + 1] == "next"
            return {"workers": [{"dispatchId": "ctx", "taskId": "task", "workerState": "ready", "server": server}]
                    if listed else [], "page": {"hasMore": False}}
        if args[:2] == ("orchestration", "worker-show"):
            assert args == ("orchestration", "worker-show", "--dispatch", "ctx", "--full")
            return mirror
        return {"task-create": {"task": {"id": "task"}}, "dispatch-show": {"dispatch": None}}[args[1]]

    def remote_rpc(target, *args):
        assert target == host
        if args[:2] == ("worktree", "create"):
            assert args[args.index("--repo") + 1] == "path:" + str(checkout)
            return {"worktree": wt}
        if args[:2] == ("worktree", "show"):
            assert args == ("worktree", "show", "--worktree", "id:wsl-tree")
            return {"worktree": wt}
        assert args[:2] == ("terminal", "read") and "wsl-term" in args
        return {"terminal": {"tail": ["ready"]}}

    def remote_command(target, path, argv):
        assert target == host
        if argv[0] == "git":
            assert path == checkout
            return {"fetch": "", "ls-remote": "abc refs/heads/main", "rev-parse": "abc",
                    "for-each-ref": "", "branch": work["branch"]}[argv[3]]
        assert str(path) == wt["path"]
        assert tuple(argv[1:]) == ("orchestration", "worker-show", "--dispatch", "ctx", "--full",
                                   "--environment", "local", "--json")
        return json.dumps({"ok": True, "result": {
            "dispatch": dict(dispatch, runId="wsl-run", assigneeHandle="wsl-term"),
            "worker": {"dispatchId": "ctx", "state": "ready", "stage": "input_accepted",
                       "agentTerminalHandle": "wsl-term", "worktreeId": execution_worktree},
            "terminal": {"handle": "wsl-term", "worktreeId": wt["id"]},
            "observation": {"agentWait": None, "exactWorker": True, "status": "live"},
        }})

    stack.enter_context(patch.object(tick, "runtime", side_effect=home_rpc))
    stack.enter_context(patch.object(tick, "supervisor", return_value=([], "run")))
    stack.enter_context(patch.object(tick, "project_fields", return_value=("p", [], ("field", "waiting"))))
    stack.enter_context(patch.object(pick.account_usage, "remote_output", side_effect=remote_command))
    stack.enter_context(patch.object(pick.account_usage, "host_orca", side_effect=remote_rpc))
    select = stack.enter_context(patch.object(pick.account_usage, "remote_account",
                                             side_effect=[{}, {"codex": {"activeAccountId": "seat"}}]))
    stack.enter_context(patch.object(tick, "remote_tree_settings"))

    def count_workers(path, tickets, name, emit, target, counts):
        assert name == repo
        assert path == (root if target == "local" else checkout)
        counts[target] = 1 if target == "local" else 0
        return set()

    stack.enter_context(patch.object(tick, "inventory", side_effect=count_workers))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={
        "local_target_tickets": 1, "hosts": {host: {"target_tickets": 1}}} if dot else {}))
    registered = stack.enter_context(patch.object(pick.account_usage, "host_checkout", return_value=str(checkout)))
    stack.enter_context(patch.object(tick, "command", side_effect=lambda *argv: {
        "remote": f"git@github.com:julianthant/{repo}.git", "for-each-ref": "",
        "ls-remote": "abc refs/heads/main"}.get(argv[3], "") if argv[0] == "git" else ""))
    args = SimpleNamespace(repo_path=root, issue=99, dry_run=False, holds=root / "holds.json")
    if dot:
        stack.enter_context(patch.object(tick, "tickets", return_value=[work]))
        picker = stack.enter_context(patch.object(pick, "pick", return_value=(dict(account, host=host), [])))
        tick.fill_tick(args, db, issues=[work])
        registered.assert_called_once_with(host, repo)
        assert all(call.args[4] == host for call in picker.call_args_list)
    elif has_receipt:
        with check.assertRaisesRegex(tick.OrcaError, "unverified worker"):
            tick.launch(db, checkout, work, dict(account, host=host), args, args.holds)
        assert tick.review_state(db, "run", 99) is None
        execution_worktree = wt["id"]
    else:
        with check.assertRaisesRegex(tick.OrcaError, "no Dispatch"):
            tick.launch(db, checkout, work, dict(account, host=host), args, args.holds)
    listed = True
    with redirect_stdout(io.StringIO()) as output:
        tick.fill_tick(args, db, issues=[])
    assert "pending" not in output.getvalue(), output.getvalue()
    assert len(starts) == 1
    assert select.call_args_list[0].args == (host, "select", "--agent", "codex", "--account", "seat")
    assert json.loads(args.holds.read_text())["wsl-term"]["host"] == host
    assert db.execute("SELECT receipt FROM starts WHERE issue=99").fetchone() == ("done",)
    state = tick.review_state(db, "run", 99)
    assert (state["coder"], state["expected"], state["coder_terminal"], state["stage"]) == (
        "ctx", "ctx", "wsl-term", "Coding")
    assert (state["path"], state["host"]) == (wt["path"], host)

    review_ticket = dict(work, item="card", agent=state["agent"])
    with (patch.object(tick, "tickets", return_value=[review_ticket]),
          patch.object(tick, "green_report", return_value=None) as check_ci,
          patch.object(tick, "review_card"), patch.object(tick, "runtime", side_effect=[
              {"messages": [report("ctx", "waiting for CI")]}, {"tasks": []},
              *followup_replies("fix", "wsl-fix", previous="ctx")]) as review_rpc):
        tick.review_tick(SimpleNamespace(issue=99, coder=None), db)
    check_ci.assert_called_once()
    assert review_rpc.call_args_list[-2].args[:4] == ("orchestration", "send", "--to", "dispatch:ctx")
    assert json.loads(args.holds.read_text())["wsl-fix"]["account"] == "seat"


for has_receipt, repo in ((True, "hr-relay"), (False, "hr-relay"), (True, "dot-agents")):
    with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
        remote_start(Path(tmp).resolve(), stack, db, has_receipt, repo)

# Review rows written before host tracking identify their coder through the Run's server records.
with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
    host = "Juzzark-WSL"
    ticket, state = review_fixture()
    ticket["body"] += " with Opus advisor"
    state.update(path="/remote/worktree", coder_terminal="wsl-coder")
    tick.review_state(db, "run", 41)
    tick.save_review(db, "run", 41, state)
    messages = [dict(recorded, created_at="2026-11-23T18:00:00Z")]
    sends = []
    starts = []
    server = {"environmentId": "env_wsl", "name": host, "asOf": "2026-11-23T18:00:00Z"}
    attempt = {"id": "wsl-review", "taskId": "review-task", "runId": "run"}

    def review_rpc(*args):
        match args[:2]:
            case "orchestration", "inbox":
                return {"messages": messages}
            case "orchestration", "task-list":
                return {"tasks": starts}
            case "orchestration", "worker-list":
                assert "--include-remote" in args
                return {"workers": [{"dispatchId": "coder", "taskId": "task", "server": server},
                                    {"dispatchId": "wsl-review", "taskId": "review-task", "server": server}],
                        "page": {"hasMore": False}}
            case "orchestration", "worker-start":
                assert args[args.index("--on") + 1] == host
                assert args[args.index("--worktree") + 1] == "path:/remote/worktree"
                starts.append({"id": "review-task", "run_id": "run", "created_at": "2026-11-23T18:00:01Z",
                               "task_title": args[args.index("--task-title") + 1],
                               "spec": args[args.index("--spec") + 1]})
                raise tick.OrcaError("lost reviewer receipt")
            case "orchestration", "worker-show":
                if args[3] == "wsl-land":
                    return followup_worker("wsl-land", None, task="merge-task")
                assert args[2:4] == ("--dispatch", "wsl-review")
                return {"dispatch": attempt, "worker": {"stage": "server_record", "worktreeId": None},
                        "terminal": None}
            case "orchestration", "send":
                # Dispatch-addressed mail is relayed by the Run home to the worker server.
                assert args[2:4] == ("--to", "dispatch:coder") and "--on" not in args
                sends.append(args[-1])
                return {"delivery": "follow_up", "followUpOf": "coder", "dispatchId": "wsl-land"}
        raise AssertionError(args)

    def reviewer_execution(target, path, argv):
        assert target == host and path == Path("/remote/worktree")
        if argv[4] == "wsl-land":
            return json.dumps({"ok": True, "result": followup_worker("wsl-land", "wsl-merge", task="merge-task")})
        assert argv == ("orca", "orchestration", "worker-show", "--dispatch", "wsl-review",
                        "--full", "--environment", "local", "--json")
        return json.dumps({"ok": True, "result": {
            "dispatch": dict(attempt, runId="wsl-run", assigneeHandle="wsl-reader"),
            "worker": {"worktreeId": "wsl-tree", "startOptions": {"launch": {"effective": {
                "agent": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}}}},
        }})

    stack.enter_context(patch.object(tick, "supervisor", return_value=([], "run")))
    stack.enter_context(patch.object(tick, "tickets", return_value=[ticket]))
    stack.enter_context(patch.object(tick, "green_report", return_value=sha))
    stack.enter_context(patch.object(tick, "review_card"))
    stack.enter_context(patch.object(tick, "runtime", side_effect=review_rpc))
    stack.enter_context(patch.object(tick, "worker_settings", side_effect=AssertionError("local settings")))
    settings = stack.enter_context(patch.object(tick, "remote_tree_settings"))
    tree = stack.enter_context(patch.object(pick.account_usage, "host_orca", return_value={
        "worktree": {"id": "wsl-tree", "path": state["path"]}}))
    stack.enter_context(patch.object(pick.account_usage, "remote_output", side_effect=reviewer_execution))
    select = stack.enter_context(patch.object(pick.account_usage, "remote_account",
                                             side_effect=[{}, {"claude": {"activeAccountId": "seat"}}]))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={}))
    seat = stack.enter_context(patch.object(pick, "pick", return_value=(dict(account, host=host), [])))
    holds = Path(tmp) / "holds.json"
    stack.enter_context(patch.object(pick, "HOLDS", holds))
    args = SimpleNamespace(issue=41, coder=None)
    with check.assertRaisesRegex(tick.OrcaError, "lost reviewer receipt"):
        tick.review_tick(args, db)
    tick.review_tick(args, db)
    assert len(starts) == 1
    tree.assert_called_once_with(host, "worktree", "show", "--worktree", "path:/remote/worktree")
    assert seat.call_args.args[4] == host
    assert settings.call_args.args[0] == host and settings.call_args.args[2]["path"] == state["path"]
    assert select.call_args_list[0].args == (host, "select", "--agent", "claude", "--account", "seat")
    assert json.loads(holds.read_text())["wsl-reader"]["host"] == host
    assert tick.review_state(db, "run", 41)["host"] == host
    messages = [recorded, dict(report("wsl-review", "REVIEW VERDICT: PUSH " + sha, msg_id="verdict"), sequence=2)]
    tick.review_tick(args, db)
    assert len(sends) == 1 and "Land the approved change" in sends[0]
    state = tick.review_state(db, "run", 41)
    assert (state["coder"], state["coder_terminal"], state["stage"], state["host"]) == (
        "wsl-land", "wsl-merge", "Merging", host)

# launchd can recover a closed anchor and uses the saved replacement on the next invocation.
def check_anchor(scenario):
    with TemporaryDirectory() as tmp, ExitStack() as stack:
        root = Path(tmp).resolve()
        args = SimpleNamespace(anchor="term_old", run="run_live", repo_paths=[root])
        owner = "term_other" if scenario == "other-owner" else "term_old"
        terminals = {"term_other"} if scenario == "other-owner" else set()
        if scenario == "live-unbound":
            terminals.add("term_old")
        calls, watched = [], []
        fail_bind = scenario.startswith("bind-")

        def rpc(*args):
            nonlocal owner, fail_bind
            verb = args[:2]
            calls.append(args)
            if verb == ("orchestration", "run-current"):
                return {"runId": "run_live" if args[-1] in terminals and args[-1] == owner
                        and scenario != "live-unbound" else None}
            if verb == ("orchestration", "run-show"):
                return {"run": {"id": "run_live", "coordinator_handle": owner}}
            if verb == ("terminal", "list"):
                return {"terminals": [{"handle": h} for h in terminals], "truncated": False}
            if verb == ("terminal", "create"):
                assert args[2:4] == ("--worktree", f"path:{root}")
                terminals.add("term_new")
                if scenario == "owner-raced":
                    owner = "term_other"
                    terminals.add(owner)
                return {"terminal": {"handle": "term_new"}}
            if verb == ("orchestration", "run-use"):
                assert args[2:] == ("--id", "run_live", "--from", "term_new")
                if fail_bind:
                    fail_bind = False
                    if scenario == "bind-receipt-lost":
                        owner = "term_new"
                    raise tick.OrcaError("interrupted bind")
                owner = "term_new"
                return {"runId": "run_live"}
            raise AssertionError(args)

        stack.enter_context(patch.dict(tick.os.environ, {}, clear=True))
        stack.enter_context(patch.object(tick, "SNAPSHOT", root / "issues.json"))
        stack.enter_context(patch.object(tick, "runtime", side_effect=rpc))
        stack.enter_context(patch.object(tick, "watch_tick", side_effect=lambda opts, db: watched.append(opts.anchor)))
        for phase in ("review_tick", "close_tick", "fill_tick", "full_run_tick"):
            stack.enter_context(patch.object(tick, phase))
        output = stack.enter_context(redirect_stdout(io.StringIO()))
        if scenario in ("other-owner", "live-unbound"):
            assert tick.scheduled_tick(args) == 1
            assert not watched and not any(call[:2] == ("terminal", "create") for call in calls)
            assert ("held by another live terminal" if scenario == "other-owner" else "not bound") in output.getvalue()
        elif scenario == "owner-raced":
            assert tick.scheduled_tick(args) == tick.scheduled_tick(args) == 1
            assert not watched and not any(call[:2] == ("orchestration", "run-use") for call in calls)
            assert "changed coordinator" in output.getvalue() and "held by another live terminal" in output.getvalue()
        else:
            if scenario.startswith("bind-"):
                assert tick.scheduled_tick(args) == 1 and not watched
            assert tick.scheduled_tick(args) == tick.scheduled_tick(args) == 0
            assert watched == ["term_new", "term_new"]
            assert sum(call[:2] == ("terminal", "create") for call in calls) == 1
            assert sum(call[:2] == ("orchestration", "run-use") for call in calls) == (
                2 if scenario == "bind-interrupted" else 1)


for scenario in ("closed", "other-owner", "owner-raced", "bind-interrupted", "bind-receipt-lost", "live-unbound"):
    check_anchor(scenario)

# The tick dispatches the development full run at most every 20 minutes and never twice at once.
with TemporaryDirectory() as tmp:
    stamp = Path(tmp) / "full-run.json"
    sent = []

    def gh(runs):
        def run(*a):
            sent.append(a)
            return json.dumps(runs) if a[:3] == ("gh", "run", "list") else ""
        return run

    def lines(now, runs):
        out = io.StringIO()
        with patch.object(tick, "FULL_RUN_STAMP", stamp), patch.object(tick, "command", gh(runs)), redirect_stdout(out):
            tick.full_run_tick(now)
        return out.getvalue()

    listing = ("gh", "run", "list", "-R", "julianthant/hr-relay", "--workflow", "full.yml",
               "--branch", "development", "--limit", "20", "--json", "status,event")
    dispatch = ("gh", "workflow", "run", "full.yml", "-R", "julianthant/hr-relay", "--ref", "development")
    assert "dispatched" in lines(10_000, [{"status": "completed", "event": "push"}])
    assert sent == [listing, dispatch]
    sent.clear()
    assert "within 20 minutes" in lines(10_000 + 1199, []) and sent == []
    assert "dispatched" in lines(10_000 + 1200, [])
    sent.clear()
    assert "already queued or running" in lines(20_000, [{"status": "in_progress", "event": "schedule"}])
    assert sent == [listing]

# Run fill against a real detached checkout and an Orca receipt with a pasted, unrun Grok prompt.
with TemporaryDirectory() as tmp, ExitStack() as stack, sqlite3.connect(":memory:") as db:
    root = Path(tmp).resolve()
    checkout = root / "checkout"
    git = ("git", "-C", str(checkout))
    checkout.mkdir()
    subprocess.check_output((*git, "init", "-b", "development"), stderr=subprocess.STDOUT)
    subprocess.check_output((*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                             "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "base"))
    head = subprocess.check_output((*git, "rev-parse", "HEAD"), text=True).strip()
    subprocess.check_call((*git, "update-ref", "refs/remotes/origin/development", head))
    ticket = dict(research_ticket, status="Todo", item="test-card")
    grok = dict(id="grok-seat", label="Grok", agent="grok", host="local", enabled=True, home="", slots=1)
    mail, operations, edits = [], [], []
    transcript = [{"role": "user", "blocks": [{"type": "text"}]}]

    def command(*argv):
        if "get-url" in argv:
            return "https://github.com/julianthant/hr-relay.git"
        if "ls-remote" in argv:
            return f"{head}\trefs/heads/development\n"
        if "fetch" in argv:
            return ""
        if argv[:3] == ("gh", "project", "item-edit"):
            edits.append(argv[-1])
            return ""
        assert argv[0] == "git", argv
        return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT)

    def orca(*argv):
        operations.append(argv[:2])
        match argv[:2]:
            case "worktree", "list":
                return {"worktrees": [], "truncated": False}
            case "terminal", "list":
                return {"terminals": [], "truncated": False}
            case "worktree", "show":
                path = argv[-1].removeprefix("path:")
                return {"worktree": {"id": "research-tree", "path": path, "branch": "", "isMainWorktree": False}}
            case "orchestration", "task-create":
                assert argv[argv.index("--task-title") + 1] == "#762 research"
                assert "Work read-only from this detached origin/development checkout" in argv[argv.index("--spec") + 1]
                assert "worker_done" in argv[argv.index("--spec") + 1]
                return {"task": {"id": "research-task"}}
            case "orchestration", "worker-start":
                path = argv[argv.index("--worktree") + 1].removeprefix("path:")
                assert command("git", "-C", path, "branch", "--show-current") == ""
                assert command("git", "-C", path, "rev-parse", "HEAD").strip() == head
                assert argv[argv.index("--agent"):argv.index("--effort")+2] == (
                    "--agent", "grok", "--model", "grok-4.7", "--effort", "xhigh")
                assert "--on" not in argv and "--keep-open" not in argv
                return dict(full_start("research-dispatch", "research-pane"), turnStart="unsupported")
            case "orchestration", "dispatch-show":
                assert argv[argv.index("--task") + 1] == "research-task"
                return {"dispatch": {"id": "research-dispatch"}}
            case "orchestration", "worker-show":
                return {"dispatch": {"id": "research-dispatch", "taskId": "research-task", "runId": "run",
                                     "assigneeHandle": "research-pane", "status": "dispatched"},
                        "worker": {"worktreeId": "research-tree", "state": "ready"}}
            case "orchestration", "worker-read":
                return {"dispatchId": "research-dispatch", "provider": "grok", "source": "transcript",
                        "sourceExact": True, "transcript": {"messages": transcript}}
            case "orchestration", "send":
                assert argv[argv.index("--to") + 1] == "run:run"
                assert "no observed Grok turn" in argv[-1]
                return {"messageId": "need-input"}
            case "orchestration", "inbox":
                return {"messages": mail}
            case "orchestration", "task-list":
                return {"tasks": [{"title": "#762 research", "id": "research-task"}]}
        raise AssertionError(argv)

    stack.enter_context(patch.object(tick, "SNAPSHOT", root / "issues.json"))
    stack.enter_context(patch.object(tick, "tickets", return_value=[ticket]))
    stack.enter_context(patch.object(tick, "supervisor", return_value=([], "run")))
    stack.enter_context(patch.object(tick, "project_fields", return_value=(
        "project", [("status", "progress"), ("stage", "coding"), ("agent", None)], ("stage", "needs-you"))))
    stack.enter_context(patch.object(tick, "command", side_effect=command))
    stack.enter_context(patch.object(tick, "runtime", side_effect=orca))
    stack.enter_context(patch.object(pick, "mac_host", side_effect=lambda host: host == "local"))
    stack.enter_context(patch.object(pick.account_usage, "load_config", return_value={"account": [grok]}))
    stack.enter_context(patch.object(pick.account_usage, "orca_usage", return_value={
        ("grok", None): (1, [(84, None, "weekly")])}))
    stack.enter_context(redirect_stdout(io.StringIO()))
    args = SimpleNamespace(repo_path=checkout, dry_run=False, holds=root / "holds.json", issue=None, coder=None)
    assert tick.fill_tick(args, db) == {762: None}
    assert edits == ["needs-you"]
    tick.fill_tick(args, db)
    assert edits == ["needs-you"] and operations.count(("orchestration", "send")) == 1
    transcript.append({"role": "assistant", "blocks": [{"type": "text"}]})
    tick.fill_tick(args, db)
    assert edits == ["needs-you", "progress", "coding", "research grok-4.7 xhigh @Grok"]
    mail.append(report("research-dispatch", "Research complete. Findings delivered to the Run."))
    tick.review_tick(args, db)
    tick.fill_tick(args, db)
    assert operations.count(("orchestration", "worker-start")) == 1
    assert operations.count(("orchestration", "task-create")) == 1
    assert tick.review_state(db, "run", 762) is None
    assert json.loads(args.holds.read_text())["research-pane"]["account"] == "grok-seat"
