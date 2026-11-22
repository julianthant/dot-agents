import tempfile
import unittest
from pathlib import Path

from link_skills import apply, plan


def skill(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")


class LinkSkillsTest(unittest.TestCase):
    def test_links_replaces_unlinks_and_keeps_foreign_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, target, backup = base / "agents", base / "claude", base / "backup"
            skill(source, "new")
            skill(source, "shadowed")
            skill(target, "shadowed")
            (target / "synced").mkdir()
            gone = source / "retired"
            (target / "retired").symlink_to(gone)

            self.assertIn(("conflict", "shadowed"), plan(source, target, replace=False))
            actions = plan(source, target, replace=True)
            self.assertEqual(
                sorted(actions), [("link", "new"), ("replace", "shadowed"), ("unlink", "retired")]
            )
            apply(source, target, actions, backup)

            self.assertEqual((target / "new").resolve(), (source / "new").resolve())
            self.assertEqual((target / "shadowed").resolve(), (source / "shadowed").resolve())
            self.assertTrue((backup / "shadowed" / "SKILL.md").is_file())
            self.assertFalse((target / "retired").is_symlink())
            self.assertTrue((target / "synced").is_dir())
            self.assertEqual(plan(source, target, replace=True), [])


if __name__ == "__main__":
    unittest.main()
