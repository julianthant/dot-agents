"""Print hr-relay work as roadmap JSON for the roadmap app: Area and milestone order, every open issue
and the newest closed ones, and the release pipeline.

  roadmap.py > data.json

Issues come from tick.py's shared snapshot, so the rebuild and the tick read GitHub's issues once a minute.
"""
import datetime
import json
import subprocess
import sys

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


def last_snapshot():
    """The shared snapshot, or its last good copy when GitHub refuses the refetch."""
    try:
        return tick.snapshot()
    except REFUSED:
        return json.loads(tick.SNAPSHOT.read_text())


def build(snap):
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
    pipeline = dict(devPrs=dev_prs, mergedSinceRelease=(merged or {}).get("total_count", 0),
                    devAheadOfStaging=cmp("staging", "development"), devAheadOfMain=cmp("main", "development"),
                    stagingPr=[dict(n=p["number"], head=p["headRefName"], checks=checks(p["statusCheckRollup"])) for p in stg],
                    release=(rel[0] if rel else {"tagName": None, "publishedAt": main_date}), cuts=cuts,
                    fullRun=(full[0] if full else None))
    milestones = [m["title"] for m in ghq("api", f"repos/{REPO}/milestones?state=open&per_page=50") or []]
    return {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(), "areas": area_order,
            "milestones": milestones, "issues": rows, "pipeline": pipeline}


if __name__ == "__main__":
    json.dump(build(last_snapshot()), sys.stdout, separators=(",", ":"))
