"""Exercise refusal and discovery behavior with temporary repositories."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from audit import audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_detects_pair_drift_even_when_sizes_match(self):
        self.write("AGENTS.md", "one")
        self.write("CLAUDE.md", "two")
        result = audit(self.root, 100)
        self.assertFalse(result["pairs"][0]["equal"])
        self.assertTrue(result["issues"])

    def test_counts_utf8_and_ancestor_joins(self):
        self.write("AGENTS.md", "é" * 10)
        self.write("CLAUDE.md", "é" * 10)
        self.write("src/AGENTS.md", "nested")
        result = audit(self.root, 27)
        nested = next(c for c in result["project_chains"] if c["directory"] == "src")
        self.assertEqual(nested["bytes"], 28)
        self.assertTrue(result["issues"])

    def test_override_replaces_root_and_missing_twin_is_reported(self):
        self.write("AGENTS.md", "long root" * 20)
        self.write("AGENTS.override.md", "tiny")
        result = audit(self.root, 4)
        self.assertEqual(result["project_chains"][0]["files"], ["AGENTS.override.md"])
        self.assertEqual(result["single_name_directories"], ["."])
        self.assertFalse(result["issues"])

    def test_git_ignored_docs_do_not_enter_inventory(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/AGENTS.md", "x" * 100)
        self.write("guide.md", "useful")
        result = audit(self.root, 10)
        self.assertEqual([d["path"] for d in result["documents"]], ["guide.md"])

    def test_subtree_cannot_understate_the_instruction_chain(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("AGENTS.md", "large parent" * 100)
        (self.root / "src").mkdir()
        with self.assertRaisesRegex(ValueError, "repository root"):
            audit(self.root / "src", 100)

    def test_check_cli_fails_for_drift(self):
        self.write("AGENTS.md", "one")
        self.write("CLAUDE.md", "two")
        import sys
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("audit.py")),
                                 str(self.root), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("instruction pair differs", result.stdout)

    def test_external_symlink_is_refused_without_reading_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.md"
            target.write_text("private outside content")
            (self.root / "AGENTS.md").symlink_to(target)
            result = audit(self.root, 100)
            self.assertTrue(result["issues"])
            self.assertFalse(result["documents"])

    def test_audit_leaves_bytes_unchanged(self):
        self.write("AGENTS.md", "same")
        self.write("CLAUDE.md", "same")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertFalse(audit(self.root, 100)["issues"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})


if __name__ == "__main__":
    unittest.main()
