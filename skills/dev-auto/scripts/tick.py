"""Headless Orca coordinator for dev-auto, run by launchd.

  tick.py scheduled --run <run> [--anchor <initial-pane>] --repo-path <checkout> [--repo-path <checkout>]
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
from contextlib import closing
import datetime
import hashlib
import inspect
import shlex
import sqlite3
import tempfile
import tomllib
import json
import os
import re
import subprocess
import sys
import time
import uuid

import github_poller

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


class UncertainReview(OrcaError):
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
        dispatch = attempt.get("retryOfDispatchId") or attempt.get("followUpOf")
    return False


def follow_root(dispatch, attempts, run):
    """The coder Task, and its attempt, that the follow-up's lineage reaches; it outranks any report."""
    start, seen = dispatch, set()
    while dispatch and dispatch not in seen:
        seen.add(dispatch)
        attempt = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")["dispatch"]
        if attempt["id"] != dispatch or attempt["runId"] != run:
            break
        if attempt.get("taskId") in attempts:
            return attempt["taskId"], dispatch
        dispatch = attempt.get("retryOfDispatchId") or attempt.get("followUpOf")
    raise OrcaError(f"follow-up {start} does not reach a coder Task")


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
ISSUE_FIELDS = """number title body state stateReason updatedAt issueType {name} milestone {title}
  parent {number} subIssuesSummary {total completed}
  assignees {totalCount} subIssues {totalCount} closedByPullRequestsReferences {totalCount}
  labels(first:100){totalCount nodes{name}}
  blockedBy(first:100){totalCount nodes{number state}}
  blocking(first:100){totalCount nodes{state}}
  projectItems(first:20){totalCount nodes{id project{number owner{... on User{login} ... on Organization{login}}}
    status:fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}
    agent:fieldValueByName(name:"Agent"){... on ProjectV2ItemFieldTextValue{text}}
    stage:fieldValueByName(name:"Agent state"){... on ProjectV2ItemFieldSingleSelectValue{name}}
    area:fieldValueByName(name:"Area"){... on ProjectV2ItemFieldSingleSelectValue{name}}
  }}"""
# Open issues keep creation order so an update mid-read cannot skip one; closed issues come newest first.
ISSUES_QUERY = """query($endCursor:String,$states:IssueState!,$since:DateTime) {
  repository(owner:"julianthant",name:"hr-relay") {
    issues(first:50,states:[$states],after:$endCursor,filterBy:{since:$since}%s) {
      pageInfo { hasNextPage endCursor }
      nodes { """ + ISSUE_FIELDS + """ }
    }
  }
}
"""
SNAPSHOT = Path.home() / ".local/state/dev-auto/issues.json"
CLOSED_FULL_SECONDS = 600
CLOSED_KEPT = 400
# The Project's id and its field and option ids are stable; field-list alone costs about 100 GraphQL points.
PROJECT_CACHE = SNAPSHOT.with_name("project.json")


class StartNotApplied(OrcaError):
    pass


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=900 if "worker-start" in args else 90)
    if args[:3] == ("gh", "project", "item-edit"):
        if result.returncode:
            stale_snapshot()
            PROJECT_CACHE.unlink(missing_ok=True)  # a failed edit may mean renamed ids: refetch them next time
        else:
            write_through(args)
    if result.returncode:
        if args[1:3] == ("orchestration", "worker-start"):
            try:
                reply = json.loads(result.stdout)
            except ValueError:
                reply = {}
            if ("--agent" in args and args[args.index("--agent") + 1] == "grok"
                    and isinstance(reply, dict) and reply.get("ok") is True and (reply.get("result") or {}).get("dispatchId")):
                return result.stdout  # A full Grok receipt can describe a live worker before its turn is observed.
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
    for phrase, path in ((r"backend storage", "src/backend/storage"),
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
    m = re.fullmatch(r"(grok|cursor) (grok-4\.7(?:-x?high)?) (x?high)", route)
    if m and m[2] == ("grok-4.7" if m[1] == "grok" else f"grok-4.7-{m[3]}"):
        return (*m.groups(), None)


def research_brief(body):
    return all(re.match(r"none\b", section(body, field), re.I) for field in ("Branch", "Reviewer"))


def research_root():
    return (SNAPSHOT.parent / "research").resolve()


def issue_nodes(state, since=None, limit=None):
    order = ",orderBy:{field:UPDATED_AT,direction:DESC}" if state == "CLOSED" else ""
    out, cursor = [], None
    while limit is None or len(out) < limit:
        args = ["gh", "api", "graphql", "-f", "query=" + ISSUES_QUERY % order, "-f", "states=" + state]
        args += ["-f", "since=" + since] if since else []
        args += ["-f", "endCursor=" + cursor] if cursor else []
        page = json.loads(command(*args))["data"]["repository"]["issues"]
        out += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out[:limit]


def github_state(source, **query):
    try:
        data = github_poller.read(source, **query)
    except github_poller.Unavailable as e:
        raise OrcaError(str(e)) from e
    if data is None:
        raise OrcaError(f"roadmap {source} lookup pending")
    return data


def snapshot():
    return github_state("issues")


def refresh_snapshot():
    """Refresh the issue snapshot in the roadmap server."""
    import fcntl
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOT.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            old = json.loads(SNAPSHOT.read_text())
        except (OSError, ValueError):
            old = {}
        now = time.time()
        opened = issue_nodes("OPEN")
        # Closed issues change only when updated, so read the recent ones and a full list every ten minutes.
        full = not 0 <= now - old.get("closed_full", 0) < CLOSED_FULL_SECONDS
        since = None if full else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old["read"] - 120))
        closed = {} if full else {i["number"]: i for i in old.get("closed", [])}
        closed.update((i["number"], i) for i in issue_nodes("CLOSED", since))
        numbers = {i["number"] for i in opened}
        closed = sorted((i for n, i in closed.items() if n not in numbers), key=lambda i: i["updatedAt"], reverse=True)
        new = {"fetched": now, "read": now, "closed_full": now if full else old["closed_full"],
               "open": opened, "closed": closed}
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(new))
        tmp.replace(SNAPSHOT)
        return new


def edit_snapshot(change):
    """Replace the snapshot with change(snapshot) under its lock; no snapshot yet means nothing to change."""
    import fcntl
    if not SNAPSHOT.exists():
        return
    with SNAPSHOT.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            old = json.loads(SNAPSHOT.read_text())
        except (OSError, ValueError):
            return
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(change(old)))
        tmp.replace(SNAPSHOT)


def stale_snapshot():
    """Make readers wait for the poller when a Project write cannot be reconciled."""
    edit_snapshot(lambda old: {**old, "fetched": 0})


CARD_KEYS = {"Status": "status", "Agent": "agent", "Agent state": "stage", "Area": "area"}


def write_through(args):
    """Copy a successful card edit into the snapshot, so the roadmap shows it without reading GitHub."""
    a = dict(zip(args, args[1:]))
    try:
        field = next(f for f in json.loads(PROJECT_CACHE.read_text())["fields"].values() if f["id"] == a["--field-id"])
        key = CARD_KEYS[field["name"]]
        if "--clear" in args:
            value = None
        elif "--text" in a:
            value = {"text": a["--text"]}
        else:
            value = {"name": next(o["name"] for o in field["options"] if o["id"] == a["--single-select-option-id"])}
    except (OSError, ValueError, KeyError, StopIteration):
        return stale_snapshot()

    def change(old):
        cards = [c for i in old.get("open", []) + old.get("closed", []) for c in i["projectItems"]["nodes"]
                 if c["id"] == a["--id"]]
        for c in cards:
            c[key] = value
        return old if cards else {**old, "fetched": 0}
    edit_snapshot(change)


def tickets(fresh=False, *, rows=None):
    result = []
    if rows is None:
        rows = issue_nodes("OPEN") if fresh else snapshot()["open"]
    for i in rows:
        card = next((c for c in nodes(i["projectItems"]) if c["project"]["number"] == PROJECT
                     and c["project"]["owner"]["login"] == OWNER), None)
        body = i["body"]
        research = research_brief(body)
        branch_section = section(body, "Branch")
        m = re.search(r"\btask/([\w-]+)/([\w-]+)", branch_section)
        branch = m[0] if m and not research else None
        if not branch and not research:
            slug = re.fullmatch(r"([\w-]+)\s*/\s*([\w-]+(?:[ \t]+[\w-]+)*)(?:\s*:.*)?", i["title"].strip())
            if slug:
                lane = "-".join(slug[2].lower().split())
                branch = f"task/{slug[1].lower()}/{lane}"
        dot = not research and "dot-agents" in branch_section
        card = card or {}
        stage = (card.get("stage") or {}).get("name")
        if research:
            base = "origin/development"
        elif dot:
            base = "origin/main"
        elif "development" in branch_section and "feature/" not in branch_section:
            base = "origin/development"
        else:
            base = "origin/feature/" + branch.split("/")[1] if branch else None
        blockers = [b["number"] for b in nodes(i["blockedBy"]) if b["state"] == "OPEN"]
        result.append({
            "number": i["number"], "title": i["title"], "item": card.get("id"), "body": body,
            "agent": (card.get("agent") or {}).get("text"), "stage": stage,
            "status": (card.get("status") or {}).get("name"), "kind": (i["issueType"] or {}).get("name"),
            "assigned": bool(i["assignees"]["totalCount"] or (card.get("agent") or {}).get("text")),
            "busy": bool(i["closedByPullRequestsReferences"]["totalCount"] or stage not in (None, "Queued")),
            "later": (i["milestone"] or {}).get("title", "").lower() == "later"
                     or any(label["name"].lower() == "later" for label in nodes(i["labels"])),
            "blocked": bool(i["subIssues"]["totalCount"] or blockers), "blockers": blockers,
            "score": sum(b["state"] == "OPEN" for b in nodes(i["blocking"])),
            "route": coder_route(body), "paths": [] if research else owned_paths(body), "branch": branch,
            "base": base, "research": research,
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


def fill_plan(tickets, seat, issue=None, owed=lambda: ()):
    held = []
    branches = set()
    for t in sorted(tickets, key=lambda t: (-t["score"], t["number"])):
        if issue and t["number"] != issue:
            continue
        if t["kind"] not in ("Task", "Bug"):
            continue
        research = t.get("research")
        branch = t["repo"], t["branch"] if not research else t["number"]
        blockers = ", ".join(f"#{n}" for n in t.get("blockers", ()))
        skip = t.get("skip") or []
        reason = next((reason for condition, reason in (
            (t["later"] or t.get("stage") == "Needs you", "needs operator"),
            (t["blocked"], f"blocked by {blockers}" if blockers else "has subtasks"),
            (t["assigned"] or t["busy"] or t["status"] == "In Progress", "active work"),
            (not t["route"], "needs operator (missing coder route)"),
            (t["status"] != "Todo", "status is not Todo"),
            (not t["branch"] and not research, "missing task branch"),
            (t.get("occupied"), "existing worktree"),
            (branch in branches, "task branch already planned"),
            (skip, skip[0] if skip else None),
        ) if condition), None)
        if reason:
            yield t, None, reason
            continue
        if t["route"][0] in ("grok", "cursor"):
            effort = "xhigh" if research else "high"
            if t["route"][2] != effort:
                yield t, None, f"needs {effort} for this Grok route"
                continue
            if not research:
                try:
                    review_route(t["body"])
                except OrcaError as e:
                    yield t, None, str(e)
                    continue
        restores = owed() if t["route"][0] == "codex" else ()
        if restores:
            yield t, None, f"coordinator restore still owed by {sorted(restores)}"
            continue
        account, _ = seat(t["route"], held, local_only=True) if research else seat(t["route"], held)
        if not account:
            yield t, None, "no seat"
            continue
        held.append(dict(host=account.get("host", "local"), account=account["id"], agent=t["route"][0], model=t["route"][1], effort=t["route"][2]))
        branches.add(branch)
        yield t, account, None


def overlap_note(ticket, issues):
    matches = []
    for live in issues:
        if live["number"] == ticket["number"] or not live.get("live"):
            continue
        paths = [p for p in live["paths"] if overlap([p], ticket["paths"])]
        if paths:
            matches.append(f"- #{live['number']}: " + ", ".join(f"`{p}`" for p in paths))
    if matches:
        return ("This ticket overlaps live work on the owned paths below. Preserve the other tickets’ changes. "
                "If the PRs conflict, the PR that merges second resolves the conflict.\n\n" + "\n".join(matches))


def post_overlap_note(db, number, rec):
    note = rec.get("overlap_note")
    if not note:
        return
    # The marker also covers a GitHub reply lost after the comment was created.
    marker = "<!-- dev-auto-overlap-" + hashlib.sha256(rec["task"].encode()).hexdigest() + " -->"
    repo = f"{OWNER}/hr-relay"
    comments = command("gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments",
                       "--jq", f'.[] | select(.body | contains("{marker}")) | .id')
    if not comments.strip():
        command("gh", "issue", "comment", str(number), "-R", repo, "--body", f"{note}\n\n{marker}")
    del rec["overlap_note"]
    with db:
        db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))


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


def project_meta():
    return github_state("project")


def with_project(use):
    return use(project_meta())


def project_fields():
    return with_project(_project_fields)


def _project_fields(meta):
    project, fields = meta["id"], meta["fields"]
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
                                      model=tier[1], effort=tier[2], host=rec.get("host", "local"), at=rec["time"])


def publish_start(db, number, rec):
    tier = rec["route"]
    role = "research" if rec.get("research") else "coder"
    agent = f"{role} {tier[1]} {tier[2]}" + ("+advisor" if tier[3] else "") + " @" + rec["label"]
    for field, value in rec["fields"]:
        command("gh", "project", "item-edit", "--project-id", rec["project"], "--id", rec["item"],
                "--field-id", field, *(("--single-select-option-id", value) if value else ("--text", agent)))
    with db:
        db.execute("UPDATE starts SET receipt='done' WHERE issue=?", (number,))


def inventory(repo_path, issues, repo, emit=log, host="local", counts=None):
    import account_usage
    run = runtime if host == "local" else lambda *args: account_usage.host_orca(host, *args)
    trees = run("worktree", "list", "--repo", "path:" + str(repo_path), "--limit", "10000")
    terms = run("terminal", "list", "--limit", "10000")
    if trees["truncated"] or terms["truncated"]:
        raise OrcaError("incomplete Orca inventory")
    agents = {term["worktreeId"] for term in terms["terminals"] if term.get("agentIdentity")}
    if counts is not None:
        research = research_root() if host == "local" else None
        counts[host] = len({term["worktreeId"] for term in terms["terminals"]
                            if term.get("agentIdentity") and (
                                (term.get("branch") or "").removeprefix("refs/heads/").startswith("task/")
                                or (research and Path(term.get("worktreePath") or "").parent == research))})
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
                t["busy"] = t["live"] = True
        if not matches and tree["id"] in agents:
            emit("skip", {"branch": branch, "reason": "live worktree has no matching open issue with owned paths"})
    return {t["handle"] for t in terms["terminals"]}


def fill_tick(args, db, *, issues=None, heads=None, emit=log):
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
                if rec.get("not_applied"):
                    drop_unapplied(db, n, rec)
                elif "task" in rec:
                    actor, run = supervisor(args)
                    if rec["run"] != run:
                        raise OrcaError(f"reconcile from its owning Run {rec['run']}")
                    reconcile_start(db, n, rec, actor)
                else:
                    record_hold(rec)
                    publish_start(db, n, rec)
            except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                emit("pending", {"issue": n, "reason": str(e)})
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

    issues = mark_pending([dict(t) for t in (tickets() if issues is None else issues) if t["repo"] == repo])
    cfg = pick.account_usage.load_config()
    wsl = "Juzzark-WSL"
    targets = {"local": cfg.get("local_target_tickets", float("inf"))}
    paths = {"local": repo_path}
    remote_target = cfg.get("hosts", {}).get(wsl, {}).get("target_tickets", 0)
    if remote_target > 0:
        targets[wsl] = remote_target

    def scan(items):
        counts = {}
        live = {"local": inventory(repo_path, items, repo, emit, "local", counts)}
        if wsl in targets:
            try:
                if wsl not in paths:
                    paths[wsl] = Path(pick.account_usage.host_checkout(wsl, repo))
                live[wsl] = inventory(paths[wsl], items, repo, emit, wsl, counts)
            except (OSError, ValueError, OrcaError, subprocess.TimeoutExpired) as exc:
                emit("skip", {"host": wsl, "reason": str(exc)})
                del targets[wsl]
                paths.pop(wsl, None)
        return live, counts, time.time()

    snapshot = scan(issues)
    refs = set(command("git", "-C", str(repo_path), "for-each-ref", "--format=%(refname:short)",
                       "refs/heads", "refs/remotes/origin").splitlines())
    repo_heads = (github_state("heads") if heads is None else heads)[repo]
    for t in issues:
        t["skip"] = []
        if t["number"] in previous:
            t["skip"].append("reserved or completed")
        if t["branch"] in refs or "origin/" + (t["branch"] or "") in refs or t["branch"] in repo_heads:
            t["skip"].append("task branch exists")
        if not t["base"] or t["base"].removeprefix("origin/") not in repo_heads:
            t["skip"].append("base missing on origin")
    holds_path = (args.holds or pick.HOLDS).resolve()
    def seat(route, extra, state=snapshot, reservations=pending.copy(), *, local_only=False):
        all_holds = json.loads(holds_path.read_text()) if holds_path.exists() else {}
        reasons = []
        now = state[2]
        for host in targets:
            if local_only and host != "local":
                continue
            handles = state[0][host]
            planned = [h for h in extra if h["host"] == host]
            held = [h for handle, h in all_holds.items() if handle in handles and h["host"] == host]
            reserved = [rec for rec in reservations.values() if rec.get("host", "local") == host
                        and rec.get("route") and rec.get("account")]
            missing = sum(rec.get("terminal") not in handles for rec in reserved)
            if state[1].get(host, 0) + len(planned) + missing >= targets[host]:
                reasons.append(f"{host}: ticket target reached")
                continue
            for rec in reserved:
                if rec.get("terminal") in handles and rec["terminal"] in all_holds:
                    continue
                agent, model, effort = rec["route"][:3]
                held.append(dict(account=rec["account"], agent=agent, model=model, effort=effort))
            acct, why = pick.pick(cfg, *route[:3], host, now, holds=held + planned)
            if acct:
                return acct, reasons
            reasons.extend(why)
        return None, reasons

    count = 0
    decisions = {}
    def owed():
        return owed_restores(db) if db else ()

    for ticket, acct, reason in fill_plan(issues, seat, args.issue, owed):
        route, n = ticket["route"], ticket["number"]
        decisions[n] = reason
        if reason:
            emit("skip", {"issue": n, "reason": reason})
            continue
        host = acct.get("host", "local")
        emit("would start" if args.dry_run else "start", dict(issue=n, repo=repo, branch=ticket["branch"],
            base=ticket["base"], host=host, agent=route[0], model=route[1], effort=route[2], advisor=route[3], account=acct["label"]))
        if not args.dry_run:
            # Refresh GitHub, ownership and the picker immediately before committing to side effects.
            current = mark_pending([t for t in tickets(fresh=True) if t["repo"] == repo])
            latest = scan(current)
            if host not in targets:
                decisions[n] = "host unavailable"
                continue
            candidate = next(fill_plan(current, lambda r, extra, **opts: seat(r, extra, latest, pending, **opts),
                                       n, owed), None)
            # A failed WSL usage refresh can hide its seat while the Mac can still take other tickets.
            if host != "local" and candidate and candidate[2] == "no seat":
                decisions[n] = "no seat"
                emit("skip", {"issue": n, "host": host, "reason": "no seat"})
                continue
            if not candidate or not candidate[1] or candidate[1]["id"] != acct["id"] or candidate[1].get("host", "local") != host or any(
                candidate[0].get(k) != ticket.get(k) for k in ("branch", "base", "route", "paths", "item", "research")):
                raise OrcaError(f"#{n} readiness or seat changed")
            ref = "refs/heads/" + ticket["base"].removeprefix("origin/")
            wanted = ["refs/heads/" + ticket["branch"]] if ticket["branch"] else []
            path = paths[host]
            git = command if host == "local" else lambda *args: pick.account_usage.remote_output(host, path, args)
            git("git", "-C", str(path), "fetch", "--no-tags", "origin",
                    ref + ":refs/remotes/" + ticket["base"])
            latest_refs = git("git", "-C", str(path), "ls-remote", "--heads", "origin", ref, *wanted)
            sha = git("git", "-C", str(path), "rev-parse", "--verify", ticket["base"]).strip()
            if latest_refs.split() != [sha, ref]:
                raise OrcaError(f"#{n} base changed or task branch already exists on origin")
            try:
                launch(db, path, dict(ticket, sha=sha, overlap_note=overlap_note(ticket, current)),
                       acct, args, holds_path)
            except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                if not db or not db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone():
                    raise
                row = db.execute("SELECT receipt FROM starts WHERE issue=?", (n,)).fetchone()
                if not row or row[0] == "done":
                    raise
                pending[n] = json.loads(row[0])
                emit("pending", {"issue": n, "reason": str(e)})
                continue
        count += 1
    if not count:
        emit("fill", "no eligible start")
    return decisions


def require_pane_less(run):
    pane = any(k.startswith(("ORCA_AGENT_", "ORCA_TERMINAL_", "ORCA_PANE_", "ORCA_WORKTREE_",
                             "ORCA_WORKSPACE_", "ORCA_TAB_", "ORCA_RUNTIME_SOURCE_")) for k in os.environ)
    if not run or pane:
        raise OrcaError("an explicit supervisor anchor and Run are for a pane-less invocation only")


def supervisor(args):
    actor = []
    if args.anchor or args.run:
        require_pane_less(args.run)
        if not args.anchor:
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
        exclude = Path(command("git", "-C", str(path), "rev-parse", "--path-format=absolute",
                               "--git-path", "info/exclude").strip())
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if ".codex/" not in lines:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("".join(line + "\n" for line in [*lines, ".codex/"]))
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


def remote_tree_settings(host, repo, wt, route, *, rollback=False, change=None, existed=False):
    """Run this version of the settings and guarded cleanup helpers in the remote checkout."""
    import account_usage
    script = """import hashlib, json, os, re, subprocess, sys, tempfile, tomllib
from pathlib import Path
def command(*args):
    return subprocess.check_output(args, text=True)
def runtime(*args):
    reply = json.loads(command("orca", *args, "--environment", "local", "--json"))
    if not reply["ok"]: raise OSError("worktree removal failed")
    return reply["result"]
OrcaError = RuntimeError
def log(step, value):
    print(step, value, file=sys.stderr)
"""
    if rollback:
        script += inspect.getsource(remove_prelaunch_tree) + """
repo, wt, ref_existed, change = json.loads(sys.argv[1])
path = Path(wt["path"])
settings = None
if change:
    relative, old_digest, written_digest = change
    target = path / relative
    if target.parent.is_symlink() or target.is_symlink(): raise ValueError("settings path changed")
    written = target.read_bytes()
    if hashlib.sha256(written).hexdigest() != written_digest: raise ValueError("settings changed")
    before = None
    if old_digest is not None:
        before = subprocess.check_output(["git", "-C", str(path), "show", "HEAD:" + relative])
        if hashlib.sha256(before).hexdigest() != old_digest: raise ValueError("preserve original settings")
    settings = target, before, written
refs = ["refs/heads/" + wt["branch"].removeprefix("refs/heads/")] if ref_existed else []
remove_prelaunch_tree(Path(repo), wt, refs, settings)
print("null")
"""
        data = [str(repo), {k: wt.get(k) for k in ("id", "path", "branch", "head", "isMainWorktree")}, existed, change]
    else:
        script += inspect.getsource(configure_codex_notice) + inspect.getsource(worker_settings) + """
path, route = json.loads(sys.argv[1])
change = worker_settings(Path(path), route)
if change:
    target, before, written = change
    change = [str(target.relative_to(path)), hashlib.sha256(before).hexdigest() if before is not None else None,
              hashlib.sha256(written).hexdigest()]
print(json.dumps(change))
"""
        data = [wt["path"], route]
    return json.loads(account_usage.remote_output(host, repo, ["python3", "-c", script, json.dumps(data)]))


def start_identity(receipt):
    terminals = [effect["id"] for effect in receipt.get("effects", [])
                 if effect.get("kind") == "terminal" and effect.get("role") == "agent" and effect.get("id")]
    if not receipt.get("dispatchId") or len(terminals) != 1:
        raise OrcaError("incomplete worker start receipt")
    return receipt["dispatchId"], terminals[0]


def codex_active(route):
    """The Codex account the coordinator bot runs on, read before a Codex worker start selects its own."""
    return runtime("account", "list")["codex"]["activeAccountId"] if route[0] == "codex" else None


def owed_restores(db):
    """Coordinator Codex accounts that fill, review or watch records still owe. While one is owed the active
    Codex account may be a worker seat, so another Codex selection would record that seat as the coordinator's."""
    owed = {}
    for table, key, value in (("starts", "issue", "receipt"), ("reviews", "run || ':' || issue", "state"),
                              ("watch", "run || ':' || terminal", "state")):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            continue
        for k, raw in db.execute(f"SELECT {key},{value} FROM {table}"):
            try:
                rec = json.loads(raw or "null")
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("coordinator"):
                owed[table + ":" + str(k)] = rec["coordinator"]
    return owed


def restore_coordinator(prior):
    """After a confirmed Codex start, give the coordinator bot its account back, or a fresher one."""
    import pick
    if not prior:
        return
    target = (pick.coordinator(pick.account_usage.load_config(), "local", time.time(), prior) or {}).get("id") or prior
    runtime("account", "select", "--agent", "codex", "--account", target)
    if runtime("account", "list")["codex"]["activeAccountId"] != target:
        raise OrcaError("coordinator account selection changed")


def launch(db, repo_path, t, acct, args, holds_path):
    project, fields, waiting_field = project_fields()
    number, route = t["number"], t["route"]
    research = t.get("research", False)
    if research:
        spec = (f"Research julianthant/hr-relay#{number} ({t['title']}). Read its current issue and follow its research brief. "
                "Work read-only from this detached origin/development checkout. Change no repository files, create no branch, "
                "and make no commits, pushes, or PRs. Deliver the findings as the issue specifies, then send worker_done "
                "with the report or its location to the coordinating Run. Report a failed outcome if you cannot finish.")
    else:
        spec = (HERE.parent / "references/coder-spec.md").read_text().format(**t)
    actor, run = supervisor(args)
    if db.execute("SELECT name FROM sqlite_master WHERE name='starts'").fetchone() and db.execute(
            "SELECT 1 FROM starts WHERE issue=?", (number,)).fetchone():
        raise OrcaError(f"issue #{number} already reserved")
    import account_usage
    import pick
    host = acct.get("host", "local")
    remote = host != "local"
    managed_account = route[0] not in pick.SLOT_AGENTS
    if not managed_account and not pick.mac_host(host):
        raise OrcaError(f"{route[0]} requires the local Mac")
    if research and remote:
        raise OrcaError("detached research requires a local checkout")
    git = command if not remote else lambda *args: account_usage.remote_output(host, repo_path, args)
    tree_command = runtime if not remote else lambda *args: account_usage.host_orca(host, *args)
    account = runtime if not remote else lambda _, *args: account_usage.remote_account(host, *args)
    branch = t["branch"]
    if research:
        folder = research_root()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix=f"issue-{number}-", dir=folder))
    else:
        existing_refs = set(git("git", "-C", str(repo_path), "for-each-ref", "--format=%(refname)", "refs/heads").splitlines())
        if remote and "refs/heads/" + branch in existing_refs:
            raise OrcaError(f"{host}: task branch already exists")
        wt = tree_command("worktree", "create", "--repo", "path:" + str(repo_path), "--name", branch[5:].replace("/", "."),
                     "--branch", branch, "--base-branch", t["base"], "--no-parent", "--setup", "skip")["worktree"]
        path = Path(wt["path"])
    settings_change = task = prior = None
    reserved = False
    try:
        if research:
            git("git", "-C", str(repo_path), "worktree", "add", "--detach", str(path), t["sha"])
            wt = tree_command("worktree", "show", "--worktree", "path:" + str(path))["worktree"]
            if Path(wt["path"]).resolve() != path or wt.get("isMainWorktree"):
                raise OrcaError("Orca selected a different research worktree")
        actual_branch = git("git", "-C", str(path), "branch", "--show-current").strip()
        if (wt["branch"] or "").removeprefix("refs/heads/") != (branch or "") or actual_branch != (branch or ""):
            raise OrcaError("created worktree does not match the requested branch state")
        if git("git", "-C", str(path), "rev-parse", "HEAD").strip() != t["sha"]:
            raise OrcaError("created worktree has a different base commit")
        if remote:
            settings_change = remote_tree_settings(host, repo_path, wt, route)
        else:
            if not research:
                settings_change = worker_settings(path, route)
            prior = codex_active(route)
        # Owed before the selection: if this start fails and its restore too, the next fill restores it.
        reserve(db, number, {"not_applied": True, "coordinator": prior})
        reserved = True
        if managed_account:
            account("account", "select", "--agent", route[0], "--account", acct["id"])
        task = runtime("orchestration", "task-create", *actor, "--task-title", f"#{number} " + ("research" if research else "coder"),
                       "--spec", spec, "--full")["task"]["id"]
        if managed_account and account("account", "list")[route[0]]["activeAccountId"] != acct["id"]:
            raise OrcaError("selected account changed before worker start")
        rec = dict(host=host, task=task, run=run, worktree=wt["id"], path=str(path), account=acct["id"], label=acct["label"],
                   route=route, project=project, item=t["item"], fields=fields, time=int(time.time()),
                   holds=str(holds_path), waiting_field=waiting_field, coordinator=prior, research=research)
        if t.get("overlap_note"):
            rec["overlap_note"] = t["overlap_note"]
        # The fill lock covers preparation. A durable Task identity precedes the only start call.
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
    except Exception as e:
        left = ""
        if task:
            try:
                runtime("orchestration", "task-update", *actor, "--id", task, "--status", "failed", "--result", "cancelled")
            except (OSError, OrcaError, ValueError, subprocess.TimeoutExpired) as cancel_error:
                left = f"; inspect uncancelled Task {task} ({cancel_error})"
        tree = f"rolled back worktree {path}"
        try:
            if research:
                if (path / ".git").is_file():
                    git("git", "-C", str(repo_path), "worktree", "remove", str(path))
                else:
                    path.rmdir()
            elif remote:
                remote_tree_settings(host, repo_path, wt, route, rollback=True, change=settings_change,
                                     existed="refs/heads/" + branch in existing_refs)
            else:
                remove_prelaunch_tree(repo_path, wt, existing_refs, settings_change)
        except (OSError, OrcaError, ValueError, subprocess.TimeoutExpired) as cleanup_error:
            log("prelaunch cleanup", {"issue": number, "worktree": str(path), "reason": str(cleanup_error)})
            tree = f"inspect prepared worktree {path}"
        try:
            restore_coordinator(prior)
            if reserved:
                with db:
                    db.execute("DELETE FROM starts WHERE issue=?", (number,))
        except (OSError, OrcaError, ValueError, KeyError, subprocess.TimeoutExpired) as restore_error:
            left += f"; restore the coordinator's Codex account {prior} ({restore_error})"
            if reserved:
                with db:
                    db.execute("UPDATE starts SET receipt=? WHERE issue=?",
                               (json.dumps({"not_applied": True, "coordinator": prior}), number))
        raise OrcaError(f"#{number}: {e}{left}; {tree}") from e
    try:
        started = runtime("orchestration", "worker-start", *actor, "--task", task, *(("--on", host) if remote else ()),
                          "--worktree", "path:" + str(path),
                          "--agent", route[0], "--model", route[1], "--effort", route[2],
                          *(("--keep-open",) if not research else ()), "--full")
        rec["dispatch"], rec["terminal"] = start_identity(started)
        if route[0] == "grok" and started.get("turnStart") == "observed":
            rec["turn_observed"] = True
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
    except StartNotApplied as e:
        rec["not_applied"] = True
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
        drop_unapplied(db, number, rec)
        raise OrcaError(f"#{number}: Orca confirmed no start effects; inspect prepared worktree {path}: {e}") from e
    except (OrcaError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        log("start receipt unavailable", {"issue": number, "task": task, "reason": str(e)})
    reconcile_start(db, number, rec, actor)


def drop_unapplied(db, number, rec):
    """A start Orca confirmed never happened: restore the coordinator, then free the reservation."""
    restore_coordinator(rec.get("coordinator"))
    with db:
        db.execute("DELETE FROM starts WHERE issue=?", (number,))


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


def return_coordinator(db, number, rec):
    if rec.get("coordinator"):
        restore_coordinator(rec["coordinator"])
        rec.pop("coordinator")
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))


def fleet_workers(run):
    cursor = []
    seen = set()
    while True:
        inventory = runtime("orchestration", "worker-list", "--run", run, "--include-remote",
                            "--full", "--limit", "100", *cursor)
        yield from inventory["workers"]
        page = inventory["page"]
        if not page["hasMore"]:
            return
        next_cursor = page["nextCursor"]
        if not next_cursor or next_cursor in seen:
            raise OrcaError("incomplete Orca worker inventory")
        seen.add(next_cursor)
        cursor = ["--cursor", next_cursor]


def worker_host(worker):
    return (worker.get("server") or {}).get("name") or worker.get("host") or "local"


def remote_start_dispatch(run, task, host):
    return next((w["dispatchId"] for w in fleet_workers(run)
                 if w.get("taskId") == task and worker_host(w) == host), None)


def execution_worker_show(host, path, dispatch_id):
    import account_usage
    # A paired worker-show can return the coordinator's server record. Query Orca on the execution host.
    response = json.loads(account_usage.remote_output(
        host, Path(path), ("orca", "orchestration", "worker-show", "--dispatch", dispatch_id,
                           "--full", "--environment", "local", "--json")))
    if response.get("ok") is not True:
        raise OrcaError(f"{host}: worker-show failed: {response.get('error')}")
    return response["result"]


def record_fill_worker(db, number, rec, path):
    if rec.get("research"):
        record_hold(rec)
        return
    run = rec["run"]
    current = review_state(db, run, number)
    if current and current["coder"] != rec["dispatch"]:
        return
    record_hold(rec)
    if current:
        return
    _, model, effort, advisor = rec["route"]
    agent = f"coder {model} {effort}" + ("+advisor" if advisor else "") + " @" + rec["label"]
    state = dict(path=path, host=rec.get("host", "local"), holds=rec["holds"], model=model, agent=agent,
                 history=[], stage="Coding")
    assign_coder(state, rec["dispatch"], rec["terminal"])
    save_review(db, run, number, state)


def grok_turn_observed(dispatch):
    # Orca can mark a pasted prompt accepted. An exact transcript shows whether Grok responded.
    try:
        read = runtime("orchestration", "worker-read", "--dispatch", dispatch,
                       "--source", "transcript", "--limit", "20", "--full")
    except (OrcaError, OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (read.get("dispatchId") == dispatch and read.get("provider") == "grok"
            and read.get("source") == "transcript" and read.get("sourceExact") is True
            and any(m.get("role") in ("assistant", "reasoning", "tool") and m.get("blocks")
                    for m in (read.get("transcript") or {}).get("messages", ())))


def startup_needs_you(db, number, rec, actor, prompt):
    notice = {"issue": number, "task": rec["task"], "dispatch": rec["dispatch"], "prompt": prompt}
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
        rec["waiting_card"] = True
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?", (json.dumps(rec), number))
    post_overlap_note(db, number, rec)
    log("waiting for operator input", notice)


def reconcile_start(db, number, rec, actor):
    task, run = rec["task"], rec["run"]
    host = rec.get("host", "local")
    worker_rpc = runtime
    if host != "local":
        import account_usage
        worker_rpc = lambda *args: account_usage.host_orca(host, *args)
    if rec.get("dispatch"):
        return_coordinator(db, number, rec)
    try:
        if host == "local":
            lookup = runtime("orchestration", "dispatch-show", "--task", task, "--full")
            dispatch_id = (lookup.get("dispatch") or {}).get("id")
        else:
            dispatch_id = rec.get("dispatch") or remote_start_dispatch(run, task, host)
        if not dispatch_id:
            raise OrcaError("no Dispatch is visible")
        show = runtime("orchestration", "worker-show", "--dispatch", dispatch_id, "--full")
        bound = show["dispatch"]
        if (bound["id"], bound["taskId"], bound["runId"]) != (dispatch_id, task, run):
            raise OrcaError("coordinator Dispatch identity changed")
        path = rec.get("path")
        if host != "local":
            tree = worker_rpc("worktree", "show", "--worktree", "id:" + rec["worktree"])["worktree"]
            if tree["id"] != rec["worktree"]:
                raise OrcaError("worker worktree changed")
            path = tree["path"]
            if (show.get("worker") or {}).get("stage") == "server_record":
                show = execution_worker_show(host, path, dispatch_id)
        elif path is None:
            tree = worker_rpc("worktree", "show", "--worktree", "id:" + rec["worktree"])["worktree"]
            if tree["id"] != rec["worktree"]:
                raise OrcaError("worker worktree changed")
            path = tree["path"]
    except (OrcaError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise OrcaError(f"#{number} reserved: inspect Task {task} in Run {run}: {e}") from e
    dispatch = show["dispatch"]
    worker = show.get("worker") or {}
    if ((dispatch["id"], dispatch["taskId"]) != (dispatch_id, task)
            or worker.get("worktreeId") != rec["worktree"]):
        raise OrcaError(f"#{number} reserved: unverified worker for Task {task}")
    terminal = dispatch.get("assigneeHandle")
    if (bound.get("retryOfDispatchId") or dispatch.get("retryOfDispatchId")
            or (rec.get("dispatch") and rec["dispatch"] != dispatch_id)):
        raise OrcaError(f"#{number} reserved: Task {task} has a different attempt; inspect {dispatch_id}")
    if rec.get("terminal") and rec["terminal"] != terminal:
        raise OrcaError(f"#{number} reserved: assignee changed for Dispatch {dispatch_id}")
    # The verified attempt has consumed the worker's selection; the coordinator's comes back before anything else can fail.
    return_coordinator(db, number, rec)
    if host != "local" and not rec.get("dispatch") and terminal:
        with db:
            db.execute("UPDATE starts SET receipt=? WHERE issue=?",
                       (json.dumps(dict(rec, terminal=terminal, dispatch=dispatch_id)), number))
    rec = dict(rec, terminal=terminal, dispatch=dispatch_id)
    resource = (show.get("projection") or {}).get("resource") or {}
    released_failure = worker["state"] == "failed" and resource.get("terminalState") == "released"
    if terminal and not released_failure:
        record_fill_worker(db, number, rec, path)
    grok = rec["route"][0] == "grok"
    if (grok and dispatch.get("status") != "completed" and not rec.get("turn_observed")
            and not grok_turn_observed(dispatch_id)):
        startup_needs_you(db, number, rec, actor, "Orca has no observed Grok turn. Inspect the worker before retrying.")
        return
    if released_failure or dispatch.get("status") == "completed":
        if terminal and (show.get("terminal") or {}).get("handle") == terminal and not released_failure:
            post_overlap_note(db, number, rec)
        log("already settled", {"issue": number, "task": task, "dispatch": dispatch_id, "state": worker["state"]})
        with db:
            db.execute("UPDATE starts SET receipt='done' WHERE issue=?", (number,))
        return
    if not terminal:
        raise OrcaError(f"#{number} reserved: inspect {dispatch_id}, worker state {worker['state']}")
    if rec["route"][0] == "codex":
        observed_terminal = show.get("terminal") or {}
        observation = show.get("observation") or {}
        wait = observation.get("agentWait") or observed_terminal.get("agentWait")
        prompt = None
        if observed_terminal:
            if observed_terminal.get("handle") != terminal:
                raise OrcaError(f"#{number} reserved: worker terminal identity changed")
            tail = worker_rpc("terminal", "read", "--terminal", terminal, "--screen")["terminal"]["tail"]
            prompt = codex_prompt(tail)
        if not observed_terminal and "agentWait" not in observation:
            raise OrcaError(f"#{number} reserved: no Codex startup observation for {dispatch_id}")
        if prompt or wait:
            startup_needs_you(db, number, rec, actor, prompt or wait)
            return
    if worker["state"] != "ready" and not (grok and worker.get("stage") == "turn_start_unobserved"):
        raise OrcaError(f"#{number} reserved: inspect {dispatch_id}, worker state {worker['state']}")
    post_overlap_note(db, number, rec)
    publish_start(db, number, rec)
    log("started", {"issue": number, "dispatch": dispatch_id})


def fill(args, *, issues=None, heads=None, emit=log):
    import fcntl
    state = Path.home() / ".local/state/dev-auto"
    path = state / "starts.sqlite"
    if args.dry_run:
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                return fill_tick(args, db, issues=issues, heads=heads, emit=emit)
        return fill_tick(args, None, issues=issues, heads=heads, emit=emit)
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
                    row = db.execute("SELECT receipt FROM starts WHERE issue=?", (args.release,)).fetchone()
                    if row and row[0] not in (None, "done"):
                        restore_coordinator(json.loads(row[0]).get("coordinator"))
                    with db:
                        deleted = db.execute("DELETE FROM starts WHERE issue=? AND receipt IS NOT 'done'",
                                             (args.release,)).rowcount
                log("released" if deleted else "no pending reservation", {"issue": args.release})
                return
            fill_tick(args, db)


def review_route(body):
    match = re.search(r"(?im)\bReviewer:[ \t]*([^\n·]+)", body)
    value = re.split(r"(?i)\bTier:", match[1])[0].strip(" .") if match else ""
    coder = coder_route(body)
    route = coder_route("Coder: " + value)
    if coder and coder[0] in ("grok", "cursor") and not research_brief(body):
        if not route or route[0] != "claude" or not route[1].startswith("claude-sonnet-"):
            raise OrcaError("Grok coding needs a Sonnet reviewer")
    if re.match(r"(?i)^none\b", value) and re.search(r"(?i)\bTier:\s*UI,?\s+simple\b", body):
        return None
    if not route or route[0] not in ("claude", "codex"):
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


CHECKPOINT_HANDOFF = "handed off with a checkpoint"
USAGE_REPORT = re.compile(r"\b(?:usage|rate|5[- ]?hour|5h|weekly|session)\s+limit\b", re.IGNORECASE)


def failed_reviews(db, run, state, messages):
    """The reviewer's failed reports, except while watch restarts it from a handoff."""
    terminal = (state.get("reviewer") or {}).get("terminal")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='watch'").fetchone():
        row = db.execute("SELECT state FROM watch WHERE run=? AND terminal=?", (run, terminal)).fetchone()
        watch = json.loads(row[0]) if row else {}
        pending = watch.get("pending")
        if watch.get("handoff") or (pending and not pending.get("crash")):
            return []
    return [m for m in messages if m["type"] == "worker_done" and mail_payload(m).get("outcome") == "failed"
            and mail_payload(m).get("dispatchId") == state["expected"]]


def latest_coder_report(state, run, messages, handled=()):
    reports = review_mail(messages, state["coder"])
    latest = reports[-1] if reports else {}
    terminal = latest.get("from_handle") or state.get("coder_terminal")
    for message in sorted(messages, key=lambda m: m["sequence"], reverse=True):
        if latest and message["sequence"] <= latest["sequence"]:
            break
        payload = mail_payload(message) if message["type"] == "worker_done" else {}
        dispatch = payload.get("dispatchId")
        if not dispatch or payload.get("outcome") != "succeeded" or dispatch == state["coder"] or message["id"] in handled:
            continue
        if in_coder_chain(dispatch, state["coder"], run):
            state["coder"] = state["expected"] = dispatch
            state["coder_terminal"] = message.get("from_handle") or terminal
            return [message]
    return [latest] if latest else []


def green_report(ticket, body):
    name = ticket["repo"]
    repo = f"{OWNER}/{name}"
    branch = ticket["branch"]
    head = github_state("heads")[name].get(branch)
    if head not in re.findall(r"(?i)\b[0-9a-f]{40}\b", body):
        return None
    workflow, event = github_poller.task_workflow(name)
    cached = github_state("runs", repo=name, sha=head, workflow=workflow, event=event)
    if not cached or (cached[0]["head_branch"], cached[0]["status"], cached[0]["conclusion"]) != (
            branch, "completed", "success"):
        return None
    # The report would start a reviewer or advance to merge. Recheck the exact head once.
    current = command("gh", "api", f"repos/{repo}/git/matching-refs/heads/{branch}", "--jq",
                      f'.[] | select(.ref == "refs/heads/{branch}") | .object.sha').strip()
    if current != head:
        return None
    runs = json.loads(command("gh", "run", "list", "--repo", repo, "--branch", branch, "--commit", head,
                              "--workflow", workflow, "--event", event, "--limit", "1",
                              "--json", "headSha,headBranch,status,conclusion"))
    if runs and runs[0] == {"headSha": head, "headBranch": branch, "status": "completed", "conclusion": "success"}:
        return head


def review_account(route, host):
    import pick
    account, reasons = pick.pick(pick.account_usage.load_config(), *route[:3], host, time.time())
    if account is None:
        raise OrcaError("no reviewer seat: " + ", ".join(reasons))
    return account


def review_card(ticket, stage, agent):
    def ids(meta):
        fields = meta["fields"]
        options = {o["name"]: o["id"] for o in fields["Agent state"]["options"]}
        return meta["id"], fields["Agent"]["id"], fields["Agent state"]["id"], options[stage]
    project, agent_field, stage_field, option = with_project(ids)
    for field, flag, value in ((agent_field, "--text", agent), (stage_field, "--single-select-option-id", option)):
        command("gh", "project", "item-edit", "--project-id", project, "--id", ticket["item"],
                "--field-id", field, flag, value)


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


def assign_coder(state, dispatch, terminal):
    state.update(coder=dispatch, expected=dispatch, coder_terminal=terminal)


def assign_reviewer(state, dispatch, terminal, hold):
    if hold.get("account"):
        record_hold(dict(hold, terminal=terminal))
    state.pop("hold", None)
    state["expected"] = dispatch
    state["reviewer"] = {"dispatch": dispatch, "terminal": terminal, "holds": hold["holds"]}
    route = hold["route"]
    advisor = "+advisor" if route[3] else ""
    state["card_agent"] = f"{state['agent']} · reviewer {route[1]} {route[2]}{advisor} @{hold['label']}"


def review_dispatch(run, task, state):
    host = state.get("host", "local")
    if host != "local":
        return remote_start_dispatch(run, task, host)
    return (runtime("orchestration", "dispatch-show", "--task", task, "--full").get("dispatch") or {}).get("id")


def adopt_reviewer(db, run, ticket, state, messages):
    import pick
    number = ticket["number"]
    error = UncertainReview(f"inspect uncertain Orca action for message {state['pending']} before continuing")
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
    if not matches:
        # A newer report can supersede the reservation once the Run shows no matching reviewer Task.
        newer = [m for m in messages if m["sequence"] > report["sequence"]]
        handled = {i for (i,) in db.execute("SELECT id FROM review_mail WHERE run=?", (run,))}
        coder = state["coder"]
        if latest_coder_report(state, run, newer, handled) and state["coder"] != coder:
            for key in ("pending", "review_title", "hold", "followup"):
                state.pop(key, None)
            save_review(db, run, number, state)
            return
    if len(matches) != 1:
        raise error
    task = matches[0]["id"]
    dispatch = review_dispatch(run, task, state)
    if not dispatch:
        raise error
    show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
    attempt = show["dispatch"]
    host = state.get("host", "local")
    execution = show
    if host != "local":
        import account_usage
        tree = account_usage.host_orca(host, "worktree", "show", "--worktree", "path:" + state["path"])["worktree"]
        if (show.get("worker") or {}).get("stage") == "server_record":
            execution = execution_worker_show(host, tree["path"], dispatch)
    else:
        tree = runtime("worktree", "show", "--worktree", "id:" + show["worker"]["worktreeId"])["worktree"]
    worker = execution["worker"]
    effective = worker["startOptions"]["launch"]["effective"]
    executed = execution["dispatch"]
    terminal = executed.get("assigneeHandle")
    if (not terminal or attempt["id"] != dispatch or attempt["runId"] != run or attempt["taskId"] != task
            or attempt.get("retryOfDispatchId") or attempt.get("followUpOf")
            or (executed["id"], executed["taskId"]) != (dispatch, task)
            or executed.get("retryOfDispatchId") or executed.get("followUpOf")
            or attempt.get("assigneeHandle") not in (None, terminal)
            or tree["id"] != worker["worktreeId"] or tree["path"] != state["path"]
            or tuple(effective.get(k) for k in ("agent", "model", "effort")) != route[:3]):
        raise error
    hold = state.get("hold")
    if not hold:
        with pick.edit_holds(pick.HOLDS) as holds:
            entry = holds.get(terminal)
        if (entry and entry.get("host", "local") == host
                and all(entry.get(k) == v for k, v in zip(("agent", "model", "effort"), route))):
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


def followup_attempt(run, state, dispatch):
    attempt = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")["dispatch"]
    if (attempt["id"], attempt["runId"]) != (dispatch, run):
        raise OrcaError(f"unverified follow-up Dispatch {dispatch} in Run {run}")
    host = state.get("host", "local")
    if host != "local" and not attempt.get("assigneeHandle"):
        execution = execution_worker_show(host, state["path"], dispatch)["dispatch"]
        if (execution["id"], execution["taskId"]) != (dispatch, attempt["taskId"]):
            raise OrcaError(f"unverified follow-up Dispatch {dispatch} on {host}")
        attempt = dict(attempt, assigneeHandle=execution.get("assigneeHandle"))
    return attempt


def finish_followup(run, state, stage, attempt, previous):
    import pick
    dispatch = attempt["id"]
    terminal = attempt.get("assigneeHandle")
    if not terminal:
        raise OrcaError(f"no terminal assigned to follow-up Dispatch {dispatch}")
    if attempt.get("followUpOf") not in (None, previous):
        raise OrcaError(f"follow-up Dispatch {dispatch} changed predecessor")
    if previous == state["coder"]:
        source = state.get("coder_terminal")
        saved_hold = (state.get("followup") or {}).get("hold")
    else:
        source = followup_attempt(run, state, previous).get("assigneeHandle")
        saved_hold = None
    if source or saved_hold:
        with pick.edit_holds(Path(state.get("holds") or pick.HOLDS)) as holds:
            hold = holds.get(source) or saved_hold
            if hold and hold.get("account") and hold.get("host", "local") == state.get("host", "local"):
                holds.setdefault(terminal, hold)
    assign_coder(state, dispatch, terminal)
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
    dispatch = review_dispatch(run, task, state)
    if not dispatch:
        return False
    attempt = followup_attempt(run, state, dispatch)
    previous = attempt.get("retryOfDispatchId") or attempt.get("followUpOf")
    if attempt["taskId"] != task or not in_coder_chain(previous, state["coder"], run):
        return False
    finish_followup(run, state, stage, attempt, previous)
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


def resume_pending(db, run, number, dispatches):
    """A watch restart of this ticket whose resume note or card restore has no receipt yet."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='watch'").fetchone():
        return False
    for (raw,) in db.execute("SELECT state FROM watch WHERE run=?", (run,)):
        row = json.loads(raw)
        if (row.get("note") or {}).get("to") in dispatches or (row.get("card") or {}).get("number") == number:
            return True
    return False


def review_coordinator(db, run, number, state):
    """Give the coordinator its Codex account back once the reviewer start is settled, before any card write."""
    if state.get("coordinator") and not state.get("pending"):
        restore_coordinator(state["coordinator"])
        state.pop("coordinator")
        save_review(db, run, number, state)


def review_step(db, run, actor, ticket, initial, messages):
    import account_usage
    import pick
    number = ticket["number"]
    state = review_state(db, run, number) or initial.copy()
    host = state.get("host", "local")
    db.execute("CREATE TABLE IF NOT EXISTS review_mail (run TEXT, id TEXT, PRIMARY KEY(run,id))")
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
    review_coordinator(db, run, number, state)
    if state.get("publish"):
        publish_review(db, run, ticket, state)
    if state["stage"] in ("Merging", "Needs you"):
        return
    if resume_pending(db, run, number, {state["coder"], state["expected"]}):
        return
    reports = (sorted(review_mail(messages, state["expected"]) + failed_reviews(db, run, state, messages),
                      key=lambda m: m["sequence"]) if state["stage"] == "Reviewing"
               else latest_coder_report(state, run, messages,
                                        {i for (i,) in db.execute("SELECT id FROM review_mail WHERE run=?", (run,))}))
    for message in reports:
        if not db.execute("SELECT 1 FROM review_mail WHERE run=? AND id=?", (run, message["id"])).fetchone():
            break
    else:
        return

    if state["stage"] != "Reviewing":
        state["agent"] = held_label(state["agent"], message.get("from_handle") or state.get("coder_terminal"))
    body = message["body"]
    stage = "Fixing"
    instructions = GREEN_INSTRUCTIONS
    account = None
    if state["stage"] == "Reviewing":
        header = body.splitlines()[0] if body else ""
        verdict = re.fullmatch(r"REVIEW VERDICT: (PUSH|FIX) ([a-f0-9]{40})", header)
        if not verdict and (not header.startswith("REVIEW VERDICT:") or header.startswith(message["subject"])):
            verdict = re.fullmatch(r"REVIEW VERDICT: (PUSH|FIX) ([a-f0-9]{40})", message["subject"])
        failed = mail_payload(message).get("outcome") == "failed"
        if failed or not verdict or verdict[2] != state["sha"]:
            why = "reviewer failed" if failed else "invalid verdict"
            state.update(stage="Needs you", publish=True, card_agent=f"coordinator · {why} {message['id']}")
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
            instructions = (f"The reviewer's PUSH of {state['sha']} was voided: the branch head moved "
                            f"or its task CI is not green.\n{GREEN_INSTRUCTIONS}")
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
                owed = host == "local" and route[0] == "codex" and set(owed_restores(db)) - {f"reviews:{run}:{number}"}
                if owed:
                    log("review", {"issue": number, "waiting": f"coordinator restore still owed by {sorted(owed)}"})
                    return
                account = review_account(route, host)
                path = Path(state["path"])
                if host == "local":
                    worker_settings(path, route)
                    account_rpc = lambda *args: runtime("account", *args)
                    prior = state.get("coordinator") or codex_active(route)
                else:
                    if route[0] == "codex" or route[3]:
                        remote_tree_settings(host, path, {"path": state["path"]}, route)
                    account_rpc = lambda *args: account_usage.remote_account(host, *args)
                    prior = None
                if prior:
                    # Owed from here on: the next tick restores it before reading this report again.
                    state["coordinator"] = prior
                    save_review(db, run, number, state)
                account_rpc("select", "--agent", route[0], "--account", account["id"])
                if account_rpc("list")[route[0]]["activeAccountId"] != account["id"]:
                    raise OrcaError("reviewer account selection changed")
                stage = "Reviewing"
            else:
                stage = "Merging"

    dot = ticket["repo"] == "dot-agents"
    if stage == "Merging":
        closes = f"Closes {OWNER}/hr-relay#{number}" if dot else f"Closes #{number}"
        trunk = "main" if dot else "development"
        instructions = (f"Starting at approved {state['sha']}, merge origin/{trunk}, "
                        f"push {ticket['branch']}, open or update the PR into {trunk}, "
                        f"end the body with {closes}, mark the PR ready, and enable auto-merge through required checks.")
    if account:
        spec = reviewer_spec(ticket, state["sha"], route[1] == state["model"])
        state["review_title"] = f"#{number} reviewer {message['id']}"
        state["hold"] = {"account": account["id"], "label": account["label"], "route": route,
                         "time": int(time.time()), "holds": str(pick.HOLDS), "host": host}
    else:
        spec = coder_spec(ticket, stage, instructions)
        if stage != "Needs you":
            hold = (pick.account_usage.load_json(state.get("holds") or pick.HOLDS) or {}).get(state.get("coder_terminal"))
            state["followup"] = {"stage": stage, "spec": spec, "hold": hold}
    # A lost response may already have launched a worker. Reserve before issuing the command.
    state["pending"] = message["id"]
    with db:
        db.execute("INSERT INTO review_mail VALUES (?,?)", (run, message["id"]))
        db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(state)))
    state["card_agent"] = state["agent"]
    if account:
        try:
            result = runtime("orchestration", "worker-start", *actor, *(("--on", host) if host != "local" else ()),
                             "--worktree", "path:" + state["path"],
                             "--agent", route[0], "--model", route[1], "--effort", route[2],
                             "--task-title", state["review_title"], "--spec", spec, "--full")
        except StartNotApplied as e:
            # No reviewer exists: drop the reservation so the report is read again, then restore the coordinator.
            for key in ("pending", "review_title", "hold"):
                state.pop(key, None)
            with db:
                db.execute("DELETE FROM review_mail WHERE run=? AND id=?", (run, message["id"]))
                db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(state)))
            review_coordinator(db, run, number, state)
            raise OrcaError(f"#{number}: Orca confirmed no reviewer start effects: {e}") from e
        dispatch, terminal = start_identity(result)
        assign_reviewer(state, dispatch, terminal, state["hold"])
        state.pop("review_title")
    elif stage == "Needs you":
        # From the Run's own pane Orca accepts only status mail to run:<id> (sender_not_assignee otherwise).
        runtime("orchestration", "send", *actor, "--to", "run:" + run, "--type", "status",
                "--subject", f"#{number}: third FIX", "--body", instructions)
        state["card_agent"] = "coordinator"
    else:
        # The Run home relays Dispatch-addressed mail to the coder's execution server.
        result = runtime("orchestration", "send", *actor, "--to", "dispatch:" + state["coder"],
                         "--subject", f"#{number}: {stage}", "--body", spec)
        dispatch = followup_dispatch(result, state["coder"], run)
        finish_followup(run, state, stage, followup_attempt(run, state, dispatch), result["followUpOf"])
    if stage == "Needs you" and state.get("reviewer"):
        state["release"] = state.pop("reviewer")
    state.pop("pending")
    state.update(stage=stage, publish=True)
    save_review(db, run, number, state)
    review_coordinator(db, run, number, state)
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
        if ticket.get("research") or research_brief(ticket.get("body", "")):
            continue
        try:
            if args.coder:
                owner = runtime("orchestration", "worker-show", "--dispatch", args.coder, "--full")["dispatch"]["runId"]
                if owner != run:
                    raise OrcaError(f"coder Dispatch {args.coder} belongs to Run {owner}, not the bound Run {run}")
            state = review_state(db, run, number)
            if not state:
                ids = [t["id"] for t in tasks if t["title"] == f"#{number} coder"]
                if not ids and not args.coder:
                    if args.issue:
                        raise OrcaError("supply --coder with the initial coder Dispatch")
                    continue
                if not ticket["item"] or (not args.issue and ticket["stage"] not in ("Coding", "CI running")):
                    continue
                followed = task_id = None
                if args.coder:
                    dispatch = args.coder
                else:
                    # A Task whose start never produced a Dispatch has no attempt to review.
                    attempts = {i: d["id"] for i in ids
                                if (d := runtime("orchestration", "dispatch-show", "--task", i, "--full")["dispatch"])}
                    if not attempts:
                        continue
                    # Restarts can leave several coder Tasks: the newest succeeded report names the current one.
                    task_id = max(attempts, key=lambda i: max((m["sequence"] for m in review_mail(messages, attempts[i])),
                                                              default=-1))
                    dispatch = attempts[task_id]
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='followups'").fetchone():
                        row = db.execute("SELECT dispatch FROM followups WHERE run=? AND issue=?", (run, number)).fetchone()
                        # Watch sent a resume note to the settled coder: its follow-up Dispatch is the current coder.
                        if row:
                            task_id, first = follow_root(row[0], attempts, run)
                            # Its Task may have retried since: read the current attempt, validated below.
                            task = runtime("orchestration", "worker-show", "--dispatch", row[0], "--full")["dispatch"]["taskId"]
                            current = (runtime("orchestration", "dispatch-show", "--task", task, "--full")["dispatch"]
                                       or {}).get("id")
                            followed, dispatch = row[0], current
                if not review_mail(messages, dispatch):
                    if args.coder:
                        log("review", {"issue": number, "waiting": f"no succeeded report from {dispatch}"})
                    continue
                show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
                worker = show["worker"]
                if (show["dispatch"]["id"] != dispatch or show["dispatch"]["runId"] != run or
                        (not args.coder and show["dispatch"]["taskId"] != task_id
                         and not (followed and in_coder_chain(dispatch, first, run)))):
                    raise OrcaError("coder Dispatch identity changed")
                tree = runtime("worktree", "show", "--worktree", "id:" + worker["worktreeId"])["worktree"]
                remote = command("git", "-C", tree["path"], "remote", "get-url", "origin").strip()
                if (tree["id"] != worker["worktreeId"] or tree["branch"].removeprefix("refs/heads/") != ticket["branch"]
                        or not re.fullmatch(r"(?:https://github\.com/|git@github\.com:)" + OWNER + "/"
                                            + ticket["repo"] + r"(?:\.git)?", remote)):
                    raise OrcaError("coder worktree does not match the issue repository and branch")
                model = worker["startOptions"]["launch"]["effective"]["model"]
                agent = held_label((ticket["agent"] or "").split(" · ")[0] or f"coder {model}",
                                   show["dispatch"].get("assigneeHandle"))
                state = {"coder": dispatch, "expected": dispatch, "coder_terminal": show["dispatch"].get("assigneeHandle"),
                         "path": tree["path"], "host": "local", "model": model,
                         "agent": agent, "history": [], "stage": "Coding"}
            elif "host" not in state and state["stage"] not in ("Merging", "Needs you"):
                worker = next((w for w in fleet_workers(run) if w["dispatchId"] == state["coder"]), None)
                if worker is None:
                    raise OrcaError(f"cannot locate coder Dispatch {state['coder']} in the Run's worker inventory")
                state["host"] = worker_host(worker)
                save_review(db, run, number, state)
            review_step(db, run, actor, ticket, state, messages)
        except UncertainReview as e:
            log("Needs you", {"issue": number, "reason": str(e)})
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
query($number:Int!) {
  repository(owner:"julianthant",name:"hr-relay") {
    issue(number:$number) {
        state
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
"""


def close_cards(repo, issue):
    rows = [row for row in snapshot()["closed"] if not issue or row["number"] == issue]
    for ticket in tickets(rows=rows):
        if not ticket["research"] and ticket["repo"] == repo and ticket["item"] and (ticket["agent"] or ticket["stage"]):
            yield ticket


def verify_closed(number, item):
    response = json.loads(command("gh", "api", "graphql", "-f", "query=" + CLOSE_QUERY, "-F", f"number={number}"))
    issue = response["data"]["repository"]["issue"]
    if not issue or issue["state"] != "CLOSED" or not any(
            card["id"] == item for card in nodes(issue["projectItems"])):
        raise OrcaError("ticket closure or Project card changed")
    return nodes(issue["closedByPullRequestsReferences"])


def close_workers(run):
    cursor = []
    seen = set()
    while True:
        inventory = runtime("orchestration", "worker-list", "--run", run, "--full", "--limit", "100", *cursor)
        for worker in inventory["workers"]:
            if worker.get("server") or worker.get("host") not in (None, "local"):
                continue
            if worker["runId"] != run:
                raise OrcaError("worker-list returned a different Run")
            host = (worker.get("projection") or {}).get("host") or {}
            if not host.get("kind"):
                raise OrcaError("worker-list row has no projected host")
            if host["kind"] == "local":
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
            try:
                result = runtime("orchestration", "worker-release", "--dispatch", dispatch, "--full")
            except OrcaError as e:
                # The CLI exits 1 for release_unknown: a recorded handle that no longer resolves.
                if "release_unknown" not in str(e):
                    raise
                result = {"state": "release_unknown"}
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
        settled = worker["dispatchStatus"] in ("completed", "failed")
        # A settled worker whose handle is gone is released once its worktree has no agent terminal (checked below).
        if terminal not in live and (state in RELEASED or (state == "release_unknown" and settled) or
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
        workspace = (worker.get("projection") or {}).get("workspace")
        if workspace:
            roster.setdefault(workspace["id"], []).append(worker)
    trees = runtime("worktree", "list", "--repo", "path:" + str(repo), "--limit", "10000")
    if trees["truncated"]:
        raise OrcaError("incomplete Orca worktree inventory")
    project, (_, (stage, _), (agent, _)), (_, needs_you) = project_fields()
    # Retry the same workspace if a partial close already removed the checkout.
    db.execute("CREATE TABLE IF NOT EXISTS closing (run TEXT, issue INTEGER, workspace TEXT, PRIMARY KEY(run,issue))")
    db.execute("CREATE TABLE IF NOT EXISTS close_notice (run TEXT, issue INTEGER, reason TEXT, sent INTEGER,"
               " carded INTEGER, PRIMARY KEY(run,issue))")
    # Its own table, since a tick from before request ids still reads and writes close_notice.
    db.execute("CREATE TABLE IF NOT EXISTS close_request (run TEXT, issue INTEGER, reason TEXT, request TEXT,"
               " PRIMARY KEY(run,issue))")
    unconfirmed = []
    for ticket in candidates:
        number, branch, item = ticket["number"], ticket["branch"], ticket["item"]
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
        edit = ("gh", "project", "item-edit", "--project-id", project, "--id", item, "--field-id")
        try:
            if (not workers or not branch or len(matching) > 1 or (saved and workspace != saved[0])
                    or (recorded_path and workspace.partition("::")[2] != recorded_path)):
                raise OrcaError("no unique ticket worktree and worker inventory in this Run")
            merged_prs = verify_closed(number, item)
            with db:
                db.execute("INSERT OR IGNORE INTO closing VALUES (?,?,?)", (run, number, workspace))
            paths = {pick.HOLDS, args.holds or pick.HOLDS}
            paths.update(Path(review[key]["holds"]) for key in ("hold", "reviewer", "release")
                         if review.get(key) and review[key].get("holds"))
            release_close_workers(workers, workspace, run, paths)
            heads = {pr["headRefOid"] for pr in merged_prs
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
                db.execute("DELETE FROM close_notice WHERE run=? AND issue=?", (run, number))
                db.execute("DELETE FROM close_request WHERE run=? AND issue=?", (run, number))
            log("closed", {"issue": number, "branch": branch, "head": sha})
        except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
            # One message per blocked state, its request id saved before sending; its card write is retried on its own.
            # Orca may rewrite a body in transit, so the subject alone names the blocked state.
            digest = hashlib.sha256(str(e).encode()).hexdigest()[:12]
            subject = f"#{number}: Needs you after close ({digest})"
            notice = db.execute("SELECT reason,sent,carded FROM close_notice WHERE run=? AND issue=?",
                                (run, number)).fetchone()
            if not notice or notice[0] != str(e):
                notice, request = (str(e), 0, 0), str(uuid.uuid4())
                with db:
                    db.execute("INSERT OR REPLACE INTO close_notice VALUES (?,?,?,0,0)", (run, number, str(e)))
                    db.execute("INSERT OR REPLACE INTO close_request VALUES (?,?,?,?)", (run, number, str(e), request))
                retry = ["python3", __file__, "close", "--repo-path", str(repo), "--issue", str(number)]
                if actor:
                    retry.extend(["--anchor", actor[1], "--run", run])
                log("Needs you", {"issue": number, "reason": str(e), "retry": shlex.join(retry),
                                  "retry_from": "plain shell outside Orca" if actor else "coordinating Orca pane"})
            else:
                # A request saved for another reason belongs to another state's notice.
                request = db.execute("SELECT request FROM close_request WHERE run=? AND issue=? AND reason=?",
                                     (run, number, str(e))).fetchone()
                request = request and request[0]
            if not notice[1]:
                landed = False
                if not request:
                    reply = runtime("orchestration", "inbox", "--terminal", "run:" + run, "--limit", "10000", "--full")
                    messages = reply["messages"]
                    # Sent without a saved request, so under the legacy subject: plain, or in a rejected escalation's
                    # wrapper that keeps this reason after its first "Original body:" line.
                    legacy = f"#{number}: Needs you after close"
                    for m in messages:
                        head, sep, tail = (m.get("body") or "").partition("\n\nOriginal body:\n")
                        landed = landed or m.get("type") == "status" and (
                            (m.get("subject") == legacy and m.get("body") == str(e))
                            or (m.get("subject") == "Rejected escalation: " + legacy and sep
                                and head.startswith("Orca rejected this escalation: ") and tail == str(e)))
                    if not landed and not (len(messages) >= 10000 or reply.get("truncated")):
                        request = str(uuid.uuid4())
                        with db:
                            db.execute("INSERT OR REPLACE INTO close_request VALUES (?,?,?,?)",
                                       (run, number, str(e), request))
                    elif not landed:
                        # It may be past the listing's end: send nothing, keep it pending and fail this tick.
                        unconfirmed.append(number)
                if request:
                    runtime("orchestration", "send", *actor, "--to", "run:" + run, "--type", "status",
                            "--subject", subject, "--body", str(e), "--retry-request", request)
                if request or landed:
                    with db:
                        db.execute("UPDATE close_notice SET sent=1 WHERE run=? AND issue=?", (run, number))
            if not notice[2]:
                command(*edit, stage, "--single-select-option-id", needs_you)
                with db:
                    db.execute("UPDATE close_notice SET carded=1 WHERE run=? AND issue=?", (run, number))
    if unconfirmed:
        raise OrcaError("incomplete Run mailbox: close notice unconfirmed for " + ", ".join(f"#{n}" for n in unconfirmed))


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
QUIET_MINUTES = 20
HANDOFF = ("Your account is at its usage handoff point ({why}). Finish the current step, run /handoff, then send "
           "worker_done --outcome failed with the handoff path. A fresh worker on another account continues from it.")
NUDGE = ("Your screen has not changed for {minutes} minutes. If you are blocked, report it with orca orchestration "
         "ask or send; otherwise continue the task.")


def screen_limit(tail, launch):
    """Why this screen means the worker cannot hand off: a limit line or the model-switch menu."""
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
    # The Codex status line's model is not a limit: it differs from the launch model on healthy workers.
    return None


def held_label(agent, terminal):
    """The card text with the coder's seat taken from the terminal's hold, when one is recorded."""
    import pick
    label = ((pick.account_usage.load_json(pick.HOLDS) or {}).get(terminal) or {}).get("label")
    return card_agent(agent, False, label) if label else agent


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
    import io
    import pick
    actor, run = supervisor(args)
    now = time.time()
    # While a restore is owed the active Codex account may be a worker's seat, not the coordinator's.
    owed = owed_restores(db)
    try:
        if owed:
            actions.append({"coordinator": f"rotation deferred: restore owed by {sorted(owed)}"})
        else:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                pick.main(["coordinator"])
            if out.getvalue().strip():
                actions.append({"coordinator": out.getvalue().strip()})
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, SystemExit, sqlite3.Error) as e:
        errors.append({"coordinator": str(e)})
    cfg = pick.account_usage.load_config()
    holds_path = Path(args.holds or pick.HOLDS)
    holds = json.loads(holds_path.read_text()) if holds_path.exists() else {}
    due = {handle: why for _, handle, why in pick.due(cfg, "local", now)}
    tasks = {t["id"]: t for t in runtime("orchestration", "task-list", *(actor or ["--run", run]), "--full")["tasks"]}
    cards = {t["number"]: t for t in tickets()}
    grok_starting = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='starts'").fetchone():
        for (raw,) in db.execute("SELECT receipt FROM starts WHERE receipt IS NOT NULL AND receipt != 'done'"):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("run") == run and (rec.get("route") or [None])[0] == "grok":
                grok_starting.add(rec.get("task"))
    db.execute("CREATE TABLE IF NOT EXISTS watch (run TEXT, terminal TEXT, state TEXT, PRIMARY KEY(run,terminal))")
    saved = dict(db.execute("SELECT terminal,state FROM watch WHERE run=?", (run,)))
    db.execute("CREATE TABLE IF NOT EXISTS watch_replaced (run TEXT, dispatch TEXT, PRIMARY KEY(run,dispatch))")
    replaced = {row[0] for row in db.execute("SELECT dispatch FROM watch_replaced WHERE run=?", (run,))}

    def save(terminal, state, *, replaced_dispatch=None):
        with db:
            if replaced_dispatch:
                db.execute("INSERT OR IGNORE INTO watch_replaced VALUES (?,?)", (run, replaced_dispatch))
            if state is None:
                db.execute("DELETE FROM watch WHERE run=? AND terminal=?", (run, terminal))
                saved.pop(terminal, None)
            else:
                db.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?)", (run, terminal, json.dumps(state)))
                saved[terminal] = json.dumps(state)

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

    def task_role(task_id):
        task = tasks.get(task_id) or {}
        title, spec = (task.get(key) or "" for key in ("task_title", "spec"))
        if role := re.search(r"(?m)^Role: .*roles/(coder|reviewer)\.md\b", spec):
            return role[1]
        if role := re.search(r"\b(coder|review(?:er)?)\b", title, re.I):
            return "coder" if role[1].lower() == "coder" else "reviewer"
        if spec.startswith("/goal Implement "):
            return "coder"
        return None

    def role_of(d):
        """Find the role named by this Task or its retry and follow-up ancestry."""
        for _ in range(10):
            if role := task_role(d.get("taskId")):
                return role
            up = d.get("retryOfDispatchId") or d.get("followUpOf")
            if not up:
                return None
            d = runtime("orchestration", "worker-show", "--dispatch", up, "--full")["dispatch"]
        return None

    def replacement_for(w, show, number, fresh_handoff, recorded_coder):
        dispatch, task_id = w["dispatchId"], w["taskId"]
        worktree = show["worker"]["worktreeId"]
        role = role_of(show["dispatch"])
        created = tasks.get(task_id, {}).get("created_at")
        for other in roster:
            found, other_task_id = other["dispatchId"], other["taskId"]
            local = found in task_of
            if found == dispatch or found in replaced or (local and other_task_id == task_id):
                continue
            if not local and other_task_id not in tasks:
                if role == "coder" and found == recorded_coder:
                    return found
                continue
            projection = other.get("projection") or {}
            resource = other.get("resource") or {}
            workspace_id = (projection.get("workspace") or {}).get("id") or resource.get("worktreeId")
            same_tree = worktree and local and workspace_id == worktree
            if local:
                newer = worker_order[found] < worker_order[dispatch]
            else:
                # Orca appends paired Dispatches without timestamps after each local page.
                other_created = tasks.get(other_task_id, {}).get("created_at")
                newer = bool(created and other_created and other_created > created)
            fresh_history = fresh_handoff and newer and same_tree
            live_on_tree = (same_tree and other["dispatchStatus"] == "dispatched"
                            and (projection.get("liveness") or {}).get("verdict") != "exited"
                            and resource.get("ownerDispatchId") in (None, found))
            same_ticket = newer and number is not None and issue_of(other_task_id)[1] == number
            if fresh_history or (role and (live_on_tree or same_ticket)):
                other_role = task_role(other_task_id)
                if not other_role:
                    d = runtime("orchestration", "worker-show", "--dispatch", found, "--full")["dispatch"]
                    if d["id"] != found or d["runId"] != run or d["taskId"] != other_task_id:
                        raise OrcaError("Dispatch identity changed")
                    other_role = role_of(d)
                if other_role == role or (fresh_history and other_role is None):
                    return found
        return None

    def followup_route(show, ticket, title):
        """A follow-up Dispatch records no model: its original's route, or the Coder line once that proves a coder."""
        coder = ticket and ticket.get("route") and tuple(ticket["route"][:3])
        d = show["dispatch"]
        for _ in range(10):
            if not d.get("followUpOf"):
                break
            prev = runtime("orchestration", "worker-show", "--dispatch", d["followUpOf"], "--full")
            effective = (((prev.get("worker") or {}).get("startOptions") or {}).get("launch") or {}).get("effective") or {}
            route = tuple(effective.get(k) for k in ("agent", "model", "effort"))
            if None not in route:
                # A resume Task is titled "#N: resume": the Coder line only when the ancestry names a coder.
                return coder if coder and role_of(prev["dispatch"]) == "coder" else route
            d = prev["dispatch"]
        if "review" not in title and coder:
            return coder
        raise OrcaError(f"no agent, model and effort recorded for Dispatch {show['dispatch']['id']} or its originals")

    def restart(w, show, state, why, crash, *, fresh_handoff=False):
        dispatch, terminal, task_id = w["dispatchId"], w["agentTerminalHandle"], show["dispatch"]["taskId"]
        task, number = issue_of(task_id)
        title = task.get("task_title") or ""
        ticket = cards.get(number)
        pending = state.get("pending") or {}
        # A lost worker-start response may have launched the replacement already; never replay it.
        if pending.get("starting"):
            pending.setdefault("dispatch", dispatch)
            pending.setdefault("number", number)
            resume_start(terminal, state)
            return
        review = (review_state(db, run, number) or {}) if number is not None else {}
        if review.get("stage") == "Needs you":
            return
        if replacement := replacement_for(w, show, number, fresh_handoff, review.get("coder")):
            owed = {"coordinator": state["coordinator"]} if state.get("coordinator") else None
            save(terminal, owed, replaced_dispatch=dispatch)
            replaced.add(dispatch)
            actions.append({"issue": number, "dispatch": dispatch, "replaced by": replacement})
            return
        report = failure_of(show)
        checkpoint = None
        if (not crash and isinstance(report, dict) and report.get("provenance") == "worker_report"
                and report.get("outcome") == "failed" and report.get("reportPath")):
            worktree = show["worker"]["worktreeId"]
            tree = runtime("worktree", "show", "--worktree", "id:" + worktree)["worktree"]
            if tree["id"] != worktree:
                raise OrcaError("worker worktree changed")
            branch = "refs/heads/" + tree["branch"].removeprefix("refs/heads/")
            head = command("git", "-C", tree["path"], "rev-parse", "--verify", branch).strip()
            checkpoint = {"task": task_id, "head": head}
            if checkpoint == state.get("checkpoint"):
                if state.get("checkpoint_blocked") != dispatch:
                    escalate(number, "checkpoint restart limit",
                             f"Dispatch {dispatch} reported another checkpoint at {head[:12]}. "
                             f"Inspect its handoff: {report['reportPath']}.")
                    state.setdefault("previous_stage", phase(ticket, state, title))
                    stage(ticket, "Needs you")
                    for key in ("handoff", "handoff_reason", "pending"):
                        state.pop(key, None)
                    state["checkpoint_blocked"] = dispatch
                    save(terminal, state)
                    actions.append({"issue": number, "dispatch": dispatch, "reported": "checkpoint restart limit"})
                return
        usage = not crash and why != CHECKPOINT_HANDOFF
        if usage and not state.get("account"):
            if state.get("escalated") != "account":
                escalate(number, "limit account unknown", f"Dispatch {dispatch} must restart ({why}), but no hold "
                         "records its account, so another account cannot be proven. It was left running.")
                stage(ticket, "Needs you")
                state["escalated"] = "account"
                save(terminal, state)
                actions.append({"issue": number, "dispatch": dispatch, "reported": "account"})
            return
        if usage and state.get("usage_handoff") != dispatch:
            pick.record_usage_handoff(cfg, "local", state["account"], now, why)
            state["usage_handoff"] = dispatch
        state["pending"] = {"why": why, "crash": crash}
        if not crash:
            state.setdefault("previous_stage", phase(ticket, state, title))
            stage(ticket, "Handed off")
        save(terminal, state)
        launch = show["worker"]["startOptions"]["launch"]["effective"]
        route = (launch["agent"], launch["model"], launch["effort"])
        if None in route:
            route = followup_route(show, ticket, title)
        source = state["account"] if usage and route[0] != "grok" else None
        seats = dict(cfg, account=[r for r in cfg["account"] if r["id"] != source])
        account, reasons = pick.pick(seats, *route, "local", now)
        if account is None:
            actions.append({"issue": number, "dispatch": dispatch, "waiting for seat": reasons})
            return
        owed = route[0] == "codex" and set(owed_restores(db)) - {f"watch:{run}:{terminal}"}
        if owed:
            actions.append({"issue": number, "dispatch": dispatch, "waiting for coordinator restore": sorted(owed)})
            return
        # An unconfirmed restore from an earlier start is still the coordinator's account.
        prior = state.get("coordinator") or codex_active(route)
        if prior:
            # Owed until confirmed: the next tick restores it before another start.
            state["coordinator"] = prior
            save(terminal, state)
        if route[0] not in pick.SLOT_AGENTS:
            runtime("account", "select", "--agent", route[0], "--account", account["id"])
            if runtime("account", "list")[route[0]]["activeAccountId"] != account["id"]:
                raise OrcaError("selected account changed before restart")
        note = (f"You replace Dispatch {dispatch}, which stopped: {why}."
                + (f" Its handoff: {report['reportPath']}." if isinstance(report, dict) and report.get("reportPath") else "")
                + (f" Its last report:\n{report['body'][:1500]}\n" if isinstance(report, dict) and report.get("body") else " ")
                + "Read its handoff first if there is one; otherwise continue from the last commit on this branch."
                + (f"\n\nIts own resume note, never delivered:\n{state['note']['body']}" if state.get("note") else ""))
        if show["dispatch"]["status"] == "dispatched":
            # --retry-of takes a settled attempt, so fence the old one first.
            stop = runtime("orchestration", "worker-stop", "--dispatch", dispatch, "--full")
            actions.append({"issue": number, "stopped": dispatch, "process": stop.get("processAction")})
        # --retry-of names a failed Task's last attempt; after an Orca restart the Task can be ready again.
        retry = show["dispatch"]["status"] == "dispatched" or task.get("status") != "ready"
        # Everything adoption needs if the start response is lost.
        state["pending"].update(starting=True, dispatch=dispatch, task=task_id, number=number, title=title,
                                worktree=show["worker"]["worktreeId"], route=route, account=account["id"],
                                label=account["label"], note=note, coordinator=prior, retry=retry)
        if checkpoint:
            state["pending"]["checkpoint"] = checkpoint
        save(terminal, state)
        # The same Task keeps its title, dependencies and retry lineage for the review tick.
        try:
            started = runtime("orchestration", "worker-start", *actor, "--task", task_id,
                              *(("--retry-of", dispatch) if retry else ()),
                              "--worktree", "id:" + show["worker"]["worktreeId"], "--agent", route[0],
                              "--model", route[1], "--effort", route[2], "--full")
        except StartNotApplied as e:
            # Nothing started: the next tick restarts afresh, and the coordinator's account is restored or kept owed.
            state["pending"] = {"why": why, "crash": crash}
            save(terminal, state)
            send_note(terminal, state)
            raise OrcaError(f"restart of {dispatch} not applied: {e}") from e
        adopt(terminal, state, *start_identity(started), turn_observed=started.get("turnStart") == "observed")

    def resume_start(terminal, state):
        """Adopt the retry Orca started for a lost worker-start response; otherwise ask the Run once."""
        p = state["pending"]
        if p.get("task"):
            new = (runtime("orchestration", "dispatch-show", "--task", p["task"], "--full").get("dispatch") or {}).get("id")
            if new and new != p["dispatch"]:
                show = runtime("orchestration", "worker-show", "--dispatch", new, "--full")
                d, worker = show["dispatch"], show.get("worker") or {}
                lineage = p["dispatch"] if p.get("retry", True) else None
                if (d["id"] == new and d.get("retryOfDispatchId") == lineage and d["runId"] == run
                        and d["taskId"] == p["task"] and worker.get("worktreeId") == p["worktree"]
                        and start_identity(worker)[0] == new):
                    adopt(terminal, state, *start_identity(worker))
                    return
        if state.get("escalated") != "start":
            escalate(p["number"], "restart unconfirmed",
                     f"Inspect the restart of Dispatch {p['dispatch']} in Run {run}, then clear its watch row.")
            stage(cards.get(p["number"]), "Needs you")
            state["escalated"] = "start"
            save(terminal, state)
            actions.append({"issue": p["number"], "dispatch": p["dispatch"], "reported": "start"})

    def adopt(terminal, state, new, handle, *, turn_observed=False):
        p = state["pending"]
        number, route, ticket = p["number"], p["route"], cards.get(p["number"])
        with pick.edit_holds(holds_path) as edit:
            edit.pop(terminal, None)
            edit[handle] = dict(account=p["account"], label=p["label"], agent=route[0], model=route[1],
                                effort=route[2], host="local", at=int(now))
        if route[0] == "grok" and not turn_observed and not grok_turn_observed(new):
            grok_starting.add(p["task"])
            if state.get("escalated") != "turn":
                escalate(number, "Grok restart unconfirmed", f"Orca has no observed turn for Dispatch {new}.")
                stage(ticket, "Needs you")
                state["escalated"] = "turn"
                save(terminal, state)
            return
        review = review_state(db, run, number) if number is not None else None
        if review and review.get("coder") == p["dispatch"]:
            assign_coder(review, new, handle)
            if review.get("agent"):
                review["agent"] = card_agent(review["agent"], False, p["label"])
        elif review and (review.get("reviewer") or {}).get("dispatch") == p["dispatch"]:
            review["reviewer"].update(dispatch=new, terminal=handle, holds=str(holds_path))
            review["expected"] = new
        else:
            review = None
        row = {"relaunched": bool(state.get("relaunched") or p["crash"]), "account": p["account"],
               "note": {"to": new, "subject": f"#{number}: resume" if number else "resume", "body": p["note"]}}
        if checkpoint := p.get("checkpoint") or state.get("checkpoint"):
            row["checkpoint"] = checkpoint
        if p.get("coordinator"):
            row["coordinator"] = p["coordinator"]
        if ticket:
            # The card leaves Handed off only once the resume note has a receipt.
            row["card"] = {"number": number, "stage": (state.get("card") or {}).get("stage") or phase(ticket, state, p["title"]),
                           "agent": card_agent(ticket["agent"], "review" in p["title"], p["label"])}
        # One transaction: an interruption leaves either the predecessor row (adopted again) or the new row.
        with db:
            if review:
                db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(review)))
            db.execute("DELETE FROM watch WHERE run=? AND terminal=?", (run, terminal))
            db.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?)", (run, handle, json.dumps(row)))
        saved.pop(terminal, None)
        saved[handle] = json.dumps(row)
        actions.append({"issue": number, "restarted": p["dispatch"], "as": new, "why": p["why"], "account": p["label"]})
        send_note(handle, row)

    def sent_followup(note):
        """The follow-up Orca started for an earlier send of this note, as its receipt; None if there is none."""
        for task in tasks.values():
            if task.get("task_title") != note["subject"] or task.get("spec") != note["body"]:
                continue
            new = (runtime("orchestration", "dispatch-show", "--task", task["id"], "--full").get("dispatch") or {}).get("id")
            if not new:
                continue
            attempt = runtime("orchestration", "worker-show", "--dispatch", new, "--full")["dispatch"]
            # The Task's current attempt may be a retry of the follow-up; its lineage must still reach the note's target.
            if (attempt["id"] == new and attempt["runId"] == run and attempt["taskId"] == task["id"] and new != note["to"]
                    and in_coder_chain(attempt.get("retryOfDispatchId") or attempt.get("followUpOf"), note["to"], run)):
                return {"delivery": "follow_up", "followUpOf": note["to"], "dispatchId": new}
        return None

    def send_note(handle, row):
        """Retried each tick until confirmed: the coordinator's account, the resume note, then the card."""
        failure = None
        if row.get("coordinator"):
            try:
                restore_coordinator(row["coordinator"])
                row.pop("coordinator")
                save(handle, row)
            except (OrcaError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                failure = e  # the note and card go on; the restore is retried next tick
        if row.get("note"):
            note = row["note"]
            # A send whose reply was lost may have started a follow-up already; adopt it rather than start another.
            # A row saved before request ids has neither mark, so look it up whenever no request id is saved.
            receipt = (note.get("sending") or not note.get("request")) and sent_followup(note)
            if not receipt:
                # Saved before the call, so a retry replays Orca's receipt whatever the first delivery was.
                note.setdefault("request", str(uuid.uuid4()))
                note["sending"] = True
                save(handle, row)
                receipt = runtime("orchestration", "send", *actor, "--to", "dispatch:" + note["to"],
                                  "--subject", note["subject"], "--body", note["body"],
                                  "--retry-request", note["request"])
            row.pop("note")
            found = re.match(r"#(\d+):", note["subject"])
            number = int(found[1]) if found else None
            review = None
            if receipt.get("delivery") in ("follow_up", "reopened", "started_fresh"):
                # The replacement had settled, so the note started a new Dispatch; review must expect that one.
                new = followup_dispatch(receipt, note["to"], run)
                review = review_state(db, run, number) if number is not None else None
                coder = review and review.get("coder") == note["to"]
                reviewer = review and (review.get("reviewer") or {}).get("dispatch") == note["to"]
                # The follow-up may run in a fresh pane: review, the hold and this row move with its verified terminal.
                show = runtime("orchestration", "worker-show", "--dispatch", new, "--full")
                verified = (show["dispatch"]["id"] == new and show["dispatch"]["runId"] == run
                            and start_identity(show.get("worker") or {})[0] == new)
                if (coder or reviewer) and not verified:
                    raise OrcaError(f"unverified follow-up {new} of {note['to']}")
                terminal = start_identity(show["worker"])[1] if verified else handle
                if verified and row.get("checkpoint"):
                    row["checkpoint"]["task"] = show["dispatch"]["taskId"]
                if coder:
                    assign_coder(review, new, terminal)
                elif reviewer:
                    review["reviewer"].update(dispatch=new, terminal=terminal)
                    review["expected"] = new
                else:
                    review = None
                actions.append({"issue": number, "followed up": note["to"], "as": new})
            moved = None
            if receipt.get("delivery") in ("follow_up", "reopened", "started_fresh") and terminal != handle:
                moved = terminal
                with pick.edit_holds(holds_path) as edit:
                    if handle in edit:
                        edit[moved] = edit.pop(handle)
                if handle in holds:
                    holds[moved] = holds.pop(handle)
            # One transaction: review stays gated on the note until it expects the Dispatch that received it.
            with db:
                if review:
                    db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (run, number, json.dumps(review)))
                elif receipt.get("delivery") in ("follow_up", "reopened", "started_fresh") and number is not None:
                    # No review yet: its first discovery reads the follow-up's report, not the predecessor's.
                    db.execute("CREATE TABLE IF NOT EXISTS followups (run TEXT, issue INTEGER, dispatch TEXT,"
                               " PRIMARY KEY(run,issue))")
                    db.execute("INSERT OR REPLACE INTO followups VALUES (?,?,?)", (run, number, new))
                if moved:
                    db.execute("DELETE FROM watch WHERE run=? AND terminal=?", (run, handle))
                db.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?)", (run, moved or handle, json.dumps(row)))
            if moved:
                saved.pop(handle, None)
                handle = moved
            saved[handle] = json.dumps(row)
            actions.append({"note": receipt.get("delivery"), "to": note["to"]})
        if row.get("card"):
            card = row["card"]
            stage(cards.get(card["number"]), card["stage"], card["agent"])
            row.pop("card")
            save(handle, row)
        if failure:
            raise failure

    def restore_prompt(ticket, state, title):
        """The forwarded prompt was answered: give the card back so review_tick reads the coder's report."""
        if not state.get("prompt"):
            return
        if ticket and ticket["stage"] == "Needs you":
            stage(ticket, state.get("prompt_stage") or phase(ticket, state, title))
        for key in ("prompt", "prompt_stage"):
            state.pop(key, None)
        actions.append({"issue": ticket and ticket["number"], "prompt cleared": True})

    # The retry of a lost start is listed first and hides the old row from the loop below.
    for terminal, raw in list(saved.items()):
        state = json.loads(raw)
        try:
            if (state.get("pending") or {}).get("starting") and state["pending"].get("task"):
                resume_start(terminal, state)
            elif state.get("coordinator") or state.get("note") or state.get("card"):
                send_note(terminal, state)
        except (OrcaError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
            errors.append({"terminal": terminal, "error": str(e)})

    seen_terminals, seen_tasks = set(), set()
    # Remote Dispatches can supersede a local worker, but the watch only controls local workers.
    roster, workers = [], []
    for w in fleet_workers(run):
        if w.get("server") or w.get("host") not in (None, "local"):
            if w.get("runId") == run:
                roster.append(w)
            continue
        if w["runId"] != run:
            raise OrcaError("worker-list returned a different Run")
        host = ((w.get("projection") or {}).get("host") or {}).get("kind")
        if not host:
            raise OrcaError("worker-list row has no projected host")
        roster.append(w)
        if host == "local":
            workers.append(w)
    worker_order = {w["dispatchId"]: pos for pos, w in enumerate(workers)}
    task_of = {w["dispatchId"]: w["taskId"] for w in workers}
    research = research_root()
    for w in workers:
        terminal, dispatch, task_id = w["agentTerminalHandle"], w["dispatchId"], w["taskId"]
        task, number = issue_of(task_id)
        if (task_id in grok_starting or re.fullmatch(r"#\d+ research", task.get("task_title") or "")
                or (cards.get(number) or {}).get("research")):
            continue
        owner = (w.get("resource") or {}).get("ownerDispatchId")
        # worker-list is newest first: act only on the current attempt of each pane and Task.
        if not terminal or (owner and owner != dispatch):
            # Without a pane record, or on a pane handed to another Task's follow-up, this is still its Task's
            # newest attempt; an owner in the same Task is listed later as that attempt.
            if not (owner and owner != dispatch and task_of.get(owner) == task_id):
                seen_tasks.add(task_id)
            continue
        # A newer attempt on a pane a follow-up took is still its Task's current one: record both first.
        history = terminal in seen_terminals or task_id in seen_tasks
        seen_terminals.add(terminal)
        seen_tasks.add(task_id)
        if history or dispatch in replaced:
            continue
        state = json.loads(saved.get(terminal) or "null")
        unwatched = state is None
        live = w["dispatchStatus"] == "dispatched"
        if not live and unwatched and (w["dispatchStatus"] != "failed" or w.get("workerState") == "stopped"):
            continue
        try:
            show = runtime("orchestration", "worker-show", "--dispatch", dispatch, "--full")
            if show["dispatch"]["id"] != dispatch or show["dispatch"]["runId"] != run:
                raise OrcaError("Dispatch identity changed")
            worktree = (show.get("worker") or {}).get("worktreeId") or ""
            if Path(worktree.partition("::")[2]).parent == research:
                continue
            title = task.get("task_title") or ""
            ticket = cards.get(number)
            review = (review_state(db, run, number) or {}) if number is not None else {}
            named = {review.get("coder"), review.get("expected"), (review.get("reviewer") or {}).get("dispatch")}
            if not live and unwatched and not (ticket and (ticket["stage"] in ACTIVE or dispatch in named)):
                continue  # history: only a ticket still in work can have a current crash
            state = state or {}
            hold = (holds.get(terminal) or {}).get("account")
            if hold and state.get("account") != hold:
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
                checkpoint = reported and report.get("outcome") == "failed" and report.get("reportPath")
                if reported and (state.get("handoff") or checkpoint):
                    detail = " ".join(report.get(key) or "" for key in ("subject", "body"))
                    limit = USAGE_REPORT.search(detail)
                    reason = state.get("handoff_reason") or (limit[0] if limit else None)
                    why = CHECKPOINT_HANDOFF
                    if state.get("handoff") or limit:
                        why = f"handed off at a usage limit ({reason or 'window unknown'})"
                    restart(w, show, state, why, False, fresh_handoff=unwatched)
                    continue
                if w["dispatchStatus"] != "failed" or reported or stopped:
                    restore_prompt(ticket, state, title)  # answered, then finished before this tick
                    # Finished, reported or stopped; an unconfirmed restore, resume note or card target is kept.
                    keep = state.get("coordinator") or state.get("note") or state.get("card")
                    save(terminal, state if keep else None)
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
                    state.update(handoff=int(now), handoff_reason=near, previous_stage=phase(ticket, state, title))
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


FULL_RUN_REPO = "julianthant/hr-relay"
FULL_RUN_WORKFLOW = "full.yml"
FULL_RUN_BRANCH = "development"
FULL_RUN_SECONDS = 20 * 60
FULL_RUN_STAMP = SNAPSHOT.with_name("full-run.json")


def full_run_tick(now=None):
    """Dispatch the development full run at most every 20 minutes; the workflow decides whether it is needed."""
    now = time.time() if now is None else now
    try:
        last = json.loads(FULL_RUN_STAMP.read_text()).get("at", 0)
    except (OSError, ValueError, AttributeError):
        last = 0
    if now - last < FULL_RUN_SECONDS:
        log("full-run", "skipped: dispatched within 20 minutes")
        return
    runs = github_state("runs", repo="hr-relay", workflow=FULL_RUN_WORKFLOW, branch=FULL_RUN_BRANCH)
    if any(r.get("status") in ("queued", "in_progress", "waiting", "requested", "pending") for r in runs):
        log("full-run", "skipped: a full run is already queued or running")
        return
    command("gh", "workflow", "run", FULL_RUN_WORKFLOW, "-R", FULL_RUN_REPO, "--ref", FULL_RUN_BRANCH)
    FULL_RUN_STAMP.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    FULL_RUN_STAMP.write_text(json.dumps({"at": now}))
    log("full-run", f"dispatched {FULL_RUN_WORKFLOW} on {FULL_RUN_BRANCH}")


def scheduled_supervisor(opts, path, checkout):
    saved = json.loads(path.read_text()) if path.exists() else {}
    if saved.get("run") != opts.run:
        saved = {}
    opts.anchor = saved.get("anchor") or opts.anchor

    def store(pending=False):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"run": opts.run, "anchor": opts.anchor, "pending": pending}))
        tmp.replace(path)

    try:
        supervisor(opts)
    except OrcaError:
        require_pane_less(opts.run)
        listing = runtime("terminal", "list", "--limit", "10000")
        if listing["truncated"]:
            raise OrcaError("incomplete Orca terminal inventory for supervisor recovery")
        handles = {t["handle"] for t in listing["terminals"]}

        def coordinator():
            run = runtime("orchestration", "run-show", "--id", opts.run, "--full")["run"]
            if run["id"] != opts.run:
                raise OrcaError("supervisor recovery returned a different Run")
            return run["coordinator_handle"]

        owner = coordinator()
        if owner in handles and owner != opts.anchor:
            raise OrcaError(f"configured Run is held by another live terminal: {owner}")
        if owner and not owner.startswith("term_"):
            raise OrcaError(f"cannot verify whether the configured Run's coordinator is absent: {owner}")
        if opts.anchor in handles and not saved.get("pending"):
            raise OrcaError("live supervisor anchor is not bound to the configured Run")
        if opts.anchor not in handles:
            result = runtime("terminal", "create", "--worktree", f"path:{checkout.resolve()}",
                             "--title", "dev-auto supervisor")
            opts.anchor = result["terminal"]["handle"]
            store(pending=True)
            if coordinator() != owner:
                raise OrcaError("configured Run changed coordinator during supervisor recovery")
        runtime("orchestration", "run-use", "--id", opts.run, "--from", opts.anchor)
        supervisor(opts)
        log("adopt", {"run": opts.run, "anchor": opts.anchor})
    if saved.get("anchor") != opts.anchor or saved.get("pending"):
        store()


def scheduled_tick(args):
    import fcntl

    state = SNAPSHOT.parent
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "fill.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("scheduled", "another tick is active")
            return 0

        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        status = {"lastRun": started, "errors": []}
        log("tick start", started)

        def step(phase, action, *params):
            try:
                action(*params)
            except Exception as e:
                # CLI diagnostics can name accounts, panes and local paths. Keep them in the local log.
                status["errors"].append({"phase": phase, "error": type(e).__name__})
                log("tick error", {"phase": phase, "reason": str(e)})
                return False
            return True

        opts = argparse.Namespace(anchor=args.anchor, run=args.run, issue=None, holds=None, coder=None,
                                  dry_run=False, repo_path=None)
        # The existing Run and state contain the dispatches, holds and card reservations to adopt.
        if step("adopt", scheduled_supervisor, opts, state / "anchor.json", args.repo_paths[0]):
            def advance():
                with sqlite3.connect(state / "starts.sqlite") as db:
                    step("watch", watch_tick, opts, db)
                    step("review", review_tick, opts, db)
                    for phase, action in (("close", close_tick), ("fill", fill_tick)):
                        for repo in args.repo_paths:
                            opts.repo_path = repo
                            step(phase, action, opts, db)
            step("state", advance)
            step("full-run", full_run_tick)

        path = state / "tick.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        tmp.replace(path)
        log("tick end", {**status, "finishedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        return int(bool(status["errors"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    scheduled = sub.add_parser("scheduled", help="adopt and advance the existing fleet from launchd")
    scheduled.add_argument("--anchor", help="initial anchor for an existing launchd configuration")
    scheduled.add_argument("--run", required=True)
    scheduled.add_argument("--repo-path", dest="repo_paths", type=Path, action="append", required=True)
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
    if args.cmd == "scheduled":
        return scheduled_tick(args)
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
            fill(args)
            return 0
        except (OSError, OrcaError, ValueError, subprocess.TimeoutExpired) as e:
            ap.exit(1, f"fill: {e}\n")
    start(args.repo, args.state) if args.cmd == "start" else resume(args.state)


if __name__ == "__main__":
    sys.exit(main())
