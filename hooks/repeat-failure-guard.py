#!/usr/bin/env python3
"""Deny a Bash command that already failed 3 times in a row.

PostToolUseFailure records a failure, PostToolUse (success) clears the streak,
PreToolUse denies the identical command after 3 failures. Orca workers only.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from orca_worker import is_worker

REASON = "This command failed 3 times. Record the blocker in your worker report and try a different approach."


def main():
    if not os.environ.get("ORCA_TERMINAL_HANDLE"):
        return
    d = json.load(sys.stdin)
    if d.get("tool_name") != "Bash":
        return
    cmd = (d.get("tool_input") or {}).get("command", "")
    state = (
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        / f"repeat-failure-{d['session_id']}.json"
    )
    event = d.get("hook_event_name")
    if event != "PostToolUseFailure" and not state.exists():
        return
    if not is_worker():
        return
    try:
        s = json.loads(state.read_text())
    except (OSError, ValueError):
        s = {"cmd": None, "n": 0}
    if event == "PreToolUse":
        if s["cmd"] == cmd and s["n"] >= 3:
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON,
                }
            }
            print(json.dumps(out))
        return
    if event == "PostToolUseFailure":
        s = {"cmd": cmd, "n": s["n"] + 1 if s["cmd"] == cmd else 1}
    elif event == "PostToolUse":
        state.unlink(missing_ok=True)
        return
    else:
        return
    state.write_text(json.dumps(s))


if __name__ == "__main__":
    main()
