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
            ("orca", "terminal", "list", "--json"): {"terminals": [{"handle": "worker"}]},
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

print("pick: all checks pass")

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
