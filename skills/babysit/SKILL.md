---
name: babysit
description: "Use this skill when an open PR needs shepherding to mergeable without re-prompting: resolve merge conflicts, fix failing checks, act on review and bot comments, and loop until it is green and clean or a design choice needs the user. Use it for 'babysit this PR', 'keep it green', 'get it mergeable', or right after a ticket's PR opens. Not for writing the PR body (use `pr`) or a one-off fix of failing CI (use `fix-ci`)."
metadata:
  source: "https://github.com/michael-denyer/pstack-claude"
  source_path: "plugins/pstack/skills/babysit, plugins/pstack/skills/poteto-mode/references/bugbot-triage.md"
  source_version: "60ae9e25610744bccd45bd405c211413a471aaf8"
  license: "MIT"
  includes: "https://github.com/kdoroszewicz/cursor-plugins-claude@37e5b44be6861c9228c718145cbfcda98cae8e8c cursor-team-kit/skills/fix-merge-conflicts, cursor-team-kit/skills/get-pr-comments (MIT)"
  exposed: "true"
---

# Babysit a PR

Claude Code analog of Cursor's built-in `/babysit`. The implementation is a loop over `gh` CLI plus the Claude Code `loop` skill for pacing.

## When to use

- There's an open PR and the user explicitly wants it kept green.
- The user invokes `/babysit` directly.
- A subagent that opens a PR does NOT babysit — return to the parent and let the parent decide.

## Steps

1. **Fetch PR state.**

   ```bash
   gh pr view <number> --json number,title,state,mergeable,reviewDecision,statusCheckRollup,mergeStateStatus,comments,reviews
   ```

2. **Triage in priority order.**
   - Merge conflicts (`mergeStateStatus == DIRTY`): follow [references/fix-merge-conflicts.md](references/fix-merge-conflicts.md). Force-push only if the branch is yours and not shared.
   - Failing checks (`statusCheckRollup` entries with `conclusion: FAILURE`): run the **fix-ci** skill. Root-cause the failure; fix the underlying code or test; commit; push.
   - Review comments: follow [references/get-pr-comments.md](references/get-pr-comments.md) for the summary, then act only on feedback you actually agree with. When a comment has a single mechanical answer — a rename, a guard clause, a formatting nit — make the edit and quote the comment in the commit message. When it hinges on a judgement call, or you can't tell what's being asked, don't guess: leave it and reply with what you would have done.
   - Review-bot comments (Bugbot and similar automation): classify fix/dismiss/ask before acting, per [`bugbot-triage.md`](references/bugbot-triage.md). Ask by default on security, data, and high-severity findings.

3. **Loop.** Use the Claude Code `loop` skill to pace re-checks. Pick the interval from what you're watching:
   - Active dev-auto CI run: block with `~/.agents/skills/dev-auto/scripts/ci-wait <full-sha>`.
   - Awaiting reviewer: 20–30 min heartbeat.
   - Idle but want to catch new comments: hourly.

4. **When to stop.**
   - Build is green, every comment resolved, branch merges cleanly → call it ready.
   - You've run three rounds of fix → push → recheck and it still isn't fully green → stop, summarise what's still broken, and hand control back.
   - The next fix would force a design choice → pause and put it to the user with `AskUserQuestion`.

5. **Report.** Summarize fixes applied, comments addressed, comments deferred (with reason), current PR status. Cite each commit by SHA.

## Hard rules

- Don't rewrite history on a branch others may have pulled. If a rebase or force-push looks necessary, clear it with the user first.
- Don't tweak a test's expected values just to get a pass. Only change an assertion when the behaviour genuinely changed and the assertion was pinned to the old behaviour.
- Never skip hooks (`--no-verify`).
- Never bypass a failing check by marking it as not required.
- `gh pr ready` only when all checks are green and no unresolved review comments remain.

## Cross-refs

- Use `interrogate` before opening if the diff is contested; once open, babysit takes over.
- Use the `write` skill's slop pass on any prose you write here (PR comments, commit messages, status reports).

## Provenance

This is a Claude Code analog of Cursor's `/babysit`, not a port — Cursor's implementation is closed source. The skill is independently authored, with its own prose and structure; the workflow is informed by Cursor's public `/babysit` behavior. The only overlap with other PR tools is the `gh` CLI commands it runs, which are functional invocations rather than copied text.
