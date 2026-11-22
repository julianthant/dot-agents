#!/usr/bin/env python3
"""Emit deterministic skill inventories and per-agent visibility."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frontmatter_flag(path: Path, key: str) -> bool | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    prefix = f"{key}:"
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(prefix):
            value = line.split(":", 1)[1].strip().strip("'\"").lower()
            if value in {"true", "false"}:
                return value == "true"
    return None


def skill_invocation(directory: Path, skill_file: Path) -> str:
    claude = frontmatter_flag(skill_file, "disable-model-invocation") is True
    codex = any(
        (directory / "agents" / name).is_file()
        and "allow_implicit_invocation: false"
        in (directory / "agents" / name).read_text(encoding="utf-8")
        for name in ("openai.yaml", "openai.yml")
    )
    if claude and codex:
        return "explicit-only"
    if claude or codex:
        return "explicit-only-partial"
    return "implicit"


def directory_inventory(
    root: Path, entrypoint: str, collection: str, *, skills: bool
) -> dict[str, object]:
    entries: dict[str, object] = {}
    if not root.is_dir():
        return {
            "root": str(root),
            "exists": False,
            collection: entries,
            "operator_names": [],
            "internal_names": [],
        }

    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if not (directory / entrypoint).is_file():
            continue
        files = {
            str(path.relative_to(directory)): file_hash(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        }
        tree = hashlib.sha256()
        for relative, digest in files.items():
            tree.update(relative.encode("utf-8"))
            tree.update(b"\0")
            tree.update(digest.encode("ascii"))
            tree.update(b"\n")
        entry: dict[str, object] = {"tree_sha256": tree.hexdigest(), "files": files}
        if skills:
            skill_file = directory / entrypoint
            exposure = (
                "hidden-helper"
                if frontmatter_flag(skill_file, "user-invocable") is False
                else "operator"
            )
            entry["exposure"] = exposure
            entry["invocation"] = skill_invocation(directory, skill_file)
        else:
            entry["exposure"] = "internal"
        entries[directory.name] = entry

    return {
        "root": str(root),
        "exists": True,
        collection: entries,
        "operator_names": sorted(
            name for name, entry in entries.items() if entry["exposure"] == "operator"
        ),
        "internal_names": sorted(
            name for name, entry in entries.items() if entry["exposure"] == "internal"
        ),
    }


def skill_inventory(root: Path) -> dict[str, object]:
    return directory_inventory(root, "SKILL.md", "skills", skills=True)


def claude_view(shared: Path, target: Path) -> dict[str, str]:
    names = {p.name for p in shared.iterdir() if (p / "SKILL.md").is_file()}
    view: dict[str, str] = {}
    entries = {p.name: p for p in target.iterdir()} if target.is_dir() else {}
    for name in sorted(names | set(entries)):
        entry = entries.get(name)
        if entry is None:
            view[name] = "missing-link"
        elif entry.is_symlink():
            view[name] = "ok" if entry.resolve() == (shared / name).resolve() else "stale-link"
        elif name in names:
            view[name] = "shadowed"
        elif not name.startswith("."):
            view[name] = "agent-only"
    return view


def broken_references(*roots: Path) -> list[str]:
    home = Path.home()
    broken: set[str] = set()
    pattern = re.compile(r"~/\.agents/[^\s)`'\"<>|,]+")
    for root in roots:
        for md in root.rglob("*.md"):
            for ref in pattern.findall(md.read_text(encoding="utf-8", errors="ignore")):
                ref = ref.rstrip(".:;").split("#")[0]
                path = ref.replace("~", str(home), 1)
                if "<" in ref or "*" in ref:
                    continue
                if not Path(path).exists():
                    broken.add(f"{md.relative_to(home)}: {ref}")
    return sorted(broken)


PLATFORM_TERMS = re.compile(
    r"\b(Opus|Sonnet|Haiku|Fable|Luna|Astra|GPT-[0-9][\w.-]*|gemini-[0-9][\w.-]*|sup[-_](?:opus|sonnet|scout|review|sol|terra|analyst)"
    r"|AskUserQuestion|TodoWrite|WebFetch|spawn_agent|apply_patch|create_thread|set_thread_archived|view_image"
    r"|astra-orchestrator|selected provider adapter|worker adapter)\b"
)


def platform_terms(*roots: Path) -> list[str]:
    home = Path.home()
    found: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".md", ".json", ".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            hits = sorted(set(PLATFORM_TERMS.findall(text)))
            if hits:
                found.append(f"{path.relative_to(home)}: {', '.join(hits)}")
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    home = Path.home()
    repo = args.repo.resolve()
    shared = home / ".agents" / "skills"
    result = {
        "shared": {"skills": skill_inventory(shared)},
        "repository": {
            "skills": skill_inventory(repo / ".agents" / "skills"),
        },
        "agents": {
            "claude": claude_view(shared, home / ".claude" / "skills"),
            "cursor_only": sorted(p.name for p in (home / ".cursor" / "skills").glob("*/")),
            "codex_only": sorted(p.name for p in (home / ".codex" / "skills").glob("*/")),
        },
        "broken_references": broken_references(shared),
        "platform_terms": platform_terms(shared),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
