import io
from contextlib import redirect_stderr, redirect_stdout
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

print("ok")
