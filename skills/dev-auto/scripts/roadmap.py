"""Build roadmap JSON from saved data, or refresh GitHub on demand."""
from argparse import ArgumentParser, Namespace
import datetime
from contextlib import closing
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Lock
import time

import tick

REPO = "julianthant/hr-relay"
RUN = "run_c95eee175345"
PR_FIELDS = """
  nodes {
    number title headRefName autoMergeRequest { enabledAt } isDraft updatedAt
    closingIssuesReferences(first: 20) { nodes { number repository { nameWithOwner } } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  }
"""
PR_QUERY = """
query {
  relay: repository(owner: "julianthant", name: "hr-relay") {
    dev: pullRequests(first: 50, baseRefName: "development", states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {%s}
    staging: pullRequests(first: 5, baseRefName: "staging", states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {%s}
  }
  agents: repository(owner: "julianthant", name: "dot-agents") {
    task: pullRequests(first: 50, baseRefName: "main", states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {%s}
  }
}
""" % (PR_FIELDS, PR_FIELDS, PR_FIELDS)


def gh(*a):
    return json.loads(subprocess.run(["gh", *a], check=True, capture_output=True, text=True, timeout=60).stdout)


def row(n):
    it = next((i for i in n["projectItems"]["nodes"] if i["project"]["number"] == tick.PROJECT), {})
    v = lambda k: (it.get(k) or {}).get("name")
    s = n["subIssuesSummary"] or {}
    return dict(n=n["number"], t=n["title"], type=(n["issueType"] or {}).get("name"), open=n["state"] == "OPEN",
        done=n["state"] == "CLOSED" and n["stateReason"] == "COMPLETED", ms=(n["milestone"] or {}).get("title"),
        later="later" in [l["name"] for l in n["labels"]["nodes"]], parent=(n["parent"] or {}).get("number"),
        kids=n["subIssues"]["totalCount"], kidsDone=s.get("completed", 0),
        blockers=[b["number"] for b in n["blockedBy"]["nodes"] if b["state"] == "OPEN"],
        deps=[b["number"] for b in n["blockedBy"]["nodes"]],
        who=(it.get("agent") or {}).get("text"), updated=n["updatedAt"],
        status=v("status"), agent=v("stage"), area=v("area"))


def pull_requests():
    result = gh("api", "graphql", "-f", f"query={PR_QUERY}")
    if not result or result.get("errors"):
        raise ValueError("GitHub PR query failed")

    def pr(p):
        commit = p["commits"]["nodes"]
        rollup = (commit[0]["commit"]["statusCheckRollup"] or {}) if commit else {}
        checks = {"SUCCESS": "green", "ERROR": "failing", "FAILURE": "failing"}.get(rollup.get("state"), "running")
        return dict(n=p["number"], t=p["title"], head=p["headRefName"], auto=p["autoMergeRequest"] is not None,
                    draft=p["isDraft"], checks=checks, updated=p["updatedAt"],
                    issues=[i["number"] for i in p["closingIssuesReferences"]["nodes"]
                            if i and i["repository"]["nameWithOwner"] == REPO])

    data = result["data"]
    return {"devPrs": [pr(p) for p in data["relay"]["dev"]["nodes"]],
            "stagingPr": [pr(p) for p in data["relay"]["staging"]["nodes"]],
            "agentPrs": [pr(p) for p in data["agents"]["task"]["nodes"] if p["headRefName"].startswith("task/")]}


REFUSED = (tick.OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired)


STATE = tick.SNAPSHOT.with_name("roadmap.json")
RUNS = f"repos/{REPO}/actions/workflows/{tick.FULL_RUN_WORKFLOW}/runs?per_page=100"
REFRESH_ERRORS = (*REFUSED, subprocess.CalledProcessError, TypeError)


def poll(url, etag):
    """(status, etag, body) of a GET sent with If-None-Match."""
    r = subprocess.run(["gh", "api", "-i", *(["-H", f"If-None-Match: {etag}"] if etag else []), url],
                       capture_output=True, text=True, timeout=60)
    head, _, body = r.stdout.replace("\r\n", "\n").partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d+)", head)
    tag = re.search(r"(?im)^etag:\s*(.+)$", head)
    return int(status[1]) if status else 0, tag[1].strip() if tag else etag, body


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


def refresh(state):
    rest = state.setdefault("rest", {})
    runs = cached_rest(rest, RUNS, lambda data: data["workflow_runs"])
    latest = {}
    for run in runs:
        if run["event"] == "push" and run["head_branch"].startswith("task/"):
            latest.setdefault(run["head_branch"], {k: run[k] for k in ("head_sha", "status", "conclusion")})
    state["task_runs"] = {**state.get("task_runs", {}), **latest}
    full_run = next((r for r in runs if r["head_branch"] == tick.FULL_RUN_BRANCH
                     and r["event"] in ("schedule", "workflow_dispatch")), None)
    state.update(pipeline(rest, pull_requests(), full_run))

    snap = tick.snapshot(fresh=True)
    meta = tick.project_meta()
    if {row(n)["area"] for n in snap["open"] + snap["closed"]} - {None, *area_names(meta)}:
        meta = tick.project_meta(refresh=True)
    state["areas"] = area_names(meta)
    state["heads"] = branch_heads(tick.tickets(rows=snap["open"]))
    state["readiness"] = readiness(snap)
    state["githubFetchedAt"] = stamp()
    return snap


def render(full=False):
    state = read_json(STATE, {})
    state.setdefault("pipeline", {})
    if full:
        snap = refresh(state)
    else:
        snap = read_json(tick.SNAPSHOT, {"open": [], "closed": []})
    data = build(snap, state, now_rows(snap, state))
    if full:
        write_json(STATE, state)
    return data


def readiness(snap):
    tickets = tick.tickets(rows=snap["open"])
    decisions = {}
    home = Path.home()
    for name, checkout in (("hr-relay", home / "Projects/hr-relay"), ("dot-agents", home / ".agents")):
        if not any(t["repo"] == name and t["kind"] in ("Task", "Bug") for t in tickets):
            continue
        args = Namespace(repo_path=checkout, dry_run=True, holds=None, issue=None)
        try:
            decisions.update(tick.fill(args, issues=tickets, emit=lambda *_: None))
        except (*REFUSED, sqlite3.Error) as e:
            print(f"roadmap {name} readiness: {e}", file=sys.stderr)
    return decisions


def live_workers(numbers):
    path = tick.SNAPSHOT.with_name("starts.sqlite")
    if not path.is_file():
        return {}, {}
    reviews = {}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='reviews'").fetchone():
                reviews = {n: json.loads(s) for n, s in db.execute("SELECT issue,state FROM reviews WHERE run=?", (RUN,))
                           if n in numbers}
    except (sqlite3.Error, ValueError) as e:
        print(f"roadmap reviews: {e}", file=sys.stderr)

    # Run-scoped reads work from launchd without a terminal or supervisor anchor.
    try:
        workers = {w["dispatchId"]: w for w in tick.fleet_workers(RUN)}
        tasks = {t["id"]: t for t in tick.runtime("orchestration", "task-list", "--run", RUN, "--full")["tasks"]}
    except REFUSED as e:
        raise tick.OrcaError(f"roadmap worker inventory for {RUN}: {e}") from e

    # Fleet summaries identify paired workers but omit the activity and attention projection.
    for host in {tick.worker_host(w) for w in workers.values() if w.get("dispatchStatus") == "dispatched"
                 and not w.get("projection")} - {"local"}:
        try:
            remote = tick.runtime("orchestration", "worker-list", "--run", RUN, "--on", host, "--full", "--limit", "100")
            for w in remote["workers"]:
                if w["dispatchId"] in workers:
                    workers[w["dispatchId"]]["projection"] = w.get("projection")
        except REFUSED as e:
            print(f"roadmap paired workers: {e}", file=sys.stderr)

    current = {}
    for w in workers.values():
        t = tasks.get(w.get("taskId"))
        if not t:
            continue
        if w.get("dispatchStatus") != "dispatched" and worker_status(w) != "stopped":
            continue
        title = t.get("task_title") or ""
        match = re.search(r"#(\d+)", title) or re.search(r"hr-relay ?#(\d+)", t.get("spec") or "")
        if match and (n := int(match[1])) in numbers:
            role = "reviewer" if re.match(r"^#\d+ reviewer\b", title) else "coder"
            old = current.get((n, role))
            if old is None or (old.get("dispatchStatus") != "dispatched" and w.get("dispatchStatus") == "dispatched"):
                current[n, role] = w

    for n, s in reviews.items():
        for role, dispatch in (("coder", s.get("coder")), ("reviewer", (s.get("reviewer") or {}).get("dispatch"))):
            w = workers.get(dispatch)
            if w and (w.get("dispatchStatus") == "dispatched" or (n, role) not in current):
                current[n, role] = w

    active = {f"dispatch:{w['dispatchId']}": w for w in current.values() if w.get("dispatchStatus") == "dispatched"}
    if active:
        try:
            mail = tick.runtime("orchestration", "inbox", "--run", RUN, "--limit", "500", "--full")["messages"]
        except REFUSED as e:
            print(f"roadmap worker questions: {e}", file=sys.stderr)
            mail = []
        replies = {(m["thread_id"], m.get("to_handle")) for m in mail if m["type"] == "status" and m.get("thread_id")}
        questions = {}
        for m in mail:
            sender = m.get("from_handle")
            thread = m.get("thread_id") or m.get("id")
            if m["type"] == "question" and sender in active and (thread, sender) not in replies:
                questions.setdefault(sender, set()).add(thread)
        for sender, threads in questions.items():
            worker = active[sender]
            # Only consult mail when the question could change the projected status.
            if worker_status(worker) != "worker state unavailable":
                continue
            try:
                inbox = tick.runtime("orchestration", "inbox", "--terminal", sender, "--limit", "500", "--full")["messages"]
                threads.difference_update(m["thread_id"] for m in inbox if m["type"] == "status"
                                          and m.get("to_handle") == sender and m.get("thread_id"))
            except REFUSED as e:
                print(f"roadmap worker question replies: {e}", file=sys.stderr)
            if threads:
                worker["operatorQuestion"] = True
    return reviews, current


def worker_status(worker):
    projection = worker.get("projection") or {}
    if worker.get("workerState") in ("failed", "stopped", "abandoned") or worker.get("dispatchStatus") == "failed":
        return "stopped"
    if worker.get("dispatchStatus") == "completed":
        return "finished"
    if (projection.get("liveness") or {}).get("verdict") == "exited":
        return "stopped"
    attention = (projection.get("attention") or {}).get("categories", [])
    activity = (projection.get("stage") or {}).get("activity")
    if {"input", "approval"}.intersection(attention) or activity == "waiting" or worker.get("operatorQuestion"):
        return "needs you"
    return "working" if activity == "working" else "worker state unavailable"


def branch_heads(tickets):
    heads = {}
    for name in {t["repo"] for t in tickets if t["branch"]}:
        try:
            refs = subprocess.run(["git", "ls-remote", "--heads", f"https://github.com/julianthant/{name}.git", "refs/heads/task/*"],
                                  check=True, capture_output=True, text=True, timeout=4).stdout
            heads[name] = {ref.removeprefix("refs/heads/"): sha
                           for sha, ref in (line.split() for line in refs.splitlines())}
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise tick.OrcaError(f"roadmap {name} task heads: {e}") from e
    return heads


def now_rows(snap, state):
    tickets = [t for t in tick.tickets(rows=snap["open"]) if t["kind"] in ("Task", "Bug")]
    reviews, workers = live_workers({t["number"] for t in tickets})
    heads = state.get("heads", {})
    pipeline = state["pipeline"]
    pr_groups = {"hr-relay": pipeline.get("devPrs", []), "dot-agents": pipeline.get("agentPrs", [])}
    rows = {}
    for t in tickets:
        n, repo, branch = t["number"], t["repo"], t["branch"]
        review = reviews.get(n, {})
        reviewed, phase = review.get("sha"), review.get("stage")
        pr = next((p for p in pr_groups[repo] if n in p["issues"] or p["head"] == branch), None)
        coder, reviewer = workers.get((n, "coder")), workers.get((n, "reviewer"))
        head = heads.get(repo, {}).get(branch)
        if not (pr or review or coder or reviewer or head):
            continue

        active = lambda w: w and w.get("dispatchStatus") == "dispatched"
        named = ("reviewer", "coder") if phase == "Reviewing" else ("coder", "reviewer")
        candidates = [(role, workers[n, role]) for role in named if (n, role) in workers]
        current = [pair for pair in candidates if active(pair[1])] or candidates[:1]
        who = []
        for role, worker in current:
            projection = worker.get("projection") or {}
            host = tick.worker_host(worker)
            who.append(dict(role=role, host={"local": "Mac", "Juzzark-WSL": "WSL"}.get(host, host),
                            model=(projection.get("provider") or {}).get("model") or (review.get("model") if role == "coder" else None)))
        statuses = [worker_status(worker) for _, worker in current]
        status = "needs you" if phase == "Needs you" or "needs you" in statuses else next(iter(statuses), "no worker")
        warning = status in ("stopped", "worker state unavailable")

        run = state.get("task_runs", {}).get(branch) if repo == "hr-relay" else None
        run = run if head and run and run["head_sha"] == head else None
        ci_complete = run is not None and run["status"] == "completed"
        ci_failed = ci_complete and run["conclusion"] != "success"
        pr_failed = pr is not None and pr["checks"] == "failing"
        if pr:
            stage = 3
            if pr_failed:
                warning = True
                status = "PR checks failing" if status in ("finished", "no worker") else status + " · PR checks failing"
            elif status in ("finished", "no worker"):
                status = "PR checks running" if pr["checks"] == "running" else "PR checks green"
        elif head and head == reviewed and phase == "Merging":
            stage = 3
            if status in ("finished", "no worker"):
                status = "waiting for PR"
        elif ci_complete and not ci_failed:
            stage = 0 if phase == "Fixing" and reviewed == head else 2
            if stage == 2 and status in ("finished", "no worker"):
                status = "waiting for review"
        elif head and not (phase == "Fixing" and reviewed == head and status == "working"):
            stage = 1
            if ci_failed:
                warning = True
                status = "CI failed" if status in ("finished", "no worker") else status + " · CI failed"
            elif status in ("finished", "no worker"):
                status = "CI running" if run else "waiting for CI"
        else:
            stage = 0
        if t["blockers"] and all(worker_status(worker) in ("stopped", "finished") for _, worker in candidates):
            status = "parked on " + ", ".join(f"#{blocker}" for blocker in t["blockers"])
            warning = ci_failed or pr_failed
            if ci_failed:
                status += " · CI failed"
            if pr_failed:
                status += " · PR checks failing"
        rows[n] = dict(stage=stage, status=status, who=who, warning=warning, pr={**pr, "repo": repo} if pr else None)
    return rows


def area_names(meta):
    return [o["name"] for o in meta.get("fields", {}).get("Area", {}).get("options", [])]


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build(snap, state, live):
    issues = sorted(snap["open"], key=lambda n: n["updatedAt"], reverse=True) + snap["closed"]
    rows = [row(n) for n in issues]
    decisions = state.get("readiness", {})
    for r in rows:
        r["now"] = live.get(r["n"])
        if r["open"] and r["type"] in ("Task", "Bug"):
            reason = decisions.get(str(r["n"]), "readiness unavailable")
            r.update(ready=reason is None, holdReason=reason)
    areas = state.get("areas")
    if areas is None:
        areas = area_names(read_json(tick.PROJECT_CACHE, {}))
    last_tick = read_json(tick.SNAPSHOT.with_name("tick.json"), None)
    return {"generated": stamp(), "githubFetchedAt": state.get("githubFetchedAt"), "areas": areas,
            "milestones": state.get("milestones", []), "issues": rows, "pipeline": state["pipeline"], "tick": last_tick}


def cached_rest(cache, endpoint, select):
    """Use the saved value when GitHub confirms its ETag is current."""
    etag, value = cache.get(endpoint, (None, None))
    status, tag, body = poll(endpoint, etag)
    if status == 200:
        value = select(json.loads(body))
        cache[endpoint] = tag, value
    elif status != 304 or not etag:
        raise ValueError(f"GitHub REST request returned {status}: {endpoint}")
    return value


def pipeline(rest, prs, full_run):
    """The release pipeline and open milestones from a manual refresh."""
    def api(path, select):
        return cached_rest(rest, f"repos/{REPO}/{path}", select)

    release = api("releases?per_page=1", lambda rows: {"tagName": rows[0]["tag_name"],
                                                     "publishedAt": rows[0]["published_at"]} if rows else None)
    if release is None:
        main_date = api("commits/main", lambda result: result.get("commit", {}).get("committer", {}).get("date"))
        release = {"tagName": None, "publishedAt": main_date}
    since = (release["publishedAt"] or "2026-01-01")[:10]
    merged = gh("api", "-X", "GET", "search/issues", "-f", "per_page=1", "-f",
                f"q=repo:{REPO} is:pr is:merged base:development merged:>{since}")
    cuts = api("git/matching-refs/heads/cut/", lambda rows: [r["ref"].removeprefix("refs/heads/") for r in rows])
    stages = dict(**prs, mergedSinceRelease=merged["total_count"],
                  devAheadOfStaging=api("compare/staging...development?per_page=1", lambda result: result["ahead_by"]),
                  devAheadOfMain=api("compare/main...development?per_page=1", lambda result: result["ahead_by"]),
                  release=release, cuts=cuts,
                  fullRun=dict(status=full_run["status"], conclusion=full_run["conclusion"],
                               createdAt=full_run["created_at"], headSha=full_run["head_sha"],
                               url=full_run["html_url"]) if full_run else None)
    return {"pipeline": stages, "milestones": api("milestones?state=open&per_page=50", lambda rows: [m["title"] for m in rows])}


def publish(full, directory=None):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = render(full)
        if directory is None:
            json.dump(data, sys.stdout, separators=(",", ":"))
        else:
            write_json(directory / "data.json", data)


def make_server(directory, bind, port):
    refresh_lock = Lock()
    completed = float("-inf")
    status = 204

    class Handler(SimpleHTTPRequestHandler):
        def do_POST(self):
            nonlocal completed, status
            if self.path != "/refresh":
                self.send_error(404)
                return
            # Cross-origin browser requests cannot send this header without a CORS preflight.
            if self.headers.get("X-Roadmap-Refresh") != "1":
                self.send_error(403)
                return
            if not refresh_lock.acquire(blocking=False):
                self.send_error(409)
                return
            try:
                if time.monotonic() - completed >= 2:
                    try:
                        publish(True, directory)
                    except REFRESH_ERRORS as e:
                        self.log_error("roadmap refresh: %s", e)
                        status = 503
                    else:
                        status = 204
                    completed = time.monotonic()
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            finally:
                refresh_lock.release()

    return ThreadingHTTPServer((bind, port), partial(Handler, directory=directory))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("local", "full", "serve"), default="local")
    parser.add_argument("--directory", type=Path, help="serve this directory or write data.json here")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if args.mode == "serve":
        if args.directory is None:
            parser.error("serve requires --directory")
        with make_server(args.directory, args.bind, args.port) as server:
            server.serve_forever()
    else:
        publish(args.mode == "full", args.directory)


if __name__ == "__main__":
    main()
