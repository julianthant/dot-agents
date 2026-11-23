import json
import sqlite3
from contextlib import closing, redirect_stdout
from io import StringIO
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import tick
import roadmap
import pick

# Replies as `gh api -i` prints them (headers trimmed to the ones read); a 304 exits 1 with an empty body.
R304 = 'HTTP/2.0 304 Not Modified\r\nEtag: "{tag}"\r\nX-Ratelimit-Remaining: 3992\r\n\r\n'
R200 = 'HTTP/2.0 200 OK\r\nCache-Control: private, max-age=60, s-maxage=60\r\nEtag: W/"{tag}"\r\n\r\n{body}'
SINCE = "2026-11-23T13:00:00Z"
COMMAND = tick.command  # the real one, run over a faked gh process
READINESS = roadmap.readiness


def issue(n, stage):
    return {"number": n, "title": f"lane / {n}", "issueType": {"name": "Task"}, "state": "OPEN", "stateReason": None,
            "body": "Coder: codex / gpt-6.2-sol / medium", "blocking": {"nodes": [], "totalCount": 0},
            "assignees": {"totalCount": 0}, "closedByPullRequestsReferences": {"totalCount": 0},
            "milestone": None, "labels": {"nodes": [], "totalCount": 0}, "parent": None, "subIssues": {"totalCount": 0},
            "subIssuesSummary": None, "blockedBy": {"nodes": [], "totalCount": 0}, "updatedAt": "2026-11-23T12:00:00Z",
            "projectItems": {"totalCount": 1, "nodes": [{"id": f"item{n}", "project": {"number": 5, "owner": {"login": "julianthant"}},
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
        self.prs = None
        # Any GitHub read outside the conditional polls fails the test unless it expects one.
        self.reads = []
        for target, name in ((tick, "command"), (roadmap, "gh")):
            p = patch.object(target, name, side_effect=lambda *a: self.reads.append(a) or self.answer(a))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(roadmap.subprocess, "run", side_effect=self.gh_api)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(roadmap, "readiness", return_value={})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(roadmap, "pull_requests", side_effect=lambda: self.prs)
        p.start()
        self.addCleanup(p.stop)

    def answer(self, a):
        raise AssertionError(f"unexpected GitHub read {a}")

    def gh_api(self, argv, **kw):
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, getattr(self, "heads", ""), "")
        self.sent.append(argv)
        name = next((k for k, url in roadmap.POLLS.items() if argv[-1].startswith(url.split("?")[0] + "?")), argv[-1])
        reply = self.replies.get(name) or R304.format(tag=name)
        return subprocess.CompletedProcess(argv, 1 if reply.startswith("HTTP/2.0 304") else 0, reply, "")

    def warm(self, built=None, anchored=None, seen=()):
        roadmap.STATE.write_text(json.dumps({"etags": {k: f'"{k}"' for k in roadmap.POLLS}, "since": SINCE,
                                             "anchored": anchored or time.time(), "seen": list(seen),
                                             "built": built or time.time(), **GITHUB}))

    def issues(self, *pairs, tag="i"):
        self.replies["issues"] = R200.format(tag=tag, body=json.dumps([{"number": n, "updated_at": u} for n, u in pairs]))

    def runs(self, *runs):
        self.replies["runs"] = R200.format(tag="r2", body=json.dumps({"workflow_runs": runs}))

    def fetches(self, fail=False):
        """Patch the snapshot read to record each fresh flag, failing as a refused refetch when asked."""
        calls = []
        def snapshot(fresh=False):
            calls.append(fresh)
            if fail and fresh:
                raise tick.OrcaError("gh api failed: rate limit")
            return json.loads(tick.SNAPSHOT.read_text())
        return calls, patch.object(tick, "snapshot", side_effect=snapshot)

    def edit(self, field, *value, item="item1"):
        with patch.object(tick.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            COMMAND("gh", "project", "item-edit", "--project-id", "proj", "--id", item, "--field-id", field, *value)

    def test_ready_rows_match_fill_dry_run_and_held_rows_explain_why(self):
        self.warm()
        bug, blocked, full, later, missing = [issue(n, "Queued") for n in range(1, 6)]
        for n in (bug, blocked, full, later, missing):
            n["projectItems"]["nodes"][0]["status"]["name"] = "Todo"
        bug["issueType"]["name"] = "Bug"
        blocked["blockedBy"] = {"totalCount": 1, "nodes": [{"number": 1, "state": "OPEN"}]}
        later["milestone"] = {"title": "Later"}
        missing["title"] = "missing / base"
        snap = json.loads(tick.SNAPSHOT.read_text())
        snap["open"] = [bug, blocked, full, later, missing]
        tick.SNAPSHOT.write_text(json.dumps(snap))

        def refs(argv):
            return {"remote": "https://github.com/julianthant/hr-relay.git",
                    "for-each-ref": "", "ls-remote": "abc refs/heads/feature/lane"}[argv[3]]

        def seat(*args, holds):
            return (None, ["capacity"]) if holds else ({"id": "seat", "label": "seat"}, [])

        with (patch.object(roadmap, "readiness", READINESS), patch.object(self, "answer", side_effect=refs),
              patch.object(Path, "home", return_value=tick.SNAPSHOT.parent),
              patch.object(tick, "inventory", return_value=set()),
              patch.object(pick.account_usage, "load_config", return_value={}),
              patch.object(pick, "pick", side_effect=seat)):
            rows = roadmap.render()["issues"]
            with redirect_stdout(StringIO()) as output:
                tick.fill(SimpleNamespace(repo_path=Path("/fixture"), issue=None, dry_run=True, holds=None))
        starts = [json.loads(line.split("would start ", 1)[-1])["issue"]
                  for line in output.getvalue().splitlines() if "would start " in line]
        self.assertEqual(starts, [1])
        self.assertEqual([r["n"] for r in rows if r["ready"]], starts)
        self.assertEqual({r["n"]: r["holdReason"] for r in rows},
                         {1: None, 2: "blocked by #1", 3: "no seat", 4: "needs operator",
                          5: "base missing on origin"})

    def test_tick_edits_reach_the_roadmap_without_a_github_read(self):
        self.warm()
        self.edit("f_stage", "--single-select-option-id", "o_coding")
        self.edit("f_agent", "--text", "coder m e @a")
        self.edit("f_stage", "--clear", item="item2")
        rows = {r["n"]: r for r in roadmap.render()["issues"]}
        self.assertEqual((rows[1]["agent"], rows[1]["who"], rows[2]["agent"]), ("Coding", "coder m e @a", None))
        self.assertEqual(self.reads, [])
        self.assertGreater(json.loads(tick.SNAPSHOT.read_text())["fetched"], 0)  # still fresh: no refetch owed

    def test_each_render_reads_the_latest_tick_even_when_github_is_unchanged(self):
        self.warm()
        self.assertIsNone(roadmap.render()["tick"])
        status = {"lastRun": SINCE, "errors": [{"phase": "watch", "error": "OrcaError"}]}
        tick.SNAPSHOT.with_name("tick.json").write_text(json.dumps(status))
        self.assertEqual(roadmap.render()["tick"], status)
        status["errors"] = []
        tick.SNAPSHOT.with_name("tick.json").write_text(json.dumps(status))
        self.assertEqual(roadmap.render()["tick"], status)
        self.assertEqual(self.reads, [])

    def local_worker(self, worker, *, stage="Coding", sha=None, mail=(), tracked=True):
        self.warm()
        with closing(sqlite3.connect(tick.SNAPSHOT.with_name("starts.sqlite"))) as db, db:
            db.execute("CREATE TABLE reviews (run TEXT, issue INTEGER, state TEXT)")
            if tracked:
                db.execute("INSERT INTO reviews VALUES (?, ?, ?)", ("run_c95eee175345", 1, json.dumps(
                    {"coder": "ctx_coder", "expected": "ctx_coder", "stage": stage,
                     "sha": sha, "model": "gpt-6.2-sol"})))
        worker = {"dispatchId": "ctx_coder", "taskId": "task_coder",
                  "dispatchStatus": "dispatched", "workerState": "ready",
                  "projection": {"stage": {"activity": "working"}, "liveness": {"verdict": "live"}},
                  **worker}

        def orca(*args):
            if args[:2] == ("orchestration", "worker-list"):
                if "--on" in args:
                    self.assertEqual(args[args.index("--on") + 1], "Juzzark-WSL")
                    return {"workers": [{**worker, "projection": {"stage": {"activity": "working"}}}]}
                return {"workers": [dict(worker)], "page": {"hasMore": False}}
            if args[:2] == ("orchestration", "task-list"):
                return {"tasks": [{"id": "task_coder", "task_title": "#1 coder", "status": "dispatched"}]}
            if args[:2] == ("orchestration", "inbox"):
                return {"messages": mail}
            raise AssertionError(args)

        return patch.object(tick, "runtime", side_effect=orca)

    def live_rows(self, worker):
        with self.local_worker(worker):
            return [r["now"] for r in roadmap.render()["issues"]]

    def test_a_stopped_worker_overrides_a_coding_card(self):
        live, other = self.live_rows({"dispatchStatus": "failed", "workerState": "stopped"})
        self.assertEqual((live["stage"], live["status"], live["who"][0]["host"]), (0, "stopped", "Mac"))
        self.assertIsNone(other)

    def test_a_working_wsl_worker_names_its_host(self):
        live, _ = self.live_rows({"server": {"name": "Juzzark-WSL"}, "projection": None})
        self.assertEqual(live["status"], "working")
        self.assertEqual(live["who"], [{"role": "coder", "model": "gpt-6.2-sol", "host": "WSL"}])

    def test_prompt_and_operator_question_need_you_without_changing_stage(self):
        for reason in ("approval", "input"):
            with self.subTest(reason=reason):
                live, _ = self.live_rows({"projection": {"attention": {"categories": [reason]}}})
                self.assertEqual((live["stage"], live["status"]), (0, "needs you"))
                tick.SNAPSHOT.with_name("starts.sqlite").unlink()
        mail = [{"type": "question", "thread_id": "q1", "from_handle": "dispatch:ctx_coder", "read": 1}]
        with self.local_worker({}, mail=mail, tracked=False):
            live = roadmap.render()["issues"][0]["now"]
            self.assertEqual((live["stage"], live["status"]), (0, "needs you"))
            mail.append({"type": "status", "thread_id": "q1", "to_handle": "dispatch:ctx_coder"})
            self.assertEqual(roadmap.render()["issues"][0]["now"]["status"], "working")

    def test_a_red_pr_without_a_worker_still_warns_at_the_pr_stage(self):
        self.warm()
        state = json.loads(roadmap.STATE.read_text())
        state["pipeline"]["devPrs"] = [{"n": 637, "head": "task/other", "checks": "failing", "issues": [2]}]
        roadmap.STATE.write_text(json.dumps(state))
        live = roadmap.render()["issues"][1]["now"]
        self.assertEqual((live["stage"], live["status"], live["who"]), (3, "PR checks failing", []))

    def test_task_ci_uses_only_the_pushed_head_and_updates_on_the_existing_run_poll(self):
        self.heads = "new refs/heads/task/lane/1\n"
        run = {"head_branch": "task/lane/1", "head_sha": "old", "event": "push",
               "status": "completed", "conclusion": "success"}
        self.runs(run)
        with self.local_worker({"dispatchStatus": "completed", "workerState": "succeeded"}, stage="Reviewing", sha="old"):
            live = roadmap.render()["issues"][0]["now"]
            self.assertEqual((live["stage"], live["status"]), (1, "waiting for CI"))
            self.runs(dict(run, head_sha="new", conclusion="failure"), run)
            live = roadmap.render()["issues"][0]["now"]
            self.assertEqual((live["stage"], live["status"], live["warning"]), (1, "CI failed", True))
            self.replies.clear()
            self.assertEqual(roadmap.render()["issues"][0]["now"], live)

    def test_a_close_time_edit_reaches_the_cached_closed_card(self):
        snap = json.loads(tick.SNAPSHOT.read_text())
        done = dict(issue(3, "Reviewing"), state="CLOSED", stateReason="COMPLETED")
        tick.SNAPSHOT.write_text(json.dumps({**snap, "closed": [done]}))
        self.warm()
        self.edit("f_stage", "--clear", item="item3")
        rows = {r["n"]: r for r in roadmap.render()["issues"]}
        self.assertEqual((rows[3]["agent"], self.reads), (None, []))
        self.assertGreater(json.loads(tick.SNAPSHOT.read_text())["fetched"], 0)

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
        self.assertEqual(len(self.sent), 4)
        for argv in self.sent:
            self.assertIn("-H", argv)
            self.assertTrue(argv[argv.index("-H") + 1].startswith("If-None-Match: "))
        self.assertIn(f"since={SINCE}", self.sent[0][-1])
        self.assertEqual(self.reads, [])

    def test_a_second_update_in_the_same_second_is_a_change_and_a_repeat_is_not(self):
        t = "2026-11-23T13:05:00Z"
        self.warm(seen=[[1, t]])
        calls, snapshot = self.fetches()
        with snapshot, patch.object(roadmap, "pipeline", return_value=GITHUB):
            self.issues((1, t), (2, t), tag="i2")
            roadmap.render()
            self.issues((2, t), (1, t), tag="i2")  # the same updates again: no change
            roadmap.render()
        self.assertEqual(calls, [True, False])
        state = json.loads(roadmap.STATE.read_text())
        self.assertEqual((state["since"], state["etags"]["issues"]), (SINCE, 'W/"i2"'))  # the query stays stable

    def test_a_moved_since_overlaps_the_old_window_and_keeps_its_new_etag(self):
        t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
        self.warm(anchored=time.time() - roadmap.ANCHOR_SECONDS - 1, seen=[[1, SINCE + "x"], [2, t]])
        calls, snapshot = self.fetches()
        with snapshot:
            self.issues((2, t), tag="moved")
            roadmap.render()
            self.replies.clear()
            roadmap.render()
        self.assertEqual(calls, [False, False])  # the overlap repeats a known update: nothing changed
        self.assertNotIn("-H", self.sent[0])  # only the first poll of the new window is unconditional
        self.assertNotIn(f"since={SINCE}", self.sent[0][-1])
        self.assertIn('If-None-Match: W/"moved"', self.sent[2])
        self.assertEqual(self.reads, [])

    def test_a_refused_refresh_stays_pending_until_it_succeeds(self):
        self.warm()
        built = []
        rebuild = patch.object(roadmap, "pipeline", side_effect=lambda _: built.append(1) or GITHUB)
        calls, snapshot = self.fetches(fail=True)
        with snapshot, rebuild:
            self.issues((1, "2026-11-23T13:05:00Z"))
            rows = roadmap.render()["issues"]
            self.replies.clear()
            roadmap.render()  # every poll 304: the failed refresh is still owed
        self.assertEqual((calls, len(rows)), ([True, True], 2))
        calls, snapshot = self.fetches()
        with snapshot, rebuild:
            roadmap.render()
            roadmap.render()
        self.assertEqual(calls, [True, False])
        self.assertEqual(len(built), 1)  # only the real change rebuilt the pipeline; retries reuse the cache

    def test_runs_update_the_full_run_without_rebuilding_the_pipeline(self):
        self.warm()
        full = {"head_branch": "development", "event": "workflow_dispatch", "status": "in_progress",
                "conclusion": None, "created_at": SINCE, "head_sha": "abc", "html_url": "https://example.com/run"}
        self.runs(dict(full, event="push", head_sha="other"), full)
        expected = {"status": "in_progress", "conclusion": None, "createdAt": SINCE,
                    "headSha": "abc", "url": "https://example.com/run"}
        with patch.object(roadmap, "pipeline", return_value=GITHUB) as rebuild:
            self.assertEqual(roadmap.render()["pipeline"].get("fullRun"), expected)
            self.replies.clear()
            self.assertEqual(roadmap.render()["pipeline"].get("fullRun"), expected)
            self.runs(dict(full, event="push", head_sha="other"))
            self.assertEqual(roadmap.render()["pipeline"].get("fullRun"), expected)
        rebuild.assert_not_called()
        self.assertEqual(self.reads, [])

    def test_issue_and_pr_changes_and_the_minute_rebuild_the_pipeline(self):
        calls, snapshot = self.fetches()
        with snapshot, patch.object(roadmap, "pipeline", return_value=GITHUB) as rebuild:
            self.warm()
            self.issues((1, "2026-11-23T13:05:00Z"))
            roadmap.render()
            self.assertEqual(rebuild.call_count, 1)
            self.replies.clear()
            self.prs = {"devPrs": [{"n": 7, "head": "task/example", "issues": [], "checks": "running"}]}
            roadmap.render()
            self.assertEqual(rebuild.call_count, 2)
            self.prs["devPrs"][0]["checks"] = "failing"
            roadmap.render()
            self.assertEqual(rebuild.call_count, 2)
            self.warm(built=time.time() - 61)
            roadmap.render()
            self.assertEqual(rebuild.call_count, 3)
        self.assertEqual(calls, [True, False, False, False])
        self.assertEqual(self.reads, [])

    def test_minute_refresh_reuses_unchanged_rest_fields(self):
        endpoints = {"releases?per_page=1": [{"tag_name": "v1", "published_at": SINCE}],
                     "git/matching-refs/heads/cut/": [{"ref": "refs/heads/cut/old"}],
                     "compare/staging...development?per_page=1": {"ahead_by": 8},
                     "compare/main...development?per_page=1": {"ahead_by": 9},
                     "milestones?state=open&per_page=50": [{"title": "Staging cut"}]}
        self.warm(built=time.time() - 61)
        for path, body in endpoints.items():
            self.replies[f"repos/{roadmap.REPO}/{path}"] = R200.format(tag="cached", body=json.dumps(body))
        with patch.object(self, "answer", side_effect=lambda a: [] if a[:2] == ("pr", "list") else {"total_count": 7}):
            first = roadmap.render()
            self.sent.clear()
            self.replies.clear()
            state = json.loads(roadmap.STATE.read_text())
            roadmap.STATE.write_text(json.dumps({**state, "built": time.time() - 61}))
            second = roadmap.render()
        self.assertEqual(first["pipeline"]["release"], {"tagName": "v1", "publishedAt": SINCE})
        self.assertEqual(first["pipeline"]["cuts"], ["cut/old"])
        self.assertEqual(first["pipeline"]["devAheadOfStaging"], 8)
        self.assertEqual(first["pipeline"]["devAheadOfMain"], 9)
        self.assertEqual(first["milestones"], ["Staging cut"])
        self.assertEqual((second["pipeline"], second["milestones"]), (first["pipeline"], first["milestones"]))
        self.assertEqual([a[-1] for a in self.sent[2:]], [f"repos/{roadmap.REPO}/{path}" for path in endpoints])
        self.assertTrue(all('If-None-Match: W/"cached"' in a for a in self.sent[2:]))


if __name__ == "__main__":
    unittest.main()
