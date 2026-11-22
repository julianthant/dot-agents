#!/usr/bin/env python3
"""Copy pinned upstream skill files, unchanged, into ~/.agents/skills/<skill>/.

upstream.json pins each repository to a commit and lists, per skill, the upstream folders or files
it carries. An upstream SKILL.md is written as <folder>.md, so no agent registers it as a skill of
its own. Default: rewrite each listed skill's references/ and licence files plus its
metadata.source_version line. --check compares disk with the pinned upstream. --bump moves every
pin to its repository's current HEAD, then syncs.
"""

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "upstream.json"
SKILLS = Path.home() / ".agents" / "skills"
DEFAULT_EXCLUDE = ["agents/openai.yaml", "agents/openai.yml", ".DS_Store", "*/.DS_Store"]
VERSION_LINE = re.compile(r"^(\s+source_version: ).*$", re.M)


def gh(path: str):
    out = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def tree_files(repo: str, sha: str, folder: str) -> list[str]:
    parent, _, name = folder.rpartition("/")
    listing = gh(f"repos/{repo}/contents/{parent}?ref={sha}")
    tree_sha = next(e["sha"] for e in listing if e["name"] == name and e["type"] == "dir")
    tree = gh(f"repos/{repo}/git/trees/{tree_sha}?recursive=1")
    if tree.get("truncated"):
        raise SystemExit(f"{repo}:{folder}: tree listing truncated")
    return [e["path"] for e in tree["tree"] if e["type"] == "blob"]


def excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def dest_for(entry: dict, path: str) -> str:
    if "as" in entry:
        return entry["as"]
    to = entry["to"]
    if path == "SKILL.md":
        path = f"{Path(to).name}.md"
    dest = f"{to}/{path}"
    if Path(dest).name == "SKILL.md":
        raise SystemExit(f"{dest}: a nested SKILL.md would register as its own skill")
    return dest


def plan(manifest: dict, skill: str, list_tree=tree_files) -> list[tuple[str, str]]:
    items = []
    for entry in manifest["skills"][skill]:
        repo, folder = entry["repo"], entry.get("from", "")
        sha = manifest["repos"][repo]
        paths = entry.get("files") or [
            p for p in list_tree(repo, sha, folder)
            if not excluded(p, DEFAULT_EXCLUDE + entry.get("exclude", []))
        ]
        for p in paths:
            src = f"{folder}/{p}" if folder else p
            items.append((dest_for(entry, p), f"https://raw.githubusercontent.com/{repo}/{sha}/{src}"))
    return items


def versions(manifest: dict, skill: str) -> str:
    shas = [manifest["repos"][e["repo"]] for e in manifest["skills"][skill]]
    return ", ".join(dict.fromkeys(shas))


def set_version(text: str, value: str) -> str:
    new, count = VERSION_LINE.subn(lambda m: f'{m.group(1)}"{value}"', text, count=1)
    if not count:
        raise SystemExit("SKILL.md has no metadata.source_version line")
    return new


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def fetch_all(items: list[tuple[str, str]]) -> list[bytes]:
    with ThreadPoolExecutor(16) as pool:
        return list(pool.map(fetch, [url for _, url in items]))


def sync(manifest: dict, skill: str) -> None:
    root = SKILLS / skill
    items = plan(manifest, skill)
    blobs = fetch_all(items)  # fetch everything before touching disk
    shutil.rmtree(root / "references", ignore_errors=True)
    for (dest, _), data in zip(items, blobs):
        target = root / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    skill_md = root / "SKILL.md"
    skill_md.write_text(set_version(skill_md.read_text(), versions(manifest, skill)))
    print(f"synced   {skill} ({len(items)} files)")


def check(manifest: dict, skill: str) -> list[str]:
    root = SKILLS / skill
    items = plan(manifest, skill)
    problems = []
    for (dest, _), data in zip(items, fetch_all(items)):
        path = root / dest
        if not path.is_file():
            problems.append(f"{skill}: missing {dest}")
        elif path.read_bytes() != data:
            problems.append(f"{skill}: differs {dest}")
    expected = {dest for dest, _ in items}
    for path in sorted((root / "references").rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file() and path.name != ".DS_Store" and rel not in expected:
            problems.append(f"{skill}: extra {rel}")
    want = versions(manifest, skill)
    if f'source_version: "{want}"' not in (root / "SKILL.md").read_text():
        problems.append(f"{skill}: source_version is not {want}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("skills", nargs="*", help="default: every skill in upstream.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bump", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    names = args.skills or list(manifest["skills"])
    if args.check:
        problems = [p for name in names for p in check(manifest, name)]
        print("\n".join(problems) or "in sync with upstream")
        raise SystemExit(1 if problems else 0)
    if args.bump:
        for repo in manifest["repos"]:
            manifest["repos"][repo] = gh(f"repos/{repo}/commits/HEAD")["sha"]
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    for name in names:
        sync(manifest, name)


if __name__ == "__main__":
    main()
