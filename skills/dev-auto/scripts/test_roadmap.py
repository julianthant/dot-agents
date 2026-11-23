import json
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tick
import roadmap

# Replies as `gh api -i` prints them (headers trimmed to the ones read); a 304 exits 1 with an empty body.
R304 = 'HTTP/2.0 304 Not Modified\r\nEtag: "{tag}"\r\nX-Ratelimit-Remaining: 3992\r\n\r\n'
R200 = 'HTTP/2.0 200 OK\r\nCache-Control: private, max-age=60, s-maxage=60\r\nEtag: W/"{tag}"\r\n\r\n{body}'
SINCE = "2026-11-23T13:00:00Z"
COMMAND = tick.command  # the real one, run over a faked gh process


def issue(n, stage):
    return {"number": n, "title": f"lane / {n}", "issueType": {"name": "Task"}, "state": "OPEN", "stateReason": None,
            "milestone": None, "labels": {"nodes": []}, "parent": None, "subIssues": {"totalCount": 0},
            "subIssuesSummary": None, "blockedBy": {"nodes": []}, "updatedAt": "2026-11-23T12:00:00Z",
            "projectItems": {"nodes": [{"id": f"item{n}", "project": {"number": 5, "owner": {"login": "julianthant"}},
                                        "status": {"name": "In Progress"}, "agent": None,
                                        "stage": stage and {"name": stage}, "area": None}]}}


FIELDS = {"Status": {"id": "f_status", "name": "Status", "options": [{"id": "o_done", "name": "Done"}]},
          "Agent": {"id": "f_agent", "name": "Agent"},
          "Agent state": {"id": "f_stage", "name": "Agent state",
                          "options": [{"id": "o_coding", "name": "Coding"}, {"id": "o_review", "name": "Reviewing"}]},
          "Area": {"id": "f_area", "name": "Area", "options": [{"id": "o_tick", "name": "Tick"}]}}
GITHUB = {"pipeline": {"devPrs": []}, "milestones": ["Staging cut"]}


class Roadmap(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for target, name in ((tick, "SNAPSHOT"), (tick, "PROJECT_CACHE"), (roadmap, "STATE")):
            p = patch.object(target, name, d / {"SNAPSHOT": "issues.json", "PROJECT_CACHE": "project.json",
                                                 "STATE": "roadmap.json"}[name])
            p.start()
            self.addCleanup(p.stop)
        tick.SNAPSHOT.write_text(json.dumps({"fetched": time.time(), "read": time.time(), "closed_full": time.time(),
                                             "open": [issue(1, "Queued"), issue(2, None)], "closed": []}))
        tick.PROJECT_CACHE.write_text(json.dumps({"id": "proj", "fields": FIELDS}))
        self.replies, self.sent = {}, []
        # Any GitHub read outside the conditional polls fails the test unless it expects one.
        self.reads = []
        for target, name in ((tick, "command"), (roadmap, "gh")):
            p = patch.object(target, name, side_effect=lambda *a: self.reads.append(a) or self.answer(a))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(roadmap.subprocess, "run", side_effect=self.gh_api)
        p.start()
        self.addCleanup(p.stop)

    def answer(self, a):
        raise AssertionError(f"unexpected GitHub read {a}")

    def gh_api(self, argv, **kw):
        self.sent.append(argv)
        name = next(k for k, url in roadmap.POLLS.items() if argv[-1].startswith(url.split("?")[0] + "?"))
        reply = self.replies.get(name) or R304.format(tag=name)
        return subprocess.CompletedProcess(argv, 1 if reply.startswith("HTTP/2.0 304") else 0, reply, "")

    def warm(self, built=None):
        roadmap.STATE.write_text(json.dumps({"etags": {k: f'"{k}"' for k in roadmap.POLLS}, "since": SINCE,
                                             "built": built or time.time(), **GITHUB}))

    def edit(self, field, *value, item="item1"):
        with patch.object(tick.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            COMMAND("gh", "project", "item-edit", "--project-id", "proj", "--id", item, "--field-id", field, *value)

    def test_tick_edits_reach_the_roadmap_without_a_github_read(self):
        self.warm()
        self.edit("f_stage", "--single-select-option-id", "o_coding")
        self.edit("f_agent", "--text", "coder m e @a")
        self.edit("f_stage", "--clear", item="item2")
        rows = {r["n"]: r for r in roadmap.render()["issues"]}
        self.assertEqual((rows[1]["agent"], rows[1]["who"], rows[2]["agent"]), ("Coding", "coder m e @a", None))
        self.assertEqual(self.reads, [])
        self.assertGreater(json.loads(tick.SNAPSHOT.read_text())["fetched"], 0)  # still fresh: no refetch owed

    def test_an_edit_the_snapshot_cannot_place_marks_it_stale(self):
        self.edit("f_stage", "--single-select-option-id", "o_coding", item="elsewhere")
        self.assertEqual(json.loads(tick.SNAPSHOT.read_text())["fetched"], 0)
        tick.SNAPSHOT.write_text(json.dumps({**json.loads(tick.SNAPSHOT.read_text()), "fetched": 1}))
        self.edit("f_unknown", "--text", "x")
        self.assertEqual(json.loads(tick.SNAPSHOT.read_text())["fetched"], 0)

    def test_an_unchanged_poll_sends_only_conditional_requests_that_return_304(self):
        self.warm()
        roadmap.render()
        roadmap.render()
        self.assertEqual(len(self.sent), 6)
        for argv in self.sent:
            self.assertIn("-H", argv)
            self.assertTrue(argv[argv.index("-H") + 1].startswith("If-None-Match: "))
        self.assertIn(f"since={SINCE}", self.sent[0][-1])
        self.assertEqual(self.reads, [])

    def test_a_newer_issue_refetches_the_snapshot_and_moves_since(self):
        self.warm()
        refetched = []
        with patch.object(tick, "snapshot", side_effect=lambda fresh=False: refetched.append(fresh) or
                          json.loads(tick.SNAPSHOT.read_text())), patch.object(roadmap, "pipeline", return_value=GITHUB):
            self.replies["issues"] = R200.format(tag="i2", body=json.dumps([{"updated_at": "2026-11-23T13:05:00Z"}]))
            roadmap.render()
            state = json.loads(roadmap.STATE.read_text())
            self.assertEqual((refetched, state["since"], state["etags"]["issues"]), ([True], "2026-11-23T13:05:00Z", None))
            # The moved since= returns that same issue again: no change, and its ETag is kept for the next 304.
            self.replies["issues"] = R200.format(tag="i3", body=json.dumps([{"updated_at": "2026-11-23T13:05:00Z"}]))
            roadmap.render()
            self.assertEqual(refetched, [True, False])
            self.assertEqual(json.loads(roadmap.STATE.read_text())["etags"]["issues"], 'W/"i3"')

    def test_pr_or_run_changes_and_the_minute_rebuild_the_pipeline_only(self):
        built = []
        with patch.object(roadmap, "pipeline", side_effect=lambda: built.append(1) or GITHUB):
            self.warm()
            self.replies["runs"] = R200.format(tag="r2", body="{}")
            roadmap.render()
            self.assertEqual(len(built), 1)
            self.replies.clear()
            roadmap.render()
            self.assertEqual(len(built), 1)
            self.warm(built=time.time() - 61)
            roadmap.render()
            self.assertEqual(len(built), 2)
        self.assertEqual(self.reads, [])  # the issue snapshot stayed within its minute throughout


if __name__ == "__main__":
    unittest.main()
