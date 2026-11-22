# Working instructions

- Complete authorized work. Resolve routine choices yourself; ask only for missing intent, changed
  scope or required permission. Preserve settled decisions. Usage-limit resumptions are expected:
  recover verified state and continue without restarting discovery or repeating completed work.
- Never push without explicit instruction. Use a dedicated branch for implementation; use an owned
  worktree for concurrent work or a shared, dirty or integration-only checkout. Verify ownership,
  base, path and merge conditions. Stage exact paths, commit by logical category and preserve peers.
- Read the nearest module instructions before editing. Keep existing AGENTS.md / CLAUDE.md twins
  byte-identical; do not create missing twins unasked. Put lessons in their linked ledger, not in
  instruction files. Keep comments sparse: current behavior or constraints only.
- Match checks to changed behavior and the approved gate schedule. Update relevant tests; if tests
  run, run lint too. Preserve assertions, membership, isolation, coverage floors and timeouts.
  Report actual outcomes and unrun obligations; a worker's claim is not completion evidence.
- Plan with `/dev-plan`; execute with `/cook`. Planning alone never edits code or launches workers.
  The operator chooses session models. Launch workers only under explicit authorization, through
  `dev-auto`; keep consequential decisions in the parent.
- Concord holds tickets. Claim yours, follow its brief, load exactly its `Skills:` line and log
  progress there; never address the orchestrator or any terminal. Start blocker notes
  `Waiting on: review | ci | operator | ticket:<id> | process <pid>` and review findings
  `REVIEW VERDICT: PUSH` or `REVIEW VERDICT: FIX`. Retry "database is locked" failures. Claim
  repo-relative paths.
- Give Done / Now / Remaining once at start or genuine resume; later updates report only new
  verified results, blockers or decisions. Compaction does not restart this cadence.
- Use `/ui` for frontend work. Browser work uses the `playwright-cli` skill; explain and ask before
  switching tools. Never use Chrome-extension automation or chrome-devtools.
- `/discuss` is explicit-only; it and `/grill` are stateless. Use `/grill` for real operator
  choices and `/handoff` for interrupted unique state.
- Skills live once in `~/.agents/skills/`; edit them only there. Model and worker-role choices
  belong in each tool's own instructions, never in skills.
