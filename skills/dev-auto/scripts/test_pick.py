import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pick

pro, mx = pick.LIM["claude"]["pro"], pick.LIM["claude"]["max"]
plus, lite = pick.LIM["codex"]["plus"], pick.LIM["codex"]["prolite"]
S, O = "claude-sonnet-5-5", "claude-opus-5-5"
c = pick.claude_reason

# Pro: 2 Opus + 2 Sonnet; above 60% 5-hour 1 + 1; hands off at 95%.
assert c(pro, S, [O, O, S], 10, 0) is None
assert c(pro, O, [O, O], 10, 0) == "opus 2 of 2"
assert c(pro, S, [S], 61, 0) == "sonnet 1 of 1"
assert c(pro, O, [], 95, 0).startswith("5-hour 95%")
# Max: 8 sessions, 6 Opus; above 90% half; hands off at 97%.
assert c(mx, O, [O] * 5, 50, 0) is None
assert c(mx, O, [O] * 6, 50, 0) == "opus 6 of 6"
assert c(mx, S, [O] * 4, 91, 0) == "4 of 4 sessions"
assert c(mx, S, [], 97, 0).startswith("5-hour 97%")
# Weekly: 90% one ticket (2 sessions), 97% Sonnet only.
assert c(mx, S, [S, S], 10, 90) == "2 of 2 sessions"
assert c(pro, O, [], 10, 97) == "weekly 97%: Sonnet only"
assert c(pro, S, [], 10, 97) is None

x = pick.codex_reason
M, H = "gpt-6.2-sol:medium", "gpt-6.2-sol:high"
assert x("plus", plus, M, [M, M, M], 0, 0) is None
assert x("plus", plus, H, [M, M, M], 0, 0) == "capacity 3 + 2 over 4"
assert x("plus", plus, M, [], 95, 0).startswith("5-hour 95%")
assert x("plus", plus, H, [], 0, 97) == "weekly 97%: gpt-6.2-sol medium only"
assert x("plus", plus, M, [M, M], 0, 90) == "weekly 90%: one ticket at a time"
assert x("plus", plus, "gpt-6.2-luna:low", [], 0, 0).startswith("no weight")
# Pro Lite: no caps until 97% weekly, then one Sol worker.
assert x("prolite", lite, H, [M] * 20, 99, 96) is None
assert x("prolite", lite, H, [], 0, 97) is None
assert x("prolite", lite, M, [M], 0, 97) == "weekly 97%: one gpt-6.2-sol worker only"
# Exercise the CLI against an isolated account, hold file and read-only Orca fixture.
with TemporaryDirectory() as temp:
    root = Path(temp)
    config = root / "accounts.toml"
    config.write_text(f'[[account]]\nid = "fixture"\nlabel = "Fixture"\nagent = "claude"\n'
                      f'plan = "pro"\nhome = {json.dumps(temp)}\n')
    account = {"activeAccountId": "fixture"}
    limits = {"updatedAt": 1000, "session": {"usedPercent": 96}}

    def fake_orca(args, **kwargs):
        replies = {
            ("orca", "account", "list", "--json"): {"claude": account, "rateLimits": {"claude": limits}},
            ("orca", "terminal", "list", "--limit", "10000", "--json"): {"terminals": [{"handle": "worker"}]},
        }
        return pick.subprocess.CompletedProcess(args, 0, json.dumps({"result": replies[tuple(args)]}))

    loader = pick.account_usage.load_config
    with (patch.object(pick, "HOLDS", root / "holds.json"),
          patch.object(pick.account_usage, "ORCA", "orca"),
          patch.object(pick.account_usage, "load_config", side_effect=lambda: loader(config)),
          patch.object(pick.time, "time", side_effect=[10, 11, 12, 13]),
          patch.object(pick.subprocess, "run", side_effect=fake_orca),
          redirect_stdout(io.StringIO()) as output):
        rate_file = root / "rate-limits.json"
        rate_file.write_text(json.dumps({"saved_at": 2, "rate_limits": {"five_hour": {"used_percentage": 5}}}))
        route = ["--agent", "claude", "--model", S]
        assert pick.main(["pick", *route]) == 0
        assert json.loads(output.getvalue())["select"][-1] == "fixture"
        assert pick.main(["hold", "--terminal", "worker", "--account", "Fixture", *route]) == 0
        assert json.loads(pick.HOLDS.read_text())["worker"]["account"] == "fixture"
        assert pick.main(["due"]) == 1
        limits["updatedAt"] = 3000
        assert pick.main(["due"]) == 0
        assert output.getvalue().splitlines()[-1] == "Fixture worker 5-hour 96%"


# Bug 2: starts and restarts skip an account nearly out (under 10% of 5 hours or 5% of the week left).
accounts = {"account": [dict(id=i, label=i, agent="codex", plan=p, host="local", enabled=True)
                        for i, p in (("ais", "plus"), ("lite", "prolite"), ("free", "plus"))]}
use = {"ais": (91, 10), "lite": (0, 97), "free": (20, 20)}
seat = lambda model: pick.pick(accounts, "codex", model, "medium", "local", 0, holds=[])
with patch.object(pick, "windows", lambda cfg, row, now: use[row["id"]]):
    chosen, why = seat("gpt-6.2-astra")
    assert chosen["id"] == "free" and why == ["lite: weekly 97%: under 5% left", "ais: 5-hour 91%: under 10% left"], why
    use["lite"] = (99, 50)  # Pro Lite has no 5-hour window to run out of
    # Operator 2026-11-23: Astra goes to Pro Lite before any Plus seat; Sol keeps Plus first, Pro Lite last.
    assert seat("gpt-6.2-astra")[0]["id"] == "lite"
    assert seat("gpt-6.2-sol")[0]["id"] == "free"
    use["free"] = (95, 20)
    assert seat("gpt-6.2-sol")[0]["id"] == "lite"

# Coordinator: keep the selected Codex account while under its handoff point, else switch.
row = {"id": "a", "label": "a", "agent": "codex", "plan": "plus"}
other = {"id": "b", "label": "b", "agent": "codex", "plan": "plus"}
cfg = {"account": [row]}
with patch.object(pick, "windows", return_value=(40, 10)), patch.object(pick, "pick", return_value=(other, [])):
    assert pick.coordinator(cfg, "local", 0, "a") is None
with patch.object(pick, "windows", return_value=(100, 10)), patch.object(pick, "pick", return_value=(other, [])):
    assert pick.coordinator(cfg, "local", 0, "a") is other
with patch.object(pick, "windows", return_value=(100, 10)), patch.object(pick, "pick", return_value=(None, [])):
    assert pick.coordinator(cfg, "local", 0, "a") is None
assert pick.past_handoff(row, 10, 100).startswith("limit reached")

# Fill: ready = open leaf Task, no open blocker, nobody on it, not parked.
mk = lambda **k: {"open": True, "kids": 0, "type": "Task", "blockers": [], "later": False, "ms": "Staging cut", "status": "Todo", "agent": None, **k}
data = {"issues": [mk(n=1), mk(n=2, blockers=[1]), mk(n=3, agent="Coding"), mk(n=4, later=True), mk(n=5, kids=3), mk(n=6, open=False)]}
assert [i["n"] for i in pick.ready_tickets(data)] == [1]
with patch.object(pick, "pick", return_value=(None, [])):
    assert pick.fill({}, "local", 0, data)[1] is False

# A partial terminal inventory must never prune another fleet worker's hold.
with TemporaryDirectory() as temp, patch.object(pick, "HOLDS", Path(temp) / "holds.json"):
    original = '{"worker": {"host": "local"}}'
    pick.HOLDS.write_text(original)
    with patch.object(pick.subprocess, "run", return_value=pick.subprocess.CompletedProcess(
            [], 0, '{"result":{"terminals":[],"truncated":true}}')):
        try:
            pick.live_holds("local")
        except SystemExit:
            pass
    assert pick.HOLDS.read_text() == original

# A prune and a hold on the same ledger use the same lock, including fill's writer.
import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import tick

with TemporaryDirectory() as temp, patch.object(pick, "HOLDS", Path(temp) / "holds.json"):
    pick.HOLDS.write_text('{"dead":{"host":"local"},"remote":{"host":"other"}}')
    pruning, resume = threading.Event(), threading.Event()
    def inventory(host):
        pruning.set()
        assert resume.wait(5)
        return set()
    with patch.object(pick, "live_handles", inventory), ThreadPoolExecutor(3) as pool:
        prune = pool.submit(pick.live_holds, "local")
        assert pruning.wait(5)
        try:
            with pick.HOLDS.with_suffix(".lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    raise AssertionError("pruning must hold the ledger lock during inventory")
                except BlockingIOError:
                    pass
            fleet = pool.submit(pick.hold, {"account": [row]}, SimpleNamespace(
                terminal="fleet", account="a", agent="codex", model=M, effort="medium", host="local"))
            fill = pool.submit(tick.record_hold, dict(terminal="fill", account="a", label="a",
                               route=("codex", M, "medium", None), time=0, holds=str(pick.HOLDS)))
        finally:
            resume.set()
        for job in (prune, fleet, fill):
            job.result()
    assert set(json.loads(pick.HOLDS.read_text())) == {"fleet", "fill", "remote"}

print("pick: all checks pass")

# Codex homes share one sessions folder whose newest row names no writer: Codex reads only Orca's per-account
# reading, even as the active account; each window comes from the newest reading that has it.
now = 1_000_000
row = {"agent": "codex", "id": "seat", "host": "local", "home": "/h"}
orca = {("codex", "seat"): (now - 600, [(100, now + 3600, "five_hour"), (40, now + 86400, "weekly")])}
shared = (now - 60, [(23, now + 3600, "five_hour"), (94, now + 86400, "weekly")])
with patch.object(pick.account_usage, "orca_usage", return_value=orca), \
        patch.object(pick.account_usage, "orca_key", return_value=("codex", "seat")), \
        patch.object(pick.account_usage, "file_usage", return_value=shared):
    assert pick.windows({}, row, now) == (100.0, 40.0)  # another account wrote the file
    assert pick.windows({}, dict(row, agent="claude"), now) == (23.0, 94.0)
    orca[("active", "codex")] = "seat"
    # A newer reading another account wrote does not override this seat's 97% weekly.
    orca[("codex", "seat")] = (now - 600, [(10, now + 3600, "five_hour"), (97, now + 86400, "weekly")])
    assert pick.windows({}, row, now) == (10.0, 97.0)
    del orca[("active", "codex")]
    orca[("codex", "seat")] = (now - 600, [(100, now + 3600, "five_hour"), (40, now + 86400, "weekly")])
with patch.object(pick.account_usage, "orca_usage", return_value={}), \
        patch.object(pick.account_usage, "orca_key", return_value=("codex", "seat")), \
        patch.object(pick.account_usage, "file_usage", return_value=shared):
    assert pick.windows({}, row, now) == (None, None)  # never read: the shared file is not this account's
# A newer reading without the weekly window keeps the older weekly use instead of reading 0.
weekly_only = (now - 30, [(10, now + 3600, "five_hour")])
with patch.object(pick.account_usage, "orca_usage", return_value=orca), \
        patch.object(pick.account_usage, "orca_key", return_value=("codex", "seat")), \
        patch.object(pick.account_usage, "file_usage", return_value=weekly_only):
    assert pick.windows({}, dict(row, agent="claude"), now) == (10.0, 40.0)

# The same Orca id may exist on two hosts. Remote usage, including inactive
# accounts, must never pick up the local reading or a local CLI file.
remote = {"agent": "codex", "host": "Juzzark-WSL", "id": "seat", "home": "", "plan": "plus"}
remote_result = {"codex": {"activeAccountId": "other"}, "rateLimits": {"inactiveCodexAccounts": [
    {"accountId": "seat", "rateLimits": {"updatedAt": 2000, "session": {"usedPercent": 36}}}]}}
with patch.object(pick.account_usage, "remote_account", return_value=remote_result) as read:
    pick.account_usage.ORCA_USAGE.clear()
    assert pick.windows({}, remote, 3) == (36, 0)
    assert pick.windows({}, remote, 3) == (36, 0)
    read.assert_called_once_with("Juzzark-WSL", "list")
