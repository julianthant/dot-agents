import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def run(script, payload, **env):
    e = {
        k: v for k, v in os.environ.items() if not k.startswith(("CONCORD", "HANDOFF"))
    }
    e.update(env)
    r = subprocess.run(
        [sys.executable, str(HERE / script)],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env=e,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        use = {
            "input_tokens": 10,
            "cache_read_input_tokens": 250000,
            "cache_creation_input_tokens": 60000,
        }
        small = {"input_tokens": 1}
        lines = [
            {"type": "assistant", "message": {"usage": small}},
            {"type": "assistant", "message": {"usage": use}},
        ]
        (t / "tr.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        self.payload = {"session_id": "s1", "transcript_path": str(t / "tr.jsonl")}
        self.env = {"TMPDIR": self.tmp.name, "CONCORD_AGENT_ID": "w1"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_fires_once(self):
        out = json.loads(run("context-handoff.py", self.payload, **self.env))
        self.assertIn("handoff", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(run("context-handoff.py", self.payload, **self.env), "")

    def test_ignored_without_agent_id(self):
        self.env.pop("CONCORD_AGENT_ID")
        self.assertEqual(run("context-handoff.py", self.payload, **self.env), "")

    def test_below_limit(self):
        self.assertEqual(
            run(
                "context-handoff.py",
                self.payload,
                HANDOFF_AT_TOKENS="400000",
                **self.env,
            ),
            "",
        )


def guard(tmp, name, cmd, **env):
    p = {
        "session_id": "s",
        "hook_event_name": name,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }
    return run("repeat-failure-guard.py", p, TMPDIR=tmp, **env)


class RepeatTest(unittest.TestCase):
    def test_denies_after_three(self):
        with tempfile.TemporaryDirectory() as t:

            def ev(name, cmd):
                return guard(t, name, cmd, CONCORD_AGENT_ID="w1")

            for _ in range(2):
                ev("PostToolUseFailure", "make")
            self.assertEqual(ev("PreToolUse", "make"), "")
            ev("PostToolUseFailure", "make")
            out = json.loads(ev("PreToolUse", "make"))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(ev("PreToolUse", "other"), "")
            ev("PostToolUse", "ls")
            self.assertEqual(ev("PreToolUse", "make"), "")

    def test_ignored_without_agent_id(self):
        with tempfile.TemporaryDirectory() as t:
            for _ in range(3):
                self.assertEqual(guard(t, "PostToolUseFailure", "make"), "")
            self.assertEqual(guard(t, "PreToolUse", "make"), "")
            self.assertEqual(list(Path(t).iterdir()), [], "records nothing")


if __name__ == "__main__":
    unittest.main()
