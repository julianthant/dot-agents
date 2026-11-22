# AGENTS.md

Shared instructions for every coding agent on the operator's machines (Claude Code, Codex, Grok,
Cursor). A repository's own AGENTS.md is closer to the code and wins where they differ; the
operator's chat overrides both.

## Overview

- Work arrives as an Orca orchestration task from the coordinator (the Subaru bot) or directly
  from the operator. Planned work is a GitHub issue in the repository's Project; your spec names it.
- Complete authorized work. Resolve routine choices yourself; ask only for missing intent, changed
  scope or required permission. Preserve settled decisions.
- Usage-limit resumptions are expected: recover verified state and continue without restarting
  discovery or repeating completed work.

## Workflow

- Read the nearest module instructions before editing. Load exactly the skills your spec or issue
  names. Use `/ui` for frontend work.
- Report through Orca mail: `orca orchestration ask` for a question only the operator or
  coordinator can answer, and `worker_done` when finished. Never type into another agent's terminal.
- Start blocker notes `Waiting on: review | ci | operator | issue:<n> | process <pid>`. Review
  results start `REVIEW VERDICT: PUSH <sha>` or `REVIEW VERDICT: FIX <sha>`.
- Give Done / Now / Remaining once at start or genuine resume; later updates report only new
  verified results, blockers or decisions. Compaction does not restart this cadence.
- Hand off with `/handoff` when told to, when a context hook says so, or when stopping mid-task;
  report the handoff path in `worker_done` and stop. Never resume a session older than an hour;
  a fresh session continues from the handoff.
- Plan with `/dev-plan`; execute with `/cook`. Planning alone never edits code or starts workers.
  Only the coordinator starts workers, through `orca orchestration worker-start` and the account
  picker in `~/.agents/skills/dev-auto`.
- `/discuss` is explicit-only; it and `/grill` are stateless. Use `/grill` for real operator choices.

## Git

- Work on a dedicated branch in an owned worktree. Verify ownership, base, path and merge
  conditions. Stage exact paths, commit by logical step and preserve peers' work.
- Push only with explicit instruction, except your own `task/**` branch, which you push so CI runs.
  Never force-push published history; never push `main` or `staging`.

## Testing

- Match checks to the changed behavior and the repository's gate schedule. Update relevant tests;
  if tests run, run lint too. Preserve assertions, membership, isolation, coverage floors and
  timeouts.
- After pushing a task branch, wait on its CI in one blocking command
  (`gh run watch <run-id> --interval 30 --exit-status`); never poll in a loop. The default
  3-second refresh drains the account's shared GitHub API limit.
- Report actual outcomes and unrun obligations. A worker's claim is not completion evidence.

## PR instructions

- Write PR bodies with the `pr` skill. A PR that finishes an issue says `Closes #<n>`.
- Merge only through the repository's required checks; releases to production are the operator's.

## Tools

- Browser work uses the `playwright-cli` skill; explain and ask before switching tools. Never use
  Chrome-extension automation or chrome-devtools.
- Skills live once in `~/.agents/skills/`; edit them only there. Model and worker-role choices
  belong in each tool's own instructions, never in skills.

## Security

- Public repositories and PRs carry no account emails, local paths, terminal handles, secrets or
  personal data.
- Keep existing AGENTS.md / CLAUDE.md twins byte-identical; do not create missing twins unasked.
  Put lessons in their linked ledger, not in instruction files. Keep comments sparse: current
  behavior or constraints only.
