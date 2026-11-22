---
name: duo
description: >-
  Use this skill when a ticket names a coder and a reviewer, or when you are either one: it runs
  the review loop that clears a local commit before any push. The reviewer is a second session of
  the kind the ticket's Reviewer line names; coder and reviewer each run in their own Orca terminal
  in the ticket's worktree. Not for a one-off review of a diff (review) or for opening and landing
  the PR (ship).
metadata:
  exposed: "true"
---

# duo

One coder, one reviewer, one ticket. The reviewer is a second session of the kind the ticket's
`Reviewer` line names, in its own terminal in the ticket's worktree; never a third agent on the
ticket. The coder runs `ship`; its `review` step, and `interrogate` on hard work, is this loop.

## Rules

- Before each round, merge the latest base into the branch (never rebase), resolve conflicts, pass
  typecheck, lint and the ticket's checks, and commit locally. Conflict resolutions are part of the
  reviewed diff.
- Merge only the exact SHA the latest `REVIEW VERDICT: PUSH <sha>` cleared. Every later commit,
  including a merge or a fix, needs a new verdict first.
- Take turns: the coder does not change the commit under review.

## Terminals and tickets

Coder and reviewer, each of the kind its ticket line names, run in their own Orca terminal in the ticket's
worktree and talk only through the ticket. The coder posts a note with a line starting `REVIEW READY <sha>`, first or any later line, and a blocker note starting `Waiting on: review`; the reviewer posts a finding on the coder's ticket starting `REVIEW VERDICT: PUSH <sha>` or `REVIEW VERDICT: FIX <sha>`. The reviewer holds a review ticket, `<ticket>-review`, and posts its verdicts on
the coder's ticket.

Coder:

1. Record `REVIEW READY <sha>` on the ticket, then the blocker `Waiting on: review of <sha>`, and
   end your turn. The supervisor wakes you when a verdict lands.
2. On `REVIEW VERDICT: FIX`, fix each finding or answer it on the ticket with evidence, then go
   back to step 1.
3. On `REVIEW VERDICT: PUSH <sha>`, run `done-check`, then merge exactly that SHA into the feature branch as `ship`'s PR rules say. With `Feature branch: none`, record the PR title and body on the ticket and complete it with `finish_work`; the orchestrator pushes and opens the PR, so do not wait for it.

Reviewer:

1. Claim the review ticket and read the coder's ticket brief and acceptance.
2. For each `REVIEW READY <sha>` newer than your last verdict, run `review` on that commit against
   the ticket, plus `interrogate` on hard work, and rerun the ticket's checks.
3. Post `REVIEW VERDICT: PUSH <sha>`, or `REVIEW VERDICT: FIX <sha>` followed by each finding with
   its file, line and evidence, on the coder's ticket. Post only `REVIEW VERDICT:` findings there,
   never a `Waiting on:` note.
4. After a verdict, end your turn and post nothing until woken. The supervisor's nudge on a quiet
   terminal is your cue to check the ticket again.
5. Stay on the ticket until the coder's ticket is complete or closed, then finish the review ticket
   with `finish_work`.
