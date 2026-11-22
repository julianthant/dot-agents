import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def run(script, payload, **env):
    r = subprocess.run(
        [sys.executable, str(HERE / script)],
        input=json.dumps(payload),
        capture_output=True,
        check=False,
        text=True,
        env={"PATH": "", **env},
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def worker_env(directory):
    cli = Path(directory) / "orca"
    cli.write_text(
        "#!/bin/sh\n"
        """case "$*" in
"orchestration worker-list --environment local --limit 100 --json")
  echo '{"result":{"workers":[{"terminal":"settled","outcome":"succeeded"}],"nextCursor":"2"}}' ;;
"orchestration worker-list --environment local --limit 100 --json --cursor 2")
  echo '{"result":{"workers":[{"terminal":"w1","outcome":"in_progress"}],"nextCursor":null}}' ;;
*) exit 1 ;;
esac
"""
    )
    cli.chmod(0o755)
    return {"PATH": directory, "ORCA_TERMINAL_HANDLE": "w1"}


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        use = {
            "input_tokens": 10,
            "cache_read_input_tokens": 250000,
            "cache_creation_input_tokens": 49990,
        }
        small = {"input_tokens": 1}
        lines = [
            {"type": "assistant", "message": {"usage": small}},
            {"type": "assistant", "message": {"usage": use}},
        ]
        (t / "tr.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        self.payload = {"session_id": "s1", "transcript_path": str(t / "tr.jsonl")}
        self.env = {"TMPDIR": self.tmp.name, **worker_env(self.tmp.name)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_fires_once_at_300k(self):
        out = json.loads(run("context-handoff.py", self.payload, **self.env))
        message = out["hookSpecificOutput"]["additionalContext"]
        for instruction in (
            "Finish",
            "/handoff",
            "handoff path",
            "worker_done",
            "stop",
        ):
            self.assertIn(instruction, message)
        self.assertEqual(run("context-handoff.py", self.payload, **self.env), "")

    def test_ignored_outside_live_dispatch(self):
        for handle in ("", "ordinary", "settled"):
            with self.subTest(handle=handle):
                self.env["ORCA_TERMINAL_HANDLE"] = handle
                self.assertEqual(
                    run("context-handoff.py", self.payload, **self.env), ""
                )

    def test_below_limit(self):
        self.assertEqual(
            run(
                "context-handoff.py",
                self.payload,
                HANDOFF_AT_TOKENS="300001",
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
            env = worker_env(t)

            def ev(name, cmd):
                return guard(t, name, cmd, **env)

            for _ in range(2):
                ev("PostToolUseFailure", "make")
            self.assertEqual(ev("PreToolUse", "make"), "")
            ev("PostToolUseFailure", "make")
            out = json.loads(ev("PreToolUse", "make"))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn(
                "worker report", out["hookSpecificOutput"]["permissionDecisionReason"]
            )
            self.assertEqual(ev("PreToolUse", "other"), "")
            ev("PostToolUse", "ls")
            self.assertEqual(ev("PreToolUse", "make"), "")

    def test_ignored_without_terminal_handle(self):
        with tempfile.TemporaryDirectory() as t:
            for _ in range(3):
                self.assertEqual(guard(t, "PostToolUseFailure", "make"), "")
            self.assertEqual(guard(t, "PreToolUse", "make"), "")
            self.assertEqual(list(Path(t).iterdir()), [], "records nothing")


if __name__ == "__main__":
    unittest.main()
