#!/usr/bin/env python3
"""Report upstream changes since each copied source's pinned commit.

Reads sources.json in this skill. Each entry names the skill that carries the adapted copy (`local`),
the upstream repo and path, and the pinned `commit`. Each repo is downloaded with `gh` into a
temporary folder; nothing from it is executed. The report compares the path at the pin with HEAD, or
with the entry's `ref` when it tracks a fixed revision.
"""

import argparse
import filecmp
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "sources.json"
IGNORE = {".DS_Store", "__pycache__", "node_modules"}


def fetch(repo: str, cache: dict[tuple[str, str], tuple[Path, str]], tmp: Path,
          ref: str = "HEAD") -> tuple[Path, str]:
    key = (repo, ref)
    if key not in cache:
        sha = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        archive = tmp / (repo.replace("/", "__") + ".tgz")
        with archive.open("wb") as out:
            subprocess.run(["gh", "api", f"repos/{repo}/tarball/{sha}"], check=True, stdout=out)
        dest = tmp / repo.replace("/", "__")
        with tarfile.open(archive) as tar:
            top = tar.getnames()[0].split("/")[0]
            tar.extractall(dest, filter="data")
        cache[key] = (dest / top, sha)
    return cache[key]


def walk(base: Path) -> set[str]:
    if base.is_file():
        return {""}
    return {
        str(p.relative_to(base))
        for p in base.rglob("*")
        if p.is_file() and not IGNORE & set(p.parts)
    }


def compare(upstream: Path, pinned: Path) -> dict[str, list[str]]:
    up, old = walk(upstream), walk(pinned) if pinned.exists() else set()
    changed = sorted(
        f for f in up & old
        if not filecmp.cmp(upstream / f if f else upstream, pinned / f if f else pinned, shallow=False)
    )
    return {"added": sorted(up - old), "removed": sorted(old - up), "changed": changed}


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    entries = json.loads(MANIFEST.read_text())
    cache: dict[tuple[str, str], tuple[Path, str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for entry in entries:
            name, repo = entry["name"], entry.get("repo")
            if not repo:
                print(f"{name:36} no upstream recorded")
                continue
            head_root, sha = fetch(repo, cache, Path(tmp), entry.get("ref", "HEAD"))
            pin_root, _ = fetch(repo, cache, Path(tmp), entry["commit"])
            upstream, pinned = head_root / entry["path"], pin_root / entry["path"]
            if not upstream.exists():
                print(f"{name:36} upstream path missing at {sha}: {entry['path']}")
                continue
            diff = compare(upstream, pinned)
            total = sum(len(v) for v in diff.values())
            status = "current" if not total else f"{total} differ (+{len(diff['added'])} -{len(diff['removed'])} ~{len(diff['changed'])})"
            print(f"{name:36} {entry['local']:14} {sha}  {status}")
            for kind, files in diff.items():
                for f in files[:8]:
                    print(f"{'':38}{kind}: {f or entry['path']}")


if __name__ == "__main__":
    main()
