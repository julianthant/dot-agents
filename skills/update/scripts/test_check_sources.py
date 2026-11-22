import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from check_sources import MANIFEST, compare, fetch


class CheckSourcesTest(unittest.TestCase):
    def test_fetch_separates_head_and_pinned_revision(self) -> None:
        def download(args, **kwargs):
            if "/commits/" in args[2]:
                return SimpleNamespace(stdout="pinned-sha" if args[2].endswith("/old") else "head-sha")
            sha = args[2].rsplit("/", 1)[-1]
            with tarfile.open(fileobj=kwargs["stdout"], mode="w:gz") as archive:
                info = tarfile.TarInfo(f"repo-{sha}/SKILL.md")
                content = sha.encode()
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            return SimpleNamespace()

        with tempfile.TemporaryDirectory() as tmp, patch("check_sources.subprocess.run", side_effect=download) as run:
            cache = {}
            head, head_sha = fetch("owner/repo", cache, Path(tmp))
            pinned, pinned_sha = fetch("owner/repo", cache, Path(tmp), "old")
            self.assertEqual((head_sha, pinned_sha), ("head-sha", "pinned-sha"))
            self.assertEqual((head / "SKILL.md").read_text(), "head-sha")
            self.assertEqual((pinned / "SKILL.md").read_text(), "pinned-sha")
            self.assertEqual(fetch("owner/repo", cache, Path(tmp)), (head, head_sha))
            self.assertEqual(run.call_count, 4)

    def test_compare_lists_upstream_changes_since_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            up, old = Path(tmp) / "up", Path(tmp) / "old"
            (up / "ref").mkdir(parents=True)
            (old / "ref").mkdir(parents=True)
            (up / "SKILL.md").write_text("new")
            (old / "SKILL.md").write_text("old")
            (up / "same.md").write_text("s")
            (old / "same.md").write_text("s")
            (up / "ref" / "added.md").write_text("x")
            (old / "ref" / "gone.md").write_text("x")

            diff = compare(up, old)
            self.assertEqual(diff, {"added": ["ref/added.md"], "removed": ["ref/gone.md"], "changed": ["SKILL.md"]})
            self.assertEqual(compare(up, up), {"added": [], "removed": [], "changed": []})

    def test_single_file_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            up, old = Path(tmp) / "a.json", Path(tmp) / "old" / "a.json"
            up.write_text("1")
            self.assertEqual(compare(up, old)["added"], [""])
            old.parent.mkdir()
            old.write_text("1")
            self.assertEqual(compare(up, old), {"added": [], "removed": [], "changed": []})

    def test_manifest_entries_name_a_skill_and_a_pin(self) -> None:
        for entry in json.loads(MANIFEST.read_text()):
            self.assertTrue(entry["local"] and entry["commit"] and entry["path"], entry["name"])


if __name__ == "__main__":
    unittest.main()
