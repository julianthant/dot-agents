import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).parent / "pre-push"


def git(cwd, *a, **kw):
    return subprocess.run(
        ["git", *a],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=kw.get("check", True),
    )


class PrePushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        git(t, "init", "-q", "--bare", "remote.git")
        git(t, "init", "-q", "-b", "staging", "w")
        self.w = t / "w"
        for k, v in (
            ("user.name", "t"),
            ("user.email", "t@t"),
            ("commit.gpgsign", "false"),
        ):
            git(self.w, "config", k, v)
        git(self.w, "remote", "add", "origin", str(t / "remote.git"))
        git(self.w, "commit", "-q", "--allow-empty", "-m", "base")
        git(self.w, "push", "-q", "origin", "staging")

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, ref, max_ahead="200"):
        sha = git(self.w, "rev-parse", ref).stdout.strip()
        r = subprocess.run(
            ["sh", str(HOOK), "origin", "url"],
            input=f"{ref} {sha} refs/heads/x {'0' * 40}\n",
            cwd=self.w,
            capture_output=True,
            check=False,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                "PUSH_MAX_AHEAD": max_ahead,
            },
        )
        return r

    def test_normal_passes(self):
        git(self.w, "checkout", "-q", "-b", "f")
        git(self.w, "commit", "-q", "--allow-empty", "-m", "c")
        r = self.hook("f")
        self.assertEqual(r.returncode, 0)
        self.assertIn("1 commits ahead", r.stdout)

    def test_unrelated_refused(self):
        git(self.w, "checkout", "-q", "--orphan", "o")
        git(self.w, "commit", "-q", "--allow-empty", "-m", "o")
        self.assertEqual(self.hook("o").returncode, 1)

    def test_over_limit_refused(self):
        git(self.w, "checkout", "-q", "-b", "f")
        for _ in range(3):
            git(self.w, "commit", "-q", "--allow-empty", "-m", "c")
        r = self.hook("f", max_ahead="2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("3 commits ahead", r.stdout)

    def test_delete_passes(self):
        r = subprocess.run(
            ["sh", str(HOOK), "origin", "url"],
            input=f"(delete) {'0' * 40} refs/heads/x abc\n",
            cwd=self.w,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
