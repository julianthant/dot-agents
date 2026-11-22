import io
from contextlib import redirect_stderr
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
    run.return_value.stdout = '{"ok": true, "result": {}}'
    assert tick.orca("status") == {"ok": True, "result": {}}
    env = run.call_args.kwargs["env"]
    assert "ORCA_TERMINAL_HANDLE" not in env and env["ORCA_CLI_COMMAND"] == "orca"
    assert run.call_args.args[0] == ["orca", "status", "--json"]

print("ok")


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
RM = ("worktree", "rm")
st = {"worktree": "path:/scratch", "anchor": "term_a", "started": True, "dispatch": "ctx_1"}
ok_rm = {RM: {"ok": True, "result": {"removed": True}}}

# Confirmed release: anchor closed, checkout removed.
run, calls = fake({REL: {"ok": True, "result": {"state": "released"}}, **ok_rm})
with patch.object(tick, "orca", run):
    assert tick.cleanup(dict(st)) == [] and RM in calls

# Unconfirmed release, failed release, or a start with no dispatch: the checkout stays.
for rel in ({"ok": True, "result": {"state": "retained"}}, {"ok": False, "error": {"code": "release_unknown"}}):
    run, calls = fake({REL: rel, **ok_rm})
    with patch.object(tick, "orca", run):
        errs = tick.cleanup(dict(st))
    assert RM not in calls and errs[-1] == "kept scratch worktree path:/scratch", errs
run, calls = fake(ok_rm)
with patch.object(tick, "orca", run):
    errs = tick.cleanup({"worktree": "path:/scratch", "started": True})
assert RM not in calls and REL not in calls and "may be live" in errs[0]

# No start attempted: nothing to release, the checkout goes.
run, calls = fake(ok_rm)
with patch.object(tick, "orca", run):
    assert tick.cleanup({"worktree": "path:/scratch"}) == [] and calls == [RM]

# A failed rm is reported.
run, calls = fake({REL: {"ok": True, "result": {"state": "released"}}, RM: {"ok": False, "error": "busy"}})
with patch.object(tick, "orca", run):
    assert "worktree rm failed" in tick.cleanup(dict(st))[0]

# finish: a cleanup failure exits 1; a proof failure is re-raised unchanged over a cleanup failure.
run, _ = fake({REL: {"ok": True, "result": {"state": "retained"}}})
with patch.object(tick, "orca", run), redirect_stderr(io.StringIO()) as err:
    try:
        tick.finish(dict(st))
        raise AssertionError("finish should exit")
    except SystemExit as e:
        assert e.code == 1
    boom = tick.OrcaError("worker-start failed")
    try:
        tick.finish(dict(st), boom)
        raise AssertionError("finish should raise")
    except tick.OrcaError as e:
        assert e is boom
assert "kept scratch worktree" in err.getvalue()

print("cleanup ok")
