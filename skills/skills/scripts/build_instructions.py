#!/usr/bin/env python3
"""Build ~/.codex/AGENTS.md from the shared core and the Codex section.

Codex cannot import files, so its instructions are generated. The codex_workflow tool's managed
block (between its start/end markers) is carried over from the current file unchanged.
--check exits 1 when the file is out of date instead of writing it.
"""

import argparse
import sys
from pathlib import Path

START = "<!-- codex-workflow-user-managed-start -->"
END = "<!-- codex-workflow-user-managed-end -->"
HEADER = (
    "<!-- Generated from ~/.agents/AGENTS.md and ~/.agents/platforms/codex.md by "
    "~/.agents/skills/skills/scripts/build_instructions.py. Edit those files, then rebuild. -->\n\n"
)


def managed_block(current: str) -> str:
    start, end = current.find(START), current.find(END)
    if start == -1 or end == -1:
        return ""
    return current[start : end + len(END)] + "\n"


def build(shared: str, section: str, current: str) -> str:
    parts = [HEADER + shared.rstrip() + "\n", section.rstrip() + "\n"]
    block = managed_block(current)
    if block:
        parts.append(block)
    return "\n".join(parts)


def main() -> None:
    home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument("--shared", type=Path, default=home / ".agents" / "AGENTS.md")
    parser.add_argument("--section", type=Path, default=home / ".agents" / "platforms" / "codex.md")
    parser.add_argument("--out", type=Path, default=home / ".codex" / "AGENTS.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
    result = build(args.shared.read_text(encoding="utf-8"), args.section.read_text(encoding="utf-8"), current)
    if args.check:
        sys.exit(0 if result == current else 1)
    args.out.write_text(result, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
