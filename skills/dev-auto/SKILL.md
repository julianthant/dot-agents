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
