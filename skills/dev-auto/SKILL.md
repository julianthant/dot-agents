---
name: dev-auto
description: Pick an eligible account for an Orca worker, record its terminal, or check which live workers are due for handoff.
disable-model-invocation: true
---

# Orca account picker

Run these commands from this skill's directory. The picker reads accounts in
`accounts.toml` order (override the path with `DEV_AUTO_ACCOUNTS`) and applies
`limits.toml` to the newest usage reading from Orca or the account's CLI files.
Codex homes share one sessions folder, so a Codex account reads its CLI files
only when Orca has no reading for it.
Pass the same `--host` to all three commands for a remote environment. The
default host is `local` and the default effort is `medium`.

1. **Pick an account** before starting a worker. The JSON output includes the
   account ID, label, plan, and selection command for the caller to run. When no
   account qualifies, the picker exits 1 and writes skip reasons to stderr.
   It skips an account with under 10% of its 5-hour window or 5% of its week
   left, so no start or restart lands on an account about to hand off.

   ```sh
   python3 scripts/pick.py pick --agent codex --model gpt-6.2-sol
   ```

2. **Record the hold** after the worker starts. Supply the terminal handle,
   the selected account ID or label, and the worker's model and effort.

   ```sh
   python3 scripts/pick.py hold --terminal "$terminal" --account "$account" --agent codex --model gpt-6.2-sol
   ```

3. **Check for handoffs**. This prints the account, terminal and usage for each
   live worker at its plan's handoff threshold. It exits 0 when any are due and
   1 when none are due.

   ```sh
   python3 scripts/pick.py due
   ```

## Fill and startup recovery

Run `scripts/tick.py fill --repo-path <checkout> --dry-run` to see the plan.
A real tick fetches the selected issue's base and verifies the new checkout's
commit. Use the same accounts file for every picker and fill process. Their
shared ledger is `holds.json` beside that file. Deploy the updated picker to
every writer before enabling fill. The ledger lock coordinates writes and
pruning. The separate pick, account-select, start and hold commands still
require the fleet coordinator to serialize launches on the same host.

Fill records the Orca Task before calling worker-start. On an uncertain
response, rerun fill from the same coordinating Run. It looks up the existing
Dispatch and verifies its Run, Task and worktree before recording the hold and
Project fields. If Orca still cannot establish the outcome, inspect the
reported Task with `orca orchestration dispatch-show --task <task> --full`.
A missing Dispatch keeps that issue pending while disjoint issues can fill
other available seats. After inspecting the Task and confirming a replacement
would be safe, run `scripts/tick.py fill --release <issue>` to clear just its
pending reservation. Release exits without launching anything. Preparation errors and an explicit no-effects refusal leave no issue reservation. A
preparation error after the Task exists cancels that Task, and the error names
the worktree (and the Task, if the cancel failed). Inspect any prepared
worktree before reusing or removing it. After a confirmed Codex start, fill
selects the coordinator bot's prior Codex account again, or the account
`pick.coordinator` prefers once that one is past its handoff point; the
reviewer start and the watch restart do the same.

For Codex workers, fill sets `notice.hide_rate_limit_model_nudge = true` in
the new worktree's project config and adds `.codex/` once to the clone's shared
`info/exclude` (`git rev-parse --git-path info/exclude` resolves to the common git
directory), so normal staging leaves untracked `.codex/` settings out in the main
checkout and every worktree of that clone; Git still permits force-adding them. Codex applies project settings in trusted
workspaces. The installed 0.171.0 TUI reads this preference before showing its
model-switch menu. Startup hook consent depends on the hooks' trust state.
An operator reviews new or changed hooks. [Model nudge](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/chatwidget/rate_limits.rs#L475-L480) · [Hook review](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/startup_hooks_review.rs#L283-L293)

If a startup prompt appears, fill forwards the observed prompt to the owning
Run, sets the Project's Agent state to **Needs you**, and retains the hold
and reservation. After the operator answers in the worker pane, the next real
tick checks that same Dispatch before recording Coding.

## Issue snapshot

Every tick verb reads open issues from one snapshot,
`~/.local/state/dev-auto/issues.json`, refetched at most once a minute. A
Project write marks it stale, and fill's check right before a start always
refetches. Closed issues (the newest 400) are read in full every ten minutes
and only the recently updated ones in between. `scripts/roadmap.py > data.json`
renders the roadmap from the same snapshot. The Project's id and its field and
option ids are cached in `project.json` beside it (`gh project field-list`
costs about 100 GraphQL points); an unknown field or option refetches them, and
a failed Project edit drops the cache.

## Watch

Run `scripts/tick.py watch` from the coordinating Run (or pass its launchd
`--anchor` and `--run`). Each tick runs `pick.py coordinator` (deferred while a
coordinator restore is still owed), then checks
the current attempt of every live worker in the Run and logs one `watch` line:

- A limit screen or the Codex model-switch menu (a Codex status line naming another model is not a limit):
  once `pick.py` gives a different account, `worker-stop`, then the same Task
  restarts with `worker-start --task <task> --retry-of <dispatch>` (without
  `--retry-of` when an Orca restart left the Task ready). The
  replacement then gets mail with the predecessor's `reportPath` and report.
- A near-limit screen or an account `pick.py due` lists: one handoff request
  to the Dispatch. Its failed `worker_done` starts the replacement.
- The limited account comes from the worker's hold, remembered while it runs.
  With none recorded, the watch stops nothing and escalates to the Run.
- The card shows **Handed off** until the replacement starts; with no seat it
  stays there and each tick retries.
- A crash relaunches once in the worker's own phase, then goes to the Run as
  an escalation (Needs you).
- A screen unchanged for 20 minutes gets one nudge, then an escalation.
- A permission prompt goes to the Run and the card goes to **Needs you**.

The watch never types into a pane or answers a menu. A restart whose
`worker-start` response was lost is never repeated. The next tick adopts the
newest Dispatch of the same Task when it retries the old one in the same Run
and worktree, and resends the resume note until Orca returns a receipt.
Otherwise it escalates: inspect the Run, then delete that terminal's row from
the `watch` table in `starts.sqlite`. A failed worker of a **Merging** ticket
is still current when the review state names its Dispatch.

The review tick moves a ticket to **Needs you** on an invalid verdict or a
reviewer's failed `worker_done` (unless watch is restarting it from a
handoff). A PUSH whose branch head moved or whose task CI is not green goes
back to the coder with that reason. Close-tick and third-FIX mail goes to the
Run as `status`.
