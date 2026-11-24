---
name: dev-auto
description: Pick an eligible account for an Orca worker, record its terminal, or check which live workers are due for handoff.
disable-model-invocation: true
---

# Orca account picker

Run these commands from this skill's directory. The picker reads accounts in
`accounts.toml` order (override the path with `DEV_AUTO_ACCOUNTS`) and applies
`limits.toml` to the newest usage reading from Orca or the account's CLI files.
Codex homes share one sessions folder that names no account, so a Codex
account reads only Orca's per-account reading, never its CLI files.
Pass the same `--host` to all three commands for a remote environment. The
default host is `local` and the default effort is `medium`.

Grok and Cursor use their signed-in CLI accounts on the local Mac. Set
`slots` on each account row. Their picker receipts have an empty `select`
command because Orca does not switch those sign-ins. Grok needs an Orca
weekly usage reading below 85%, and its live holds become due at 85%.
Cursor eligibility depends on its available slots.

1. **Pick an account** before starting a worker. The JSON output includes the
   account ID, label, plan, and selection command for the caller to run. When no
   account qualifies, the picker exits 1 and writes skip reasons to stderr.
   It skips an account with under 10% of its 5-hour window or 5% of its week
   left. Watch records usage handoffs in `usage-handoffs.json` beside the holds,
   so the picker skips the account through that window's reset even when its
   usage reading lags. If the reset time is unavailable, watch uses the affected
   window's duration from the handoff. Each
   window comes from the newest reading that has it. A Codex account never reads
   the shared sessions file. A plan's
   `prefer_models` in `limits.toml` put those models on its accounts first and
   every other model on them last (Pro Lite takes gpt-6.2-astra before Plus).

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
A routed Task or Bug can start when its branch and seat are available and its
card is ready. Fill posts one comment on a newly started ticket if its owned
paths overlap live work. The comment names the live tickets and their
overlapping paths. The PR that merges second resolves any conflict.
A real tick fetches the selected issue's base and verifies the new checkout's
commit. Use the same accounts file for every picker and fill process. Their
shared ledger is `holds.json` beside that file. Deploy the updated picker to
every writer before enabling fill. The ledger lock coordinates writes and
pruning. The separate pick, account-select, start and hold commands still
require the fleet coordinator to serialize launches on the same host.

Use `grok / grok-4.7 / high` or `cursor / grok-4.7-high / high` for
coding tickets with a Sonnet reviewer. For research, use `xhigh` in the
effort and the Cursor model suffix. A ticket with `Branch: none` and
`Reviewer: none` starts once from a detached local checkout pinned to
`origin/development`. The brief limits the worker to read-only work and
sends its `worker_done` report to the owning Run for the orchestrator to
act on. The review tick skips it. The detached checkout stays available
for inspecting findings and can be removed after the report is handled.

Fill records the Orca Task before calling worker-start. On an uncertain
response, rerun fill from the same coordinating Run. It looks up the existing
Dispatch and verifies its Run, Task and worktree before recording the hold and
Project fields. If Orca still cannot establish the outcome, inspect the
reported Task with `orca orchestration dispatch-show --task <task> --full`.
A missing Dispatch keeps that issue pending while other issues can fill
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

Grok startup also needs an observed turn or assistant activity in its exact
Orca transcript. If Orca only accepted the input, fill marks **Needs you**
on the same reservation and asks the Run to inspect the worker. The next
tick checks that Dispatch again.

## Issue snapshot

Every tick verb reads open issues from one snapshot,
`~/.local/state/dev-auto/issues.json`, refetched at most once a minute. A
Project write the tick makes is copied straight into the snapshot (a failed
write, or one on a card, open or closed, or field the snapshot cannot place,
marks it stale),
and fill's check right before a start always refetches. Closed issues (the
newest 400) are read in full every ten minutes and only the recently updated
ones in between.

`scripts/roadmap.py > data.json` renders the roadmap from the same snapshot;
for each open Task and Bug it adds `ready` and `holdReason` from the same
read-only plan used by fill. Readiness is computed per checkout for the next
fill of `~/Projects/hr-relay` or `~/.agents`.
The roadmap page in `skills/dev-auto/roadmap-app/` polls this JSON. From that
folder on the Mac, run `pnpm install && pnpm run publish` to deploy the page
to `~/.orca/bots/subaru-2/roadmap/`.
Launchd runs it every 15 seconds (`StartInterval` 15). Each run polls issues
(`since=`), PRs and workflow runs with `If-None-Match` and keeps the ETags in
`roadmap.json` beside the snapshot; a 304 costs no quota. The issue poll's
`since=` moves every ten minutes with a minute of overlap, and a change is a
different set of (number, `updated_at`) after it. A changed issue refetches
the snapshot (a refused refetch stays owed to the next run), any change
rebuilds the release pipeline, and both
otherwise refresh once a minute, which also catches Project edits made outside
the tick (they do not change an issue's `updated_at`). The Project's id and its field and
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
- A failed `worker_done` with a handoff path starts a fresh session on the same
  agent, model and effort. For a context handoff the original account remains
  eligible under the standard picker rules. Each Task lineage gets one checkpoint
  restart per branch head. A second checkpoint at the same head goes to **Needs you**.
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
handoff). It logs an unconfirmed review reservation as **Needs you** for that
ticket and continues with the rest. A newer coder report can supersede an old
reservation when its Dispatch traces back to the coder and no matching
reviewer Task exists. A PUSH whose branch head moved or whose task CI is not green
goes back to the coder with that reason. Close-tick and third-FIX mail goes to the
Run as `status`. A blocked close logs its reason and a retry command once per
blocked state.

## launchd cutover

The launchd Run is local to this machine.

Pause manual fleet ticks and starts. Keep the coordinating Run and its live
workers in place. Use a plain shell outside the worker sessions to create a
shell anchor in a long-lived worktree, then bind it to the existing Run.

```sh
orca terminal create --worktree path:<main-checkout> --title "dev-auto supervisor" --json
orca orchestration run-use --id <existing-run> --from <anchor> --json
python3 scripts/install_tick.py --run <existing-run> --anchor <anchor> \
  --repo-path <hr-relay-root> --repo-path <dot-agents-root>
```

Run the installer from a checkout that will stay on disk. When trialing from
a worktree, set `DEV_AUTO_ACCOUNTS` to the fleet's existing `accounts.toml`
so launchd uses the holds beside it. The installer validates the anchor and
installs `com.relay.dev-auto` in the current user's GUI domain.
launchd starts the job at load and every minute. Each invocation holds the
same lock as the standalone commands and runs watch, review, close and fill
against the existing Run and local state. After those, a `full-run` step runs
`gh workflow run development.yml --ref development` in julianthant/hr-relay at
most once every 20 minutes (stamp in `~/.local/state/dev-auto/full-run.json`),
and skips while a full run is queued or running, because GitHub never fires
that schedule. The workflow's decide job judges whether CI is idle and the head
untested. The tick dispatches nothing else: staging, production and release
workflows stay operator-only. Existing dispatches keep their hold
records and Project cards. The tick saves its anchor for this Run in
`~/.local/state/dev-auto/anchor.json`. If the anchor closes, the tick checks
the Run's owner and the live terminal inventory, then opens a replacement in the
first configured checkout and binds it to the same Run. If another live terminal
owns the Run, the tick reports that and stops before any fleet action.

Disable automatic starts with one command. This persists across logins and
unloads the running job.

```sh
launchctl disable "gui/$(id -u)/com.relay.dev-auto" && launchctl bootout "gui/$(id -u)/com.relay.dev-auto"
```

Use `~/.local/state/dev-auto/tick.log` to capture the two-hour run. Its UTC
start and end records show the observed cadence. Record the issue number
from the automatic start, its merged PR, and the original dispatches that
finished during the run. Keep local paths, terminal handles, account data
and raw CLI diagnostics out of the PR log excerpt.

Each roadmap render reads `tick.lastRun` and `tick.errors` from the latest
finished invocation. `tick` is `null` before the first run. An error lists
its phase and exception type. The matching local `tick.log` entry has the
CLI diagnostic.

## WSL accounts and fill placement

Keep one `[[account]]` row per managed login in the private, untracked
`accounts.toml`, in picker preference order within each plan. Get the ID from
`orca account list --json` **on WSL**. Disabled rows stay in the file.

```toml
[[account]]
host = "Juzzark-WSL"
agent = "codex"
id = "<WSL Orca account ID>"
label = "wsl-codex-1"
plan = "plus"
enabled = true
```

Set `local_target_tickets` at the top level and `target_tickets` in
`[hosts.Juzzark-WSL]`. A missing or zero remote target disables overflow.
For hr-relay, fill counts distinct live agent worktrees on task branches
and pending starts, tries the Mac first, then uses the registered hr-relay
checkout on WSL. Dry-run output includes the selected host. The Task and
Run stay on the coordinator. Account selection, Git preparation, worker
settings, terminal reads and holds belong to the execution host.

Orca's account commands operate on the machine running the CLI. The picker
reads active and inactive WSL windows through a short-lived WSL terminal and
closes it after the read. Use the picker's returned selection command so the
account switch runs on that host.
