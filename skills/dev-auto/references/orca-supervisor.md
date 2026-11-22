# Headless Orca supervisor

Proven on Orca 1.4.269 by `scripts/tick.py start --repo id:<repo-id> --state <file>`, then
`scripts/tick.py resume --state <file>` in a new process, each with every `ORCA_*` pane variable
stripped, as launchd runs it. Every command below takes `--json`.

## Identity and mail

Coordinator verbs (`run-create`, `run-use`, `worker-start`, `reply`, `send`, `deny`) refuse a
caller with no Orca pane (`no_active_sender_terminal`), and refuse a made-up `--from` such as
`script:x` or `bot:<name>` (`stable_pane_required`, seen on `run-current`). So the script opens
its own plain shell pane, the anchor, and passes that handle as `--from` on every call:

```sh
orca worktree create --repo id:<repo-id> --name <scratch> --base-branch origin/main --setup skip --no-parent
orca terminal create --worktree path:<worktree> --title "dev-auto supervisor"   # -> anchor handle
orca orchestration run-create --objective "<goal>" --from <anchor>              # -> run_<id>
```

The Run is the identity; its mailbox is `run:<run_id>`, not the pane. Read it with
`orca orchestration check --terminal <anchor> --run <run_id>`: plain `check` returns the oldest
unread batch and marks it handled; `--wait --timeout-ms <n>` blocks for it; `--peek` reads
without consuming. `orca orchestration inbox --terminal run:<run_id> --limit <n> --full` shows
the history, handled mail included.

## Recovery

Each result below was proven separately; do not read one as another.

- **Process recovery (proven).** `start` stores the Run id, worktree and Dispatch in its state
  file, closes its anchor and exits after answering the worker's ask. With no supervisor process
  alive for 60 seconds, `resume`, a new Python process, checks that the old pid is gone and then
  works from the state file alone. It opens a new anchor, runs
  `orca orchestration run-use --id <run_id> --from <new-anchor>`, and finds that the worker's
  `worker_done` is already in `check --peek`. A consuming `check` returns it once, with the
  stored Dispatch id, and `resume` then finishes the lifecycle.
- **Anchor rebind (proven).** A closed anchor loses no mail: it lives in `run:<run_id>`, and
  `run-use` binds the Run to any new pane.
- **Lost response.** A `check` response lost in transit is replayed with the `--retry-request <id>`
  the command printed. This comes from Orca's help and was not exercised.
- **Orca app restart (not verified).** See below.

## Workers and messages

| Step | Command | Arrives as |
| --- | --- | --- |
| Start | `worker-start --environment local --from <anchor> --run <run> --spec-file <f> --worktree path:<wt> --agent <a> [--model <m> --effort <e>]` | receipt with `dispatchId`, `terminal` |
| Ask | worker runs `orca orchestration ask` | `type: question`, `id`, `payload.dispatchId` |
| Answer | `reply --from <anchor> --run <run> --id <msg_id> --body <text>` | worker wakes with the answer |
| Done | worker sends `worker_done` | `type: worker_done`, `payload.outcome` |
| Follow-up | `send --from <anchor> --run <run> --to dispatch:<id> --subject <s> --body <b>` | receipt `delivery: follow_up` with a **new** `dispatchId`; use it from then on |
| Release | `worker-release --dispatch <newest id>` | `released`, `closed_agent_terminal`; older ids in the chain report `retained` / `ownership_transferred` |
| Cleanup | `terminal close --terminal <anchor>`, then `worktree rm --worktree path:<wt> --force --delete-branch` | `removed: true` |

A receipt counts only when the command exits 0 with valid JSON, `ok` is literally `true` and the
expected result field (`removed`, `ptyKilled`) is literally `true`; a string, a number or any
other truthy value fails. A timeout or invalid JSON is a failed receipt. `tick.py` removes the checkout and
branch only when all of these hold:

- `worker-release` reported `released` or `already_released`, or no `worker-start` was attempted;
- no follow-up `send` is still waiting on its receipt;
- `terminal close` reported `ptyKilled` for the anchor. The anchor handle is kept until it does.

After `worktree rm` reports `removed`, `git worktree list --porcelain` must succeed and must
not list the checkout path. Then `git show-ref --exists refs/heads/<branch>` must exit 2, git's
documented result for a missing ref. Exit 0 means the branch is still there. Anything else, such
as exit 1 for a broken ref or exit 128 when there is no repo, means git couldn't tell. Note that
`for-each-ref` skips broken refs, so it can't prove absence.

These checks run after the deletion, so they can fail even when `worktree rm` really did remove
the checkout. A failure then means git couldn't confirm the removal, not that the deletion was
undone. If a check before the deletion fails, the scratch worktree stays. In every failure
case the reasons go to stderr and the exit status is 1. A failed proof step re-raises its own
error, even when cleanup crashes.

A `worker_done` counts only with `outcome: succeeded`, from the Dispatch the script expects (the
start, then each follow-up's new id), and only if `worker-show` puts that Dispatch in the same
Run. Any other `worker_done` fails the proof.

`worker_done` also schedules release about two minutes later (`autoReleaseAt`); `send` reopens a
released worker. `worker-stop` fences one that must be cancelled.

## Permission prompts

A worker parked on a prompt arrives as an `escalation` carrying the approval id, and
`worker-show --full` sets `terminal.agentWait`. Answer it with
`orca orchestration deny --from <anchor> --dispatch <id> --prompt <approval_id> --reason <text>`;
the worker gets the reason and carries on. `approve` takes the same flags but works only when the
person has turned on "Let coordinators approve worker permission prompts" in Orca's settings;
otherwise it returns `approval_requires_person`. Leave that setting as the person chose it.

## Screens, limits and silence

`worker-show --dispatch <id> --full` gives `dispatch.assigneeHandle`, `terminal.lastOutputAt`
(epoch ms) and `terminal.agentWait`. `orca terminal read --terminal <handle> --screen` gives the
visible lines in `result.terminal.tail`; `worker-read --dispatch <id>` gives the transcript,
readable after release. `tick.py` calls a screen limited when a line opens with an agent's limit
message and no output line (`⏺`, `•`) follows it, and a worker silent when its Dispatch is not
completed and `lastOutputAt` is over 30 minutes old.

## Not headless

- **No pane-less identity.** The script needs its anchor pane; a closed anchor loses nothing but
  needs `run-use` from a new one.
- **Live permission prompts were not verified.** Workers launch under the person's own Orca
  setting for new agent tabs, which here is bypass permissions, and no flag overrides it. The
  proof's inert `touch` raised no prompt. Only the guards were simulated, with a made-up approval
  id: `deny` on a settled Dispatch returns `dispatch_inactive`, and `approve` returns
  `approval_requires_person`.
- **An Orca app restart was not verified;** only the script's process and its anchor pane
  restarted. Per
  Orca's orchestration guide, `runtime_unavailable` while Orca restarts is retried after a few
  seconds, and `orca open` (which brings the window forward) is only for an app that
  `orca status --json` reports as not running, so a launchd tick skips rather than opens it.
