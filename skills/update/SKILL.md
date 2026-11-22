---
name: update
description: Check that every agent's plugins and the copied UI sources are current, apply plugin updates, and refresh copied sources only after review. Use for /update, "are my plugins up to date", "update everything", or a scheduled check.
disable-model-invocation: true
---

# /update

Report first, then change. Plugin updates apply automatically; copied sources change only after the
user approves the diff, because an upstream release can rename or remove what our skills call.

## 1. Plugins

- **Claude Code:** `claude plugin marketplace update`, then `claude plugin update <id> --json` for each
  id from `claude plugin list --json`, skipping claude.ai-synced (`@synced`) plugins. A plugin from a
  local marketplace (for example `@repos`) is only as current as that folder; say so instead of
  calling it latest. Updates apply after a restart.
- **Codex:** `codex plugin marketplace upgrade`, then `codex plugin list` for versions.
- **Other agents:** report the version of plugins or extensions installed there (ponytail included);
  update through each one's own installer.

A version number alone can lag its repository. When a plugin says "latest" but its GitHub repo has
newer releases, report the gap.

## 2. Copied sources

Run `python3 <this-skill>/scripts/check_sources.py`. For every entry in [sources.json](sources.json)
it compares the upstream path at the pinned `commit` with upstream HEAD (read with `gh`, never
executed) and prints current, changed or missing paths. `local` names the skill that carries the
adapted copy.

For each changed source:

1. Read the upstream changes that matter: renamed or removed files, changed commands or flags, new
   rule ids, license changes.
2. Show the user what would change and what our skills would need, then wait for approval.
3. Port the approved changes into the `local` skill by hand; its files are adapted, so never copy
   upstream over them. Set the entry's `commit` and the skill's `metadata.source_version` to the new
   SHA, update every citing file, run `python3 ~/.agents/skills/skills/scripts/audit.py` until
   `broken_references` is empty, and commit in `~/.agents`.

Sources without a recorded upstream are reported as such; add one to `sources.json` when it becomes
known.

## 3. Report

One table: item, before, after, action (updated, current, held for review, no upstream), and anything
that needs a restart or the user's decision.

## Scheduled check

On request only, schedule a weekly check-only run with the host's scheduler (for example a recurring
task that runs `/update` in report mode). A scheduled run never applies copied-source changes.
