import json
import unittest
from pathlib import Path

from sync_upstream import MANIFEST, plan, set_version, versions

MANIFEST_FIXTURE = {
    "repos": {"o/a": "aaa", "o/b": "bbb"},
    "skills": {
        "ui-x": [
            {"repo": "o/a", "from": "skills/one", "to": "references/one", "exclude": ["evals/*"]},
            {"repo": "o/b", "from": "skills/two", "to": "references/two", "files": ["SKILL.md"]},
            {"repo": "o/a", "files": ["LICENSE"], "as": "LICENSE-a"},
        ]
    },
}


def fake_tree(repo: str, sha: str, folder: str) -> list[str]:
    return ["SKILL.md", "rules/x.md", "evals/evals.json", "agents/openai.yaml"]


class SyncUpstreamTest(unittest.TestCase):
    def test_plan_renames_skill_md_applies_excludes_and_pins_urls(self) -> None:
        items = dict(plan(MANIFEST_FIXTURE, "ui-x", list_tree=fake_tree))
        self.assertEqual(
            items,
            {
                "references/one/one.md": "https://raw.githubusercontent.com/o/a/aaa/skills/one/SKILL.md",
                "references/one/rules/x.md": "https://raw.githubusercontent.com/o/a/aaa/skills/one/rules/x.md",
                "references/two/two.md": "https://raw.githubusercontent.com/o/b/bbb/skills/two/SKILL.md",
                "LICENSE-a": "https://raw.githubusercontent.com/o/a/aaa/LICENSE",
            },
        )

    def test_nested_skill_md_is_refused(self) -> None:
        manifest = {"repos": {"o/a": "aaa"}, "skills": {"ui-x": [
            {"repo": "o/a", "from": "s", "to": "references/s", "files": ["inner/SKILL.md"]}
        ]}}
        with self.assertRaises(SystemExit):
            plan(manifest, "ui-x")

    def test_versions_and_frontmatter_line(self) -> None:
        self.assertEqual(versions(MANIFEST_FIXTURE, "ui-x"), "aaa, bbb")
        text = '---\nname: ui-x\nmetadata:\n  source_version: "old"\n---\nbody\n'
        self.assertIn('  source_version: "aaa, bbb"\n', set_version(text, "aaa, bbb"))
        with self.assertRaises(SystemExit):
            set_version("---\nname: ui-x\n---\n", "aaa")

    def test_manifest_pins_every_repo_it_uses(self) -> None:
        manifest = json.loads(MANIFEST.read_text())
        used = {e["repo"] for entries in manifest["skills"].values() for e in entries}
        self.assertEqual(used, set(manifest["repos"]))
        for skill in manifest["skills"]:
            self.assertTrue(skill.startswith("ui-"), skill)


if __name__ == "__main__":
    unittest.main()
