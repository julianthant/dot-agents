import json
import os
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
REST = {
    "releases?per_page=1": [{"tag_name": "v1", "published_at": SINCE}],
    "git/matching-refs/heads/cut/": [{"ref": "refs/heads/cut/old"}],
    "compare/staging...development?per_page=1": {"ahead_by": 8},
    "compare/main...development?per_page=1": {"ahead_by": 9},
    "milestones?state=open&per_page=50": [{"title": "Staging cut"}],
}


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
        self.prs = {"devPrs": [], "stagingPr": [], "agentPrs": []}
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
        reply = self.replies.get(argv[-1], R304.format(tag="cached"))
        return subprocess.CompletedProcess(argv, 1 if reply.startswith("HTTP/2.0 304") else 0, reply, "")

    def warm(self):
        if not roadmap.STATE.exists():
            roadmap.STATE.write_text(json.dumps(GITHUB))

    def runs(self, *runs):
        self.replies[roadmap.RUNS] = R200.format(tag="runs", body=json.dumps({"workflow_runs": runs}))

    def full(self, *, render=True):
        for path, body in REST.items():
            self.replies[f"repos/{roadmap.REPO}/{path}"] = R200.format(tag="cached", body=json.dumps(body))
        self.replies.setdefault(roadmap.RUNS, R200.format(tag="runs", body='{"workflow_runs":[]}'))

        def github(*args):
            if args[:2] == ("api", "-X"):
                self.reads.append(args)
                return {"total_count": 7}
            return self.answer(args)

        opened = json.loads(tick.SNAPSHOT.read_text())["open"]
        patches = (patch.object(roadmap, "gh", side_effect=github),
                   patch.object(tick, "issue_nodes", side_effect=lambda state, *_: opened if state == "OPEN" else []))
        if not render:
            return patches
        with patches[0], patches[1]:
            return roadmap.render(full=True)

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
            rows = self.full()["issues"]
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

    def test_local_render_with_stale_snapshot_and_cold_caches_stays_offline(self):
        snap = json.loads(tick.SNAPSHOT.read_text())
        tick.SNAPSHOT.write_text(json.dumps({**snap, "fetched": 0}))
        tick.PROJECT_CACHE.unlink()
        with (patch.object(roadmap, "readiness", READINESS),
              patch.object(roadmap.subprocess, "run", side_effect=AssertionError("unexpected subprocess")),
              patch.object(roadmap, "pull_requests", side_effect=AssertionError("unexpected PR query"))):
            data = roadmap.render()
        self.assertEqual({r["n"] for r in data["issues"]}, {1, 2})
        self.assertIsNone(data["githubFetchedAt"])
        self.assertEqual(self.reads, [])

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

    def local_worker(self, worker, *, stage="Coding", sha=None, mail=(), dispatch_mail=(), tracked=True):
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
                if "--terminal" in args:
                    self.assertEqual(args[args.index("--terminal") + 1], "dispatch:ctx_coder")
                    return {"messages": dispatch_mail}
                return {"messages": mail}
            raise AssertionError(args)

        return patch.object(tick, "runtime", side_effect=orca)

    def live_rows(self, worker):
        with self.local_worker(worker):
            return [r["now"] for r in roadmap.render()["issues"]]

    def test_headless_render_reads_the_explicit_run_even_if_terminal_inventory_fails(self):
        diagnostic = "terminal_surface_ownership_unavailable"

        def command(*args):
            self.assertEqual(args[:2], (tick.ORCA, "orchestration"))
            self.assertEqual(args[-3:], ("--environment", "local", "--json"))
            self.assertEqual(args[args.index("--run") + 1], roadmap.RUN)
            self.assertNotIn("--from", args)
            result = {
                "worker-list": {"workers": [{
                    "dispatchId": "ctx_coder", "taskId": "task_coder", "dispatchStatus": "dispatched",
                    "projection": {"stage": {"activity": "working"}},
                }], "page": {"hasMore": False}},
                "task-list": {"tasks": [{"id": "task_coder", "task_title": "#1 coder"}]},
                "inbox": {"messages": []},
            }[args[2]]
            return json.dumps({"ok": True, "result": result})

        self.warm()
        tick.SNAPSHOT.with_name("starts.sqlite").touch()
        log = StringIO()
        with (patch.dict(os.environ, {}, clear=True), patch.object(tick, "command", side_effect=command),
              redirect_stderr(log)):
            live = roadmap.render()["issues"][0]["now"]
        self.assertEqual(live["status"], "working")
        self.assertEqual(log.getvalue(), "")

    def test_refused_worker_inventory_fails_the_render_instead_of_publishing_empty_now(self):
        diagnostic = "terminal_surface_ownership_unavailable"
        self.warm()
        tick.SNAPSHOT.with_name("starts.sqlite").touch()
        with patch.object(tick, "runtime", side_effect=tick.OrcaError(diagnostic)):
            with self.assertRaisesRegex(tick.OrcaError, f"roadmap worker inventory.*{diagnostic}"):
                roadmap.render()

    def test_a_stopped_worker_overrides_a_coding_card(self):
        live, other = self.live_rows({"dispatchStatus": "failed", "workerState": "stopped"})
        self.assertEqual((live["stage"], live["status"], live["who"][0]["host"]), (0, "stopped", "Mac"))
        self.assertTrue(live["warning"])
        self.assertIsNone(other)

    def block_ticket(self, *blockers):
        snap = json.loads(tick.SNAPSHOT.read_text())
        snap["open"][0]["blockedBy"] = {"nodes": list(blockers), "totalCount": len(blockers)}
        tick.SNAPSHOT.write_text(json.dumps(snap))

    def test_a_parked_ticket_keeps_its_review_stage(self):
        self.block_ticket({"number": 653, "state": "OPEN"}, {"number": 654, "state": "CLOSED"},
                          {"number": 655, "state": "OPEN"})
        self.heads = "green refs/heads/task/lane/1\n"
        self.runs({"head_branch": "task/lane/1", "head_sha": "green", "event": "push",
                   "status": "completed", "conclusion": "success"})
        self.full()
        with self.local_worker({"dispatchStatus": "failed", "workerState": "stopped"}, stage="Merging", sha="green"):
            live = roadmap.render()["issues"][0]["now"]
        self.assertEqual((live["stage"], live["status"], live["warning"]), (3, "parked on #653, #655", False))

    def test_a_parked_ticket_with_failed_task_ci_still_warns(self):
        self.block_ticket({"number": 653, "state": "OPEN"})
        self.heads = "red refs/heads/task/lane/1\n"
        self.runs({"head_branch": "task/lane/1", "head_sha": "red", "event": "push",
                   "status": "completed", "conclusion": "failure"})
        self.full()
        with self.local_worker({"dispatchStatus": "completed", "workerState": "succeeded"}):
            live = roadmap.render()["issues"][0]["now"]
        self.assertEqual((live["stage"], live["status"], live["warning"]), (1, "parked on #653 · CI failed", True))

    def test_a_parked_ticket_with_failed_pr_checks_still_warns(self):
        self.block_ticket({"number": 653, "state": "OPEN"})
        self.prs = {"devPrs": [{"n": 637, "head": "task/lane/1", "checks": "failing", "issues": [1]}]}
        self.full()
        live, _ = self.live_rows({"dispatchStatus": "failed", "workerState": "stopped"})
        self.assertEqual((live["stage"], live["status"], live["warning"]),
                         (3, "parked on #653 · PR checks failing", True))

    def test_a_blocked_ticket_with_a_working_coder_still_shows_activity(self):
        self.block_ticket({"number": 653, "state": "OPEN"})
        live, _ = self.live_rows({})
        self.assertEqual((live["status"], live["warning"]), ("working", False))

    def test_a_working_wsl_worker_names_its_host(self):
        live, _ = self.live_rows({"server": {"name": "Juzzark-WSL"}, "projection": None})
        self.assertEqual(live["status"], "working")
        self.assertEqual(live["who"], [{"role": "coder", "model": "gpt-6.2-sol", "host": "WSL"}])

    def test_prompts_need_you_even_while_working(self):
        for reason in ("approval", "input"):
            with self.subTest(reason=reason):
                live, _ = self.live_rows({"projection": {
                    "stage": {"activity": "working"}, "attention": {"categories": [reason]},
                }})
                self.assertEqual((live["stage"], live["status"]), (0, "needs you"))
                tick.SNAPSHOT.with_name("starts.sqlite").unlink()

    def test_question_badge_tracks_worker_activity_and_threaded_replies(self):
        mail = [{"type": "question", "thread_id": "q1", "from_handle": "dispatch:ctx_coder", "read": 1}]
        reply = {"type": "status", "thread_id": "q1", "to_handle": "dispatch:ctx_coder"}
        cases = (
            ("working", (), (), "working"),
            ("waiting", (), (), "needs you"),
            ("idle", (), (), "needs you"),
            ("idle", (), (dict(reply, thread_id="other"),), "needs you"),
            ("idle", (), (reply,), "worker state unavailable"),
            ("idle", (reply,), (), "worker state unavailable"),
        )
        for activity, run_replies, dispatch_mail, status in cases:
            with self.subTest(activity=activity, run_replies=run_replies, dispatch_mail=dispatch_mail):
                worker = {"projection": {"stage": {"activity": activity}}}
                with self.local_worker(worker, mail=[*mail, *run_replies], dispatch_mail=dispatch_mail, tracked=False):
                    live = roadmap.render()["issues"][0]["now"]
                tick.SNAPSHOT.with_name("starts.sqlite").unlink()
                self.assertEqual((live["stage"], live["status"]), (0, status))

    def test_a_red_pr_without_a_worker_still_warns_at_the_pr_stage(self):
        self.warm()
        state = json.loads(roadmap.STATE.read_text())
        state["pipeline"]["devPrs"] = [{"n": 637, "head": "task/other", "checks": "failing", "issues": [2]}]
        roadmap.STATE.write_text(json.dumps(state))
        live = roadmap.render()["issues"][1]["now"]
        self.assertEqual((live["stage"], live["status"], live["who"]), (3, "PR checks failing", []))

    def test_task_ci_uses_only_the_pushed_head_from_the_last_manual_refresh(self):
        self.heads = "new refs/heads/task/lane/1\n"
        run = {"head_branch": "task/lane/1", "head_sha": "old", "event": "push",
               "status": "completed", "conclusion": "success"}
        self.runs(run)
        self.full()
        with self.local_worker({"dispatchStatus": "completed", "workerState": "succeeded"}, stage="Reviewing", sha="old"):
            live = roadmap.render()["issues"][0]["now"]
            self.assertEqual((live["stage"], live["status"]), (1, "waiting for CI"))
            self.runs(dict(run, head_sha="new", conclusion="failure"), run)
            self.assertEqual(roadmap.render()["issues"][0]["now"], live)
            live = self.full()["issues"][0]["now"]
            self.assertEqual((live["stage"], live["status"], live["warning"]), (1, "CI failed", True))
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

    def test_full_refresh_reuses_etags_and_local_renders_keep_the_github_timestamp(self):
        full = {"head_branch": "development", "event": "workflow_dispatch", "status": "in_progress",
                "conclusion": None, "created_at": SINCE, "head_sha": "abc", "html_url": "https://example.com/run"}
        self.runs(dict(full, event="push", head_sha="other"), full)
        self.prs["devPrs"] = [{"n": 7, "head": "task/example", "issues": [], "checks": "running"}]
        gh, issues = self.full(render=False)
        with gh, issues:
            first = roadmap.render(full=True)
            self.sent.clear()
            self.replies.clear()
            self.prs = {**self.prs, "devPrs": [dict(self.prs["devPrs"][0], checks="failing")]}
            second = roadmap.render(full=True)
        self.assertEqual(len(self.sent), 6)
        self.assertTrue(all("-H" in argv for argv in self.sent))
        self.assertEqual(first["pipeline"]["fullRun"]["headSha"], "abc")
        self.assertEqual(second["pipeline"]["release"], {"tagName": "v1", "publishedAt": SINCE})
        self.assertEqual(second["pipeline"]["mergedSinceRelease"], 7)
        self.assertEqual(second["pipeline"]["devPrs"][0]["checks"], "failing")
        self.assertEqual(second["milestones"], ["Staging cut"])
        self.assertNotEqual(first["githubFetchedAt"], second["githubFetchedAt"])
        self.assertEqual(roadmap.render()["githubFetchedAt"], second["githubFetchedAt"])

    def test_refresh_endpoint_publishes_once_for_overlapping_and_rapid_clicks(self):
        directory = tick.SNAPSHOT.parent
        entered, release = Event(), Event()
        gh, issues = self.full(render=False)

        def wait_for_github(argv, **kwargs):
            if argv[-1] == roadmap.RUNS:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the refresh")
            return self.gh_api(argv, **kwargs)

        def request(method, *, header=True, path="/refresh"):
            headers = {"X-Roadmap-Refresh": "1"} if header else {}
            try:
                with urlopen(Request(base + path, method=method, headers=headers), timeout=5) as response:
                    return response.status, response.read()
            except HTTPError as error:
                return error.code, b""

        with roadmap.make_server(directory, "127.0.0.1", 0) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            serving = Thread(target=server.serve_forever, daemon=True)
            serving.start()
            first = []
            clicking = Thread(target=lambda: first.append(request("POST")[0]), daemon=True)
            try:
                self.assertEqual(request("POST", header=False)[0], 403)
                with gh, issues, patch.object(roadmap.subprocess, "run", side_effect=wait_for_github):
                    clicking.start()
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(request("POST")[0], 409)
                    local = Thread(target=roadmap.publish, args=(False, directory), daemon=True)
                    local.start()
                    local.join(2)
                    self.assertFalse(local.is_alive(), "local render waited on GitHub")
                    _, body = request("GET", path="/data.json")
                    self.assertIsNone(json.loads(body)["githubFetchedAt"])
                    release.set()
                    clicking.join(5)
                    self.assertEqual(first, [204])
                    self.assertEqual(request("POST")[0], 204)
                    _, body = request("GET", path="/data.json")
                    data = json.loads(body)
                    self.assertIsNotNone(data["githubFetchedAt"])
                    self.assertEqual(sum(argv[-1] == roadmap.RUNS for argv in self.sent), 1)
            finally:
                release.set()
                server.shutdown()
                serving.join(5)

    def test_failed_manual_refresh_preserves_the_published_data(self):
        directory = tick.SNAPSHOT.parent
        gh, issues = self.full(render=False)
        with gh, issues:
            roadmap.publish(True, directory)
        saved = (directory / "data.json").read_bytes()
        self.replies[roadmap.RUNS] = "HTTP/2.0 403 Forbidden\r\n\r\n"
        with self.assertRaisesRegex(ValueError, "GitHub REST request returned 403"):
            roadmap.publish(True, directory)
        self.assertEqual((directory / "data.json").read_bytes(), saved)
        self.assertEqual(roadmap.render()["githubFetchedAt"], json.loads(saved)["githubFetchedAt"])


if __name__ == "__main__":
    unittest.main()
