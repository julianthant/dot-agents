"""Serve the roadmap and the shared GitHub snapshot."""
from argparse import ArgumentParser, Namespace
import datetime
from contextlib import closing
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Event, Thread
from urllib.parse import parse_qsl, urlencode, urlsplit

import github_poller
import tick

REPO = "julianthant/hr-relay"
RUN = "run_c95eee175345"
PR_FIELDS = """
  nodes {
    number title headRefName headRefOid autoMergeRequest { enabledAt } isDraft updatedAt
    closingIssuesReferences(first: 20) { nodes { number repository { nameWithOwner } } }
    baseRef {
      refUpdateRule { requiredStatusCheckContexts }
      rules(first: 50) { nodes { parameters { ... on RequiredStatusChecksParameters {
        requiredStatusChecks { context }
      } } } }
    }
    commits(last: 1) { nodes { commit { statusCheckRollup {
      contexts(first: 100) { nodes {
        ... on CheckRun { name conclusion }
        ... on StatusContext { name: context state }
      } }
    } } } }
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
PROJECT_QUERY = """
query { user(login: "julianthant") { projectV2(number: 5) {
  id fields(first: 100) { nodes {
    ... on ProjectV2FieldCommon { id name }
    ... on ProjectV2SingleSelectField { options { id name } }
  } }
} } }
"""


def github_query(query):
    body = json.loads(subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                                     check=True, capture_output=True, text=True, timeout=15).stdout)
    if body.get("errors"):
        raise ValueError("GitHub GraphQL query failed")
    return body["data"]


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
    def pr(p):
        commit = p["commits"]["nodes"]
        rollup = (commit[0]["commit"]["statusCheckRollup"] or {}) if commit else {}
        base = p["baseRef"] or {}
        required = set((base.get("refUpdateRule") or {}).get("requiredStatusCheckContexts") or ())
        required.update(check["context"] for rule in base.get("rules", {}).get("nodes", [])
                        for check in (rule["parameters"] or {}).get("requiredStatusChecks", []))
        outcomes = {check["name"]: check.get("conclusion", check.get("state"))
                    for check in rollup.get("contexts", {}).get("nodes", []) if check and check["name"] in required}
        passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        checks = ("failing" if any(outcome not in {*passing, None, "PENDING", "EXPECTED"} for outcome in outcomes.values())
                  else "green" if all(outcomes.get(name) in passing for name in required) else "running")
        return dict(n=p["number"], t=p["title"], head=p["headRefName"], auto=p["autoMergeRequest"] is not None,
                    draft=p["isDraft"], checks=checks, updated=p["updatedAt"], sha=p["headRefOid"],
                    issues=[i["number"] for i in p["closingIssuesReferences"]["nodes"]
                            if i and i["repository"]["nameWithOwner"] == REPO])

    data = github_query(PR_QUERY)
    return {"devPrs": [pr(p) for p in data["relay"]["dev"]["nodes"]],
            "stagingPr": [pr(p) for p in data["relay"]["staging"]["nodes"]],
            "agentPrs": [pr(p) for p in data["agents"]["task"]["nodes"] if p["headRefName"].startswith("task/")]}


REFUSED = (tick.OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired)


STATE = tick.SNAPSHOT.with_name("roadmap.json")


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(path)


def render(directory=None, *, state=None):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        refreshed = state is not None
        state = state if refreshed else read_json(STATE, {})
        state = {"pipeline": {}, **state}
        snap = read_json(tick.SNAPSHOT, {"open": [], "closed": []})
        data = build(snap, state, now_rows(snap, state))
        if refreshed:
            write_json(STATE, state)
        if directory is not None:
            write_json(directory / "data.json", data)
        return data


def readiness(snap, heads):
    tickets = tick.tickets(rows=snap["open"])
    decisions = {}
    home = Path.home()
    for name, checkout in (("hr-relay", home / "Projects/hr-relay"), ("dot-agents", home / ".agents")):
        if not any(t["repo"] == name and t["kind"] in ("Task", "Bug") for t in tickets):
            continue
        args = Namespace(repo_path=checkout, dry_run=True, holds=None, issue=None)
        try:
            decisions.update(tick.fill(args, issues=tickets, heads=heads, emit=lambda *_: None))
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


def branch_heads():
    heads = {}
    for name in github_poller.REPOS:
        try:
            refs = subprocess.run(["git", "ls-remote", "--heads", f"https://github.com/julianthant/{name}.git"],
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

        run = state.get("task_runs", {}).get(repo, {}).get(branch)
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
    issues = sorted(snap["open"], key=lambda n: n["updatedAt"], reverse=True) + snap["closed"][:tick.CLOSED_KEPT]
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


def pipeline(poller):
    """Release data changes slowly and shares the REST budget with runs."""
    def api(path, select):
        return github_poller.conditional(poller.cache, f"repos/{REPO}/{path}", select, poller.rest.poll)

    release = api("releases?per_page=1", lambda rows: {"tagName": rows[0]["tag_name"],
                                                     "publishedAt": rows[0]["published_at"]} if rows else None)
    if release is None:
        main_date = api("commits/main", lambda result: result.get("commit", {}).get("committer", {}).get("date"))
        release = {"tagName": None, "publishedAt": main_date}
    since = (release["publishedAt"] or "2026-01-01")[:10]
    search = "search/issues?" + urlencode({"per_page": 1, "q": f"repo:{REPO} is:pr is:merged base:development merged:>{since}"})
    merged = github_poller.conditional(poller.cache, search, lambda value: value["total_count"], poller.rest.poll)
    stages = dict(mergedSinceRelease=merged,
                  devAheadOfStaging=api("compare/staging...development?per_page=1", lambda result: result["ahead_by"]),
                  devAheadOfMain=api("compare/main...development?per_page=1", lambda result: result["ahead_by"]),
                  release=release)
    return {"pipeline": stages, "milestones": api("milestones?state=open&per_page=50", lambda rows: [m["title"] for m in rows])}


def project():
    data = github_query(PROJECT_QUERY)["user"]["projectV2"]
    return {"id": data["id"], "fields": {field["name"]: field for field in data["fields"]["nodes"]}}


def poll_project(poller):
    meta = project()
    tick.PROJECT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    write_json(tick.PROJECT_CACHE, meta)
    poller.set("project", meta)


def poll_issues(poller):
    snap = tick.refresh_snapshot()
    poller.samples["issues"] = github_poller.Sample(snap, snap["fetched"])


def publish_board(poller, directory):
    snap = read_json(tick.SNAPSHOT, {"open": [], "closed": []})
    samples = poller.samples.copy()
    pages = poller.pages.copy()
    budget = poller.status()
    now = poller.clock()
    times = [sample.at for sample in (*samples.values(), *pages.values())] + [snap.get("fetched")]
    if any(at is None for at in times):
        return
    heads = samples["heads"].data
    latest = {}
    for repo, page in pages.items():
        workflow, event = tick.task_workflow(repo)
        latest[repo] = {}
        for run in page.data:
            if run["event"] == event and github_poller.workflow_matches(run, workflow):
                latest[repo].setdefault(run["head_branch"], run)
    full_run = next((r for r in pages["hr-relay"].data if r["head_branch"] == tick.FULL_RUN_BRANCH
                     and r["event"] in ("schedule", "workflow_dispatch")
                     and github_poller.workflow_matches(r, tick.FULL_RUN_WORKFLOW)), None)
    fields = samples["pipeline"].data
    data = {**fields, "heads": heads, "areas": area_names(samples["project"].data), "task_runs": latest,
            "pipeline": {**fields["pipeline"], **samples["prs"].data,
                         "cuts": sorted((name for name in heads["hr-relay"] if name.startswith("cut/")), reverse=True),
                         "fullRun": dict(status=full_run["status"], conclusion=full_run["conclusion"],
                                         createdAt=full_run["created_at"], headSha=full_run["head_sha"],
                                         url=full_run["html_url"]) if full_run else None},
            "githubFetchedAt": datetime.datetime.fromtimestamp(min(times), datetime.timezone.utc).isoformat()}
    if not any(source["stale"] for source in budget["sources"].values()) and now - min(times) <= github_poller.STALE_SECONDS:
        data["readiness"] = {str(number): reason for number, reason in readiness(snap, heads).items()}
    return render(directory, state=data)


def make_server(directory, bind, port, poller=None):
    poller = poller or github_poller.Poller()
    address = ip_address(bind)
    if address.version != 4 or not (address.is_loopback or address in ip_network("100.64.0.0/10")):
        raise ValueError("bind to loopback or the Mac's Tailscale IPv4 address")

    class Handler(SimpleHTTPRequestHandler):
        def log_request(self, code="-", size="-"):
            if str(code).startswith(("4", "5")):
                super().log_request(code, size)

        def send_json(self, body, code=200):
            payload = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path)
            source = path.path.strip("/")
            if source == "status":
                return self.send_json(poller.status())
            if source == "data.json":
                data = read_json(directory / "data.json", None)
                if data is None:
                    data = {"issues": [], "githubFetchedAt": None}
                return self.send_json({**data, "githubStatus": poller.status()})
            if source == "issues":
                snap = read_json(tick.SNAPSHOT, None)
                if snap is not None:
                    return self.send_json(github_poller.Sample(snap, snap["fetched"]).answer(poller.clock()))
            if source in ("runs", "jobs", "prs", "heads", "issues", "project"):
                try:
                    self.send_json(poller.read(source, dict(parse_qsl(path.query))))
                except ValueError as e:
                    self.send_json({"error": str(e)}, 400)
                except github_poller.Unavailable as e:
                    self.send_json({"error": str(e)}, 503)
                return
            super().do_GET()

    return ThreadingHTTPServer((bind, port), partial(Handler, directory=directory))


def serve(directory, bind, port):
    poller = github_poller.Poller()
    stop = Event()
    with make_server(directory, "127.0.0.1", port, poller) as loopback:
        tailnet = make_server(directory, bind, port, poller) if bind != "127.0.0.1" else None
        if tailnet:
            Thread(target=tailnet.serve_forever, daemon=True).start()
        poller.schedule(stop, 5, poller.poll_runs, rest=True)
        poller.schedule(stop, 5, poller.poll_details, rest=True)
        poller.schedule(stop, 10, lambda: poller.set("prs", pull_requests()))
        poller.schedule(stop, 10, lambda: poll_project(poller))
        poller.schedule(stop, 30, lambda: poll_issues(poller))
        poller.schedule(stop, 10, lambda: poller.set("heads", branch_heads()))
        poller.schedule(stop, 30, lambda: poller.set("pipeline", pipeline(poller)), rest=True)
        poller.schedule(stop, 5, lambda: publish_board(poller, directory))
        try:
            loopback.serve_forever()
        finally:
            stop.set()
            if tailnet:
                tailnet.shutdown()
                tailnet.server_close()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("local", "serve"), default="local")
    parser.add_argument("--directory", type=Path, help="serve this directory or write data.json here")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if args.mode == "serve":
        if args.directory is None:
            parser.error("serve requires --directory")
        serve(args.directory, args.bind, args.port)
    else:
        data = render(args.directory)
        if args.directory is None:
            json.dump(data, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
