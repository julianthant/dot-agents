---
name: status-report
description: Produce the "where are we" report for a project — four fixed sections (COMPLETE · DRIFT from plan · NEXT · NEEDS YOU), every claim verified against code, gates and git rather than restated from plan docs, delivered as Markdown with an optional derived Operating Review deck. Invoke on `/status-report`, "where are we", "how far along are we", "operating review", "what's done / what's next", "what needs my revision / approval", "how have we drifted from the plan", or "review the work so far and list the next steps". Also "what did I get done" for a time window and weekly recaps of authored commits.
disable-model-invocation: true
---

# /status-report — where are we, verified

One deliverable, always the same shape, always checked against reality. The reader is the operator who decides what happens next; write for a decision, not for a narrative.

For "what did I get done", a standup line or a weekly recap of the user's own commits, follow
[work summary](references/work-summary.md) instead of the four-section report.

## 0 · Find the sources (do not guess)

1. **The plan** — whatever the project calls its SSOT: read the project's AGENTS.md or CLAUDE.md for a pointer first, else `docs/plans/`, `ROADMAP.md`, `TODO.md`, open issues. Note which plan file(s) you used.
2. **Reality** — `git log --oneline -40` (plus the verified target-to-HEAD range, relevant worktrees and branches), the gate commands from the manifest, any runtime self-report the project instructions name, the exact task's handoff when it contains unique relevant state, the findings ledger if one exists.
3. **Scope** — whole project unless the user named a phase/slice/workflow; say which.

## 1 · Verify before you write

A plan tick is a claim. For every item you are about to call complete, verify evidence sufficient for its acceptance claim: inspect behavior and required gates bound to
the tested state. A commit, file or test merely existing proves existence, not correctness or completion.
For volatile/live claims record the observation time and scope; do not present old evidence as current. If you cannot verify, it goes in DRIFT as `claimed, unverified: <why>`, not in COMPLETE. Handle quick lookups and critical-path verification directly. When delegation is authorized and useful independent work can continue, hand
a bounded read-only investigation to a subagent. Keep final verdicts and consequential uncertainty in this session.

## 2 · The report (fixed four sections)

```markdown
# Status — <project / scope> — <YYYY-MM-DD> (HEAD <sha>)
_Plan source: <paths>. Verified against: <gates run, git range, runtime report>._

## 1 · Complete
| item | evidence | since |
(one row per done item — evidence = commit sha / file / gate / run id / screenshot path; no row without evidence)

## 2 · Drift — where the as-built differs from the plan
| plan said | reality | kind | consequence |
(kind: not-done-but-ticked · done-but-unplanned · done-differently · blocked · stale-plan-text. Consequence = what it changes for the next step. Empty section = say "none found" and what you checked.)

## 3 · Next
1. <the next unit, why it is next, what it depends on, rough size>
2. …
(ordered; ≤7; each one actionable by an agent from this line alone)

## 4 · Needs you
| # | decision / approval | options | my recommendation | blocks |
(only things an agent cannot decide: scope calls, live/real-write authorizations, conflicting rules, money/time trade-offs. Numbered so you can answer "1 A, 2 B". Explain any jargon or §-reference inline — never cite a section number the reader can't decode.)
```

Rules: proof over prose; tables over paragraphs; every row cites something a reader can open; numbered items wherever the operator might answer positionally; no "all set" without a table behind it.

## 3 · Deliver

- Write it to `<dir>/status/<YYYY-MM-DD>-status.md`, where `<dir>` is the first that exists: the location the project instructions name for task material, a gitignored `.work/` at the repo root, an existing `handoffs/` or `docs/` folder; otherwise `docs/status/`. Say where.
- Echo only the headline counts in chat (`Complete 14 · Drift 3 · Next 5 · Needs you 2`) plus the Needs-you table verbatim — that is the part the operator acts on.
- If asked for an Operating Review deck/presentation or that named template, finish the verified
  Markdown first, then read [artifact deliverables](references/artifact-deliverables.md) and derive
  the deck from it. The deck never becomes status authority.
- For another website/artifact request, keep Markdown as the source and publish it with the host's
  artifact or site capability.

## Anti-patterns
- Restating the plan's checkboxes as status.
- A "Complete" row with no evidence column filled.
- Burying a decision inside prose instead of section 4.
- Writing more than one page of Next.
