import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import roadmap

HERE = Path(__file__).parent
SCRIPT = HERE / "roadmap.py"
NOW = "2026-11-11T22:00:00.000Z"
TASK = (
    "INSERT INTO tasks (task_id, title, agent_id, assigned_agent_id, notes, status,"
    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
GH = [
    {
        "number": 7,
        "title": "t",
        "headRefName": "feat/trial-feature",
        "baseRefName": "staging",
        "state": "OPEN",
        "isDraft": False,
        "mergedAt": None,
        "url": "https://example.test/7",
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "FAILURE"},
            {"status": "IN_PROGRESS", "conclusion": ""},
        ],
    }
]


def run(env, *args, repo):
    full = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        env=full,
    )


class RoadmapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / ".concord").mkdir()
        self.repo = t / "repo"
        (self.repo / ".work" / "specs").mkdir(parents=True)
        (self.repo / ".work" / "specs" / "spec-only.md").write_text(
            "# Spec only <b>\n\nFirst paragraph here.\n\nSecond.\n"
        )
        (self.repo / ".work" / "specs" / "evil.md").write_text(
            "# Evil\n\n<script>alert(2)</script> [x](javascript:alert(3)) [ok](https://a.test/p?q=1&r=2)\n"
        )
        (self.repo / ".work" / "specs" / "trial-feature.md").write_text(
            "# Trial\n\nTrial summary.\n"
        )
        con = sqlite3.connect(t / ".concord" / "concord.db")
        con.executescript((HERE / "testdata" / "trial-store.sql").read_text())
        rows = [
            (
                "blk",
                "trial-feature / blk",
                None,
                None,
                "Brief: Blocked one.\nCoder: claude\nReviewer: codex\nBlocked by: queued-lane",
                "proposed",
            ),
            ("need", "trial-feature / need", "a:1", None, "Brief: Needs op.", "active"),
            ("assigned", "other / assigned", None, "b:2", "Do it. More.", "assigned"),
            ("nofeat", "plain title", None, None, None, "proposed"),
        ]
        for r in rows:
            con.execute(TASK, (*r, NOW, NOW))
        con.execute(
            "INSERT INTO task_updates (task_id, kind, content, agent, created_at)"
            " VALUES ('need', 'blocker', 'Waiting on: operator <script>alert(1)</script>', 'a:1', ?)",
            (NOW,),
        )
        con.commit()
        con.close()
        self.env = {
            "CONCORD_REPO_ROOT": str(t),
            "PATH": self.fakebin(GH) + os.pathsep + os.environ["PATH"],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fakebin(self, data):
        d = Path(self.tmp.name) / "bin"
        d.mkdir(exist_ok=True)
        gh = d / "gh"
        gh.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(data)}\nEOF\n")
        gh.chmod(0o755)
        return str(d)

    def test_text_tree(self):
        out = run(self.env, "--text", repo=self.repo).stdout
        print(out)
        self.assertEqual(
            out.splitlines()[0], "8 of 10 tickets left · 1 of 6 features done"
        )
        heads = [
            l
            for l in out.splitlines()
            if re.match(r"[A-Z(]", l) and "·" in l and "ROADMAP" not in l
        ]
        names = [h.split("  ")[0] for h in heads]
        self.assertEqual(
            names,
            ["TRIAL-FEATURE", "OTHER", "NO FEATURE", "PROBE", "EVIL", "SPEC-ONLY"],
        )
        self.assertIn("SPEC-ONLY  0/0 done · not ticketed yet", out)
        self.assertIn("TRIAL-FEATURE  1/7 done · needs you", out)
        self.assertIn("PR #7 open, checks 1 passing, 1 failing, 1 pending, 0 skipped", out)
        lines = out.splitlines()
        trial = lines[
            lines.index(next(l for l in lines if l.startswith("TRIAL-FEATURE"))) :
        ]
        order = [l.split()[2] for l in trial[1:12] if "─" in l]
        self.assertEqual(
            order,
            [
                "need",
                "claude-lane",
                "trial-lane",
                "wsl-lane",
                "blk",
                "queued-lane",
                "claude-lane-review",
            ],
        )
        self.assertIn("▶ trial-lane · script:mac-trial-lane", out)
        self.assertIn("▸ trial-feature · need", out)
        self.assertIn("blocked by queued-lane", out)
        self.assertIn("assigned to b:2, not accepted", out)
        self.assertTrue(all(len(l) <= 100 for l in out.splitlines()))

    def page_data(self, page):
        raw = page.split('<script id="roadmap-data" type="application/json">')[1]
        return json.loads(raw.split("</script>")[0])

    def test_render_pr_check_counts(self):
        skipped = {"status": "COMPLETED", "conclusion": "SKIPPED"}
        rollup = GH[0]["statusCheckRollup"]
        tmp = Path(self.tmp.name)
        path = tmp / "roadmap.html"
        cases = (
            (
                "mixed",
                [rollup[0]] * 9 + [rollup[1]] * 2 + [rollup[2]] + [skipped] * 7,
                "9 passing, 2 failing, 1 pending, 7 skipped",
            ),
            ("all skipped", [skipped] * 2, "0 passing, 0 failing, 0 pending, 2 skipped"),
        )
        for name, checks, counts in cases:
            with self.subTest(rollup=name):
                (tmp / "roadmap-prs.json").unlink(missing_ok=True)
                self.fakebin([{**GH[0], "statusCheckRollup": checks}])
                expected = f"PR #7 open, checks {counts}"

                result = run(self.env, "--html", str(path), repo=self.repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                features = self.page_data(path.read_text())["matrix"]["features"]
                trial = next(f for f in features if f["name"] == "trial-feature")
                self.assertEqual(trial["pr"], expected)

                result = run(self.env, "--text", repo=self.repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"  {expected}", result.stdout.splitlines())

    def dashboard(self):
        with mock.patch.dict(os.environ, self.env):
            now = roadmap.datetime(2026, 11, 11, 22, 5, tzinfo=roadmap.UTC)
            with mock.patch.object(roadmap, "read_accounts", return_value={}):
                return roadmap.dashboard_data(*roadmap.build(self.repo, now), now)

    def test_page_data_escaped_and_atomic(self):
        con = sqlite3.connect(Path(self.tmp.name) / ".concord" / "concord.db")
        row = (
            "evil-lane",
            "trial-feature / x</script><b>y",
            None,
            None,
            None,
            "proposed",
        )
        con.execute(TASK, (*row, NOW, NOW))
        con.commit()
        con.close()
        path = Path(self.tmp.name) / "r.html"
        r = run(self.env, "--html", str(path), repo=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = path.read_text()
        self.assertNotIn("</script><b>", page)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("javascript:alert", page)
        self.assertIn('http-equiv="refresh" content="15"', page)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        data = self.page_data(page)
        self.assertEqual(data["project"], Path(self.tmp.name).name)
        titles = [t["title"] for t in data["nextUp"]]
        self.assertIn("trial-feature / x</script><b>y", titles)

    def test_path_ownership_changes_readiness_in_both_views(self):
        tmp = Path(self.tmp.name)
        db = tmp / ".concord" / "concord.db"
        with closing(sqlite3.connect(db)) as con, con:
            con.execute(
                "UPDATE tasks SET notes = ? WHERE task_id = 'wsl-lane'",
                (
                    "Owned paths: src/shared/ (story and check files, src/shared-other/, "
                    "including nested notes (examples, docs/)), `src/exact.py` (read).",
                ),
            )
            for tid, owned, gate in (
                ("nested", "src/shared/nested.py (new file)", "Blocked by: probe-overlap"),
                ("exact", "`src/exact.py` (update)", "Blocked by: probe-overlap"),
                ("nearby", "src/shared-other/nested.py", "Blocked by: probe-overlap"),
                ("explicit", "src/shared/explicit.py", "Blocked by: queued-lane"),
                ("approval", "src/shared/approval.py", "Waiting on: operator (approval)"),
            ):
                row = (
                    tid, f"trial-feature / {tid}", None, None,
                    f"Owned paths: {owned}\n{gate}", "proposed",
                )
                con.execute(TASK, (*row, NOW, NOW))

        sup = tmp / "supervisor"
        sup.mkdir()
        (sup / "state.json").write_text(
            json.dumps({"workers": {
                "wsl-lane": {"project": "", "host": "Juzzark-WSL", "agent": "codex"},
            }})
        )
        path = tmp / "roadmap.html"
        for held in (True, False):
            with self.subTest(held=held):
                if not held:
                    with closing(sqlite3.connect(db)) as con, con:
                        con.execute(
                            "UPDATE tasks SET status = 'complete' WHERE task_id = 'wsl-lane'"
                        )
                result = run(self.env, "--text", "--html", str(path), repo=self.repo)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = self.page_data(path.read_text())
                ready = {"queued-lane", "assigned", "nofeat", "nearby"}
                if not held:
                    ready |= {"nested", "exact"}
                self.assertEqual({t["id"] for t in data["nextUp"]}, ready)
                self.assertEqual(data["tickets"]["remaining"], len(ready))
                holds = {t["id"]: t["holds"] for t in data["blocked"]["top"]}
                self.assertEqual(holds, {"queued-lane": 2, **({"wsl-lane": 2} if held else {})})
                squares = {
                    t["id"]: t for f in data["matrix"]["features"] for t in f["tickets"]
                }
                for tid in ("nested", "exact"):
                    self.assertEqual(squares[tid]["state"], "blocked" if held else "remaining")
                    blockers = [["probe-overlap", "done"]]
                    if held:
                        blockers.append(["wsl-lane", "in progress"])
                    self.assertEqual(squares[tid]["detail"]["blockers"], blockers)
                    detail = "blocked by wsl-lane" if held else "unowned"
                    self.assertIn(f"{tid} · {detail}", result.stdout)
                self.assertIn("nearby · unowned", result.stdout)
                self.assertIn("explicit · blocked by queued-lane", result.stdout)
                self.assertEqual(squares["approval"]["state"], "needs you")
                self.assertIn("approval · operator approval", result.stdout)

    def test_dashboard_sections(self):
        d = self.dashboard()
        self.assertEqual(d["groups"], {"staging": 0, "merging": 1, "remaining": 2})
        feats = {f["name"]: f for f in d["matrix"]["features"]}
        trial = feats["trial-feature"]
        self.assertEqual((trial["done"], trial["total"]), (1, 7))
        self.assertTrue(trial["pr"].startswith("PR #7 open"), trial["pr"])
        self.assertEqual(sum(t["state"] == "in progress" for t in trial["tickets"]), 3)
        squares = [t for f in feats.values() for t in f["tickets"]]
        self.assertEqual(sum(t["state"] != "done" for t in squares), 8)
        self.assertEqual(len({t["id"] for t in squares}), 10)
        self.assertTrue(all(t["wave"] >= 1 for t in squares))
        idle = {n for n, f in feats.items() if f["state"] == "not ticketed"}
        self.assertEqual(idle, {"spec-only", "evil"})
        self.assertEqual(d["blocked"]["total"], 1)
        self.assertEqual((d["tickets"]["blocked"], d["tickets"]["needs you"]), (1, 1))
        top = {"id": "queued-lane", "title": "trial-feature / queued-lane"}
        self.assertEqual(
            d["blocked"]["top"], [top | {"state": "remaining", "holds": 1}]
        )
        self.assertEqual(len(d["nextUp"]), 3)
        self.assertEqual(d["agents"], {"worktrees": [], "counts": {}})
        self.assertIsNone(d["tickAt"])

    def test_merged_group_follows_repo_base(self):
        con = sqlite3.connect(Path(self.tmp.name) / ".concord" / "concord.db")
        con.execute(
            TASK, ("shipped", "shipped / lane", None, None, None, "complete", NOW, NOW)
        )
        con.commit()
        con.close()
        config = Path(self.tmp.name) / "accounts.toml"
        self.env["DEV_AUTO_ACCOUNTS"] = str(config)
        git = Path(self.tmp.name) / "bin" / "git"
        orca = git.with_name("orca")
        orca.write_text('#!/bin/sh\necho \'{"ok":true,"result":{}}\'\n')
        orca.chmod(0o755)
        development = '[repos."o/r"]\nbase = "development"\n'
        cases = (
            ("https", development, "https://github.com/o/r.git", "development"),
            ("ssh", development, "git@github.com:o/r.git", "development"),
            ("no-entry", '[repos."other/repo"]\nbase = "development"\n', "git@github.com:o/r.git", "staging"),
            ("no-base", '[repos."o/r"]\n', "git@github.com:o/r.git", "staging"),
            ("no-repos", "", "git@github.com:o/r.git", "staging"),
        )
        for name, settings, url, base in cases:
            with self.subTest(name=name):
                self.fakebin([
                    {**GH[0], "baseRefName": base},
                    {**GH[0], "number": 8, "headRefName": "feature/shipped",
                     "baseRefName": base, "state": "MERGED", "mergedAt": NOW},
                ])
                (Path(self.tmp.name) / "roadmap-prs.json").unlink(missing_ok=True)
                config.write_text(settings)
                git.write_text(
                    '#!/bin/sh\n[ "$*" = "remote get-url origin" ] || exit 2\n'
                    "cat <<'EOF'\n" + url + "\nEOF\n"
                )
                git.chmod(0o755)
                page = Path(self.tmp.name) / f"{name}.html"
                out = run(self.env, "--text", "--html", str(page), repo=self.repo)
                self.assertEqual(out.returncode, 0, out.stderr)
                data = self.page_data(page.read_text())
                self.assertEqual(data["groups"], {base: 1, "merging": 1, "remaining": 2})
                self.assertEqual(data["base"], base)
                self.assertIn(f"1 in {base} · 1 merging · 2 remaining", out.stdout)

    def test_delivery_uses_the_declared_branch_and_target_base(self):
        con = sqlite3.connect(Path(self.tmp.name) / ".concord" / "concord.db")
        with con:
            con.executemany(TASK, [
                ("limits", "storage-limits / lane", None, None,
                 "Feature branch: `feature/storage-limits`", "complete", NOW, NOW),
                ("quota", "quota / lane", None, None,
                 "Feature branch: `feature/quota`", "complete", NOW, NOW),
                ("patch", "patch / lane", None, None,
                 "Branch: `fix/custom` · Feature branch: none", "complete", NOW, NOW),
            ])
        con.close()
        self.fakebin([
            {**GH[0], "headRefName": "feature/storage-limits-followup", "baseRefName": "staging"},
            {**GH[0], "number": 8, "headRefName": "feature/quota", "baseRefName": "preview",
             "state": "MERGED", "mergedAt": NOW},
            {**GH[0], "number": 9, "headRefName": "fix/custom", "baseRefName": "staging",
             "state": "MERGED", "mergedAt": NOW},
        ])
        data = self.dashboard()
        features = {f["name"]: f for f in data["matrix"]["features"]}
        self.assertEqual(features["storage-limits"]["pr"], "no PR")
        self.assertIn("PR #9 merged", features["patch"]["pr"] or "")
        self.assertEqual(data["groups"], {"staging": 1, "merging": 0, "remaining": 3})

    def test_phase_from_ledger(self):
        rows = self.tickets({"a": "", "a-review": ""})
        by = {t["task_id"]: t for t in rows}
        a = by["a"]
        self.assertEqual(roadmap.phase(a, by), "coding")
        for wait, want in (
            ("Waiting on: ci run 12", "waiting on CI"),
            ("Waiting on: operator (pick one)", "waiting on you"),
            ("Waiting on: ticket:x-1 to merge", "waiting on x-1"),
            ("Waiting on: review", "waiting on review"),
        ):
            a["wait"] = wait
            self.assertEqual(roadmap.phase(a, by), want)
        by["a-review"]["worker"] = {"status": {"state": "working"}}
        self.assertEqual(roadmap.phase(a, by), "in review")
        a["state"] = "done"
        self.assertEqual(roadmap.phase(a, by), "done, closing")

    def test_phase_wait_on_a_ticket_with_no_id_is_ordinary(self):
        rows = self.tickets({"a": "", "a-review": ""})
        by = {t["task_id"]: t for t in rows}
        for wait in ("Waiting on: ticket:", "Waiting on: ticket:  "):
            by["a"]["wait"] = wait
            self.assertEqual(roadmap.phase(by["a"], by), "coding")

    def review_pair(self, *updates):
        """Coder a and reviewer a-review; updates are (ticket, agent, content), in id order."""
        rows = [
            {
                "task_id": k,
                "title": f"f / {k}",
                "status": "active",
                "agent": None,
                "agent_id": owner,
                "assigned_agent_id": None,
                "notes": "",
                "updated_at": NOW,
                "updates": [
                    {
                        "kind": "blocker" if content.startswith("Waiting on:") else "progress",
                        "content": content,
                        "agent": agent,
                        "created_at": f"2026-11-15T04:{i:02d}:00.000Z",
                        "superseded_by": None,
                    }
                    for i, (tid, agent, content) in enumerate(updates)
                    if tid == k
                ],
            }
            for k, owner in (("a", "claude:a"), ("a-review", "codex:a-review"))
        ]
        rows = roadmap.classify(rows, roadmap.datetime.now(roadmap.UTC))
        by = {t["task_id"]: t for t in rows}
        by["a-review"]["worker"] = {"status": {"state": "waiting"}}
        return by

    def test_phase_in_review_once_reviewer_woken(self):
        ready = ("a", "claude:a", "REVIEW READY 1a2b3c4")
        wait = ("a", "claude:a", "Waiting on: review of 1a2b3c4")
        old_wait = ("a-review", "codex:a-review", "Waiting on: ticket:a")
        wake = (
            "a-review",
            "script:dev-auto",
            "dev-auto: REVIEW READY was posted on ticket a. Review it as your ticket says.",
        )
        by = self.review_pair(old_wait, ready, wait)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")
        by = self.review_pair(old_wait, ready, wait, wake)
        self.assertEqual(roadmap.phase(by["a"], by), "in review")
        by = self.review_pair(old_wait, wake, ready, wait)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")
        by["a-review"]["worker"] = {"status": {"state": "working"}}
        self.assertEqual(roadmap.phase(by["a"], by), "in review")

    def test_phase_wake_covers_a_repeated_request_for_its_sha(self):
        ready = ("a", "claude:a", "REVIEW READY a71ff2252")
        wake = (
            "a-review",
            "script:dev-auto",
            "dev-auto: REVIEW READY was posted on ticket a. Review it as your ticket says.",
        )
        again = ("a", "claude:a", "Waiting on: review of a71ff2252; REVIEW READY a71ff2252")
        by = self.review_pair(ready, wake, again)
        self.assertEqual(roadmap.phase(by["a"], by), "in review")
        other = ("a", "claude:a", "Waiting on: review of 5e6f7a8; REVIEW READY 5e6f7a8")
        by = self.review_pair(ready, wake, other)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")
        both = ("a", "claude:a", "REVIEW READY a71ff2252\nREVIEW READY 5e6f7a8")
        by = self.review_pair(ready, wake, both)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")

    def test_phase_fixing_after_fix_verdict(self):
        ready = ("a", "claude:a", "REVIEW READY 1a2b3c4")
        wait = ("a", "claude:a", "Waiting on: review of 1a2b3c4")
        fix = ("a", "codex:a-review", "REVIEW VERDICT: FIX 1a2b3c4 one finding")
        by = self.review_pair(ready, wait, fix)
        self.assertEqual(roadmap.phase(by["a"], by), "fixing")
        by["a-review"]["worker"] = {"status": {"state": "working"}}
        self.assertEqual(roadmap.phase(by["a"], by), "fixing")
        again = ("a", "claude:a", "REVIEW READY 5e6f7a8")
        by = self.review_pair(ready, wait, fix, again, wait)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")
        # A wait line naming two SHAs is no request, as dev-auto reads it.
        two = ("a", "claude:a", "Waiting on: review of 1a2b3c4; REVIEW READY 5e6f7a8")
        by = self.review_pair(ready, wait, fix, two)
        self.assertEqual(roadmap.phase(by["a"], by), "fixing")
        one = ("a", "claude:a", "Waiting on: review; REVIEW READY 5e6f7a8")
        by = self.review_pair(ready, wait, fix, one)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on review")
        ci = ("a", "claude:a", "Waiting on: ci run 12")
        by = self.review_pair(ready, wait, fix, ci)
        self.assertEqual(roadmap.phase(by["a"], by), "waiting on CI")

    def test_lost_record_is_a_paused_row_in_its_group(self):
        rows, now = self.fleet()
        for t in rows:
            t["worker"]["worktree"] = f"/w/f.{t['task_id'].removesuffix('-review')}/"
            t["worker"]["model"] = "m"
        a = roadmap.agents(rows, now)
        group = next(w for w in a["worktrees"] if w["worktree"] == "f.c")
        self.assertEqual((group["title"], group["host"]), ("f / c", "local"))
        self.assertEqual([x["key"] for x in group["agents"]], ["c", "c-review"])
        for x in group["agents"]:
            self.assertEqual(
                (x["role"], x["agent"], x["model"], x["account"]), ("", "", "", "")
            )
            self.assertEqual(
                (x["state"], x["detail"], x["since"], x["stale"]),
                ("paused", "", None, False),
            )
        self.assertNotIn("paused", a["counts"])

    def test_fix_rounds_counts_fix_verdicts(self):
        said = (
            "REVIEW VERDICT: FIX one",
            "x",
            "REVIEW VERDICT: PUSH",
            "REVIEW VERDICT: FIX 2",
        )
        self.assertEqual(
            roadmap.fix_rounds({"updates": [{"content": c} for c in said]}), 2
        )
        self.assertEqual(roadmap.fix_rounds({"updates": []}), 0)

    def test_agents_table_matches_running(self):
        rows, now = self.fleet()
        for t in rows:
            t["worker"]["worktree"] = f"/w/f.{t['task_id'].removesuffix('-review')}/"
            t["worker"]["model"] = "m"
        rows[0]["worker"]["status"] = {
            "state": "waiting",
            "detail": "operator",
            "since": 1500,
            "seen": 1590,
        }
        rows[1]["worker"]["status"] = {"state": "working", "since": 900, "seen": 1000}
        rows[2]["worker"]["status"] = "garbled"
        rows[0]["updates"] = [{"content": "REVIEW VERDICT: FIX x"}]
        a = roadmap.agents(rows, now, scan_at=1590)
        self.assertEqual(
            sum(len(w["agents"]) for w in a["worktrees"]),
            len(roadmap.running(rows, now)),
        )
        self.assertNotIn("lost", a)
        self.assertEqual(a["counts"], {"waiting": 1, "working": 1, "no status": 2})
        first = a["worktrees"][0]
        self.assertEqual(
            (first["title"], first["worktree"], first["host"]),
            ("f / a", "f.a", "local"),
        )
        self.assertEqual(first["fixRounds"], 1)
        self.assertEqual([x["role"] for x in first["agents"]], ["coder", "reviewer"])
        coder, reviewer = first["agents"]
        self.assertEqual(
            (coder["state"], coder["detail"], coder["since"]),
            ("waiting", "operator", 1500),
        )
        self.assertFalse(coder["stale"])
        self.assertTrue(reviewer["stale"])

    def test_render_separates_heartbeat_and_worker_freshness(self):
        con = sqlite3.connect(Path(self.tmp.name) / ".concord" / "concord.db")
        con.execute(
            TASK,
            (
                "trial-lane-review",
                "trial-feature / trial-lane-review",
                "b:2",
                None,
                "",
                "active",
                NOW,
                NOW,
            ),
        )
        con.commit()
        con.close()
        sup = Path(self.tmp.name) / "supervisor"
        sup.mkdir()
        scan = int(roadmap.datetime(2026, 11, 11, 22, 5, tzinfo=roadmap.UTC).timestamp())
        rows = [
            {
                "ticket": ticket,
                "agent": "codex",
                "host": "local",
                "worktree": "/work/trial-lane",
                "status": {"state": state, "seen": seen},
            }
            for ticket, state, seen in (
                ("trial-lane", "waiting", scan),
                ("trial-lane-review", "stuck", scan - 86),
            )
        ]
        (sup / "status.json").write_text(json.dumps({"at": scan, "rows": rows}))
        completed_scan = {"at": scan + 27, "seconds": 26.9}
        for tick, render_delay, late, stale in (
            (completed_scan, 96, False, [False, True]),
            (completed_scan, 208, True, [False, True]),
            ({"at": scan + 87, "seconds": 26.9}, 96, False, [False, True]),
            ({"at": scan - 86 + 26, "seconds": 26.9}, 96, False, [False, False]),
            ({"at": scan + 27}, 96, False, [False, False]),
            ({"at": scan - 120, "seconds": 26.9}, 96, True, [False, False]),
            ({}, 96, True, [False, False]),
        ):
            with self.subTest(tick=tick, render_delay=render_delay):
                now = roadmap.datetime.fromtimestamp(scan + render_delay, roadmap.UTC)
                (sup / "state.json").write_text(json.dumps({"tick": tick}))
                with mock.patch.dict(os.environ, self.env), mock.patch.object(
                    roadmap, "read_accounts", return_value={}
                ):
                    page = roadmap.render_html(*roadmap.build(self.repo, now), now)
                data = self.page_data(page)
                (worktree,) = data["agents"]["worktrees"]
                agents = worktree["agents"]
                self.assertEqual(
                    [agent["state"] for agent in agents], ["waiting", "stuck"]
                )
                self.assertEqual([agent["stale"] for agent in agents], stale)
                self.assertEqual(data["tickAt"], tick.get("at"))
                self.assertEqual(data["tickLate"], late)

    def test_bad_supervisor_file_still_renders(self):
        sup = Path(self.tmp.name) / "supervisor"
        sup.mkdir()
        path = Path(self.tmp.name) / "r.html"
        bad = '{"workers": {"trial-lane": {"host": "local", "status": 3, "launched_at": 1}}}'
        for body in ("", "{bad", "[]", '{"workers": [], "tick": 5}', bad):
            (sup / "state.json").write_text(body)
            r = run(self.env, "--html", str(path), repo=self.repo)
            self.assertEqual(r.returncode, 0, (body, r.stderr))
            d = self.page_data(path.read_text())
            self.assertIsNone(d["tickAt"])
        self.assertEqual(d["agents"]["worktrees"][0]["agents"][0]["state"], "no status")

    def test_pr_list_cached_for_a_minute(self):
        calls = Path(self.tmp.name) / "calls"
        gh = Path(self.tmp.name) / "bin" / "gh"
        gh.write_text(
            f"#!/bin/sh\necho x >> {calls}\ncat <<'EOF'\n{json.dumps(GH)}\nEOF\n"
        )
        for _ in range(2):
            self.assertIn("PR #7 open", run(self.env, "--text", repo=self.repo).stdout)
        self.assertEqual(len(calls.read_text().split()), 2)
        os.utime(Path(self.tmp.name) / "roadmap-prs.json", (1, 1))
        run(self.env, "--text", repo=self.repo)
        self.assertEqual(len(calls.read_text().split()), 4)

    def test_gh_missing(self):
        env = {**self.env, "PATH": "/usr/bin:/bin"}
        r = run(env, "--text", repo=self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertIn("PR data unavailable: ", r.stdout)
        self.assertNotIn("PR #7", r.stdout)

    def test_gh_stderr_in_footer(self):
        gh = Path(self.tmp.name) / "bin" / "gh"
        gh.write_text(
            "#!/bin/sh\necho 'HTTP 502: Bad Gateway' >&2\necho more >&2\nexit 1\n"
        )
        r = run(self.env, "--text", repo=self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertIn("PR data unavailable: HTTP 502: Bad Gateway", r.stdout)
        self.assertNotIn("more", r.stdout)

    def test_empty_ledger(self):
        empty = tempfile.mkdtemp()
        (Path(empty) / ".concord").mkdir()
        sqlite3.connect(Path(empty) / ".concord" / "concord.db").close()
        r = run({**self.env, "CONCORD_REPO_ROOT": empty}, "--text", repo=Path(empty))
        self.assertEqual(r.returncode, 0)
        self.assertIn("no tickets yet", r.stdout)

    def test_board_text(self):
        out = run(self.env, "--board", repo=self.repo).stdout
        self.assertNotIn("ROADMAP", out)
        lines = out.splitlines()
        i = lines.index("## trial-feature (1/7 done)")
        self.assertEqual(lines[0], "8 of 10 tickets left · 1 of 6 features done")
        self.assertTrue(lines[i + 1].startswith("[###>>>>>>>>!!!xxx...] 1 done"))
        self.assertEqual(lines[i + 2], "### Needs You (1)")
        self.assertEqual(lines[i + 3], "- **need** - need [@a:1]")
        self.assertIn("### In Progress (3)", lines)
        self.assertIn("- **blk** - blk", lines)
        self.assertIn("### Done (1)", lines)

    def refresh_env(self):
        home = Path(self.tmp.name) / "home"
        ledger = home / ".concord-ledger" / self.repo.name
        ledger.mkdir(parents=True)
        scripts = home / ".agents" / "skills" / "dev-auto" / "scripts"
        scripts.parent.mkdir(parents=True)
        scripts.symlink_to(HERE, target_is_directory=True)
        bin_dir = Path(self.tmp.name) / "bin"
        for name, command in (
            ("git", 'printf "%s/.git\\n" "$PWD"'),
            ("open", "exit 0"),
            ("orca", "echo '{\"result\":{}}'"),
        ):
            stub = bin_dir / name
            stub.write_text(f"#!/bin/sh\n{command}\n")
            stub.chmod(0o755)
        return {**os.environ, **self.env, "HOME": str(home)}, ledger / "roadmap.pid"

    def test_stop_leaves_an_unrelated_pid_alone_and_open_replaces_it(self):
        env, pidfile = self.refresh_env()
        command = [str(HERE / "roadmap")]
        with subprocess.Popen(["sleep", "30"]) as unrelated:
            try:
                pidfile.write_text(f"{unrelated.pid}\n")
                stopped = subprocess.run([*command, "stop"], env=env, cwd=self.repo, timeout=5)
                self.assertEqual(stopped.returncode, 0)
                with self.assertRaises(subprocess.TimeoutExpired):
                    unrelated.wait(timeout=0.2)
                self.assertFalse(pidfile.exists())
                pidfile.write_text(f"{unrelated.pid}\n")
                started = subprocess.run(command, env=env, cwd=self.repo, timeout=5)
                self.assertEqual(started.returncode, 0)
                self.assertNotEqual(int(pidfile.read_text()), unrelated.pid)
            finally:
                if unrelated.poll() is None:
                    unrelated.terminate()
                subprocess.run([*command, "stop"], env=env, cwd=self.repo, timeout=5)

    def test_concurrent_opens_start_one_refresh_for_the_ledger(self):
        env, pidfile = self.refresh_env()
        command = [str(HERE / "roadmap")]
        bin_dir = Path(self.tmp.name) / "bin"
        ready = Path(self.tmp.name) / "readers"
        ready.mkdir()
        # Hold both legacy readers at the stale PID so both attempt to launch.
        cat = bin_dir / "cat"
        cat.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "$DA_TEST_PIDFILE" ]; then\n'
            '  touch "$DA_TEST_READERS/$$"\n'
            '  until [ "$(ls "$DA_TEST_READERS" | wc -l)" -ge 2 ]; do sleep 0.05; done\n'
            '  echo 2147483647\n'
            'else exec /bin/cat "$@"; fi\n'
        )
        cat.chmod(0o755)
        nohup = bin_dir / "nohup"
        nohup.write_text('#!/bin/sh\necho "$$" >> "$DA_TEST_LAUNCHES"\n')
        nohup.chmod(0o755)
        launches = Path(self.tmp.name) / "launches"
        env.update(DA_TEST_PIDFILE=str(pidfile), DA_TEST_READERS=str(ready),
                   DA_TEST_LAUNCHES=str(launches))
        pidfile.write_text("2147483647\n")
        openers = [
            subprocess.Popen(command, env=env, cwd=self.repo, start_new_session=True)
            for _ in range(2)
        ]
        try:
            self.assertEqual([proc.wait(timeout=8) for proc in openers], [0, 0])
            pid = int(pidfile.read_text())
            if launches.exists():
                self.assertEqual(launches.read_text().splitlines(), [str(pid)])
            os.kill(pid, 0)
            reopened = subprocess.run(command, env=env, cwd=self.repo, timeout=5)
            self.assertEqual(reopened.returncode, 0)
            self.assertEqual(int(pidfile.read_text()), pid)
        finally:
            for proc in openers:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            cat.unlink()
            subprocess.run([*command, "stop"], env=env, cwd=self.repo, timeout=5)

    def test_free_text_blocked_by_and_operator_note(self):
        def tk(tid, notes, status="proposed"):
            return {
                "task_id": tid,
                "title": f"f / {tid}",
                "status": status,
                "agent": None,
                "agent_id": None,
                "assigned_agent_id": None,
                "notes": notes,
                "updated_at": NOW,
                "updates": [],
            }

        rows = [
            tk("a-01", "Brief: A.", "complete"),
            tk("b-02", "Brief: B."),
            tk("none1", "Blocked by: none"),
            tk(
                "none2",
                "Blocked by: none. Waiting on: operator (approval of the list).",
            ),
            tk("none3", "Blocked by: none (runs alongside b-02)"),
            tk("one", "Blocked by: b-02"),
            tk("two", "Blocked by: a-01, b-02"),
            tk("doneonly", "Blocked by: a-01"),
            tk(
                "opwait",
                "Blocked by: b-02. Waiting on: operator (how Relay tells staff).",
            ),
            tk("cond", "Blocked by: b-02 only if it is already open; otherwise none"),
            tk(
                "prose",
                "Blocked by: some-feature, other-feature, tickets citing old briefs",
            ),
        ]
        by = {
            r["task_id"]: r
            for r in roadmap.classify(rows, roadmap.datetime.now(roadmap.UTC))
        }
        got = {k: (v["state"], v["blockers"]) for k, v in by.items()}
        self.assertEqual(got["none1"], ("remaining", []))
        self.assertEqual(got["none2"][0], "needs you")
        self.assertIn("approval of the list", by["none2"]["detail"])
        self.assertEqual(got["none3"], ("remaining", []))
        self.assertEqual(by["none3"]["blocked_note"], "none (runs alongside b-02)")
        self.assertEqual(got["one"], ("blocked", ["b-02"]))
        self.assertEqual(got["two"], ("blocked", ["b-02"]))
        self.assertEqual(got["doneonly"], ("remaining", []))
        self.assertEqual(got["opwait"][0], "needs you")
        self.assertEqual(got["cond"], ("blocked", ["b-02"]))
        self.assertEqual(got["prose"], ("remaining", []))
        self.assertIn("some-feature", by["prose"]["blocked_note"])
        self.assertEqual(by["none1"]["blocked_note"], "")

    def test_operator_note_in_brief_needs_you_only_while_unowned(self):
        row = {
            "task_id": "o",
            "title": "f / o",
            "status": "active",
            "agent": None,
            "agent_id": "x:1",
            "assigned_agent_id": None,
            "notes": "Brief: B. Waiting on: operator (pick one).",
            "updated_at": NOW,
            "updates": [],
        }
        done = {**row, "task_id": "d", "status": "closed"}
        unowned = {**row, "task_id": "u", "status": "proposed", "agent_id": None}
        out = roadmap.classify([row, done, unowned], roadmap.datetime.now(roadmap.UTC))
        self.assertEqual(
            [t["state"] for t in out], ["in progress", "done", "needs you"]
        )

    def test_shares_and_bars(self):
        cnt = dict.fromkeys(roadmap.SEGMENTS, 0) | {"done": 1, "remaining": 2}
        self.assertEqual(sum(roadmap.apportion(cnt, 100).values()), 100)
        self.assertEqual(sum(roadmap.apportion(cnt, 20, floor_one=True).values()), 20)
        tiny = dict.fromkeys(roadmap.SEGMENTS, 0) | {"done": 500, "needs you": 1}
        self.assertEqual(roadmap.apportion(tiny, 20, floor_one=True)["needs you"], 1)
        zero = dict.fromkeys(roadmap.SEGMENTS, 0)
        self.assertEqual(sum(roadmap.apportion(zero, 100).values()), 0)
        self.assertEqual(roadmap.text_bar(zero), "-" * roadmap.BAR_WIDTH)
        self.assertEqual(len(roadmap.text_bar(cnt)), roadmap.BAR_WIDTH)

    def test_headline(self):
        feats = [
            {"state": "done", "tickets": [{"state": "done"}]},
            {
                "state": "remaining",
                "tickets": [{"state": "blocked"}, {"state": "done"}],
            },
            {"state": "not ticketed", "tickets": []},
        ]
        self.assertEqual(
            roadmap.headline(feats), "1 of 3 tickets left · 1 of 3 features done"
        )

    def tickets(self, spec):
        rows = [
            {
                "task_id": k,
                "title": f"f / {k}",
                "status": "proposed",
                "agent": None,
                "agent_id": None,
                "assigned_agent_id": None,
                "notes": notes,
                "updated_at": NOW,
                "updates": [],
            }
            for k, notes in spec.items()
        ]
        return roadmap.classify(rows, roadmap.datetime.now(roadmap.UTC))

    def test_waves_chain_cycle_overlap(self):
        p = roadmap.plan(
            self.tickets(
                {
                    "a": "Owned paths: src/x/a.ts\nBlocked by: none",
                    "b": "Blocked by: a",
                    "c": "Blocked by: b",
                    "d": "Owned paths: src/x/a.ts, src/x/y\nBlocked by: none",
                    "e": "Owned paths: src/z/e.ts\nBlocked by: none",
                    "op": "Waiting on: operator (ok).",
                    "after-op": "Blocked by: op",
                    "cyc1": "Blocked by: cyc2",
                    "cyc2": "Blocked by: cyc1",
                }
            )
        )
        lanes = lambda w: sorted(r["lane"] for g in w["groups"] for r in g)
        self.assertEqual([r["lane"] for r in p["needs"]], ["op"])
        by = {w["n"]: w for w in p["waves"]}
        self.assertEqual(lanes(by[1]), ["a", "cyc2", "d", "e"])
        self.assertEqual(lanes(by[2]), ["after-op", "b", "cyc1"])
        self.assertIn("c", lanes(by[3]))
        placed = [r["lane"] for w in p["waves"] for g in w["groups"] for r in g]
        self.assertEqual(len(placed), 8)  # the cycle drops one edge and still lands
        one_at_a_time = [[r["lane"] for r in g] for g in by[1]["groups"] if len(g) > 1]
        self.assertEqual(one_at_a_time, [["a", "d"]])
        self.assertEqual(len(by[1]["groups"]), 3)  # cyc2 and e alone, a+d together
        text = "\n".join(roadmap.plan_text(p))
        self.assertIn("Wave 1 · 4 tickets, 3 at once", text)
        self.assertIn("    one at a time:", text)

    def test_square_detail_carries_what_the_panel_shows(self):
        rows = self.tickets(
            {
                "a": "Coder: claude \u00b7 Reviewer: codex \u00b7 Tier: ui\nBlocked by: none",
                "a-review": "",
                "b": "Blocked by: a",
            }
        )
        a = rows[0]
        a["agent_id"], a["status"] = "x:1", "active"
        a["updates"] = [
            {
                "kind": "finding",
                "content": "REVIEW VERDICT: FIX one\nmore",
                "agent": "r",
                "created_at": "2026-11-11T22:00:00+00:00",
                "superseded_by": None,
            },
            {
                "kind": "blocker",
                "content": "Waiting on: review",
                "agent": "x:1",
                "created_at": "2026-11-11T22:01:00+00:00",
                "superseded_by": None,
            },
        ]
        now = roadmap.datetime(2026, 11, 11, 22, 5, tzinfo=roadmap.UTC)
        rows = roadmap.classify(rows, now)
        rows[0]["worker"] = {
            "agent": "claude",
            "model": "m",
            "status": {"state": "working", "since": 100},
        }
        feats = [
            {"name": "f", "state": "in progress", "done": 0, "prs": [], "tickets": rows}
        ]
        sq = {
            t["id"]: t
            for t in roadmap.matrix(rows, feats, now)["features"][0]["tickets"]
        }
        self.assertEqual(
            sq["a"]["detail"],
            {
                "title": "f / a",
                "tier": "ui",
                "coder": "claude",
                "reviewer": "codex",
                "who": [
                    {
                        "role": "coder",
                        "agent": "claude",
                        "model": "m",
                        "state": "working",
                        "since": 100,
                    }
                ],
                "blockers": [],
                "wait": "Waiting on: review",
                "fix": 1,
                "latest": {"line": "Waiting on: review", "at": 1794434460.0},
            },
        )
        self.assertEqual(sq["b"]["detail"]["blockers"], [["a", "in progress"]])
        self.assertEqual(sq["b"]["detail"]["who"], [])
        self.assertIsNone(sq["b"]["detail"]["latest"])

    def test_remaining_square_is_a_solid_outline(self):
        css = (HERE / "roadmap-ui" / "src" / "rail.css").read_text()
        self.assertNotIn("dashed", css.split("shape as well")[1].split("@media")[0])

    def test_agent_rows_show_device_and_account_email(self):
        rows, now = self.fleet()
        for t, acct in zip(rows, ("id-1", "id-2", "id-1", "id-1", "id-1", "id-1")):
            t["worker"]["account"] = acct
        rows[1]["worker"]["host"] = "Mac2"
        accounts = {"id-1": "a@example.test"}
        with mock.patch.object(roadmap, "local_name", return_value="Studio"):
            got = roadmap.agents(rows, now, accounts)
        flat = {x["key"]: x for g in got["worktrees"] for x in g["agents"]}
        self.assertEqual(
            (flat["a"]["device"], flat["a"]["account"]), ("Studio", "a@example.test")
        )
        self.assertEqual(
            (flat["a-review"]["device"], flat["a-review"]["account"]), ("Mac2", "id-2")
        )
        self.assertEqual(flat["b"]["device"], "wsl")
        self.assertEqual(flat["c"]["state"], "paused")
        self.assertEqual(flat["c"]["device"], "Studio")

    def test_account_label_then_id_prefix_when_orca_cannot_say(self):
        uuid = "59e7b317-4722-42d6-a7d4-cccd2497e75f"
        with mock.patch.object(roadmap, "account_labels", return_value={"x-1": "lbl"}):
            self.assertEqual(roadmap.account("cursor", {}), "cursor")
            self.assertEqual(roadmap.account("x-1", {}), "lbl")
            self.assertEqual(roadmap.account(uuid, {}), "59e7b317")
            self.assertEqual(roadmap.account(uuid, {uuid: "e@x.test"}), "e@x.test")

    def test_read_accounts_maps_ids_and_skips_a_failed_read(self):
        out = json.dumps(
            {
                "result": {
                    "claude": {"accounts": [{"id": "i1", "email": "x@y.test"}]},
                    "devin": {"accounts": []},
                    "odd": {},
                }
            }
        )
        ok = subprocess.CompletedProcess([], 0, out, "")
        env = {"CONCORD_REPO_ROOT": self.tmp.name}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(roadmap.subprocess, "run", return_value=ok) as run_:
                self.assertEqual(roadmap.read_accounts(), {"i1": "x@y.test"})
                self.assertEqual(roadmap.read_accounts(), {"i1": "x@y.test"})
                self.assertEqual(run_.call_count, 1)  # second read is the 60 s cache
            (Path(self.tmp.name) / "roadmap-accounts.json").unlink()
            with mock.patch.object(
                roadmap.subprocess, "run", side_effect=OSError("no orca")
            ):
                self.assertEqual(roadmap.read_accounts(), {})
            self.assertFalse((Path(self.tmp.name) / "roadmap-accounts.json").exists())

    def test_depth_columns(self):
        self.assertEqual(
            roadmap.depths(
                self.tickets({"a": "", "b": "Blocked by: a", "c": "Blocked by: b"})
            ),
            {"a": 1, "b": 2, "c": 3},
        )
        rows = self.tickets({"a": "", "b": "Blocked by: a"})
        rows[0]["status"] = "complete"
        rows = roadmap.classify(rows, roadmap.datetime.now(roadmap.UTC))
        self.assertEqual(
            roadmap.depths(rows), {"a": 1, "b": 2}
        )  # done keeps its column
        cyc = roadmap.depths(self.tickets({"x": "Blocked by: y", "y": "Blocked by: x"}))
        self.assertEqual(sorted(cyc), ["x", "y"])

    def test_area_prefixes(self):
        paths = roadmap.owned_paths(
            "Owned paths: src/dashboard/a.ts, src/dashboard/ds/b.ts, src/backend/runs/, "
            "src/backend/runs/x.ts, README.md, `.github/workflows/ci.yml`."
        )
        self.assertEqual(paths[-2:], ["README.md", ".github/workflows/ci.yml"])
        self.assertEqual(
            roadmap.area(paths),
            ".,.github/workflows,src/backend/runs,src/dashboard".replace(",", ", "),
        )
        self.assertEqual(
            roadmap.area(roadmap.owned_paths("Owned paths: none in the repo (x)")), "-"
        )
        self.assertEqual(
            roadmap.owned_paths("Owned paths: none in the repo (see notes, docs/, elsewhere)"),
            [],
        )
        self.assertEqual(
            roadmap.area(["docs/guides/w.md", "docs/images/i.png", "docs/g.md"]), "docs"
        )

    def test_running_and_machine(self):
        rows = self.tickets({"r": "Host: Mac2 · Tier: easy\nOwned paths: src/a/b.ts"})
        rows[0]["worker"] = {
            "agent": "claude",
            "model": "opus",
            "effort": "high",
            "account": "max-1",
            "host": "wsl",
            "launched_at": 1000,
        }
        now = roadmap.datetime.fromtimestamp(1000 + 125 * 60, roadmap.UTC)
        self.assertEqual(
            roadmap.running(rows, now),
            [
                {
                    "feature": "f",
                    "lane": "r",
                    "area": "src/a",
                    "agent": "claude opus high",
                    "account": "max-1",
                    "machine": "wsl",
                    "host": "wsl",
                    "role": "coder",
                    "lost": False,
                    "stopped": False,
                    "live_slot": True,
                    "state": "inactive",
                    "running": "125 min",
                }
            ],
        )
        rows[0]["worker"] = None
        self.assertEqual(
            roadmap.plan(rows)["waves"][0]["groups"][0][0]["machine"], "Mac2"
        )
        self.assertEqual(roadmap.running(rows, now), [])
        rows[0]["worker"] = {"agent": "claude", "launched_at": 1000}
        self.assertEqual(len(roadmap.running(rows, now)), 1)
        rows[0]["state"] = "done"
        self.assertEqual(roadmap.running(rows, now), [])

    def test_short_machine(self):
        short = roadmap.short_host
        for text, want in (
            (
                (
                    "main Mac (the document is saved in the main checkout's ignored "
                    ".work/; the worktree is for reading code only)."
                ),
                "main Mac",
            ),
            (
                (
                    "Juzzark-WSL or Mac2. Serialized after runs-write-state-02 because "
                    "both change the run record model"
                ),
                "Juzzark-WSL or Mac2",
            ),
            (
                "Mac2 (launch with --host Mac2). Source of truth: \u2026 on the main Mac",
                "Mac2",
            ),
            ("any backend host", "any backend"),
            (
                (
                    "the worker runs on the main Mac and drives PowerShell on Juzzark "
                    "through an Orca terminal"
                ),
                "main Mac",
            ),
        ):
            self.assertEqual(short(text), want)
        self.assertEqual(short("Mac2"), "Mac2")
        self.assertEqual(short("wsl"), "wsl")
        self.assertEqual(short("nothing known here"), "not assigned")
        self.assertEqual(short("not assigned"), "not assigned")

    def test_workers_file(self):
        sup = Path(self.tmp.name) / "supervisor"
        sup.mkdir()
        (sup / "state.json").write_text(
            json.dumps(
                {
                    "workers": {
                        "trial-lane": {
                            "agent": "codex",
                            "model": "m",
                            "account": "c1",
                            "host": "local",
                            "launched_at": 1,
                        }
                    }
                }
            )
        )
        out = run(self.env, "--text", repo=self.repo).stdout
        self.assertIn("RUNNING NOW · 1 worker: local 1 (1 coder, 0 reviewers)", out)
        self.assertIn("\n  local · codex m · trial-feature / trial-lane", out)

    def fleet(self):
        names = ["a", "a-review", "b", "b-review", "c", "c-review"]
        rows = self.tickets({k: "Owned paths: src/x/y.ts" for k in names})
        now = roadmap.datetime.fromtimestamp(1000 + 600, roadmap.UTC)
        hosts = ["local", "local", "wsl", "wsl", "local", "local"]
        for t, h in zip(rows, hosts):
            t["worker"] = {
                "agent": "codex",
                "account": "c1",
                "host": h,
                "launched_at": 1000,
            }
        rows[4]["worker"]["lost"] = "STUCK c: terminal gone"
        rows[5]["worker"]["lost"] = "STUCK c-review: terminal gone"
        return rows, now

    def test_running_counts_per_host(self):
        rows, now = self.fleet()
        head = roadmap.running_text(roadmap.running(rows, now))[0]
        self.assertEqual(
            head,
            "RUNNING NOW \u00b7 4 workers: local 2 (1 coder, 1 reviewer), wsl 2 (1, 1)",
        )

    def test_running_rows_lead_with_host_and_agent(self):
        rows, now = self.fleet()
        line = roadmap.running_text(roadmap.running(rows, now))[1]
        self.assertTrue(line.startswith("  local \u00b7 codex \u00b7 f / a"), line)
        rows[0]["lane"] = "x" * 200
        long = roadmap.running_text(roadmap.running(rows, now))[1]
        self.assertTrue(long.startswith("  local \u00b7 codex \u00b7 f / x"), long)

    def test_lost_workers_listed_outside_counts(self):
        rows, now = self.fleet()
        out = roadmap.running_text(roadmap.running(rows, now))
        self.assertEqual(len(roadmap.running(rows, now)), 6)
        lost = [x for x in out if "LOST" in x]
        self.assertEqual(len(lost), 2)
        self.assertIn("local \u00b7 launched 10 min ago \u00b7 c", lost[0])
        self.assertIn("local \u00b7 launched 10 min ago \u00b7 c-review", lost[1])
        self.assertEqual(sum(" \u00b7 " in x and "f / c" in x for x in out), 0)

    def test_stopped_record_is_paused_not_running(self):
        rows, now = self.fleet()
        rows[0]["worker"]["stopped"] = {"why": "usage handoff at 87%", "handoff": 1}
        rows[0]["worker"]["status"] = {"state": "stopped", "detail": "usage handoff"}
        rows[1]["worker"]["status"] = {"state": "handing off"}
        out = roadmap.running_text(roadmap.running(rows, now))
        self.assertEqual(
            out[0],
            "RUNNING NOW · 3 workers: wsl 2 (1 coder, 1 reviewer), local 1 (0, 1)",
        )
        self.assertEqual(sum("f / a \u00b7" in x for x in out), 0)
        self.assertEqual(sum("PAUSED" in x for x in out), 1)
        a = roadmap.agents(rows, now)
        paused = [x for w in a["worktrees"] for x in w["agents"] if x["state"] == "paused"]
        self.assertIn("a", [x["key"] for x in paused])
        self.assertEqual(a["counts"].get("handing off"), 1)
        self.assertNotIn("stopped", a["counts"])

    def test_ui_orders_and_tones_every_recorded_state(self):
        src = (Path(roadmap.__file__).parent / "roadmap-ui/src/shared.jsx").read_text()
        order = re.search(r"STATE_ORDER = (\[[^\]]*\])", src)[1]
        tones = re.search(r"TONE = \{(.*?)\n\};", src, re.DOTALL)[1]
        for s in ("stopped", "handing off", "no-agent"):
            self.assertIn(f'"{s}"', order)
            self.assertRegex(tones, rf'"?{s}"?:')

    def test_every_lost_record_keeps_host_and_age_when_long(self):
        rows, now = self.fleet()
        for i, t in enumerate(rows[:3]):
            t["lane"] = f"dashboard-checks-with-a-long-lane-name-{i}-review-pair"
            t["worker"]["lost"] = "STUCK"
        lost = [
            x for x in roadmap.running_text(roadmap.running(rows, now)) if "LOST" in x
        ]
        self.assertEqual(len(lost), 5)
        for x in lost:
            self.assertRegex(x, r"(local|wsl) \u00b7 launched 10 min ago \u00b7 ")


if __name__ == "__main__":
    unittest.main()
