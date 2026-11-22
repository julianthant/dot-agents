#!/usr/bin/env python3
"""Deny a Bash command that already failed 3 times in a row.

PostToolUseFailure records a failure, PostToolUse (success) clears the streak,
PreToolUse denies the identical command after 3 failures. Worker sessions only
(CONCORD_AGENT_ID set).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REASON = "This command failed 3 times. Record the blocker on your ticket and try a different approach."


def main():
    if not os.environ.get("CONCORD_AGENT_ID"):
        return
    d = json.load(sys.stdin)
    if d.get("tool_name") != "Bash":
        return
    cmd = (d.get("tool_input") or {}).get("command", "")
    state = (
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        / f"repeat-failure-{d['session_id']}.json"
    )
    try:
        s = json.loads(state.read_text())
    except (OSError, ValueError):
        s = {"cmd": None, "n": 0}
    event = d.get("hook_event_name")
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
        s = {"cmd": None, "n": 0}
    else:
        return
    state.write_text(json.dumps(s))


if __name__ == "__main__":
    main()
