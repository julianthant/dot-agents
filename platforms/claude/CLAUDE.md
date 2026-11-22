@~/.agents/AGENTS.md

## Claude

- Subagents use the agent types Claude Code lists; Codex model and role names do not apply here. `scout` (read-only lookups and one-command checks), `mechanic` (fully specified edits a command can judge) and `builder` (small changes inside an agreed design) carry their model and effort in `~/.claude/agents/`.
- Subagent use by worker:
  - hard work: hand the coding to `builder` and `mechanic`, which do most of the execution; keep the plan, the design, root cause, tough bugs and the final read of every diff; use `scout` only for trivial, tightly scoped lookups and checks;
  - UI and mock work: use `scout` only; make every edit yourself;
  - planner and orchestrator: use `scout` for every repo fact and keep only its `file:line` answer; write no code, so use neither `mechanic` nor `builder`.
- Never fork a session into a subagent. A subagent's report is a claim: read the diff or re-run the command before relying on it.
  `scout`, `mechanic` and `builder` carry their model and effort in `~/.claude/agents/`.
- Worker roles, launched by `dev_auto.py launch`, which starts the agent's CLI in a new Orca
  terminal in the ticket's worktree; each role's flags are its agent, model and effort:
  - planner = `--agent claude --model claude-fable-5-2 --effort high`; it works with the operator, runs
    `/expose`, then `brainstorm`, `grill`, `spec` and `tickets`, and writes tickets titled
    `feature / lane`; it launches nothing and writes no code;
  - orchestrator = `--agent claude --model claude-opus-5-5 --effort xhigh`, on any Claude account with a free Opus slot; it
    runs `dev-auto` (take ready tickets, pick tier and agent, launch, handle STUCK, NEEDS YOU and
    CLEARED REVIEW), pushes feature branches and opens their pull requests; it plans nothing and
    writes no code; a ticket that needs re-planning goes back to the planner through a note on the ticket;
  - hard work = `--agent claude --model claude-opus-5-5 --effort high`; hard tickets wait for a Claude slot and never fall back to another agent; `slot` gives no Opus worker to a Pro account above 70% used.
  - escalation = `--agent claude --model claude-fable-5-2 --effort high`, only for a hard ticket that still fails after one or two fix rounds: it diagnoses and writes the design on the ticket, writes no code, and is closed when the ticket's coder has the design;
  - UI and mock work = `--agent claude --model claude-sonnet-5-5 --effort medium`, UI and mock work only;
  - medium backend = `--agent grok --model grok-4.7 --effort xhigh` when the Grok account row has a slot, otherwise `--agent cursor --model grok-4.7-xhigh` when the Cursor account row is enabled and has a slot, otherwise `--agent codex --model gpt-6.1-sol --effort xhigh`;
  - easy backend = `--agent grok --model grok-4.7 --effort high` when the Grok account row has a slot, otherwise `--agent cursor --model grok-4.7-high` when the Cursor account row is enabled and has a slot, otherwise `--agent codex --model gpt-6.1-sol --effort medium`;
  - reviewer = `--agent codex --model gpt-6.1-sol --effort xhigh` on every ticket;
  - backend coding goes to Grok or Cursor first when it has a slot, so the Codex accounts stay free for reviewers;
  - scouts = agent type `scout`.
- Every ticket has a coder and a Codex reviewer, run through `duo`. The reviewer is always a
  second terminal in the ticket's worktree, on the Codex account `dev_auto.py slot` picks.
- The `dev-auto` supervisor script owns account slots, account swaps, wake-ups, stalls, relaunches
  and cleanup when a ticket closes; per-account capacity lives in
  `~/.agents/skills/dev-auto/accounts.toml`. Message a worker only when it must act; broadcast
  nothing.
- Context: at 300k a hook tells a Claude worker to hand off: finish the step, write the handoff to
  its ticket and stop. Codex, Cursor and Grok workers compact themselves and are never replaced for
  context. When a hook blocks a repeated failing action, record the blocker on the ticket.
- Goals: Claude, Codex and Grok prompts start with `/goal <goal line>`. Cursor prompts start with the goal line, then "Create this as your goal with your goal tool, update it as you work, and stop only when it is met."
- Reading: grep first, then Read only the needed range with `offset`/`limit`. Do not re-read an
  unchanged file already in context. Send "where is X / what does Y do" lookups to `scout` and keep
  only its `file:line` answer.
