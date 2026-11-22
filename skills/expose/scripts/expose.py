#!/usr/bin/env python3
"""Print the skills whose metadata.exposed is "true": name, description, SKILL.md path."""

import argparse
import re
import sys
from pathlib import Path


def scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse(text):
    m = re.match(r"---\n(.*?)\n---\s*(\n|$)", text, re.DOTALL)
    if not m:
        raise ValueError("no frontmatter")
    lines = m.group(1).split("\n")
    out, i = {}, 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        k = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not k or line[0] == " ":
            raise ValueError(f"bad line: {line!r}")
        key, val = k.groups()
        block = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] == " "):
            block.append(lines[i])
            i += 1
        if val in (">", "|", ">-", "|-"):
            parts = [b.strip() for b in block]
            out[key] = (" " if val[0] == ">" else "\n").join(parts).strip()
        elif val == "" and key == "metadata":
            meta = {}
            for b in block:
                if not b.strip():
                    continue
                n = re.match(r"\s+([\w-]+):\s*(.*)$", b)
                if not n:
                    raise ValueError(f"bad metadata line: {b!r}")
                meta[n.group(1)] = scalar(n.group(2))
            out[key] = meta
        elif block and any(b.strip() for b in block):
            raise ValueError(f"unexpected indentation under {key}")
        else:
            out[key] = scalar(val)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", type=Path)
    roots = ap.parse_args(argv).root
    if not roots:
        roots = [Path("~/.agents/skills").expanduser(), Path(".agents/skills")]
    found, bad = [], False
    for root in roots:
        for f in sorted(root.glob("*/SKILL.md")):
            try:
                fm = parse(f.read_text())
                if "name" not in fm or "description" not in fm:
                    raise ValueError("missing name or description")
            except (ValueError, OSError) as e:
                print(f"{f}: {e}", file=sys.stderr)
                bad = True
                continue
            if (fm.get("metadata") or {}).get("exposed") == "true":
                found.append((fm["name"], fm["description"], f.resolve()))
    for name, desc, path in sorted(found):
        print(f"{name}\n  {desc}\n  {path}\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
