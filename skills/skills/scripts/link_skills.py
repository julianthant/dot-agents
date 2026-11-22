#!/usr/bin/env python3
"""Point an agent's skill folder at the shared ~/.agents copy, one link per skill.

Dry run by default. --apply creates missing links, replaces stale links, removes links whose shared
skill is gone, and moves real folders that shadow a shared skill into --backup (only with --replace).
Entries that are not shared skills (for example claude.ai's `synced/`) are never touched.
"""

import argparse
import shutil
from pathlib import Path


def shared_skills(source: Path) -> list[str]:
    return sorted(p.name for p in source.iterdir() if (p / "SKILL.md").is_file())


def plan(source: Path, target: Path, replace: bool) -> list[tuple[str, str]]:
    names = set(shared_skills(source))
    actions: list[tuple[str, str]] = []
    for name in sorted(names):
        link = target / name
        want = source / name
        if link.is_symlink():
            if Path(link.readlink()) != want:
                actions.append(("relink", name))
        elif link.exists():
            actions.append(("replace" if replace else "conflict", name))
        else:
            actions.append(("link", name))
    if target.is_dir():
        for entry in sorted(target.iterdir()):
            if entry.is_symlink() and entry.name not in names:
                dest = Path(entry.readlink())
                if dest.parent == source:
                    actions.append(("unlink", entry.name))
    return actions


def apply(source: Path, target: Path, actions: list[tuple[str, str]], backup: Path | None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for action, name in actions:
        link = target / name
        if action == "unlink" or action == "relink":
            link.unlink()
        if action == "replace":
            if backup is None:
                raise SystemExit("--replace needs --backup")
            backup.mkdir(parents=True, exist_ok=True)
            shutil.move(str(link), str(backup / name))
        if action in ("link", "relink", "replace"):
            link.symlink_to(source / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / ".agents" / "skills")
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude" / "skills")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace", action="store_true", help="move shadowing real folders to --backup")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    actions = plan(args.source, args.target, args.replace)
    for action, name in actions:
        print(f"{action:8} {name}")
    if not actions:
        print("in sync")
    if args.apply:
        if any(a == "conflict" for a, _ in actions):
            raise SystemExit("conflicts: rerun with --replace --backup <dir>")
        apply(args.source, args.target, actions, args.backup)


if __name__ == "__main__":
    main()
