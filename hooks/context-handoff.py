#!/usr/bin/env python3
"""PostToolUse hook: once per worker session, tell the model to hand off when context is large."""

import json
import os
import sys
import tempfile
from pathlib import Path


def context_tokens(path):
    latest = 0
    try:
        for line in Path(path).read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = (
                (d.get("message") or {}).get("usage")
                if d.get("type") == "assistant"
                else None
            )
            if u:
                latest = sum(
                    u.get(k) or 0
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
    except OSError:
        pass
    return latest


def main():
    if not os.environ.get("CONCORD_AGENT_ID"):
        return
    d = json.load(sys.stdin)
    limit = int(os.environ.get("HANDOFF_AT_TOKENS", "300000"))
    marker = (
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        / f"context-handoff-{d['session_id']}"
    )
    if marker.exists() or context_tokens(d.get("transcript_path", "")) < limit:
        return
    marker.touch()
    msg = (
        "Context limit reached. Finish your current step, write the handoff to your ticket, then stop. "
        "Do not start new work."
    )
    out = {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
