"""Headless Orca coordinator stub for dev-auto: no terminal of its own, run by launchd.

  tick.py review [--issue N]                         advance reports from the owning Run mailbox
  tick.py close --repo-path <checkout> [--issue N]   release workers and clean up closed tickets
  tick.py watch                                      hand off, relaunch, nudge or report live workers
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
# Claude Code's own status lines under its limit line, never output (from the retired dev_auto.py tick).
LIMIT_STATUS = re.compile(r"[⏺●] (?:Goal paused · )?usage limit reached · ", re.IGNORECASE)
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
        elif hit and line.lstrip().startswith(OUTPUT) and not LIMIT_STATUS.match(line.lstrip()):
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


def in_coder_chain(dispatch, previous, run):
    seen = set()
    while dispatch and dispatch not in seen:
        if dispatch == previous:
            return True
        if not run:
            break
        seen.add(dispatch)
        attempt = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")["dispatch"]
        if attempt["id"] != dispatch or attempt["runId"] != run:
            break
        dispatch = attempt.get("followUpOf")
    return False


def followup_dispatch(receipt, previous, run=None):
    dispatch = receipt.get("dispatchId")
    if (receipt.get("delivery") not in ("follow_up", "reopened", "started_fresh") or not dispatch
            or dispatch == previous or dispatch == receipt.get("followUpOf")
            or not in_coder_chain(receipt.get("followUpOf"), previous, run)):
        raise OrcaError("incomplete coder follow-up receipt")
    return dispatch


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
        st["dispatch"] = followup_dispatch(fu, st["dispatch"], st["run"])
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
            if not branch:
                slug = re.fullmatch(r"([\w-]+)\s*/\s*([\w-]+(?:[ \t]+[\w-]+)*)(?:\s*:.*)?", i["title"].strip())
                if slug:
                    lane = "-".join(slug[2].lower().split())
                    branch = f"task/{slug[1].lower()}/{lane}"
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
                "number": i["number"], "title": i["title"], "item": card.get("id"), "body": body,
                "agent": (card.get("agent") or {}).get("text"), "stage": stage,
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
    # Orca account commands always operate on this host.
    target = () if args[0] == "account" else ("--environment", "local")
    response = json.loads(command(ORCA, *args, *target, "--json"))
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
    agents = {term["worktreeId"] for term in terms["terminals"] if term.get("agentIdentity")}
    repo_issues = [t for t in issues if t["repo"] == repo]
    for tree in trees["worktrees"]:
        branch = (tree["branch"] or "").removeprefix("refs/heads/")
        if not branch.startswith("task/"):
            continue
        # Only hr-relay worktrees link directly to the scheduling repository's issue numbers.
        linked = tree.get("linkedIssue") if repo == "hr-relay" else None
        matches = [t for t in repo_issues if t["branch"] == branch or t["number"] == linked]
        for t in matches:
            t["occupied"] = True
            if tree["id"] in agents:
                t["busy"] = True
        if not matches and tree["id"] in agents:
            log("skip", {"branch": branch, "reason": "live worktree has no matching open issue with owned paths"})
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
        return content
    log("Codex notice", "preserved existing config layout; forward any model-switch prompt")


def worker_settings(path, route):
    if route[0] == "codex":
        settings = path / ".codex/config.toml"
    elif route[3]:
        settings = path / ".claude/settings.local.json"
    else:
        return None
    if settings.parent.is_symlink() or settings.is_symlink():
        raise OrcaError("worker setting points outside the worktree")
    before = settings.read_bytes() if settings.exists() else None
    if route[0] == "codex":
        written = configure_codex_notice(path)
        if written is None:
            return None
    else:
        config = json.loads(before) if before is not None else {}
        config["advisorModel"] = route[3]
        written = json.dumps(config) + "\n"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(written)
    return settings, before, written.encode()


def remove_prelaunch_tree(repo, wt, existing_refs, settings_change):
    path = Path(wt["path"]).resolve()
    ref = "refs/heads/" + wt["branch"].removeprefix("refs/heads/")
    sha = wt.get("head")
    if not sha or ref in existing_refs or wt.get("isMainWorktree") or path == repo.resolve():
        raise OrcaError("unverified worktree ownership")
    trees = command("git", "-C", str(repo), "worktree", "list", "--porcelain", "-z").split("\0\0")
    expected = ["worktree " + str(path), "HEAD " + sha, "branch " + ref]
    if not any(tree.split("\0")[:3] == expected for tree in trees[1:]):
        raise OrcaError("checkout moved or has a different commit")
    if settings_change:
        settings, before, written = settings_change
        if settings.parent.is_symlink() or settings.is_symlink() or settings.read_bytes() != written:
            raise OrcaError("worker settings changed since preparation")
        if before is None:
            settings.unlink()
        else:
            settings.write_bytes(before)
    if command("git", "-C", str(path), "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise OrcaError("worktree contains local changes")
    removed = runtime("worktree", "rm", "--worktree", "id:" + wt["id"])
    if not removed.get("removed") or path.exists():
        raise OrcaError("worktree removal not confirmed")
    left = command("git", "-C", str(repo), "for-each-ref", "--format=%(objectname) %(worktreepath)", ref)
    if left:
        if left.rstrip("\n") != sha + " ":
            raise OrcaError("branch moved or is checked out elsewhere")
        command("git", "-C", str(repo), "update-ref", "-d", ref, sha)


def start_identity(receipt):
    terminals = [effect["id"] for effect in receipt.get("effects", [])
                 if effect.get("kind") == "terminal" and effect.get("role") == "agent" and effect.get("id")]
    if not receipt.get("dispatchId") or len(terminals) != 1:
        raise OrcaError("incomplete worker start receipt")
    return receipt["dispatchId"], terminals[0]


def launch(db, repo_path, t, acct, args, holds_path):
    project, fields, waiting_field = project_fields()
    number, route = t["number"], t["route"]
    spec = (HERE.parent / "references/coder-spec.md").read_text().format(**t)
    actor, run = supervisor(args)
    if db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone() and db.execute(
            "SELECT 1 FROM starts WHERE issue=?", (number,)).fetchone():
        raise OrcaError(f"issue #{number} already reserved")
    branch = t["branch"]
    existing_refs = set(command("git", "-C", str(repo_path), "for-each-ref", "--format=%(refname)", "refs/heads").splitlines())
    wt = runtime("worktree", "create", "--repo", "path:" + str(repo_path), "--name", branch[5:].replace("/", "."),
                 "--branch", branch, "--base-branch", t["base"], "--no-parent", "--setup", "skip")["worktree"]
    path = Path(wt["path"])
    settings_change = None
    try:
        actual_branch = command("git", "-C", str(path), "branch", "--show-current").strip()
        if wt["branch"].removeprefix("refs/heads/") != branch or actual_branch != branch:
            raise OrcaError("created worktree does not own the requested branch")
        if command("git", "-C", str(path), "rev-parse", "HEAD").strip() != t["sha"]:
            raise OrcaError("created worktree has a different base commit")
        settings_change = worker_settings(path, route)
        runtime("account", "select", "--agent", route[0], "--account", acct["id"])
        task = runtime("orchestration", "task-create", *actor, "--task-title", f"#{number} coder",
                       "--spec", spec, "--full")["task"]["id"]
        selected = runtime("account", "list")[route[0]]["activeAccountId"]
        if selected != acct["id"]:
            raise OrcaError(f"selected account changed before worker start; inspect prepared Task {task}")
        rec = dict(task=task, run=run, worktree=wt["id"], account=acct["id"], label=acct["label"],
                   route=route, project=project, item=t["item"], fields=fields, time=int(time.time()),
                   holds=str(holds_path), waiting_field=waiting_field)
        # The fill lock covers preparation. A durable Task identity precedes the only start call.
        reserve(db, number, rec)
    except Exception:
        try:
            remove_prelaunch_tree(repo_path, wt, existing_refs, settings_change)
        except (OSError, OrcaError, subprocess.TimeoutExpired) as cleanup_error:
            log("prelaunch cleanup", {"issue": number, "worktree": wt["path"], "reason": str(cleanup_error)})
        raise
    try:
        started = runtime("orchestration", "worker-start", *actor, "--task", task, "--worktree", "path:" + str(path),
                          "--agent", route[0], "--model", route[1], "--effort", route[2], "--keep-open", "--full")
        rec["dispatch"], rec["terminal"] = start_identity(started)
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


def review_route(body):
    match = re.search(r"(?im)\bReviewer:[ \t]*([^\n·]+)", body)
    value = re.split(r"(?i)\bTier:", match[1])[0].strip(" .") if match else ""
    if re.match(r"(?i)^none\b", value) and re.search(r"(?i)\bTier:\s*UI,?\s+simple\b", body):
        return None
    route = coder_route("Coder: " + value)
    if not route:
        raise OrcaError("issue needs a reviewer route or an explicit simple UI exemption")
    return route


def mail_payload(message):
    payload = message["payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def review_mail(messages, dispatch):
    reports = []
    for message in messages:
        if message["type"] != "worker_done":
            continue
        payload = mail_payload(message)
        if payload.get("dispatchId") == dispatch and payload.get("outcome") == "succeeded":
            reports.append(message)
    return sorted(reports, key=lambda m: m["sequence"])


def latest_coder_report(state, run, messages):
    reports = review_mail(messages, state["coder"])
    latest = reports[-1] if reports else {}
    terminal = latest.get("from_handle") or state.get("coder_terminal")
    if terminal:
        for message in sorted(messages, key=lambda m: m["sequence"], reverse=True):
            if latest and message["sequence"] <= latest["sequence"]:
                break
            if message["type"] != "worker_done" or message.get("from_handle") != terminal:
                continue
            payload = mail_payload(message)
            dispatch = payload.get("dispatchId")
            if payload.get("outcome") == "succeeded" and in_coder_chain(dispatch, state["coder"], run):
                state["coder"] = state["expected"] = dispatch
                return [message]
    return [latest] if latest else []


def green_report(ticket, body):
    repo = f"{OWNER}/{ticket['repo']}"
    branch = ticket["branch"]
    head = command("gh", "api", f"repos/{repo}/git/matching-refs/heads/{branch}", "--jq",
                   f'.[] | select(.ref == "refs/heads/{branch}") | .object.sha').strip()
    if head not in re.findall(r"(?i)\b[0-9a-f]{40}\b", body):
        return None
    dot = ticket["repo"] == "dot-agents"
    runs = json.loads(command("gh", "run", "list", "--repo", repo, "--branch", branch, "--commit", head,
                              "--workflow", "tests.yml" if dot else "development.yml",
                              "--event", "pull_request" if dot else "push", "--limit", "1",
                              "--json", "headSha,headBranch,status,conclusion"))
    if runs and runs[0] == {"headSha": head, "headBranch": branch, "status": "completed", "conclusion": "success"}:
        return head


def review_account(route):
    import pick
    account, reasons = pick.pick(pick.account_usage.load_config(), *route[:3], "local", time.time())
    if account is None:
        raise OrcaError("no reviewer seat: " + ", ".join(reasons))
    return account


def review_card(ticket, stage, agent):
    args = (str(PROJECT), "--owner", OWNER, "--format", "json")
    project = json.loads(command("gh", "project", "view", *args))["id"]
    fields = json.loads(command("gh", "project", "field-list", *args))["fields"]
    fields = {field["name"]: field for field in fields}
    options = {o["name"]: o["id"] for o in fields["Agent state"]["options"]}
    for name, flag, value in (("Agent", "--text", agent), ("Agent state", "--single-select-option-id", options[stage])):
        command("gh", "project", "item-edit", "--project-id", project, "--id", ticket["item"],
                "--field-id", fields[name]["id"], flag, value)


def review_state(db, run, number):
    db.execute("CREATE TABLE IF NOT EXISTS reviews (run TEXT, issue INTEGER, state TEXT, PRIMARY KEY(run,issue))")
    row = db.execute("SELECT state FROM reviews WHERE run=? AND issue=?", (run, number)).fetchone()
    return json.loads(row[0]) if row else None


def save_review(db, run, number, state):
    with db:
        db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(state)))


def reviewer_spec(ticket, sha, same_model):
    return (f"/goal Review {sha} and return its verdict.\nRole: follow ~/.agents/roles/reviewer.md.\n"
            f"Issue {OWNER}/hr-relay#{ticket['number']}. Branch {ticket['branch']}, base {ticket['base']}.\n"
            f"Review the issue's checks at {sha}."
            + (" The multi-step UI route explicitly assigns both roles to Sonnet with the Opus advisor."
               if same_model else ""))


def assign_reviewer(state, dispatch, terminal, hold):
    state["expected"] = dispatch
    state["reviewer"] = {"dispatch": dispatch, "terminal": terminal, "holds": hold["holds"]}
    route = hold["route"]
    advisor = "+advisor" if route[3] else ""
    state["card_agent"] = f"{state['agent']} · reviewer {route[1]} {route[2]}{advisor} @{hold['label']}"


def adopt_reviewer(db, run, ticket, state, messages):
    import pick
    number = ticket["number"]
    error = OrcaError(f"#{number}: inspect uncertain Orca action for message {state['pending']} before continuing")
    report = next((m for m in review_mail(messages, state["coder"]) if m["id"] == state["pending"]), None)
    if not report or not state.get("sha") or state["stage"] not in ("Coding", "Fixing") or not report.get("created_at"):
        raise error
    # Older reservations stored only the message ID. Match the original task spec and creation time.
    title = state.get("review_title", f"#{number} reviewer")
    tasks = runtime("orchestration", "task-list", "--run", run, "--full")["tasks"]
    route = review_route(ticket["body"])
    if not route:
        raise error
    spec = reviewer_spec(ticket, state["sha"], route[1] == state["model"])
    matches = [t for t in tasks if t["run_id"] == run and t["task_title"] == title
               and t["spec"] == spec
               and t["created_at"].replace(" ", "T")[:19] >= report["created_at"][:19]]
    if len(matches) != 1:
        raise error
    task = matches[0]["id"]
    dispatch = (runtime("orchestration", "dispatch-show", "--task", task, "--full").get("dispatch") or {}).get("id")
    if not dispatch:
        raise error
    show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
    attempt, worker = show["dispatch"], show["worker"]
    tree = runtime("worktree", "show", "--worktree", "id:" + worker["worktreeId"])["worktree"]
    effective = worker["startOptions"]["launch"]["effective"]
    terminal = attempt.get("assigneeHandle")
    if (not terminal or attempt["id"] != dispatch or attempt["runId"] != run or attempt["taskId"] != task
            or attempt.get("retryOfDispatchId") or attempt.get("followUpOf")
            or tree["id"] != worker["worktreeId"] or tree["path"] != state["path"]
            or tuple(effective.get(k) for k in ("agent", "model", "effort")) != route[:3]):
        raise error
    hold = state.get("hold")
    if hold:
        hold["terminal"] = terminal
    else:
        with pick.edit_holds(pick.HOLDS) as holds:
            entry = holds.get(terminal)
        if entry and all(entry.get(k) == v for k, v in zip(("agent", "model", "effort"), route)):
            label = entry["label"]
        elif (not entry and ticket["stage"] == "Reviewing" and
              " · reviewer " in (ticket["agent"] or "") and "@" in ticket["agent"].split(" · reviewer ")[-1]):
            # An already-released reviewer can have its manually recorded hold pruned.
            label = ticket["agent"].rsplit("@", 1)[1]
        else:
            raise error
        hold = {"route": route, "label": label, "holds": str(pick.HOLDS)}
    assign_reviewer(state, dispatch, terminal, hold)
    state.pop("pending")
    state.pop("review_title", None)
    state.update(stage="Reviewing", publish=True)
    save_review(db, run, number, state)


GREEN_INSTRUCTIONS = "Report the pushed branch head as a full SHA after its task CI is green."


def coder_spec(ticket, stage, instructions):
    goal = "Land the approved change through its PR." if stage == "Merging" else "Return a green task SHA."
    return (f"/goal {goal}\nRole: follow ~/.agents/roles/coder.md.\n"
            f"Issue {OWNER}/hr-relay#{ticket['number']}. Branch {ticket['branch']}, base {ticket['base']}.\n{instructions}")


def finish_followup(state, stage, dispatch):
    state["coder"] = state["expected"] = dispatch
    state["card_agent"] = state["agent"]
    if stage == "Fixing" and state.get("reviewer"):
        state["release"] = state.pop("reviewer")
    state.pop("followup", None)


def recover_followup(run, ticket, state, messages, message_id):
    report = next((m for m in review_mail(messages, state["expected"]) if m["id"] == message_id), None)
    if not report or not report.get("created_at"):
        return False
    followup = state.get("followup")
    if not followup:
        # The old reservation for a stale PUSH did not save its outbound spec.
        if (state["stage"] != "Reviewing" or not state.get("sha")
                or f"REVIEW VERDICT: PUSH {state['sha']}" not in (report["subject"], *report["body"].splitlines()[:1])):
            return False
        followup = {"stage": "Fixing", "spec": coder_spec(ticket, "Fixing", GREEN_INSTRUCTIONS)}
    stage = followup["stage"]
    tasks = runtime("orchestration", "task-list", "--run", run, "--full")["tasks"]
    matches = [t for t in tasks if t["run_id"] == run and t["task_title"] == f"#{ticket['number']}: {stage}"
               and t["spec"] == followup["spec"]
               and t["created_at"].replace(" ", "T")[:19] >= report["created_at"][:19]]
    if len(matches) != 1:
        return False
    task = matches[0]["id"]
    dispatch = (runtime("orchestration", "dispatch-show", "--task", task, "--full").get("dispatch") or {}).get("id")
    if not dispatch:
        return False
    attempt = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")["dispatch"]
    if (attempt["id"] != dispatch or attempt["taskId"] != task or attempt["runId"] != run
            or not in_coder_chain(attempt.get("followUpOf"), state["coder"], run)):
        return False
    finish_followup(state, stage, dispatch)
    state.update(stage=stage, publish=True)
    return True


def publish_review(db, run, ticket, state):
    if state.get("release"):
        import pick
        reviewer = state["release"]
        result = runtime("orchestration", "worker-release", "--dispatch", reviewer["dispatch"])
        if result.get("state") not in RELEASED:
            raise OrcaError("previous reviewer release unconfirmed")
        with pick.edit_holds(Path(reviewer["holds"])) as holds:
            holds.pop(reviewer["terminal"], None)
        state.pop("release")
        save_review(db, run, ticket["number"], state)
    if state.get("hold"):
        record_hold(state["hold"])
    review_card(ticket, state["stage"], state.get("card_agent", state["agent"]))
    state.pop("hold", None)
    state.pop("publish")
    save_review(db, run, ticket["number"], state)
    log("review", {"issue": ticket["number"], "stage": state["stage"], "dispatch": state["expected"]})


def review_step(db, run, actor, ticket, initial, messages):
    import pick
    number = ticket["number"]
    state = review_state(db, run, number) or initial.copy()
    reservation = state.get("pending") or state.get("released")
    if reservation:
        if recover_followup(run, ticket, state, messages, reservation):
            state.pop("pending", None)
            state.pop("released", None)
            save_review(db, run, number, state)
        elif state.get("pending"):
            adopt_reviewer(db, run, ticket, state, messages)
        else:
            state.pop("released")
            state.pop("followup", None)
            state.pop("review_title", None)
            state.pop("hold", None)
            save_review(db, run, number, state)
    if state.get("publish"):
        publish_review(db, run, ticket, state)
    if state["stage"] in ("Merging", "Needs you"):
        return
    db.execute("CREATE TABLE IF NOT EXISTS review_mail (run TEXT, id TEXT, PRIMARY KEY(run,id))")
    reports = (review_mail(messages, state["expected"]) if state["stage"] == "Reviewing"
               else latest_coder_report(state, run, messages))
    for message in reports:
        if not db.execute("SELECT 1 FROM review_mail WHERE run=? AND id=?", (run, message["id"])).fetchone():
            break
    else:
        return

    body = message["body"]
    stage = "Fixing"
    instructions = GREEN_INSTRUCTIONS
    account = None
    if state["stage"] == "Reviewing":
        header = body.splitlines()[0] if body else ""
        verdict = re.fullmatch(r"REVIEW VERDICT: (PUSH|FIX) ([a-f0-9]{40})", header)
        if not verdict and (not header.startswith("REVIEW VERDICT:") or header.startswith(message["subject"])):
            verdict = re.fullmatch(r"REVIEW VERDICT: (PUSH|FIX) ([a-f0-9]{40})", message["subject"])
        if not verdict or verdict[2] != state["sha"]:
            state.update(stage="Needs you", publish=True, card_agent=f"coordinator · invalid verdict {message['id']}")
            with db:
                db.execute("INSERT INTO review_mail VALUES (?,?)", (run, message["id"]))
                save_review(db, run, number, state)
            publish_review(db, run, ticket, state)
            return
        if verdict[1] == "FIX":
            state["history"] = [*state["history"], body]
            instructions = body
            if len(state["history"]) == 3:
                stage = "Needs you"
                instructions = "\n\n".join(f"Round {i}\n{v}" for i, v in enumerate(state["history"], 1))
        elif green_report(ticket, state["sha"]):
            stage = "Merging"
    else:
        sha = green_report(ticket, message["subject"] + "\n" + body)
        if sha:
            state["sha"] = sha
            route = review_route(ticket["body"])
            if route:
                ui_advisor = (route == ("claude", "claude-sonnet-5-5", "medium", "opus")
                              and ticket["route"] == route
                              and re.search(r"(?i)\bTier:\s*UI,?\s+multi-step\b", ticket["body"]))
                if route[1] == state["model"] and not ui_advisor:
                    raise OrcaError("reviewer must use a different model from the coder")
                account = review_account(route)
                worker_settings(Path(state["path"]), route)
                runtime("account", "select", "--agent", route[0], "--account", account["id"])
                if runtime("account", "list")[route[0]]["activeAccountId"] != account["id"]:
                    raise OrcaError("reviewer account selection changed")
                stage = "Reviewing"
            else:
                stage = "Merging"

    dot = ticket["repo"] == "dot-agents"
    if stage == "Merging":
        closes = f"Closes {OWNER}/hr-relay#{number}" if dot else f"Closes #{number}"
        instructions = (f"Starting at approved {state['sha']}, merge origin/{'main' if dot else 'development'}, "
                        f"push {ticket['branch']}, open or update the PR into {ticket['base'].removeprefix('origin/')}, "
                        f"end the body with {closes}, mark the PR ready, and enable auto-merge through required checks.")
    if account:
        spec = reviewer_spec(ticket, state["sha"], route[1] == state["model"])
        state["review_title"] = f"#{number} reviewer {message['id']}"
        state["hold"] = {"account": account["id"], "label": account["label"], "route": route,
                         "time": int(time.time()), "holds": str(pick.HOLDS)}
    else:
        spec = coder_spec(ticket, stage, instructions)
        if stage != "Needs you":
            state["followup"] = {"stage": stage, "spec": spec}
    # A lost response may already have launched a worker. Reserve before issuing the command.
    state["pending"] = message["id"]
    with db:
        db.execute("INSERT INTO review_mail VALUES (?,?)", (run, message["id"]))
        db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(state)))
    state["card_agent"] = state["agent"]
    if account:
        result = runtime("orchestration", "worker-start", *actor, "--worktree", "path:" + state["path"],
                         "--agent", route[0], "--model", route[1], "--effort", route[2],
                         "--task-title", state["review_title"], "--spec", spec, "--full")
        dispatch, terminal = start_identity(result)
        state["hold"]["terminal"] = terminal
        assign_reviewer(state, dispatch, terminal, state["hold"])
        state.pop("review_title")
    elif stage == "Needs you":
        # From the Run's own pane Orca accepts only status mail to run:<id> (sender_not_assignee otherwise).
        runtime("orchestration", "send", *actor, "--to", "run:" + run, "--type", "status",
                "--subject", f"#{number}: third FIX", "--body", instructions)
        state["card_agent"] = "coordinator"
    else:
        result = runtime("orchestration", "send", *actor, "--to", "dispatch:" + state["coder"],
                         "--subject", f"#{number}: {stage}", "--body", spec)
        finish_followup(state, stage, followup_dispatch(result, state["coder"], run))
    if stage == "Needs you" and state.get("reviewer"):
        state["release"] = state.pop("reviewer")
    state.pop("pending")
    state.update(stage=stage, publish=True)
    save_review(db, run, number, state)
    publish_review(db, run, ticket, state)


def review_tick(args, db):
    actor, run = supervisor(args)
    inbox = runtime("orchestration", "inbox", "--terminal", "run:" + run, "--limit", "10000", "--full")
    messages = inbox["messages"]
    if len(messages) >= 10000 or inbox.get("truncated"):
        raise OrcaError("incomplete Run mailbox")
    tasks = runtime("orchestration", "task-list", *(actor or ["--run", run]))["tasks"]
    failures = []
    for ticket in tickets():
        number = ticket["number"]
        if args.issue and args.issue != number:
            continue
        try:
            state = review_state(db, run, number)
            if not state:
                ids = [t["id"] for t in tasks if t["title"] == f"#{number} coder"]
                if not ids and not args.coder:
                    if args.issue:
                        raise OrcaError("supply --coder with the initial coder Dispatch")
                    continue
                if not ticket["item"] or (not args.issue and ticket["stage"] not in ("Coding", "CI running")):
                    continue
                if args.coder:
                    dispatch = args.coder
                elif len(ids) == 1:
                    dispatch = runtime("orchestration", "dispatch-show", "--task", ids[0], "--full")["dispatch"]["id"]
                else:
                    raise OrcaError("multiple coder Tasks; select the intended Dispatch with --issue and --coder")
                if not review_mail(messages, dispatch):
                    continue
                show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
                worker = show["worker"]
                if (show["dispatch"]["id"] != dispatch or show["dispatch"]["runId"] != run or
                        (not args.coder and show["dispatch"]["taskId"] != ids[0])):
                    raise OrcaError("coder Dispatch identity changed")
                tree = runtime("worktree", "show", "--worktree", "id:" + worker["worktreeId"])["worktree"]
                remote = command("git", "-C", tree["path"], "remote", "get-url", "origin").strip()
                if (tree["id"] != worker["worktreeId"] or tree["branch"].removeprefix("refs/heads/") != ticket["branch"]
                        or not re.fullmatch(r"(?:https://github\.com/|git@github\.com:)" + OWNER + "/"
                                            + ticket["repo"] + r"(?:\.git)?", remote)):
                    raise OrcaError("coder worktree does not match the issue repository and branch")
                model = worker["startOptions"]["launch"]["effective"]["model"]
                agent = (ticket["agent"] or "").split(" · ")[0] or f"coder {model}"
                state = {"coder": dispatch, "expected": dispatch, "coder_terminal": show["dispatch"].get("assigneeHandle"),
                         "path": tree["path"], "model": model,
                         "agent": agent, "history": [], "stage": "Coding"}
            review_step(db, run, actor, ticket, state, messages)
        except (OrcaError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as e:
            failures.append(f"#{number}: {e}")
    if failures:
        raise OrcaError("\n".join(failures))


def review(args):
    import fcntl
    state = Path.home() / ".local/state/dev-auto"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "fill.lock").open("a+") as lock:
        if args.release is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OrcaError("another tick is active; retry release after it finishes") from None
            _, run = supervisor(args)
            with sqlite3.connect(state / "starts.sqlite") as db:
                row = review_state(db, run, args.release)
                if row and row.get("pending"):
                    # Keep the handled message ID. The next tick may adopt a proven send, never replay it.
                    row["released"] = row.pop("pending")
                    save_review(db, run, args.release, row)
                    log("released", {"issue": args.release})
                else:
                    log("no pending reservation", {"issue": args.release})
            return
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(state / "starts.sqlite") as db:
            review_tick(args, db)


CLOSE_QUERY = """
query($endCursor:String) {
  repository(owner:"julianthant",name:"hr-relay") {
    issues(first:50,states:CLOSED,after:$endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body
        projectItems(first:20) { totalCount nodes {
          id project { number owner { ... on User { login } ... on Organization { login } } }
          agent:fieldValueByName(name:"Agent") { ... on ProjectV2ItemFieldTextValue { text } }
          stage:fieldValueByName(name:"Agent state") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
        }}
        closedByPullRequestsReferences(first:50) { totalCount nodes {
          merged headRefName headRefOid isCrossRepository repository { nameWithOwner }
        }}
      }
    }
  }
}
"""


def close_cards(repo, issue):
    pages = json.loads(command("gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + CLOSE_QUERY))
    for page in pages:
        for row in page["data"]["repository"]["issues"]["nodes"]:
            if issue and row["number"] != issue:
                continue
            branch_section = section(row["body"], "Branch")
            if repo != ("dot-agents" if "dot-agents" in branch_section else "hr-relay"):
                continue
            card = next((c for c in nodes(row["projectItems"])
                         if c["project"] == {"number": PROJECT, "owner": {"login": OWNER}}), None)
            if not card or not (card["agent"] or card["stage"]):
                continue
            match = re.search(r"\btask/[\w-]+/[\w-]+", branch_section)
            branch = match[0] if match else None
            if not branch:
                slug = re.fullmatch(r"([\w-]+)\s*/\s*([\w-]+(?:[ \t]+[\w-]+)*)(?:\s*:.*)?", row["title"].strip())
                if slug:
                    branch = f"task/{slug[1].lower()}/" + "-".join(slug[2].lower().split())
            yield row, card, branch


def close_workers(run):
    cursor = []
    seen = set()
    while True:
        inventory = runtime("orchestration", "worker-list", "--run", run, "--full", "--limit", "100", *cursor)
        for worker in inventory["workers"]:
            if worker["runId"] != run:
                raise OrcaError("worker-list returned a different Run")
            if worker["projection"]["host"]["kind"] == "local":
                yield worker
        page = inventory["page"]
        if not page["hasMore"]:
            return
        next_cursor = page["nextCursor"]
        if not next_cursor or next_cursor in seen:
            raise OrcaError("incomplete Orca worker inventory")
        seen.add(next_cursor)
        cursor = ["--cursor", next_cursor]


def release_close_workers(workers, workspace, run, paths):
    import pick
    releases, errors = [], []
    for worker in workers:
        dispatch = worker["dispatchId"]
        terminal = worker["agentTerminalHandle"]
        try:
            if not terminal:
                # Predecessors can lose their resource row when a follow-up reuses the pane.
                show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
                attempt, receipt = show["dispatch"], show.get("worker")
                if attempt["id"] != dispatch or attempt["runId"] != run:
                    raise OrcaError("Dispatch identity changed")
                terminal = attempt.get("assigneeHandle")
                if receipt and receipt["worktreeId"] != workspace:
                    raise OrcaError("Dispatch worktree changed")
                if receipt and any(e.get("kind") == "terminal" and e.get("role") == "agent" for e in receipt["effects"]):
                    if start_identity(receipt) != (dispatch, terminal):
                        raise OrcaError("Dispatch terminal changed")
            result = runtime("orchestration", "worker-release", "--dispatch", dispatch, "--full")
            releases.append((worker, terminal, result))
        except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            errors.append(f"{dispatch}: {e}")
    inventory = runtime("terminal", "list", "--limit", "10000")
    if inventory["truncated"]:
        raise OrcaError("incomplete terminal inventory after release")
    live = {t["handle"] for t in inventory["terminals"]}
    dropped = set()
    for worker, terminal, result in releases:
        state = result.get("state")
        if terminal not in live and (state in RELEASED or
                (state == "retained" and worker["dispatchStatus"] == "completed"
                 and (terminal or result.get("reason") == "ownership_transferred"))):
            if terminal:
                dropped.add(terminal)
        else:
            errors.append(f"{worker['dispatchId']}: release unconfirmed ({state}, {result.get('reason')})")
    for path in paths:
        if dropped and path.exists():
            with pick.edit_holds(path) as holds:
                for terminal in dropped:
                    holds.pop(terminal, None)
    if any(t.get("worktreeId") == workspace and t.get("agentIdentity") for t in inventory["terminals"]):
        errors.append("worktree still has a live agent terminal")
    if errors:
        raise OrcaError("; ".join(errors))


def remove_closed_tree(repo, workspace, tree, branch, sha):
    ref = "refs/heads/" + branch
    git_repo = ("git", "-C", str(repo))
    path = workspace.partition("::")[2]
    if not Path(path).is_absolute():
        raise OrcaError("invalid Orca worktree path")
    checkouts = [row.split("\0") for row in
                 command(*git_repo, "worktree", "list", "--porcelain", "-z").split("\0\0") if row]
    matching = [row for row in checkouts if "branch " + ref in row or row[0] == "worktree " + path]
    if tree:
        if (tree["isMainWorktree"] or path != tree["path"] or path == str(repo) or
                len(matching) != 1 or matching[0][:3] != ["worktree " + path, "HEAD " + sha, "branch " + ref]
                or matching[0] == checkouts[0]):
            raise OrcaError("worktree does not match the merged PR head")
        if command("git", "-C", path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"):
            raise OrcaError("worktree contains local changes or untracked files")
    elif matching or Path(path).exists():
        raise OrcaError("worktree still exists outside the verified Orca inventory")

    local_tips = command(*git_repo, "for-each-ref", "--format=%(objectname) %(refname)", ref).splitlines()
    local = [line for line in local_tips if line.endswith(" " + ref)]
    if local and local != [sha + " " + ref]:
        raise OrcaError("local task branch differs from the merged PR head")
    remote = command(*git_repo, "ls-remote", "--heads", "origin", ref).split()
    if remote and remote != [sha, ref]:
        raise OrcaError("remote task branch differs from the merged PR head")
    if tree:
        receipt = runtime("worktree", "rm", "--worktree", "id:" + workspace)
        if receipt.get("removed") is not True or Path(path).exists() or (
                f"worktree {path}\0" in command(*git_repo, "worktree", "list", "--porcelain", "-z")):
            raise OrcaError("worktree removal not confirmed")
    if remote:
        try:
            command(*git_repo, "push", f"--force-with-lease={ref}:{sha}", "origin", ":" + ref)
        except OrcaError:
            if command(*git_repo, "ls-remote", "--heads", "origin", ref).strip():
                raise
    if local:
        if "branch " + ref + "\0" in command(*git_repo, "worktree", "list", "--porcelain", "-z"):
            raise OrcaError("task branch was checked out during cleanup")
        command(*git_repo, "update-ref", "-d", ref, sha)


def close_tick(args, db):
    import pick
    repo = args.repo_path.resolve()
    remote = command("git", "-C", str(repo), "remote", "get-url", "origin").strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)julianthant/(hr-relay|dot-agents)(?:\.git)?", remote)
    if not match:
        raise OrcaError("target checkout must belong to julianthant/hr-relay or julianthant/dot-agents")
    repo_name = match[1]
    candidates = list(close_cards(repo_name, args.issue))
    if not candidates:
        log("close", "no closed tickets with Agent fields")
        return
    actor, run = supervisor(args)
    roster = {}
    for worker in close_workers(run):
        workspace = worker["projection"].get("workspace")
        if workspace:
            roster.setdefault(workspace["id"], []).append(worker)
    trees = runtime("worktree", "list", "--repo", "path:" + str(repo), "--limit", "10000")
    if trees["truncated"]:
        raise OrcaError("incomplete Orca worktree inventory")
    project, (_, (stage, _), (agent, _)), (_, needs_you) = project_fields()
    # Retry the same workspace if a partial close already removed the checkout.
    db.execute("CREATE TABLE IF NOT EXISTS closing (run TEXT, issue INTEGER, workspace TEXT, PRIMARY KEY(run,issue))")
    for issue, card, branch in candidates:
        number = issue["number"]
        review = review_state(db, run, number) or {}
        saved = db.execute("SELECT workspace FROM closing WHERE run=? AND issue=?", (run, number)).fetchone()
        matching = [tree for tree in trees["worktrees"] if
                    tree["branch"] and tree["branch"].removeprefix("refs/heads/") == branch and not tree["isMainWorktree"]]
        tree = matching[0] if len(matching) == 1 else None
        recorded_path = review.get("path")
        historical = [identity for identity in roster if recorded_path and identity.partition("::")[2] == recorded_path]
        workspace = historical[0] if len(historical) == 1 else None
        if saved:
            workspace = saved[0]
        if tree:
            workspace = tree["id"]
        workers = roster.get(workspace)
        if not workers and not args.issue and not saved and not review:
            continue
        edit = ("gh", "project", "item-edit", "--project-id", project, "--id", card["id"], "--field-id")
        try:
            if (not workers or not branch or len(matching) > 1 or (saved and workspace != saved[0])
                    or (recorded_path and workspace.partition("::")[2] != recorded_path)):
                raise OrcaError("no unique ticket worktree and worker inventory in this Run")
            with db:
                db.execute("INSERT OR IGNORE INTO closing VALUES (?,?,?)", (run, number, workspace))
            paths = {pick.HOLDS, args.holds or pick.HOLDS}
            paths.update(Path(review[key]["holds"]) for key in ("hold", "reviewer", "release")
                         if review.get(key) and review[key].get("holds"))
            release_close_workers(workers, workspace, run, paths)
            heads = {pr["headRefOid"] for pr in nodes(issue["closedByPullRequestsReferences"])
                     if pr["merged"] and not pr["isCrossRepository"] and pr["headRefName"] == branch
                     and pr["repository"]["nameWithOwner"] == f"{OWNER}/{repo_name}"}
            if len(heads) != 1:
                raise OrcaError("closed without a uniquely verified merged task PR")
            sha = heads.pop()
            if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
                raise OrcaError("invalid merged PR head")
            remove_closed_tree(repo, workspace, tree, branch, sha)
            command(*edit, agent, "--clear")
            command(*edit, stage, "--clear")
            with db:
                db.execute("DELETE FROM closing WHERE run=? AND issue=?", (run, number))
            log("closed", {"issue": number, "branch": branch, "head": sha})
        except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            log("Needs you", {"issue": number, "reason": str(e)})
            if args.issue or (card["stage"] or {}).get("name") != "Needs you":
                runtime("orchestration", "send", *actor, "--to", "run:" + run, "--type", "escalation",
                        "--subject", f"#{number}: Needs you after close", "--body", str(e))
                command(*edit, stage, "--single-select-option-id", needs_you)


def close(args):
    import fcntl
    state = Path.home() / ".local/state/dev-auto"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "fill.lock").open("a+") as lock, sqlite3.connect(state / "starts.sqlite") as db:
        fcntl.flock(lock, fcntl.LOCK_EX)
        close_tick(args, db)


# Codex near its limit, seen 2026-11-22: "⚠ Heads up, you have less than 5% of your 5h limit left."
# and its status line "⚠ 5h limit: only 4% left · resets at 5:57 PM".
NEAR_LIMIT = re.compile(r"^\s*⚠\s*(?:heads up, you have less than \d+% of your \S+ limit left"
                        r"|\S+ limit: only \d+% left\b)", re.I)
# Codex's status line under its input box: "GPT-6.1-Sol xhigh · ~/repo".
CODEX_STATUS = re.compile(r"\s*([\w.-]+)(?: (\w+))? · (?=[~/])")
QUIET_MINUTES = 20
HANDOFF = ("Your account is at its usage handoff point ({why}). Finish the current step, run /handoff, then send "
           "worker_done --outcome failed with the handoff path. A fresh worker on another account continues from it.")
NUDGE = ("Your screen has not changed for {minutes} minutes. If you are blocked, report it with orca orchestration "
         "ask or send; otherwise continue the task.")


def screen_limit(tail, launch):
    """Why this screen means the worker cannot hand off: a limit line, the model-switch menu, or a switched model."""
    hit = limited(tail)
    if hit:
        return hit
    menu = codex_prompt(tail)
    if menu and not menu.lower().lstrip().startswith("hooks need review"):
        lines = menu.splitlines()
        below = tail[max(i for i, line in enumerate(tail) if line == lines[0]) + len(lines):]
        # A menu counts only while it is showing: newer output or the input prompt below it makes it history.
        if not any(line.lstrip().startswith(OUTPUT) or re.match(r"^\s*[›❯>](?!\s*\d+[.)] )", line) for line in below):
            return lines[0].strip()
    if launch.get("agent") == "codex":
        status = next((m for line in reversed(tail[-6:]) if (m := CODEX_STATUS.match(line))), None)
        if status and status[1].lower() != (launch.get("model") or "").lower():
            return f"model switched to {status[1]} {status[2] or ''}".strip()
    return None


def card_agent(text, reviewer, label):
    parts = (text or "").split(" · ")
    i = 1 if reviewer and len(parts) > 1 else 0
    parts[i] = re.sub(r"\s*@\S+$", "", parts[i]) + " @" + label
    return " · ".join(parts)


ACTIVE = ("Coding", "CI running", "Reviewing", "Fixing", "Handed off")


def watch_tick(args, db):
    """One pass over the Run's live workers; always logs exactly one watch line."""
    actions, errors = [], []
    try:
        watch_pass(args, db, actions, errors)
    except (OrcaError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as e:
        errors.append({"tick": str(e)})
    finally:
        log("watch", actions + errors or "no action")
    if errors:
        raise OrcaError(f"{len(errors)} watch error(s); see the watch line")


def watch_pass(args, db, actions, errors):
    import contextlib
    import hashlib
    import io
    import pick
    actor, run = supervisor(args)
    now = time.time()
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            pick.main(["coordinator"])
        if out.getvalue().strip():
            actions.append({"coordinator": out.getvalue().strip()})
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, SystemExit) as e:
        errors.append({"coordinator": str(e)})
    cfg = pick.account_usage.load_config()
    holds_path = Path(args.holds or pick.HOLDS)
    holds = json.loads(holds_path.read_text()) if holds_path.exists() else {}
    due = {handle: why for _, handle, why in pick.due(cfg, "local", now)}
    tasks = {t["id"]: t for t in runtime("orchestration", "task-list", *(actor or ["--run", run]), "--full")["tasks"]}
    cards = {t["number"]: t for t in tickets()}
    db.execute("CREATE TABLE IF NOT EXISTS watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
    saved = dict(db.execute("SELECT terminal,state FROM watch WHERE run=?", (run,)))

    def save(terminal, state):
        with db:
            if state is None:
                db.execute("DELETE FROM watch WHERE run=? AND terminal=?", (run, terminal))
            else:
                db.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?)", (run, terminal, json.dumps(state)))

    def escalate(number, subject, body):
        # From the Run's own pane Orca accepts only status mail to run:<id> (sender_not_assignee otherwise).
        runtime("orchestration", "send", *actor, "--to", "run:" + run, "--type", "status",
                "--subject", f"#{number}: {subject}" if number else subject, "--body", body)

    def stage(ticket, name, agent=None):
        if ticket and ticket.get("item") and (ticket["stage"] != name or agent):
            review_card(ticket, name, agent or ticket["agent"] or "")
            ticket["stage"] = name

    def issue_of(task_id):
        task = tasks.get(task_id) or {}
        found = re.search(r"#(\d+)", task.get("task_title") or "") or re.search(r"hr-relay ?#(\d+)", task.get("spec") or "")
        return task, int(found[1]) if found else None

    def phase(ticket, state, title):
        for name in (state.get("previous_stage"), ticket and ticket["stage"]):
            if name and name not in ("Handed off", "Needs you"):
                return name
        return "Reviewing" if "review" in title else "Coding"

    def restart(w, show, state, why, crash):
        dispatch, terminal, task_id = w["dispatchId"], w["agentTerminalHandle"], show["dispatch"]["taskId"]
        task, number = issue_of(task_id)
        title = task.get("task_title") or ""
        ticket = cards.get(number)
        pending = state.get("pending") or {}
        # A lost worker-start response may have launched the replacement already; never replay it.
        if pending.get("starting") or (not crash and not state.get("account")):
            reason = "start" if pending.get("starting") else "account"
            if state.get("escalated") != reason:
                escalate(number, "restart unconfirmed" if reason == "start" else "limit account unknown",
                         f"Inspect the restart of Dispatch {dispatch} in Run {run}, then clear its watch row."
                         if reason == "start" else f"Dispatch {dispatch} must restart ({why}), but no hold records "
                         "its account, so another account cannot be proven. It was left running.")
                stage(ticket, "Needs you")
                state["escalated"] = reason
                save(terminal, state)
                actions.append({"issue": number, "dispatch": dispatch, "reported": reason})
            return
        state["pending"] = {"why": why, "crash": crash}
        if not crash:
            state.setdefault("previous_stage", phase(ticket, state, title))
            stage(ticket, "Handed off")
        save(terminal, state)
        launch = show["worker"]["startOptions"]["launch"]["effective"]
        route = (launch["agent"], launch["model"], launch["effort"])
        source = None if crash else state["account"]
        seats = dict(cfg, account=[r for r in cfg["account"] if r["id"] != source])
        account, reasons = pick.pick(seats, *route, "local", now)
        if account is None:
            actions.append({"issue": number, "dispatch": dispatch, "waiting for seat": reasons})
            return
        runtime("account", "select", "--agent", route[0], "--account", account["id"])
        if runtime("account", "list")[route[0]]["activeAccountId"] != account["id"]:
            raise OrcaError("selected account changed before restart")
        if show["dispatch"]["status"] == "dispatched":
            # --retry-of takes a settled attempt, so fence the old one first.
            stop = runtime("orchestration", "worker-stop", "--dispatch", dispatch, "--full")
            actions.append({"issue": number, "stopped": dispatch, "process": stop.get("processAction")})
        state["pending"]["starting"] = True
        save(terminal, state)
        # The same Task keeps its title, dependencies and retry lineage for the review tick.
        started = runtime("orchestration", "worker-start", *actor, "--task", task_id, "--retry-of", dispatch,
                          "--worktree", "id:" + show["worker"]["worktreeId"], "--agent", route[0],
                          "--model", route[1], "--effort", route[2], "--full")
        new, handle = start_identity(started)
        with pick.edit_holds(holds_path) as edit:
            edit.pop(terminal, None)
            edit[handle] = dict(account=account["id"], label=account["label"], agent=route[0], model=route[1],
                                effort=route[2], host="local", at=int(now))
        if number is not None:
            review = review_state(db, run, number)
            if review and review.get("coder") == dispatch:
                review.update(coder=new, expected=new, coder_terminal=handle)
                save_review(db, run, number, review)
            elif review and (review.get("reviewer") or {}).get("dispatch") == dispatch:
                review["reviewer"].update(dispatch=new, terminal=handle)
                review["expected"] = new
                save_review(db, run, number, review)
        save(terminal, None)
        save(handle, {"relaunched": bool(state.get("relaunched") or crash), "account": account["id"]})
        if ticket:
            stage(ticket, phase(ticket, state, title), card_agent(ticket["agent"], "review" in title, account["label"]))
        actions.append({"issue": number, "restarted": dispatch, "as": new, "why": why, "account": account["label"]})
        report = failure_of(show)
        note = (f"You replace Dispatch {dispatch}, which stopped: {why}."
                + (f" Its handoff: {report['reportPath']}." if isinstance(report, dict) and report.get("reportPath") else "")
                + (f" Its last report:\n{report['body'][:1500]}\n" if isinstance(report, dict) and report.get("body") else " ")
                + "Read its handoff first if there is one; otherwise continue from the last commit on this branch.")
        receipt = runtime("orchestration", "send", *actor, "--to", "dispatch:" + new,
                          "--subject", f"#{number}: resume" if number else "resume", "--body", note)
        actions[-1]["note"] = receipt.get("delivery")

    def restore_prompt(ticket, state, title):
        """The forwarded prompt was answered: give the card back so review_tick reads the coder's report."""
        if not state.get("prompt"):
            return
        if ticket and ticket["stage"] == "Needs you":
            stage(ticket, state.get("prompt_stage") or phase(ticket, state, title))
        for key in ("prompt", "prompt_stage"):
            state.pop(key, None)
        actions.append({"issue": ticket and ticket["number"], "prompt cleared": True})

    seen_terminals, seen_tasks = set(), set()
    for w in close_workers(run):
        terminal, dispatch, task_id = w["agentTerminalHandle"], w["dispatchId"], w["taskId"]
        owner = (w.get("resource") or {}).get("ownerDispatchId")
        # worker-list is newest first: act only on the current attempt of each pane and Task.
        if not terminal or terminal in seen_terminals or (owner and owner != dispatch):
            continue
        seen_terminals.add(terminal)
        if task_id in seen_tasks:
            continue
        seen_tasks.add(task_id)
        state = json.loads(saved.get(terminal) or "null")
        live = w["dispatchStatus"] == "dispatched"
        if not live and state is None and (w["dispatchStatus"] != "failed" or w.get("workerState") == "stopped"):
            continue
        try:
            show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
            if show["dispatch"]["id"] != dispatch or show["dispatch"]["runId"] != run:
                raise OrcaError("Dispatch identity changed")
            task, number = issue_of(task_id)
            title = task.get("task_title") or ""
            ticket = cards.get(number)
            if not live and state is None and not (ticket and ticket["stage"] in ACTIVE):
                continue  # history: only a ticket still in work can have a current crash
            state = state or {}
            hold = (holds.get(terminal) or {}).get("account")
            if live and hold and state.get("account") != hold:
                state["account"] = hold
                save(terminal, state)
            report = failure_of(show)
            reported = isinstance(report, dict) and report.get("provenance") == "worker_report"
            stopped = (show.get("worker") or {}).get("state") == "stopped"
            exited = (show["projection"]["liveness"]["verdict"] == "exited"
                      or (show.get("observation") or {}).get("status") == "exited")
            pending = state.get("pending")
            # A limit restart waiting for a seat stops the worker only if the limit is still on screen.
            if pending and not (live and pending["why"].startswith("usage limit") and not pending.get("starting")):
                restart(w, show, state, pending["why"], pending.get("crash", False))
                continue
            if not live:
                if state.get("handoff") and reported:
                    restart(w, show, state, "handed off at a usage limit", False)
                    continue
                if w["dispatchStatus"] != "failed" or reported or stopped:
                    restore_prompt(ticket, state, title)  # answered, then finished before this tick
                    save(terminal, None)  # finished, reported or stopped by the operator
                    continue
                exited = True
            if exited:
                if not state.get("relaunched"):
                    restart(w, show, state, "its agent exited", True)
                elif not state.get("escalated"):
                    escalate(number, "worker crashed again", f"Dispatch {dispatch} exited after its relaunch.")
                    stage(ticket, "Needs you")
                    state["escalated"] = "crash"
                    save(terminal, state)
                    actions.append({"issue": number, "dispatch": dispatch, "reported": "crashed again"})
                continue
            term = show.get("terminal") or {}
            wait = (show.get("observation") or {}).get("agentWait") or term.get("agentWait")
            launch = show["worker"]["startOptions"]["launch"]["effective"]
            tail = runtime("terminal", "read", "--terminal", terminal, "--screen")["terminal"]["tail"]
            hooks = codex_prompt(tail)
            prompt = wait or (hooks if hooks and hooks.lower().lstrip().startswith("hooks need review") else None)
            hit = screen_limit(tail, launch)
            if pending and not hit:
                stage(ticket, phase(ticket, state, title))
                for key in ("pending", "previous_stage"):
                    state.pop(key, None)
                save(terminal, state)
                actions.append({"issue": number, "dispatch": dispatch, "recovered": pending["why"]})
            if not prompt and state.get("prompt"):
                restore_prompt(ticket, state, title)
                save(terminal, state)
            if hit:
                actions.append({"issue": number, "dispatch": dispatch, "limit": hit})
                restart(w, show, state, f"usage limit ({hit})", False)
            elif prompt:
                key = json.dumps(prompt, ensure_ascii=False, sort_keys=True)
                if state.get("prompt") != key:
                    escalate(number, "permission prompt", f"Dispatch {dispatch} is waiting on: {key}")
                    state.setdefault("prompt_stage", phase(ticket, state, title))
                    stage(ticket, "Needs you")
                    state["prompt"] = key
                    save(terminal, state)
                    actions.append({"issue": number, "dispatch": dispatch, "forwarded": prompt})
            elif (near := next((line.strip() for line in tail if NEAR_LIMIT.match(line)), None) or due.get(terminal)):
                if not state.get("handoff"):
                    runtime("orchestration", "send", *actor, "--to", "dispatch:" + dispatch,
                            "--subject", f"#{number}: hand off now" if number else "hand off now",
                            "--body", HANDOFF.format(why=near))
                    state.update(handoff=int(now), previous_stage=phase(ticket, state, title))
                    stage(ticket, "Handed off")
                    save(terminal, state)
                    actions.append({"issue": number, "dispatch": dispatch, "handoff": near})
            else:
                digest = hashlib.sha256("\n".join(tail).encode()).hexdigest()
                if state.get("screen") != digest:
                    state.update(screen=digest, since=int(now))
                    save(terminal, state)
                elif now - state["since"] >= QUIET_MINUTES * 60 and state.get("escalated") != "silent":
                    if not state.get("nudged"):
                        runtime("orchestration", "send", *actor, "--to", "dispatch:" + dispatch,
                                "--subject", f"#{number}: still working?" if number else "still working?",
                                "--body", NUDGE.format(minutes=QUIET_MINUTES))
                        state.update(nudged=True, since=int(now))
                        actions.append({"issue": number, "dispatch": dispatch, "nudged": QUIET_MINUTES})
                    else:
                        escalate(number, "silent worker",
                                 f"Dispatch {dispatch} showed the same screen for {QUIET_MINUTES} minutes after a nudge.")
                        state["escalated"] = "silent"
                        actions.append({"issue": number, "dispatch": dispatch, "reported": "silent after nudge"})
                    save(terminal, state)
        except (OrcaError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
            errors.append({"dispatch": dispatch, "error": str(e)})


def failure_of(show):
    """The Dispatch's lastFailure: a worker report's fields as a dict, else Orca's string (such as "stopped")."""
    failure = show["dispatch"].get("lastFailure") or ""
    try:
        return json.loads(failure) if failure.startswith("{") else failure
    except ValueError:
        return failure


def watch_workers(args):
    import fcntl
    state = Path.home() / ".local/state/dev-auto"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "fill.lock").open("a+") as lock, sqlite3.connect(state / "starts.sqlite") as db:
        fcntl.flock(lock, fcntl.LOCK_EX)
        watch_tick(args, db)


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
    r = sub.add_parser("review", help="advance coder reports and review verdicts in the coordinating Run")
    r.add_argument("--issue", type=int, help="restrict review to this ticket")
    r.add_argument("--release", type=int, help="release one inspected pending review in the owning Run, then exit")
    r.add_argument("--coder", help="initial coder Dispatch for a manually started ticket")
    r.add_argument("--anchor", help="pane-less supervisor anchor")
    r.add_argument("--run", help="Run already bound to the anchor")
    c = sub.add_parser("close", help="release the coordinating Run's workers for closed issues",
                       description="Clean up closed issues and report blocked cleanup to the coordinator that owns the Run.")
    c.add_argument("--repo-path", type=Path, required=True, help="registered repository root checkout")
    c.add_argument("--issue", type=int, help="restrict cleanup to one ticket")
    c.add_argument("--holds", type=Path, help="existing fleet picker's holds.json")
    c.add_argument("--anchor", help="pane-less supervisor anchor")
    c.add_argument("--run", help="Run already bound to the anchor")
    w = sub.add_parser("watch", help="hand off, relaunch, nudge or report the coordinating Run's live workers",
                       description="Check every live worker once and report what needs the coordinator that owns the Run.")
    w.add_argument("--holds", type=Path, help="existing fleet picker's holds.json")
    w.add_argument("--anchor", help="pane-less supervisor anchor")
    w.add_argument("--run", help="Run already bound to the anchor")
    args = ap.parse_args(argv)
    if args.cmd == "watch":
        try:
            return watch_workers(args)
        except (OSError, OrcaError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as e:
            ap.exit(1, f"watch: {e}\n")
    if args.cmd == "close":
        if args.issue is not None and args.issue <= 0:
            c.error("--issue requires a positive issue number")
        try:
            return close(args)
        except (OSError, OrcaError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as e:
            ap.exit(1, f"close: {e}\n")
    if args.cmd == "review":
        if args.release is not None and (args.release <= 0 or args.issue is not None or args.coder):
            r.error("--release requires a positive issue number and cannot combine with --issue or --coder")
        if args.coder and not args.issue:
            r.error("--coder requires --issue")
        try:
            return review(args)
        except (OSError, OrcaError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as e:
            ap.exit(1, f"review: {e}\n")
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
