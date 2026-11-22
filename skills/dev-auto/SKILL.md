---
name: dev-auto
description: Pick an eligible account for an Orca worker, record its terminal, or check which live workers are due for handoff.
disable-model-invocation: true
---

# Orca account picker

Run these commands from this skill's directory. The picker reads accounts in
`accounts.toml` order (override the path with `DEV_AUTO_ACCOUNTS`) and applies
`limits.toml` to the newest usage reading from Orca or the account's CLI files.
Pass the same `--host` to all three commands for a remote environment. The
default host is `local` and the default effort is `medium`.

1. **Pick an account** before starting a worker. The JSON output includes the
   account ID, label, plan, and selection command for the caller to run. When no
   account qualifies, the picker exits 1 and writes skip reasons to stderr.

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
pending reservation. Release exits without launching anything. Preparation errors and an explicit no-effects refusal leave no issue reservation. Inspect
any prepared worktree before reusing or removing it.

For Codex workers, fill sets `notice.hide_rate_limit_model_nudge = true` in
the new worktree's project config. Codex applies project settings in trusted
workspaces. The installed 0.171.0 TUI reads this preference before showing its
model-switch menu. Startup hook consent depends on the hooks' trust state.
An operator reviews new or changed hooks. [Model nudge](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/chatwidget/rate_limits.rs#L475-L480) · [Hook review](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/startup_hooks_review.rs#L283-L293)

If a startup prompt appears, fill forwards the observed prompt to the owning
Run, sets the Project's Agent state to **Needs you**, and retains the hold
and reservation. After the operator answers in the worker pane, the next real
tick checks that same Dispatch before recording Coding.

## Watch

Run `scripts/tick.py watch` from the coordinating Run (or pass its launchd
`--anchor` and `--run`). Each tick runs `pick.py coordinator`, then checks
the current attempt of every live worker in the Run and logs one `watch` line:

- A limit screen, the Codex model-switch menu or a switched Codex model:
  `worker-stop`, then a fresh worker on another account `pick.py` gives,
  started from the Task's spec plus the predecessor's last report.
- A near-limit screen or an account `pick.py due` lists: one handoff request
  to the Dispatch. Its failed `worker_done` starts the fresh worker.
- The card shows **Handed off** until the replacement starts; with no seat it
  stays there and each tick retries.
- A crash relaunches once, then goes to the Run as an escalation (Needs you).
- A screen unchanged for 20 minutes gets one nudge, then an escalation.
- A permission prompt goes to the Run and the card goes to **Needs you**.

The watch never types into a pane or answers a menu. A restart whose
`worker-start` response was lost is escalated, not repeated: inspect the Run,
then delete that terminal's row from the `watch` table in `starts.sqlite`.
