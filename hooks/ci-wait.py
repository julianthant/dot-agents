#!/usr/bin/env python3
"""Redirect GitHub CLI watch commands to the shared roadmap."""
import json
from pathlib import PurePosixPath
import shlex
import sys


def watches(command):
    shell = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    shell.whitespace_split = True
    tokens = list(shell)
    for i, token in enumerate(tokens):
        if PurePosixPath(token).name != "gh":
            continue
        args = []
        for arg in tokens[i + 1:]:
            if all(char in ";&|()" for char in arg):
                break
            args.append(arg)
        if args[:2] == ["run", "watch"] or args[:2] == ["pr", "checks"] and "--watch" in args[2:]:
            return True
    return False


def main():
    try:
        event = json.load(sys.stdin)
        command = (event.get("tool_input") or {}).get("command", "")
        if event.get("tool_name") != "Bash" or not watches(command):
            return
    except ValueError:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "CI uses the roadmap. Run ~/.agents/skills/dev-auto/scripts/ci-wait <sha|branch>.",
    }}))


if __name__ == "__main__":
    main()
