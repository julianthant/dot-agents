"""Print hr-relay work as roadmap JSON for the roadmap app: Area and milestone order, every open issue
and the newest closed ones, and the release pipeline.

  roadmap.py > data.json   (launchd runs it every 15 seconds)

Issues come from tick.py's shared snapshot. Each rebuild polls issues and Development runs conditionally,
reads PR check rollups in one batch and joins current workers to the tick's local review records.
"""
import datetime
from argparse import Namespace
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
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
    return json.loads(subprocess.run(["gh", *a], check=True, capture_output=True, text=True).stdout)


def ghq(*a):
    try:
        return gh(*a)
    except Exception:
        return None


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
    result = ghq("api", "graphql", "-f", f"query={PR_QUERY}")
    if not result or result.get("errors"):
        return None

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


def last_snapshot(fresh=False):
    """The shared snapshot, or its last good copy when GitHub refuses the refetch."""
    try:
        return tick.snapshot(fresh)
    except REFUSED:
        return json.loads(tick.SNAPSHOT.read_text())


STATE = tick.SNAPSHOT.with_name("roadmap.json")
POLLS = {"issues": f"repos/{REPO}/issues?state=all&sort=updated&direction=desc&per_page=100&since={{since}}",
         "runs": f"repos/{REPO}/actions/workflows/{tick.FULL_RUN_WORKFLOW}/runs?per_page=100"}


def poll(url, etag):
    """(status, etag, body) of a GET sent with If-None-Match."""
    r = subprocess.run(["gh", "api", "-i", *(["-H", f"If-None-Match: {etag}"] if etag else []), url],
                       capture_output=True, text=True, timeout=60)
    head, _, body = r.stdout.replace("\r\n", "\n").partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d+)", head)
    tag = re.search(r"(?im)^etag:\s*(.+)$", head)
    return int(status[1]) if status else 0, tag[1].strip() if tag else etag, body


ANCHOR_SECONDS = 600  # the issue poll's since= moves this rarely, so its ETag stays valid in between


def changes(state):
    """Changes to issues, and full and task runs from the same Actions response.

    Issues compare (number, updated_at) after since=, so two updates in the same second still differ, and a
    moved since= overlaps the old window by a minute, so its first unconditional reply compares cleanly too."""
    tags = state.setdefault("etags", {})
    if "since" not in state or not 0 <= time.time() - state.get("anchored", 0) < ANCHOR_SECONDS:
        state["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        state["anchored"], tags["issues"] = time.time(), None
    since = state["since"]
    changed, full_run = set(), None
    for name, url in POLLS.items():
        try:
            status, tag, body = poll(url.format(since=since), tags.get(name))
            if status != 200:
                continue
            if name == "issues":
                seen = sorted([i["number"], i["updated_at"]] for i in json.loads(body) if i["updated_at"] > since)
                before = [s for s in state["seen"] if s[1] > since] if "seen" in state else None
                state["seen"] = seen
                if seen != before:
                    changed.add(name)
            else:
                runs = json.loads(body)["workflow_runs"]
                latest = {}
                for r in runs:
                    if r["event"] == "push" and r["head_branch"].startswith("task/"):
                        latest.setdefault(r["head_branch"], {k: r[k] for k in ("head_sha", "status", "conclusion")})
                state["task_runs"] = {**state.get("task_runs", {}), **latest}
                run = next((r for r in runs if r["head_branch"] == tick.FULL_RUN_BRANCH
                            and r["event"] in ("schedule", "workflow_dispatch")), None)
                if run:
                    full_run = dict(status=run["status"], conclusion=run["conclusion"], createdAt=run["created_at"],
                                    headSha=run["head_sha"], url=run["html_url"])
            tags[name] = tag
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            pass  # an unreadable poll counts as unchanged; the once-a-minute refresh still runs
    return changed, full_run


def render():
    """This run's roadmap, reading GitHub beyond the polls only for what changed, or once a minute."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    changed, full_run = changes(state)
    prs = pull_requests()
    if prs is not None:
        # Check-only changes update each row without rebuilding the release counters.
        old = state.get("pipeline", {})
        identity = lambda rows: [(p["n"], p.get("updated")) for p in rows]
        if any(identity(prs.get(k, [])) != identity(old.get(k, [])) for k in ("devPrs", "stagingPr")):
            changed.add("pulls")
    now = time.time()
    fresh = "issues" in changed or state.get("refresh_owed", False)  # a refused refresh is retried, alone
    try:
        snap, state["refresh_owed"] = tick.snapshot(fresh), False  # unchanged, it still refetches once a minute
    except REFUSED:
        snap, state["refresh_owed"] = json.loads(tick.SNAPSHOT.read_text()), fresh
    if changed or "pipeline" not in state or not 0 <= now - state.get("built", 0) < tick.SNAPSHOT_SECONDS:
        state.update(pipeline(state), built=now)
    if prs is not None:
        state["pipeline"].update(prs)
    if full_run is not None:
        state["pipeline"] = {**state["pipeline"], "fullRun": full_run}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)
    try:
        decisions = readiness(snap)
    except REFUSED as e:
        print(f"roadmap readiness: {e}", file=sys.stderr)
        decisions = {}
    return build(snap, state, decisions, now_rows(snap, state))


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

    try:
        workers = {w["dispatchId"]: w for w in tick.fleet_workers(RUN)}
    except REFUSED as e:
        print(f"roadmap workers: {e}", file=sys.stderr)
        return reviews, {}

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
    try:
        tasks = {t["id"]: t for t in tick.runtime("orchestration", "task-list", "--run", RUN, "--full")["tasks"]}
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
    except REFUSED as e:
        print(f"roadmap tasks: {e}", file=sys.stderr)

    for n, s in reviews.items():
        for role, dispatch in (("coder", s.get("coder")), ("reviewer", (s.get("reviewer") or {}).get("dispatch"))):
            w = workers.get(dispatch)
            if w and (w.get("dispatchStatus") == "dispatched" or (n, role) not in current):
                current[n, role] = w

    active = {f"dispatch:{w['dispatchId']}": w for w in current.values() if w.get("dispatchStatus") == "dispatched"}
    if active:
        try:
            mail = tick.runtime("orchestration", "inbox", "--run", RUN, "--limit", "500", "--full")["messages"]
            replies = {(m.get("thread_id"), m.get("to_handle")) for m in mail if m["type"] == "status"}
            for m in mail:
                sender = m.get("from_handle")
                if m["type"] == "question" and sender in active and (m.get("thread_id"), sender) not in replies:
                    active[sender]["operatorQuestion"] = True
        except REFUSED as e:
            print(f"roadmap worker questions: {e}", file=sys.stderr)
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
            for line in refs.splitlines():
                sha, ref = line.split()
                heads[name, ref.removeprefix("refs/heads/")] = sha
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"roadmap {name} task heads: {e}", file=sys.stderr)
    return heads


def now_rows(snap, state):
    tickets = [t for t in tick.tickets(rows=snap["open"]) if t["kind"] in ("Task", "Bug")]
    reviews, workers = live_workers({t["number"] for t in tickets})
    heads = branch_heads(tickets)
    pipeline = state["pipeline"]
    pr_groups = {"hr-relay": pipeline.get("devPrs", []), "dot-agents": pipeline.get("agentPrs", [])}
    rows = {}
    for t in tickets:
        n, repo, branch = t["number"], t["repo"], t["branch"]
        review = reviews.get(n, {})
        reviewed, phase = review.get("sha"), review.get("stage")
        pr = next((p for p in pr_groups[repo] if n in p["issues"] or p["head"] == branch), None)
        coder, reviewer = workers.get((n, "coder")), workers.get((n, "reviewer"))
        head = heads.get((repo, branch))
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
        if pr:
            stage = 3
            if pr["checks"] == "failing":
                warning = True
                status = "PR checks failing" if status in ("finished", "no worker") else status + " · PR checks failing"
            elif status in ("finished", "no worker"):
                status = "PR checks running" if pr["checks"] == "running" else "PR checks green"
        elif head and head == reviewed and phase == "Merging":
            stage = 3
            if status in ("finished", "no worker"):
                status = "waiting for PR"
        elif head and run and run["head_sha"] == head and run["status"] == "completed" and run["conclusion"] == "success":
            stage = 0 if phase == "Fixing" and reviewed == head else 2
            if stage == 2 and status in ("finished", "no worker"):
                status = "waiting for review"
        elif head and not (phase == "Fixing" and reviewed == head and status == "working"):
            stage = 1
            if run and run["head_sha"] == head and run["status"] == "completed":
                warning = True
                status = "CI failed" if status in ("finished", "no worker") else status + " · CI failed"
            elif status in ("finished", "no worker"):
                status = "CI running" if run and run["head_sha"] == head else "waiting for CI"
        else:
            stage = 0
        rows[n] = dict(stage=stage, status=status, who=who, warning=warning, pr={**pr, "repo": repo} if pr else None)
    return rows


def build(snap, github=None, decisions=None, live=None):
    issues = sorted(snap["open"], key=lambda n: n["updatedAt"], reverse=True) + snap["closed"]
    rows = [row(n) for n in issues]
    for r in rows:
        if live is not None:
            r["now"] = live.get(r["n"])
        if decisions is not None and r["open"] and r["type"] in ("Task", "Bug"):
            reason = decisions.get(r["n"], "readiness unavailable")
            r.update(ready=reason is None, holdReason=reason)
    areas = lambda meta: [o["name"] for o in meta["fields"]["Area"]["options"]]
    area_order = []
    try:
        area_order = tick.with_project(areas)
        if {r["area"] for r in rows} - {None, *area_order}:
            area_order = areas(tick.project_meta(refresh=True))  # an Area option added since the ids were cached
    except REFUSED:
        pass  # GitHub refused the lookup (quota 0): the cache stays, and this render keeps what it read
    try:
        last_tick = json.loads(tick.SNAPSHOT.with_name("tick.json").read_text())
    except (OSError, ValueError):
        last_tick = None
    github = github or pipeline()
    return {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(), "areas": area_order,
            "milestones": github["milestones"], "issues": rows, "pipeline": github["pipeline"], "tick": last_tick}


def cached_rest(cache, endpoint, select, default=None):
    """Keep the ETag and the fields the roadmap needs from the last successful response."""
    etag, value = cache.get(endpoint, (None, default))
    try:
        status, tag, body = poll(endpoint, etag)
        if status == 200:
            value = select(json.loads(body))
            cache[endpoint] = tag, value
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    return value


def pipeline(state=None):
    """The release pipeline and the open milestones, read from GitHub."""
    previous = state.get("pipeline", {}) if state is not None else pull_requests() or {}
    def api(path, select, default=None):
        endpoint = f"repos/{REPO}/{path}"
        if state is not None:
            return cached_rest(state.setdefault("rest", {}), endpoint, select, default)
        result = ghq("api", endpoint)
        return select(result) if result is not None else default

    def compare(base):
        return api(f"compare/{base}...development" + ("?per_page=1" if state is not None else ""),
                   lambda result: result.get("ahead_by"))

    if state is None:
        releases = ghq("release", "list", "--repo", REPO, "--limit", "1", "--json", "tagName,publishedAt") or []
        full = ghq("run", "list", "--repo", REPO, "--branch", "development", "--event", "schedule", "--limit", "5",
                   "--json", "conclusion,status,createdAt,headSha,url") or []
        release, full_run = next(iter(releases), None), next(iter(full), None)
    else:
        release = api("releases?per_page=1", lambda rows: {"tagName": rows[0]["tag_name"],
                                                         "publishedAt": rows[0]["published_at"]} if rows else None)
        full_run = state.get("pipeline", {}).get("fullRun")
    if release is None:
        main_date = api("commits/main", lambda result: result.get("commit", {}).get("committer", {}).get("date"))
        release = {"tagName": None, "publishedAt": main_date}
    since = release["publishedAt"]
    # One REST search count instead of listing every merged PR over GraphQL.
    merged = ghq("api", "-X", "GET", "search/issues", "-f", "per_page=1", "-f",
                 f"q=repo:{REPO} is:pr is:merged base:development merged:>{(since or '2026-01-01')[:10]}")
    cuts = api("git/matching-refs/heads/cut/", lambda rows: [r["ref"].removeprefix("refs/heads/") for r in rows], [])
    stages = dict(devPrs=previous.get("devPrs", []), stagingPr=previous.get("stagingPr", []),
                    mergedSinceRelease=(merged or {}).get("total_count", 0),
                    devAheadOfStaging=compare("staging"), devAheadOfMain=compare("main"),
                    release=release, cuts=cuts, fullRun=full_run)
    if state is not None:
        stages["agentPrs"] = previous.get("agentPrs", [])
    else:
        stages["stagingPr"] = [{k: p[k] for k in ("n", "head", "checks")} for p in stages["stagingPr"]]
    milestones = api("milestones?state=open&per_page=50", lambda rows: [m["title"] for m in rows], [])
    return {"pipeline": stages, "milestones": milestones}


if __name__ == "__main__":
    json.dump(render(), sys.stdout, separators=(",", ":"))
