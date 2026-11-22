# Headless Orca supervisor

Proven on Orca 1.4.269 by `scripts/tick.py prove --repo id:<repo-id>`, run with every `ORCA_*`
pane variable stripped, as launchd runs it. Every command below takes `--json`.

## Identity and mail

Coordinator verbs (`run-create`, `run-use`, `worker-start`, `reply`, `send`, `deny`) refuse a
caller with no Orca pane (`no_active_sender_terminal`), and refuse a made-up `--from` such as
`script:x` or `bot:<name>` (`stable_pane_required`, seen on `run-current`). So the script opens its own plain shell pane,
the anchor, and passes that handle as `--from` on every call:

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

After a restart, keep the run id (store it), open a new anchor and rebind:
`orca orchestration run-use --id <run_id> --from <new-anchor>`. Proven: the anchor was closed
while the worker answered; the new anchor's `check --peek` already held that `worker_done`, a
consuming `check` returned it once and a second `check` returned nothing. A response lost in
transit is replayed with the `--retry-request <id>` the command printed.

## Workers and messages

| Step | Command | Arrives as |
| --- | --- | --- |
| Start | `worker-start --environment local --from <anchor> --run <run> --spec-file <f> --worktree path:<wt> --agent <a> [--model <m> --effort <e>]` | receipt with `dispatchId`, `terminal` |
| Ask | worker runs `orca orchestration ask` | `type: question`, `id`, `payload.dispatchId` |
| Answer | `reply --from <anchor> --run <run> --id <msg_id> --body <text>` | worker wakes with the answer |
| Done | worker sends `worker_done` | `type: worker_done`, `payload.outcome` |
| Follow-up | `send --from <anchor> --run <run> --to dispatch:<id> --subject <s> --body <b>` | receipt `delivery: follow_up` with a **new** `dispatchId`; use it from then on |
| Release | `worker-release --dispatch <newest id>` | `released`, `closed_agent_terminal`; older ids in the chain report `retained` / `ownership_transferred` |
| Cleanup | `terminal close --terminal <anchor>`, `worktree rm --worktree path:<wt> --force --delete-branch` | `removed: true` |

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
- **Live permission prompts were not exercised.** Workers launch under the person's own Orca
  setting for new agent tabs, which here is bypass permissions, and no flag overrides it. The
  proof ran an inert `touch` with no prompt, and showed only the guards: `deny` on a settled
  Dispatch returns `dispatch_inactive`, and `approve` returns `approval_requires_person`.
- **An Orca app restart was not exercised;** only the script's process and pane restarted. Per
  Orca's orchestration guide, `runtime_unavailable` while Orca restarts is retried after a few
  seconds, and `orca open` (which brings the window forward) is only for an app that
  `orca status --json` reports as not running, so a launchd tick skips rather than opens it.
