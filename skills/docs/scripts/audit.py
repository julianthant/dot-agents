#!/usr/bin/env python3
"""Read-only repository Markdown inventory and instruction loading checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess

IGNORED = {".git", "node_modules", ".tracker", ".auth", ".venv", "__pycache__"}
NAMES = {"AGENTS.md", "CLAUDE.md", "AGENTS.override.md"}


def inventory(root):
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        if Path(probe.stdout.strip()).resolve() != root:
            raise ValueError("pass the Git repository root so ancestor instructions are counted")
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "-co",
                                  "--exclude-standard", "-z"],
                                 capture_output=True, check=True)
        candidates = {root / os.fsdecode(p) for p in listing.stdout.split(b"\0") if p}
    else:
        candidates = set()
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in IGNORED]
            candidates.update(Path(directory) / name for name in files)
    return sorted(p for p in candidates if p.suffix.lower() == ".md"
                  and not any(x in IGNORED for x in p.relative_to(root).parts)
                  and (p.exists() or p.is_symlink()))


def audit(root, limit):
    documents = []
    contents = {}
    issues = []
    for path in inventory(root):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            issues.append(f"external Markdown symlink: {path.relative_to(root)}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            issues.append(f"unreadable {path.relative_to(root)}: {exc}")
            continue
        contents[path] = data
        documents.append({"path": str(path.relative_to(root)), "bytes": len(data),
                          "instruction": path.name in NAMES})
    directories = sorted({p.parent for p in contents if p.name in NAMES})
    pairs = []
    chains = []
    for directory in directories:
        agent = directory / "AGENTS.md"
        claude = directory / "CLAUDE.md"
        if agent in contents and claude in contents:
            equal = contents[agent] == contents[claude]
            pairs.append({"directory": str(directory.relative_to(root)), "equal": equal})
            if not equal:
                issues.append(f"instruction pair differs: {directory.relative_to(root)}")
        lineage = [root]
        current = root
        for part in directory.relative_to(root).parts:
            current = current / part
            lineage.append(current)
        selected = []
        for folder in lineage:
            for name in ["AGENTS.override.md", "AGENTS.md"]:
                candidate = folder / name
                if contents.get(candidate, b"").strip():
                    selected.append(candidate)
                    break
        size = sum(len(contents[p]) for p in selected) + max(0, len(selected) - 1) * 2
        chains.append({"directory": str(directory.relative_to(root)), "bytes": size,
                       "files": [str(p.relative_to(root)) for p in selected]})
        if size > limit:
            issues.append(f"project instruction chain exceeds {limit}: "
                          f"{directory.relative_to(root)} ({size} bytes)")
    singles = [str(d.relative_to(root)) for d in directories
               if (d / "AGENTS.md" in contents) != (d / "CLAUDE.md" in contents)]
    return {"root": str(root), "project_doc_max_bytes_assumed": limit,
            "documents": sorted(documents, key=lambda x: (-x["bytes"], x["path"])),
            "pairs": pairs, "single_name_directories": singles,
            "project_chains": chains, "issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--project-doc-max-bytes", type=int, default=32768)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.root.is_dir() or args.project_doc_max_bytes < 1:
        parser.error("root must be a directory and the byte limit must be positive")
    try:
        result = audit(args.root.resolve(), args.project_doc_max_bytes)
    except ValueError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Markdown files: {len(result['documents'])}; instruction pairs: {len(result['pairs'])}")
        print(f"Assumed Codex project limit: {args.project_doc_max_bytes} bytes (global guidance excluded)")
        for row in result["project_chains"]:
            print(f"  {row['bytes']:>7} bytes  {row['directory']}")
        print("Largest non-instruction documents (size is not a defect):")
        for row in [d for d in result["documents"] if not d["instruction"]][:12]:
            print(f"  {row['bytes']:>7} bytes  {row['path']}")
        for directory in result["single_name_directories"]:
            print(f"Single instruction name: {directory}")
        for issue in result["issues"]:
            print(f"FAIL: {issue}")
        if not result["issues"]:
            print("PASS: existing pairs match; discovered project chains fit.")
    return 1 if args.check and result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
