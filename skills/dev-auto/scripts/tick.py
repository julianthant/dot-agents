"""Headless Orca coordinator stub for dev-auto: no terminal of its own, run by launchd.

  tick.py start --repo id:<repo-id> --state <file>   process 1: scratch worker started, its ask answered
  tick.py resume --state <file>                      process 2: rebinds the stored Run, finishes, cleans up

See references/orca-supervisor.md.

Coordinator verbs refuse a sender with no stable Orca pane, so the script opens a plain shell pane
(its anchor) and passes it as --from; the Run's mailbox is run:<id>, so mail outlives that pane.
"""

import argparse
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--repo", required=True)
    s.add_argument("--state", required=True)
    sub.add_parser("resume").add_argument("--state", required=True)
    args = ap.parse_args(argv)
    start(args.repo, args.state) if args.cmd == "start" else resume(args.state)


if __name__ == "__main__":
    main()
