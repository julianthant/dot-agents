"""Headless Orca coordinator stub for dev-auto: no terminal of its own, run by launchd.

  tick.py prove --repo id:<repo-id>    one throwaway worker, start to cleanup (see references/orca-supervisor.md)

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


def orca(*args, ok=True):
    # Headless: launchd gives no ORCA_* pane variables, so strip ours to prove the same path.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCA_") or k == "ORCA_CLI_COMMAND"}
    out = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, env=env, timeout=900)
    data = json.loads(out.stdout or "{}")
    if ok and not data.get("ok"):
        sys.exit(f"orca {args[0]} {args[1] if len(args) > 1 else ''}: {data.get('error') or out.stderr}")
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


def anchor(worktree):
    t = orca("terminal", "create", "--worktree", worktree, "--title", "dev-auto supervisor")
    return t["result"]["terminal"]["handle"]


def wait(me, run, minutes=5):
    """The next batch of the Run's mail, consumed: a restarted script resumes where it left off."""
    out = orca("orchestration", "check", "--terminal", me, "--run", run, "--wait",
               "--timeout-ms", str(minutes * 60000))
    msgs = out["result"]["messages"]
    if not msgs:
        sys.exit(f"no mail for {run} in {minutes} minutes")
    return msgs


def log(step, value):
    print(f"{step:<12} {json.dumps(value, ensure_ascii=False)}", flush=True)


SPEC = (
    "Scratch lifecycle probe; no real work. Edit nothing tracked, push nothing, use no network.\n"
    '1. Run `orca orchestration ask` with the question "PROOF-ASK: what is the codeword?" and end your turn.\n'
    "2. When the answer arrives, write it with `printf %s <codeword> > proof-answer.txt` here "
    "(an inert scratch file; it is authorized).\n"
    "3. Send worker_done --outcome succeeded quoting the codeword, then idle."
)


def prove(repo):
    name = f"scratch-supervisor-proof-{int(time.time())}"
    wt = orca("worktree", "create", "--repo", repo, "--name", name, "--branch", f"scratch/{name}",
              "--base-branch", "origin/main", "--setup", "skip", "--no-parent",
              "--comment", "throwaway dev-auto proof; removed by tick.py")["result"]["worktree"]
    where = f"path:{wt['path']}"
    log("worktree", wt["path"].rsplit("/", 1)[-1])
    me = run = dispatch = None
    try:
        me = anchor(where)
        run = orca("orchestration", "run-create", "--objective", f"scratch: {name}", "--from", me)["result"]["runId"]
        log("run", {"run": run, "anchor": "pane 1"})
        start = orca("orchestration", "worker-start", "--environment", "local", "--from", me, "--run", run,
                     "--spec", SPEC, "--task-title", "scratch probe worker", "--worktree", where,
                     "--agent", "claude", "--model", "claude-haiku-5-5", "--effort", "low")["result"]
        dispatch = start["dispatchId"]
        log("start", {k: start[k] for k in ("dispatchId", "state", "agent", "model")})

        ask = next(m for m in wait(me, run) if m["type"] == "question")
        log("ask", ask["body"])
        orca("orchestration", "reply", "--from", me, "--run", run, "--id", ask["id"], "--body", "PELICAN-549")
        log("reply", "PELICAN-549")

        # Restart: the script's pane dies; a fresh pane rebinds the same Run and reads mail that
        # arrived while nothing was listening.
        orca("terminal", "close", "--terminal", me)
        time.sleep(30)
        me = anchor(where)
        orca("orchestration", "run-use", "--id", run, "--from", me)
        log("restart", {"run": run, "anchor": "pane 2", "peek": [m["type"] for m in orca(
            "orchestration", "check", "--terminal", me, "--run", run, "--peek")["result"]["messages"]]})
        done = next(m for m in wait(me, run) if m["type"] == "worker_done")
        log("done", {"outcome": done["payload"]["outcome"], "body": done["body"][:120]})
        log("watch", watch(dispatch, time.time()))

        fu = orca("orchestration", "send", "--from", me, "--run", run, "--to", f"dispatch:{dispatch}",
                  "--subject", "PROOF-FOLLOWUP",
                  "--body", "Run the inert, authorized `touch proof-perm.txt` here, then send worker_done "
                            "--outcome succeeded saying whether a permission prompt appeared.")["result"]
        dispatch = fu["dispatchId"]
        log("follow-up", {k: fu[k] for k in ("delivery", "dispatchId", "state")})
        done = next(m for m in wait(me, run) if m["type"] == "worker_done")
        log("done", {"outcome": done["payload"]["outcome"], "body": done["body"][:160]})

        # No prompt is pending (workers follow Orca's new-tab permission setting), so these show
        # only how Orca guards the verbs.
        for verb in ("deny", "approve"):
            err = orca("orchestration", verb, "--from", me, "--dispatch", dispatch, "--prompt", "appr_none",
                       "--reason", "scratch proof", ok=False).get("error") or {}
            log(verb, err.get("code"))
        log("inbox", [m["type"] for m in orca("orchestration", "inbox", "--terminal", f"run:{run}",
                                                "--limit", "20")["result"]["messages"]])
    finally:
        if dispatch:
            log("release", orca("orchestration", "worker-release", "--dispatch", dispatch, ok=False).get("result"))
        if me:
            orca("terminal", "close", "--terminal", me, ok=False)
        gone = orca("worktree", "rm", "--worktree", where, "--force", "--delete-branch", ok=False)
        log("cleanup", gone.get("result") or gone.get("error"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prove").add_argument("--repo", required=True)
    args = ap.parse_args(argv)
    prove(args.repo)


if __name__ == "__main__":
    main()
