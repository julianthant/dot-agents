import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import expose


def skill(root, name, body):
    d = Path(root) / name
    d.mkdir()
    (d / "SKILL.md").write_text(body)


class ExposeTest(unittest.TestCase):
    def run_main(self, root):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = expose.main(["--root", str(root)])
        return code, out.getvalue(), err.getvalue()

    def test_parse_forms(self):
        fm = expose.parse(
            '---\nname: "a"\ndescription: >\n  one\n  two\nx: |\n  l1\n  l2\n'
            'metadata:\n  exposed: "true"\n---\nbody\n'
        )
        self.assertEqual(fm["name"], "a")
        self.assertEqual(fm["description"], "one two")
        self.assertEqual(fm["x"], "l1\nl2")
        self.assertEqual(fm["metadata"], {"exposed": "true"})

    def test_lists_only_exposed_sorted(self):
        with tempfile.TemporaryDirectory() as t:
            skill(
                t,
                "z",
                "---\nname: zed\ndescription: Z\nmetadata:\n  exposed: 'true'\n---\n",
            )
            skill(
                t,
                "a",
                '---\nname: alpha\ndescription: A\nmetadata:\n  exposed: "true"\n---\n',
            )
            skill(t, "h", "---\nname: hidden\ndescription: H\n---\n")
            code, out, _ = self.run_main(t)
        self.assertEqual(code, 0)
        self.assertLess(out.index("alpha"), out.index("zed"))
        self.assertNotIn("hidden", out)
        self.assertIn("SKILL.md", out)

    def test_broken_frontmatter(self):
        with tempfile.TemporaryDirectory() as t:
            skill(t, "b", "no frontmatter")
            code, _, err = self.run_main(t)
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md", err)


if __name__ == "__main__":
    unittest.main()
