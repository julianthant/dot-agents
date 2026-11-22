## Learn from chats

Turn what the user keeps asking for and correcting into durable skill edits. One mode covers three
requests: reflect on the current conversation; extract workflow preferences from recent chats; capture
the user's working style.

1. **Scope.** The current conversation, or a window of recent chats (default the last 7 days), in the
   active workspace. State it back. Never read another project's transcripts unasked.
2. **Find transcripts.** Claude Code: `~/.claude/projects/<encoded-cwd>/` (the workspace path with `/`
   turned into `-`); find the current one with
   `node ~/.agents/skills/worktree-cleanup/scripts/find-transcript.mjs <projects-dir> "<opening prompt fragment>"`.
   Codex: `~/.codex/sessions/`. Order by real modification time and read only relevant parts.
3. **Mine with three lenses** — [judgment](learn/judgment-reviewer.md), [tooling](learn/tooling-reviewer.md)
   and [divergent](learn/divergent-reviewer.md) — each passed the transcript path verbatim. With
   delegation authorized they run as parallel read-only workers; otherwise apply them one at a time.
   Look for explicit preferences and corrections ("I prefer", "always", "never", "not what I asked",
   "stop"), workflow markers and repeated decisions.
4. **Rate and cluster.** Each finding is an atom — trigger, step, decision rule, quality bar, stop
   condition, evidence — rated strong (explicit or repeated), medium (accepted, repeated tool choices),
   weak (agent-chosen, one transcript) or contradicted (ask before writing). Cluster by workflow shape:
   shipping, review, simplification, debugging, capture, communication, delegation, validation.
   One-offs are not learnings.
5. **Synthesize** per [the synthesizer](learn/synthesizer.md) into Accepted, Rejected and Backlog.
   Prefer editing an existing skill; a new skill only for a recurring multi-step workflow with clear
   triggers; the shared `AGENTS.md` only for a short rule that applies everywhere; lessons go to the
   linked ledger. Anything a lint, script, metadata flag or check would enforce better moves to
   Backlog as a `correct` task.
6. **Approve, then apply.** Present the full Accepted, Rejected and Backlog lists and wait for the user
   to pick. Make trivial edits directly; substantive ones and description tuning follow
   [create](create.md) and [evaluation](evaluation.md); then [links](links.md).

Report: scope and corpus (cite conversations by id, never private content), the preference profile,
what was applied, and open questions.

Adapted from pstack's reflect and automate-me (MIT, `../LICENSE-pstack`) and cursor-team-kit's
workflow-from-chats (MIT, `../LICENSE-cursor-team-kit`).
