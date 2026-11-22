#!/usr/bin/env python3
"""Count Claude Code skill loads (Skill tool calls and typed /commands) per skill."""

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
GLOBS = [
    (HOME / ".claude/projects", "**/*.jsonl"),
    (
        HOME / "Library/Application Support/orca/claude-profiles",
        "*/home/projects/**/*.jsonl",
    ),
]
CMD = re.compile(
    r"\A\s*(?:<command-message>[^<]*</command-message>\s*)?<command-name>/?([^<\s]+)</command-name>"
)


def transcripts():
    seen = {}
    for base, pat in GLOBS:
        for f in base.glob(pat):
            seen.setdefault(f.resolve(), f)
    return list(seen)


def count(files, since):
    tool, typed = Counter(), Counter()
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
                ts = datetime.fromisoformat(d["timestamp"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if ts < since or not isinstance(d.get("message"), dict):
                continue
            c = d["message"].get("content")
            if isinstance(c, str):
                typed.update(CMD.findall(c)) if d.get("type") == "user" else None
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") == "Skill":
                        s = (b.get("input") or {}).get("skill")
                        if s:
                            tool[s] += 1
                    elif b.get("type") == "text" and d.get("type") == "user":
                        typed.update(CMD.findall(b.get("text", "")))
    return tool, typed


def installed():
    return sorted(p.parent.name for p in (HOME / ".agents/skills").glob("*/SKILL.md"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    days = ap.parse_args(argv).days
    since = datetime.now(timezone.utc) - timedelta(days=days)  # noqa: UP017
    tool, typed = count(transcripts(), since)
    names = set(tool) | set(typed)
    rows = sorted(names, key=lambda n: (-(tool[n] + typed[n]), n))
    print(f"{'skill':32} {'total':>6} {'tool':>6} {'typed':>6}")
    for n in rows:
        print(f"{n:32} {tool[n] + typed[n]:6} {tool[n]:6} {typed[n]:6}")
    unused = [n for n in installed() if n not in names]
    print(f"\nInstalled with zero uses in {days} days: {', '.join(unused) or 'none'}")
    print("Codex and Cursor sessions are not counted.")


if __name__ == "__main__":
    main()
