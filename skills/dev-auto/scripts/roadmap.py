"""Print hr-relay work as roadmap JSON for the roadmap app: Area and milestone order, every open issue
and the newest closed ones, and the release pipeline.

  roadmap.py > data.json   (launchd runs it every 15 seconds)

Issues come from tick.py's shared snapshot, which the tick also writes its own card edits into. Each run polls
issues, PRs and workflow runs with REST conditional requests (a 304 costs no quota); only a change, or a minute
passing, refetches the snapshot or the pipeline.
"""
import datetime
import json
import re
import subprocess
import sys
import time

import tick

REPO = "julianthant/hr-relay"
PRF = "number,title,headRefName,autoMergeRequest,statusCheckRollup,isDraft,closingIssuesReferences,updatedAt"


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


def checks(roll):
    st = [(c.get("conclusion") or c.get("status") or "").upper() for c in roll or []]
    if any(x in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for x in st):
        return "failing"
    return "running" if any(x in ("IN_PROGRESS", "QUEUED", "PENDING", "") for x in st) else "green"


REFUSED = (tick.OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired)


def last_snapshot(fresh=False):
    """The shared snapshot, or its last good copy when GitHub refuses the refetch."""
    try:
        return tick.snapshot(fresh)
    except REFUSED:
        return json.loads(tick.SNAPSHOT.read_text())


STATE = tick.SNAPSHOT.with_name("roadmap.json")
POLLS = {"issues": f"repos/{REPO}/issues?state=all&sort=updated&direction=desc&per_page=100&since={{since}}",
         "pulls": f"repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=10",
         "runs": f"repos/{REPO}/actions/runs?per_page=10"}


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
    """The polled endpoints that changed since the last run.

    Issues compare (number, updated_at) after since=, so two updates in the same second still differ, and a
    moved since= overlaps the old window by a minute, so its first unconditional reply compares cleanly too."""
    tags = state.setdefault("etags", {})
    if "since" not in state or not 0 <= time.time() - state.get("anchored", 0) < ANCHOR_SECONDS:
        state["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        state["anchored"], tags["issues"] = time.time(), None
    since = state["since"]
    changed = set()
    for name, url in POLLS.items():
        try:
            status, tags[name], body = poll(url.format(since=since), tags.get(name))
            if status != 200:
                continue
            if name == "issues":
                seen = sorted([i["number"], i["updated_at"]] for i in json.loads(body) if i["updated_at"] > since)
                before = [s for s in state["seen"] if s[1] > since] if "seen" in state else None
                state["seen"] = seen
                if seen == before:
                    continue
            changed.add(name)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            pass  # an unreadable poll counts as unchanged; the once-a-minute refresh still runs
    return changed


def render():
    """This run's roadmap, reading GitHub beyond the polls only for what changed, or once a minute."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    changed, now = changes(state), time.time()
    fresh = "issues" in changed or state.get("refresh_owed", False)  # a refused refresh is retried, alone
    try:
        snap, state["refresh_owed"] = tick.snapshot(fresh), False  # unchanged, it still refetches once a minute
    except REFUSED:
        snap, state["refresh_owed"] = json.loads(tick.SNAPSHOT.read_text()), fresh
    if changed or "pipeline" not in state or not 0 <= now - state.get("built", 0) < tick.SNAPSHOT_SECONDS:
        state.update(pipeline(), built=now)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)
    return build(snap, state)


def build(snap, github=None):
    issues = sorted(snap["open"], key=lambda n: n["updatedAt"], reverse=True) + snap["closed"]
    rows = [row(n) for n in issues]
    areas = lambda meta: [o["name"] for o in meta["fields"]["Area"]["options"]]
    area_order = []
    try:
        area_order = tick.with_project(areas)
        if {r["area"] for r in rows} - {None, *area_order}:
            area_order = areas(tick.project_meta(refresh=True))  # an Area option added since the ids were cached
    except REFUSED:
        pass  # GitHub refused the lookup (quota 0): the cache stays, and this render keeps what it read
    github = github or pipeline()
    return {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(), "areas": area_order,
            "milestones": github["milestones"], "issues": rows, "pipeline": github["pipeline"]}


def pipeline():
    """The release pipeline and the open milestones, read from GitHub."""
    dev_prs = [dict(n=p["number"], t=p["title"], head=p["headRefName"], auto=p["autoMergeRequest"] is not None,
                    draft=p["isDraft"], checks=checks(p["statusCheckRollup"]),
                    issues=[i["number"] for i in p["closingIssuesReferences"]], updated=p["updatedAt"])
               for p in ghq("pr", "list", "--repo", REPO, "--base", "development", "--state", "open", "--limit", "50",
                            "--json", PRF) or []]
    stg = ghq("pr", "list", "--repo", REPO, "--base", "staging", "--state", "open", "--limit", "5", "--json", PRF) or []
    rel = ghq("release", "list", "--repo", REPO, "--limit", "1", "--json", "tagName,publishedAt") or []
    cmp = lambda a, b: (ghq("api", f"repos/{REPO}/compare/{a}...{b}") or {}).get("ahead_by")
    main_date = (ghq("api", f"repos/{REPO}/commits/main") or {}).get("commit", {}).get("committer", {}).get("date")
    since = rel[0]["publishedAt"] if rel else main_date
    # One REST search count instead of listing every merged PR over GraphQL.
    merged = ghq("api", "-X", "GET", "search/issues", "-f", "per_page=1", "-f",
                 f"q=repo:{REPO} is:pr is:merged base:development merged:>{(since or '2026-01-01')[:10]}")
    cuts = [r["ref"].split("refs/heads/", 1)[1] for r in (ghq("api", f"repos/{REPO}/git/matching-refs/heads/cut/") or [])]
    full = ghq("run", "list", "--repo", REPO, "--branch", "development", "--event", "schedule", "--limit", "5",
               "--json", "conclusion,status,createdAt,headSha,url") or []
    stages = dict(devPrs=dev_prs, mergedSinceRelease=(merged or {}).get("total_count", 0),
                    devAheadOfStaging=cmp("staging", "development"), devAheadOfMain=cmp("main", "development"),
                    stagingPr=[dict(n=p["number"], head=p["headRefName"], checks=checks(p["statusCheckRollup"])) for p in stg],
                    release=(rel[0] if rel else {"tagName": None, "publishedAt": main_date}), cuts=cuts,
                    fullRun=(full[0] if full else None))
    milestones = [m["title"] for m in ghq("api", f"repos/{REPO}/milestones?state=open&per_page=50") or []]
    return {"pipeline": stages, "milestones": milestones}


if __name__ == "__main__":
    json.dump(render(), sys.stdout, separators=(",", ":"))
