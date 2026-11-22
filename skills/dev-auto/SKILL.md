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
A missing Dispatch alone cannot establish that the start failed. Preparation
errors and an explicit no-effects refusal leave no issue reservation. Inspect
any prepared worktree before reusing or removing it.

When Codex is waiting, fill sends the observed prompt to the owning Run's
mailbox, keeps the reservation and hold, and defers the Coding update. The
operator handles hook review and model choices in the worker's own pane. The
next real tick checks that same Dispatch again.
