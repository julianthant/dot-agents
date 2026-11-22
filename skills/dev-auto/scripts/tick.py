"""Headless Orca coordinator stub for dev-auto: no terminal of its own, run by launchd.

  tick.py fill --repo-path <checkout> [--issue N] [--dry-run]
  tick.py start --repo id:<repo-id> --state <file>   process 1: scratch worker started, its ask answered
  tick.py resume --state <file>                      process 2: rebinds the stored Run, finishes, cleans up

See references/orca-supervisor.md.

Coordinator verbs refuse a sender with no stable Orca pane, so the script opens a plain shell pane
(its anchor) and passes it as --from; the Run's mailbox is run:<id>, so mail outlives that pane.
"""

from pathlib import Path
import argparse
import sqlite3
import tempfile
import tomllib
import json
import os
import re
import subprocess
import sys
import time

ORCA = os.environ.get("ORCA_CLI_COMMAND") or "orca"
# An agent's own limit message opens its line (from the retired dev_auto.py tick): Claude's
# "You've hit your 5-hour limit" and "⚠ Usage limit reached ·", Codex's "You’ve hit your usage
# limit" and "Goal hit usage limits", Cursor's "Weekly usage limit reached.".
LIMIT = re.compile(
    r"(?:\w+: )?[\s⎿■⚠️]*(?:\*(?=\w))?(?:(?:you['’]ve |you have )?hit your (?:\S+ )?limit"
    r"|(?:weekly )?usage limit reached(?: again)?(?= ?[·.*]|$)|goal hit usage limits\b)",
    re.IGNORECASE,
)
# A line that opens Claude's or Codex's next output; a limit line above one is history.
OUTPUT = ("⏺", "•")
SILENT_MINUTES = 30


class OrcaError(RuntimeError):
    pass


def orca(*args, ok=True):
    # Headless: launchd gives no ORCA_* pane variables, so strip ours to prove the same path.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCA_") or k == "ORCA_CLI_COMMAND"}
    try:
        out = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, env=env, timeout=900)
        data = json.loads(out.stdout)
        if not isinstance(data, dict):
            raise ValueError(data)
    except subprocess.TimeoutExpired:
        data = {"ok": False, "error": "timed out"}
    except ValueError:
        data = {"ok": False, "error": f"invalid JSON, exit {out.returncode}: {out.stderr.strip()[:200]}"}
    else:
        if out.returncode and data.get("ok") is True:
            data = {**data, "ok": False, "error": f"exit {out.returncode}"}
    if ok and data.get("ok") is not True:
        raise OrcaError(f"orca {' '.join(args[:2])}: {data.get('error')}")
    return data


def limited(tail):
    """The screen's newest limit line, unless output follows it."""
    hit = None
    for line in tail:
        if LIMIT.match(line):
            hit = line.strip()
        elif hit and line.lstrip().startswith(OUTPUT):
            hit = None
    return hit


def silent(show, now, minutes=SILENT_MINUTES):
    """A working worker whose terminal printed nothing for `minutes`."""
    stage = show["projection"]["stage"]
    last = (show.get("terminal") or {}).get("lastOutputAt")
    return stage.get("dispatch") != "completed" and last is not None and now * 1000 - last > minutes * 60000


def watch(dispatch, now):
    """What the worker's screen says: limit, human-only prompt, silence, or nothing."""
    show = orca("orchestration", "worker-show", "--dispatch", dispatch, "--full")["result"]
    term = show.get("terminal") or {}
    if term.get("agentWait"):
        return "waiting", term["agentWait"]
    tail = orca("terminal", "read", "--terminal", show["dispatch"]["assigneeHandle"], "--screen")
    hit = limited(tail["result"]["terminal"]["tail"])
    if hit:
        return "limit", hit
    if silent(show, now):
        return "silent", term["lastOutputAt"]
    return None, None




# A release that proves the worker's terminal is closed; anything else may leave it live.
RELEASED = ("released", "already_released")


def anchor(worktree):
    t = orca("terminal", "create", "--worktree", worktree, "--title", "dev-auto supervisor")
    return t["result"]["terminal"]["handle"]


def mail(me, run, kind, minutes=5):
    """Consume the Run's mail until a `kind` message; a restarted script resumes where it left off."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        out = orca("orchestration", "check", "--terminal", me, "--run", run, "--wait",
                   "--timeout-ms", str(int((deadline - time.time()) * 1000)))
        for m in out["result"]["messages"]:
            if m["type"] == kind:
                return m
            log("other mail", {"type": m["type"], "id": m["id"]})
    raise OrcaError(f"no {kind} for {run} in {minutes} minutes")


def completion(m, run, dispatch):
    """A worker_done that succeeded for this Run's expected Dispatch; anything else raises."""
    p = m.get("payload") or {}
    if p.get("dispatchId") != dispatch or p.get("outcome") != "succeeded":
        raise OrcaError(f"rejected worker_done {m.get('id')}: {p.get('dispatchId')} {p.get('outcome')}, expected {dispatch} succeeded")
    owner = orca("orchestration", "worker-show", "--dispatch", dispatch)["result"].get("runId")
    if owner != run:
        raise OrcaError(f"rejected worker_done {m.get('id')}: Dispatch {dispatch} belongs to {owner}, not {run}")
    return m


def close_anchor(st):
    """Close the anchor; its handle is kept until Orca confirms the pane was killed."""
    r = orca("terminal", "close", "--terminal", st["anchor"], ok=False)
    if r.get("ok") is True and ((r.get("result") or {}).get("close") or {}).get("ptyKilled") is True:
        st.pop("anchor")
        return None
    return f"anchor close not confirmed: {r.get('error') or r.get('result')}"


def git(st, *args):
    out = subprocess.run(["git", "--git-dir", st["git"], *args], capture_output=True, text=True)
    if out.returncode:
        raise OrcaError(f"git {' '.join(args[:2])} exit {out.returncode}: {out.stderr.strip()[:200]}")
    return out.stdout.splitlines()


def leftovers(st):
    """The scratch checkout or branch git still lists after removal; raises when git can't say."""
    trees = [line[9:] for line in git(st, "worktree", "list", "--porcelain") if line.startswith("worktree ")]
    if not trees:
        raise OrcaError("unreadable git worktree list output")
    path = os.path.realpath(st["path"])
    left = [f"worktree {st['path']}" for t in trees if os.path.realpath(t) == path]
    if os.path.exists(st["path"]):
        left.append(f"directory {st['path']}")
    # show-ref --exists: 0 present, 2 missing, anything else (a broken ref, no repo) is unknown.
    ref = f"refs/heads/{st['branch']}"
    out = subprocess.run(["git", "--git-dir", st["git"], "show-ref", "--exists", ref], capture_output=True, text=True)
    if out.returncode == 0:
        left.append(f"branch {st['branch']}")
    elif out.returncode != 2:
        raise OrcaError(f"git show-ref {ref} exit {out.returncode}: {out.stderr.strip()[:200]}")
    return left


def log(step, value):
    print(f"{step:<12} {json.dumps(value, ensure_ascii=False)}", flush=True)


def save(path, st):
    with open(path, "w") as f:
        json.dump(st, f)


def cleanup(st):
    """Release the worker, then drop the anchor and scratch checkout; [] when all three are proven.
    The checkout and branch go only after a confirmed release, or when no start was attempted."""
    errs = []
    if st.get("followup"):
        errs.append("a follow-up was sent but its receipt is unresolved; its worker may be live")
    if st.get("dispatch"):
        r = orca("orchestration", "worker-release", "--dispatch", st["dispatch"], ok=False)
        state = (r.get("result") or {}).get("state")
        log("release", r.get("result") or r.get("error"))
        if r.get("ok") is not True or state not in RELEASED:
            errs.append(f"release of {st['dispatch']} not confirmed: {state or r.get('error')}")
    elif st.get("started"):
        errs.append("worker-start was attempted but returned no dispatch; its worker may be live")
    if st.get("anchor"):
        errs += filter(None, [close_anchor(st)])
    if errs:
        return [*errs, f"kept scratch worktree {st['worktree']}"]
    r = orca("worktree", "rm", "--worktree", st["worktree"], "--force", "--delete-branch", ok=False)
    log("cleanup", r.get("result") or r.get("error"))
    if r.get("ok") is not True or (r.get("result") or {}).get("removed") is not True:
        return [f"worktree rm not confirmed: {r.get('error') or r.get('result')}"]
    try:
        left = leftovers(st)
    except OrcaError as e:
        return [f"absence not verified: {e}"]
    log("absent", {"worktree": not any(x.startswith(("worktree", "directory")) for x in left),
                   "branch": f"branch {st['branch']}" not in left})
    return [f"still present after rm: {x}" for x in left]


def finish(st, failure=None):
    """Clean up; a proof failure is re-raised as is, and a cleanup failure exits 1."""
    try:
        errs = cleanup(st) if st.get("worktree") else []
    except Exception as e:  # a crash in cleanup must not hide `failure`
        errs = [f"cleanup crashed: {e!r}", f"kept scratch worktree {st['worktree']}"]
    for e in errs:
        print(f"cleanup: {e}", file=sys.stderr)
    if failure:
        raise failure
    if errs:
        sys.exit(1)


SPEC = (
    "Scratch lifecycle probe; no real work. Edit nothing tracked, push nothing, use no network.\n"
    '1. Run `orca orchestration ask` with the question "PROOF-ASK: what is the codeword?" and end your turn.\n'
    "2. When the answer arrives, write it with `printf %s <codeword> > proof-answer.txt` here "
    "(an inert scratch file; it is authorized).\n"
    "3. Send worker_done --outcome succeeded quoting the codeword, then idle."
)


def start(repo, path):
    """Process 1: worker started and its ask answered; exits before the worker reports."""
    st = {"pid": os.getpid()}
    try:
        name = f"scratch-supervisor-proof-{int(time.time())}"
        wt = orca("worktree", "create", "--repo", repo, "--name", name, "--branch", f"scratch/{name}",
                  "--base-branch", "origin/main", "--setup", "skip", "--no-parent",
                  "--comment", "throwaway dev-auto proof; removed by tick.py")["result"]["worktree"]
        st.update(worktree=f"path:{wt['path']}", path=wt["path"], branch=f"scratch/{name}")
        st["git"] = subprocess.run(["git", "-C", wt["path"], "rev-parse", "--path-format=absolute",
                                    "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip()
        log("worktree", name)
        st["anchor"] = anchor(st["worktree"])
        st["run"] = orca("orchestration", "run-create", "--objective", f"scratch: {name}",
                         "--from", st["anchor"])["result"]["runId"]
        save(path, st)
        log("run", {"run": st["run"], "pid": st["pid"]})
        st["started"] = True
        save(path, st)
        r = orca("orchestration", "worker-start", "--environment", "local", "--from", st["anchor"],
                 "--run", st["run"], "--spec", SPEC, "--task-title", "scratch probe worker",
                 "--worktree", st["worktree"], "--agent", "claude", "--model", "claude-haiku-5-5",
                 "--effort", "low")["result"]
        st["dispatch"] = r["dispatchId"]
        save(path, st)
        log("start", {k: r[k] for k in ("dispatchId", "state", "agent", "model")})
        ask = mail(st["anchor"], st["run"], "question")
        log("ask", ask["body"])
        orca("orchestration", "reply", "--from", st["anchor"], "--run", st["run"], "--id", ask["id"],
             "--body", "PELICAN-549")
        log("reply", "PELICAN-549")
        err = close_anchor(st)
        save(path, st)
        if err:
            raise OrcaError(err)
        log("exit", {"pid": st["pid"], "anchor": "closed", "state": "saved"})
    except BaseException as e:
        finish(st, e)


def resume(path):
    """Process 2: from the stored Run id alone, a new anchor reads the worker_done process 1 missed."""
    with open(path) as f:
        st = json.load(f)
    try:
        os.kill(st["pid"], 0)
        sys.exit(f"process {st['pid']} is still alive; resume only after it exits")
    except ProcessLookupError:
        pass
    try:
        st["anchor"] = anchor(st["worktree"])
        orca("orchestration", "run-use", "--id", st["run"], "--from", st["anchor"])
        peek = orca("orchestration", "check", "--terminal", st["anchor"], "--run", st["run"], "--peek")
        log("resume", {"pid": os.getpid(), "previous": st["pid"], "previous_alive": False,
                       "run": st["run"], "peek": [m["type"] for m in peek["result"]["messages"]]})
        done = completion(mail(st["anchor"], st["run"], "worker_done"), st["run"], st["dispatch"])
        log("done", {"dispatch": st["dispatch"], "outcome": done["payload"]["outcome"], "body": done["body"][:120]})
        log("watch", watch(st["dispatch"], time.time()))

        st["followup"] = True
        fu = orca("orchestration", "send", "--from", st["anchor"], "--run", st["run"],
                  "--to", f"dispatch:{st['dispatch']}", "--subject", "PROOF-FOLLOWUP",
                  "--body", "Run the inert, authorized `touch proof-perm.txt` here, then send worker_done "
                            "--outcome succeeded saying whether a permission prompt appeared.")["result"]
        if fu.get("delivery") != "follow_up" or not fu.get("dispatchId"):
            raise OrcaError(f"follow-up receipt unresolved: {fu}")
        st["dispatch"] = fu["dispatchId"]
        del st["followup"]
        log("follow-up", {k: fu[k] for k in ("delivery", "dispatchId", "state")})
        done = completion(mail(st["anchor"], st["run"], "worker_done"), st["run"], st["dispatch"])
        log("done", {"dispatch": done["payload"]["dispatchId"], "outcome": done["payload"]["outcome"],
                     "body": done["body"][:160]})
        # Simulated guards only: no prompt is pending, since workers follow Orca's new-tab setting.
        for verb in ("deny", "approve"):
            err = orca("orchestration", verb, "--from", st["anchor"], "--dispatch", st["dispatch"],
                       "--prompt", "appr_none", "--reason", "scratch proof", ok=False).get("error") or {}
            log(f"{verb} guard", err.get("code"))
        log("inbox", [m["type"] for m in orca("orchestration", "inbox", "--terminal", f"run:{st['run']}",
                                                "--limit", "20")["result"]["messages"]])
    except BaseException as e:
        finish(st, e)
    finish(st)


PROJECT = 5
OWNER = "julianthant"
HERE = Path(__file__).resolve().parent

# Nested connections must be complete before their absence can establish readiness.
QUERY = """
query($endCursor:String) {
  repository(owner:"julianthant",name:"hr-relay") {
    issues(first:50,states:OPEN,after:$endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body issueType {name} milestone {title}
        assignees {totalCount} subIssues {totalCount} closedByPullRequestsReferences {totalCount}
        labels(first:100){totalCount nodes{name}}
        blockedBy(first:100){totalCount nodes{state}}
        blocking(first:100){totalCount nodes{state}}
        projectItems(first:20){totalCount nodes{id project{number owner{... on User{login} ... on Organization{login}}}
          status:fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}
          agent:fieldValueByName(name:"Agent"){... on ProjectV2ItemFieldTextValue{text}}
          stage:fieldValueByName(name:"Agent state"){... on ProjectV2ItemFieldSingleSelectValue{name}}
        }}
      }
    }
  }
}
"""


class StartNotApplied(OrcaError):
    pass


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=900 if "worker-start" in args else 90)
    if result.returncode:
        if args[1:3] == ("orchestration", "worker-start"):
            try:
                reply = json.loads(result.stdout)
            except ValueError:
                reply = {}
            error = reply.get("error") if isinstance(reply, dict) and reply.get("ok") is False else None
            data = error.get("data") if isinstance(error, dict) else None
            if isinstance(data, dict):
                recovery = data.get("recovery") or {}
                if data.get("effectsApplied") is False or recovery.get("disposition") == "not_sent":
                    raise StartNotApplied(str(error.get("message")))
                if recovery.get("orchestrationRequestId"):
                    raise OrcaError(f"inspect Orca request {recovery['orchestrationRequestId']}")
        raise OrcaError(f"{args[0]} {args[1]} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def nodes(connection):
    if len(connection["nodes"]) != connection["totalCount"]:
        raise ValueError("incomplete GitHub connection")
    return connection["nodes"]


def section(body, name):
    found = re.search(r"(?im)^(?:- )?" + name + r":[ \t]*(.*)$", body)
    if found:
        return found[1].strip()
    found = re.search(r"(?ims)^## " + name + r"\s*\n(.*?)(?:\n## |\Z)", body)
    return found[1].strip() if found else ""


def owned_paths(body):
    value = section(body, "Owned paths")
    value = re.split(r"(?i)\b(?:do not touch|keep [^.]* out of|exclude)\b", value, maxsplit=1)[0]
    paths = []
    token = r"[\w.*?/-]+"
    for part in re.split(r"[,;\n]", value):
        whole = re.sub(r"\([^()]*\)", "", part).strip().rstrip(".").strip()
        for match in re.finditer(r"`([^`]+)`|(?<![\w/.-])(" + token + r")", part):
            path = (match[1] or match[2]).rstrip(".")
            if (match[1] or whole == path or "/" in path or "." in path) and re.fullmatch(token, path):
                if not path.startswith("/") and ".." not in path and path:
                    paths.append(path)
    # These ownership descriptions in hr-relay use directory names and named config files.
    if "dot-agents" in section(body, "Branch"):
        return list(dict.fromkeys(paths)) or ["*"]
    for phrase, path in ((r"backend storage", "src/backend/storage"), (r"tests?", "tests"),
                         (r"documentation", "docs"), (r"workflows", ".github/workflows"),
                         (r"CI plan", "scripts/test/ci-plan.ts"), (r"smoke config", "playwright.smoke.config.ts")):
        if re.search(r"(?<![\w/.-])" + phrase + r"(?![\w/.-])", value, re.I):
            paths.append(path)
    return list(dict.fromkeys(paths)) or ["*"]


def coder_route(body):
    route = re.split(r"\s*(?:·|[.]?\s*Reviewer:|[.]?\s*Tier:)", section(body, "Coder"), maxsplit=1,
                     flags=re.I)[0].lower().strip(" .")
    route = re.sub(r"[(),/]", " ", route).replace("effort ", "")
    route = re.sub(r"\b(sonnet|opus|haiku|fable) (\d+)\.(\d+)\b", r"claude-\1-\2-\3", route)
    route = " ".join(route.split())
    m = re.fullmatch(r"(claude|codex) ([a-z0-9.-]+) (low|medium|high|xhigh|max|ultra)(?: (?:with |\+ ?)?(opus) advisor)?", route)
    if m and m[2].startswith("claude-" if m[1] == "claude" else "gpt-") and (not m[4] or m[1] == "claude"):
        return m.groups()


def tickets():
    pages = json.loads(command("gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + QUERY))
    result = []
    for page in pages:
        for i in page["data"]["repository"]["issues"]["nodes"]:
            card = next((c for c in nodes(i["projectItems"]) if c["project"]["number"] == PROJECT
                         and c["project"]["owner"]["login"] == OWNER), None)
            body = i["body"]
            branch_section = section(body, "Branch")
            m = re.search(r"\btask/([\w-]+)/([\w-]+)", branch_section)
            branch = m[0] if m else None
            slug = re.match(r"^([\w-]+) / ([\w-]+)(?:\s*:|$)", i["title"])
            if not branch and slug:
                branch = f"task/{slug[1]}/{slug[2]}"
            dot = "dot-agents" in branch_section
            card = card or {}
            stage = (card.get("stage") or {}).get("name")
            if dot:
                base = "origin/main"
            elif "development" in branch_section and "feature/" not in branch_section:
                base = "origin/development"
            else:
                base = "origin/feature/" + branch.split("/")[1] if branch else None
            result.append({
                "number": i["number"], "title": i["title"], "item": card.get("id"),
                "status": (card.get("status") or {}).get("name"), "kind": (i["issueType"] or {}).get("name"),
                "assigned": bool(i["assignees"]["totalCount"] or (card.get("agent") or {}).get("text")),
                "busy": bool(i["closedByPullRequestsReferences"]["totalCount"] or stage not in (None, "Queued")),
                "later": (i["milestone"] or {}).get("title", "").lower() == "later"
                         or any(label["name"].lower() == "later" for label in nodes(i["labels"])),
                "blocked": bool(i["subIssues"]["totalCount"] or any(b["state"] == "OPEN" for b in nodes(i["blockedBy"]))),
                "score": sum(b["state"] == "OPEN" for b in nodes(i["blocking"])),
                "route": coder_route(body), "paths": owned_paths(body), "branch": branch, "base": base,
                "repo": "dot-agents" if dot else "hr-relay",
            })
    return result


def overlap(left, right):
    # A glob's fixed directory owns the whole subtree for scheduling purposes.
    def root(path):
        prefix = re.split(r"[*?\[{]", path, maxsplit=1)[0]
        if prefix != path:
            prefix = prefix.rpartition("/")[0]
        return os.path.normpath(prefix).strip("/").casefold() if prefix not in ("", ".", "./") else ""

    return any(not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/")
               or ("/" not in a and a in b.split("/")) or ("/" not in b and b in a.split("/"))
               for a in map(root, left) for b in map(root, right))


def fill_plan(tickets, seat, issue=None):
    busy = [t for t in tickets if t["busy"] or t["assigned"] or t["status"] == "In Progress"]
    held = []
    for t in sorted(tickets, key=lambda t: (-t["score"], t["number"])):
        if issue and t["number"] != issue:
            continue
        reasons = list(t.get("skip", []))
        for condition, reason in ((t["status"] != "Todo", "status is not Todo"), (t["kind"] != "Task", "not a Task"),
                                  (t["later"], "later"), (t["blocked"], "open blocker or subtasks"),
                                  (t["assigned"], "assigned"), (t["busy"], "active work"),
                                  (not t["route"], "missing coder route"), (not t["branch"], "missing task branch")):
            if condition:
                reasons.append(reason)
        for b in busy:
            if b["number"] != t["number"] and b["repo"] == t["repo"] and (
                    b["branch"] == t["branch"] or overlap(b["paths"], t["paths"])):
                reasons.append(f"overlaps #{b['number']} ({', '.join(b['paths'])})")
        if reasons:
            log("skip", {"issue": t["number"], "reason": reasons})
            continue
        account, why = seat(t["route"], held)
        if not account:
            log("no seat", {"issue": t["number"], "reason": why or ["no eligible account"]})
            continue
        held.append(dict(account=account["id"], agent=t["route"][0], model=t["route"][1], effort=t["route"][2]))
        busy.append(t)
        yield t, account


def runtime(*args):
    # Production calls inherit the caller's Orca identity, including its worker restrictions.
    response = json.loads(command(ORCA, *args, "--environment", "local", "--json"))
    if response.get("ok") is not True:
        raise OrcaError(str(response.get("error")))
    return response["result"]


def reserve(db, number, receipt):
    db.execute("CREATE TABLE IF NOT EXISTS starts (issue INTEGER PRIMARY KEY, receipt TEXT)")
    try:
        with db:
            db.execute("INSERT INTO starts VALUES (?, ?)", (number, json.dumps(receipt)))
    except sqlite3.IntegrityError as e:
        raise OrcaError(f"issue #{number} already reserved") from e


def project_fields():
    args = (str(PROJECT), "--owner", OWNER, "--format", "json")
    project = json.loads(command("gh", "project", "view", *args))["id"]
    rows = json.loads(command("gh", "project", "field-list", *args))["fields"]
    fields = {f["name"]: f for f in rows}
    edits = []
    for key, option in (("Status", "In Progress"), ("Agent state", "Coding"), ("Agent", None)):
        f = fields[key]
        value = next(o["id"] for o in f["options"] if o["name"] == option) if option else None
        edits.append([f["id"], value])
    stage = fields["Agent state"]
    needs_you = next(o["id"] for o in stage["options"] if o["name"] == "Needs you")
    return project, edits, (stage["id"], needs_you)


def record_hold(rec):
    tier = rec["route"]
    import pick
    with pick.edit_holds(Path(rec["holds"])) as holds:
        holds[rec["terminal"]] = dict(account=rec["account"], label=rec["label"], agent=tier[0],
                                      model=tier[1], effort=tier[2], host="local", at=rec["time"])


def record_start(db, number, rec):
    record_hold(rec)
    tier = rec["route"]
    agent = f"coder {tier[1]} {tier[2]}" + ("+advisor" if tier[3] else "") + " @" + rec["label"]
    for field, value in rec["fields"]:
        command("gh", "project", "item-edit", "--project-id", rec["project"], "--id", rec["item"],
                "--field-id", field, *(("--single-select-option-id", value) if value else ("--text", agent)))
    with db:
        db.execute("UPDATE starts SET receipt='done' WHERE issue=?", (number,))


def inventory(repo_path, issues, repo):
    trees = runtime("worktree", "list", "--repo", "path:" + str(repo_path), "--limit", "10000")
    terms = runtime("terminal", "list", "--limit", "10000")
    if trees["truncated"] or terms["truncated"]:
        raise OrcaError("incomplete Orca inventory")
    live = {term["worktreeId"] for term in terms["terminals"]}
    by_branch = {t["branch"]: t for t in issues if t["repo"] == repo and t["branch"]}
    for tree in trees["worktrees"]:
        branch = tree["branch"]
        if branch in by_branch:
            # Existing task branches may contain user work; only live ones own other paths.
            by_branch[branch]["occupied"] = True
            if tree["id"] in live:
                by_branch[branch]["busy"] = True
        elif branch and branch.startswith("task/") and tree["id"] in live:
            raise OrcaError(f"live {branch} has no matching open issue with owned paths")
    return {t["handle"] for t in terms["terminals"]}


def fill_tick(args, db):
    import pick
    repo_path = args.repo_path.resolve()
    remote = command("git", "-C", str(repo_path), "remote", "get-url", "origin").strip()
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)julianthant/(hr-relay|dot-agents)(?:\.git)?", remote)
    if not m:
        raise OrcaError("target checkout must belong to julianthant/hr-relay or julianthant/dot-agents")
    repo = m[1]
    previous = {}
    if db and db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone():
        previous = dict(db.execute("SELECT issue,receipt FROM starts"))
        for n, receipt in previous.items():
            if receipt == "done":
                continue
            try:
                if receipt is None:
                    raise OrcaError("legacy reservation without a receipt")
                if args.dry_run:
                    raise OrcaError("pending reservation (dry-run)")
                rec = json.loads(receipt)
                if not isinstance(rec, dict):
                    raise ValueError("invalid reservation receipt")
                if "task" in rec:
                    actor, run = supervisor(args)
                    if rec["run"] != run:
                        raise OrcaError(f"reconcile from its owning Run {rec['run']}")
                    reconcile_start(db, n, rec, actor)
                else:
                    record_start(db, n, rec)
            except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                log("pending", {"issue": n, "reason": str(e)})
        previous = dict(db.execute("SELECT issue,receipt FROM starts"))

    pending = {}
    for number, receipt in previous.items():
        if receipt != "done":
            try:
                rec = json.loads(receipt or "{}")
                pending[number] = rec if isinstance(rec, dict) else {}
            except ValueError:
                pending[number] = {}

    def mark_pending(issues):
        for t in issues:
            if t["number"] in pending:
                t["busy"] = True
        return issues

    issues = mark_pending([t for t in tickets() if t["repo"] == repo])
    live = inventory(repo_path, issues, repo)
    refs = set(command("git", "-C", str(repo_path), "for-each-ref", "--format=%(refname:short)",
                       "refs/heads", "refs/remotes/origin").splitlines())
    remote_refs = command("git", "-C", str(repo_path), "ls-remote", "--heads", "origin")
    heads = {line.split()[1] for line in remote_refs.splitlines()}
    for t in issues:
        t["skip"] = []
        if t.get("occupied"):
            t["skip"].append("existing worktree")
        if t["number"] in previous:
            t["skip"].append("reserved or completed")
        if t["branch"] in refs or "origin/" + (t["branch"] or "") in refs or "refs/heads/" + (t["branch"] or "") in heads:
            t["skip"].append("task branch exists")
        if not t["base"] or "refs/heads/" + t["base"].removeprefix("origin/") not in heads:
            t["skip"].append("base missing on origin")
    cfg = pick.account_usage.load_config()
    holds_path = (args.holds or pick.HOLDS).resolve()

    def seat(route, extra, handles=live, reservations=pending.copy()):
        holds = json.loads(holds_path.read_text()) if holds_path.exists() else {}
        held = [h for handle, h in holds.items() if handle in handles and h["host"] == "local"]
        for rec in reservations.values():
            if rec.get("route") and rec.get("account") and not (rec.get("terminal") in handles and rec["terminal"] in holds):
                agent, model, effort = rec["route"][:3]
                held.append(dict(account=rec["account"], agent=agent, model=model, effort=effort))
        # pick.live_holds prunes the real file; use the read-only snapshot for planning.
        return pick.pick(cfg, *route[:3], "local", time.time(), holds=held + extra)

    count = 0
    for ticket, acct in fill_plan(issues, seat, args.issue):
        route, n = ticket["route"], ticket["number"]
        log("would start" if args.dry_run else "start", dict(issue=n, repo=repo, branch=ticket["branch"],
            base=ticket["base"], agent=route[0], model=route[1], effort=route[2], advisor=route[3], account=acct["label"]))
        if not args.dry_run:
            # Refresh GitHub, ownership and the picker immediately before committing to side effects.
            current = mark_pending([t for t in tickets() if t["repo"] == repo])
            latest = inventory(repo_path, current, repo)
            candidate = next(fill_plan(current, lambda r, extra: seat(r, extra, latest, pending), n), None)
            if not candidate or candidate[0].get("occupied") or candidate[1]["id"] != acct["id"] or any(
                candidate[0][k] != ticket[k] for k in ("branch", "base", "route", "paths", "item")):
                raise OrcaError(f"#{n} readiness or seat changed")
            ref = "refs/heads/" + ticket["base"].removeprefix("origin/")
            wanted = "refs/heads/" + ticket["branch"]
            command("git", "-C", str(repo_path), "fetch", "--no-tags", "origin",
                    ref + ":refs/remotes/" + ticket["base"])
            latest_refs = command("git", "-C", str(repo_path), "ls-remote", "--heads", "origin", ref, wanted)
            sha = command("git", "-C", str(repo_path), "rev-parse", "--verify", ticket["base"]).strip()
            if latest_refs.split() != [sha, ref]:
                raise OrcaError(f"#{n} base changed or task branch already exists on origin")
            try:
                launch(db, repo_path, dict(ticket, sha=sha), acct, args, holds_path)
            except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                if not db or not db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone():
                    raise
                row = db.execute("SELECT receipt FROM starts WHERE issue=?", (n,)).fetchone()
                if not row or row[0] == "done":
                    raise
                pending[n] = json.loads(row[0])
                log("pending", {"issue": n, "reason": str(e)})
                continue
        count += 1
    if not count:
        log("fill", "no eligible start")


def supervisor(args):
    actor = []
    if args.anchor or args.run:
        pane = any(k.startswith(("ORCA_AGENT_", "ORCA_TERMINAL_", "ORCA_PANE_", "ORCA_WORKTREE_",
                                 "ORCA_WORKSPACE_", "ORCA_TAB_", "ORCA_RUNTIME_SOURCE_")) for k in os.environ)
        if not args.anchor or not args.run or pane:
            raise OrcaError("an explicit supervisor anchor and Run are for a pane-less invocation only")
        current = runtime("orchestration", "run-current", "--from", args.anchor)
        if current["runId"] != args.run:
            raise OrcaError("supervisor anchor is not bound to the configured Run")
        actor = ["--from", args.anchor, "--run", args.run]
    else:
        if not os.environ.get("ORCA_TERMINAL_HANDLE"):
            raise OrcaError("launch from the coordinating Run, or configure its launchd anchor and Run")
        current = runtime("orchestration", "run-current")
        if not current.get("runId"):
            raise OrcaError("launch requires a coordinating Run")
    return actor, current["runId"]


def configure_codex_notice(path):
    config = path / ".codex/config.toml"
    if config.parent.is_symlink() or config.is_symlink():
        raise OrcaError("Codex config points outside its worktree")
    old = config.read_text() if config.exists() else ""
    expected = tomllib.loads(old)
    key = "hide_rate_limit_model_nudge"
    notice = expected.setdefault("notice", {})
    if notice.get(key) is True:
        return
    notice[key] = True
    # Accept an edit only if it changes exactly one parsed setting.
    dotted = "notice." + key + " = true"
    candidates = [
        re.sub(r"(?m)^(\s*(?:notice\.)?hide_rate_limit_model_nudge\s*=\s*)false\b", r"\g<1>true", old),
        re.sub(r"(?m)^(\s*\[notice\][^\n]*)$", r"\1\n" + key + " = true", old),
        re.sub(r"(\bhide_rate_limit_model_nudge\s*=\s*)false\b", r"\g<1>true", old),
        re.sub(r"(?m)^(\s*notice\s*=\s*\{)([^}]*)(\})",
               lambda m: m[1] + key + " = true" + (", " if m[2].strip() else "") + m[2] + m[3], old),
        dotted + "\n" + old,
    ]
    for content in candidates:
        try:
            if tomllib.loads(content) != expected:
                continue
        except tomllib.TOMLDecodeError:
            continue
        config.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=config.parent, delete=False) as f:
            f.write(content)
            if config.exists():
                os.fchmod(f.fileno(), config.stat().st_mode & 0o777)
        os.replace(f.name, config)
        return
    log("Codex notice", "preserved existing config layout; forward any model-switch prompt")


def launch(db, repo_path, t, acct, args, holds_path):
    project, fields, waiting_field = project_fields()
    number, route = t["number"], t["route"]
    spec = (HERE.parent / "references/coder-spec.md").read_text().format(**t)
    actor, run = supervisor(args)
    if db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone() and db.execute(
            "SELECT 1 FROM starts WHERE issue=?", (number,)).fetchone():
        raise OrcaError(f"issue #{number} already reserved")
    branch = t["branch"]
    wt = runtime("worktree", "create", "--repo", "path:" + str(repo_path), "--name", branch[5:].replace("/", "."),
                 "--branch", branch, "--base-branch", t["base"], "--no-parent", "--setup", "skip")["worktree"]
    path = Path(wt["path"])
    if wt["branch"] != branch or command("git", "-C", str(path), "branch", "--show-current").strip() != branch:
        raise OrcaError("created worktree does not own the requested branch")
    if command("git", "-C", str(path), "rev-parse", "HEAD").strip() != t["sha"]:
        raise OrcaError("created worktree has a different base commit")
    if route[0] == "codex":
        configure_codex_notice(path)
    if route[3]:
        settings = path / ".claude/settings.local.json"
        if settings.parent.is_symlink() or settings.is_symlink():
            raise OrcaError("advisor setting points outside the worktree")
        settings.parent.mkdir(exist_ok=True)
        config = json.loads(settings.read_text()) if settings.exists() else {}
        config["advisorModel"] = route[3]
        settings.write_text(json.dumps(config) + "\n")
    runtime("account", "select", "--agent", route[0], "--account", acct["id"])
    task = runtime("orchestration", "task-create", *actor, "--task-title", f"#{number} coder",
                   "--spec", spec, "--full")["task"]["id"]
    selected = runtime("account", "list")[route[0]]["activeAccountId"]
    if selected != acct["id"]:
        raise OrcaError(f"selected account changed before worker start; inspect prepared Task {task} and worktree {path}")
    rec = dict(task=task, run=run, worktree=wt["id"], account=acct["id"], label=acct["label"],
               route=route, project=project, item=t["item"], fields=fields, time=int(time.time()),
               holds=str(holds_path), waiting_field=waiting_field)
    # The fill lock covers preparation. A durable Task identity precedes the only start call.
    reserve(db, number, rec)
    try:
        started = runtime("orchestration", "worker-start", *actor, "--task", task, "--worktree", "path:" + str(path),
                          "--agent", route[0], "--model", route[1], "--effort", route[2], "--keep-open", "--full")
        if started.get("dispatchId"):
            rec["dispatch"] = started["dispatchId"]
            terminal = started.get("terminal") or started.get("agentTerminalHandle")
            rec["terminal"] = terminal.get("handle") if isinstance(terminal, dict) else terminal
            with db:
                db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
    except StartNotApplied as e:
        with db:
            db.execute("DELETE FROM starts WHERE issue=?", (number,))
        raise OrcaError(f"#{number}: Orca confirmed no start effects; inspect prepared worktree {path}: {e}") from e
    except (OrcaError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        log("start receipt unavailable", {"issue": number, "task": task, "reason": str(e)})
    reconcile_start(db, number, rec, actor)


def codex_prompt(tail):
    for index in reversed(range(len(tail))):
        if re.match(r"^\s*(?:hooks need review\b|switch model\?|approaching rate limits\s*$)", tail[index], re.I):
            choices = []
            for line in tail[index + 1:]:
                if re.match(r"^\s*[›❯>]?\s*\d+[.)] ", line):
                    choices.append(line)
                elif line.strip():
                    break
            if tail[index].strip().lower() != "approaching rate limits" or choices:
                return "\n".join([tail[index], *choices])
    return None


def reconcile_start(db, number, rec, actor):
    task = rec["task"]
    try:
        lookup = runtime("orchestration", "dispatch-show", "--task", task, "--full")
        dispatch_id = (lookup.get("dispatch") or {}).get("id")
        if not dispatch_id:
            raise OrcaError("no Dispatch is visible")
        show = runtime("orchestration", "worker-show", "--dispatch", dispatch_id, "--full")
    except (OrcaError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise OrcaError(f"#{number} reserved: inspect Task {task} in Run {rec['run']}: {e}") from e
    dispatch = show["dispatch"]
    worker = show.get("worker") or {}
    if (dispatch["id"] != dispatch_id or dispatch["taskId"] != task or dispatch["runId"] != rec["run"]
            or worker.get("worktreeId") != rec["worktree"]):
        raise OrcaError(f"#{number} reserved: unverified worker for Task {task}")
    terminal = dispatch.get("assigneeHandle")
    if dispatch.get("retryOfDispatchId") or (rec.get("dispatch") and rec["dispatch"] != dispatch_id):
        raise OrcaError(f"#{number} reserved: Task {task} has a different attempt; inspect {dispatch_id}")
    if rec.get("terminal") and rec["terminal"] != terminal:
        raise OrcaError(f"#{number} reserved: assignee changed for Dispatch {dispatch_id}")
    resource = (show.get("projection") or {}).get("resource") or {}
    released_failure = worker["state"] == "failed" and resource.get("terminalState") == "released"
    if released_failure or dispatch.get("status") == "completed":
        if terminal and (show.get("terminal") or {}).get("handle") == terminal and not released_failure:
            record_hold(dict(rec, terminal=terminal))
        log("already settled", {"issue": number, "task": task, "dispatch": dispatch_id, "state": worker["state"]})
        with db:
            db.execute("UPDATE starts SET receipt='done' WHERE issue=?", (number,))
        return
    if not terminal:
        raise OrcaError(f"#{number} reserved: inspect {dispatch_id}, worker state {worker['state']}")
    rec = dict(rec, terminal=terminal, dispatch=dispatch_id)
    if rec["route"][0] == "codex":
        observed_terminal = show.get("terminal") or {}
        observation = show.get("observation") or {}
        wait = observation.get("agentWait") or observed_terminal.get("agentWait")
        prompt = None
        if observed_terminal:
            if observed_terminal.get("handle") != terminal:
                raise OrcaError(f"#{number} reserved: worker terminal identity changed")
            tail = runtime("terminal", "read", "--terminal", terminal, "--screen")["terminal"]["tail"]
            prompt = codex_prompt(tail)
        if not observed_terminal and "agentWait" not in observation:
            raise OrcaError(f"#{number} reserved: no Codex startup observation for {dispatch_id}")
        if prompt or wait:
            record_hold(rec)
            notice = {"issue": number, "task": task, "dispatch": dispatch_id, "prompt": prompt or wait}
            if rec.get("notice") != notice:
                runtime("orchestration", "send", *actor, "--to", "run:" + rec["run"], "--type", "status",
                        "--subject", f"#{number} waiting for operator input", "--body", json.dumps(notice, ensure_ascii=False))
                rec.update(notice=notice, waiting_card=False)
                with db:
                    db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
            if not rec.get("waiting_card"):
                field, option = rec.get("waiting_field") or project_fields()[2]
                command("gh", "project", "item-edit", "--project-id", rec["project"], "--id", rec["item"],
                        "--field-id", field, "--single-select-option-id", option)
                with db:
                    db.execute("UPDATE starts SET receipt=? WHERE issue=?",
                               (json.dumps(dict(rec, waiting_card=True)), number))
            log("waiting for operator input", notice)
            return
    if worker["state"] != "ready":
        raise OrcaError(f"#{number} reserved: inspect {dispatch_id}, worker state {worker['state']}")
    record_start(db, number, rec)
    log("started", {"issue": number, "dispatch": dispatch_id})


def fill(args):
    import fcntl
    state = Path.home() / ".local/state/dev-auto"
    path = state / "starts.sqlite"
    if args.dry_run:
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                return fill_tick(args, db)
        return fill_tick(args, None)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "fill.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if getattr(args, "release", None) is not None:
                raise OrcaError("another tick is active; retry release after it finishes")
            log("fill", "another tick is active")
            return
        with sqlite3.connect(path) as db:
            if getattr(args, "release", None) is not None:
                deleted = 0
                if db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone():
                    with db:
                        deleted = db.execute("DELETE FROM starts WHERE issue=? AND receipt IS NOT 'done'",
                                             (args.release,)).rowcount
                log("released" if deleted else "no pending reservation", {"issue": args.release})
                return
            fill_tick(args, db)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--repo", required=True)
    s.add_argument("--state", required=True)
    sub.add_parser("resume").add_argument("--state", required=True)
    f = sub.add_parser("fill")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--repo-path", type=Path, help="registered target repository checkout")
    f.add_argument("--release", type=int, help="release one pending reservation after inspecting its Task, then exit")
    f.add_argument("--issue", type=int, help="restrict the tick to one issue")
    f.add_argument("--holds", type=Path, help="existing fleet picker's holds.json when testing from a worktree")
    f.add_argument("--anchor", help="pane-less supervisor anchor from #549")
    f.add_argument("--run", help="Run already bound to that anchor")
    args = ap.parse_args(argv)
    if args.cmd == "fill":
        if args.release is None and not args.repo_path:
            f.error("fill requires --repo-path unless releasing a reservation")
        if args.release is not None and (args.release <= 0 or args.dry_run or args.issue is not None):
            f.error("--release requires a positive issue number and cannot combine with --issue or --dry-run")
        try:
            return fill(args)
        except (OSError, OrcaError, ValueError, subprocess.TimeoutExpired) as e:
            ap.exit(1, f"fill: {e}\n")
    start(args.repo, args.state) if args.cmd == "start" else resume(args.state)


if __name__ == "__main__":
    main()
