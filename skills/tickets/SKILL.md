---
name: tickets
description: >-
  Use this skill when a spec, plan or conversation has to be split into work for other agents:
  tracer-bullet vertical slices with blocking edges, each published as an unowned Concord task that
  carries its brief, the skills its worker loads, the coder and reviewer kinds and the tier. Use it
  whenever work is handed to other sessions, even if the user only says "break this up"; not for
  writing the spec (spec) or doing the work.
metadata:
  source: "https://github.com/mattpocock/skills"
  source_path: "skills/engineering/to-tickets"
  source_version: "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d"
  license: "MIT"
---

# To Tickets

Break a plan, spec, or conversation into a set of **tickets**: tracer-bullet vertical slices, each declaring the tickets that **block** it.

Each ticket becomes one Concord task that sits unowned until a worker claims it.

## Process

### 1. Run `expose`

Load the `expose` skill and read its whole output. Every ticket's `Skills:` line names skills from that list only.

### 2. Gather context

Work from whatever is already in the conversation context. If the user passes a reference (a spec path, an issue number or URL) as an argument, fetch it and read its full body and comments.

### 3. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code. Ticket titles and descriptions should use the project's domain glossary vocabulary, and respect ADRs in the area you're touching.

Look for opportunities to prefactor the code to make the implementation easier. "Make the change easy, then make the easy change."

### 4. Draft vertical slices

Break the work into **tracer bullet** tickets.

<vertical-slice-rules>

- Each slice cuts a narrow but COMPLETE path through every layer (schema, API, UI, tests): vertical, NOT a horizontal slice of one layer
- A completed slice is demoable or verifiable on its own
- Each slice is sized to fit in a single fresh context window
- Any prefactoring should be done first

</vertical-slice-rules>

Give each ticket its **blocking edges**: the other tickets that must complete before it can start. A ticket with no blockers can start immediately.

**Wide refactors are the exception to vertical slicing.** A **wide refactor** is one mechanical change (rename a column, retype a shared symbol) whose **blast radius** fans across the whole codebase, so a single edit breaks thousands of call sites at once and no vertical slice can land green. Don't force it into a tracer bullet; sequence it as **expand–contract**. First expand: add the new form beside the old so nothing breaks. Then migrate the call sites over in batches sized by blast radius (per package, per directory), each batch its own ticket blocked by the expand, keeping CI green batch to batch because the old form still exists. Finally contract: delete the old form once no caller remains, in a ticket blocked by every migrate batch. When even the batches can't stay green alone, keep the sequence but let them share an integration branch that all block a final integrate-and-verify ticket; green is promised only there.

For each ticket also choose the skills its worker needs (exactly those, no more), the coder and reviewer agent kinds, and the tier. Owned paths of tickets that can run at the same time must not overlap.

### 5. Quiz the user

Present the proposed breakdown as a numbered list. For each ticket, show:

- **Title**: `<feature> / <lane>`
- **Blocked by**: which other tickets (if any) must complete first
- **What it delivers**: the end-to-end behaviour this ticket makes work
- **Skills, coder, reviewer, tier**

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the blocking edges correct: does each ticket only depend on tickets that genuinely gate it?
- Should any tickets be merged or split further?

Iterate until the user approves the breakdown.

### 6. Publish the tickets to Concord

Publish the approved tickets in dependency order (blockers first). Give each a stable task id, `<feature-slug>-<NN>`, numbered from `01`.

1. Create it with `start_work`: `task_id`, `title` (`<feature> / <lane>`), `kind` (your own agent kind), a one-line `summary`, `notes` from the template below, and `expected_files` holding the owned paths, repo-relative.
2. Release it with `transfer_work`: same `task_id`, `action: "release"`, `expected_version` set to the version `start_work` returned, and a short `reason`. The task is now unowned and waits for a worker to claim it.
3. A call that fails with `database is locked` is retried unchanged after a short pause. Any other failure stops publishing; report which tickets were created.

If the Concord tools are not available, stop and say so instead of publishing elsewhere.

Work the **frontier**: any ticket whose blockers are all done. For a purely linear chain that means top to bottom.

Do NOT close or modify any parent issue.

<ticket-notes-template>

Outcome: the end-to-end behaviour this ticket makes work, from the user's perspective, not a layer-by-layer implementation list.
Stop when: the point at which the worker is done (for example: checks pass and the work is committed).
Owned paths: the repo-relative paths this worker may change.
Branch: `task/<feature>/<lane>` · Feature branch: `feature/<feature>`
Checks: the commands that prove the outcome.
Do not touch: paths, systems and decisions outside this ticket.
Acceptance:
- [ ] Criterion 1
- [ ] Criterion 2
Skills: a, b, c
Coder: <agent kind> · Reviewer: <agent kind, or none> · Tier: <tier>
Blocked by: <ticket ids>, or "none"

</ticket-notes-template>

A feature's last ticket, or a separate ticket, covers opening the feature's pull request only when the plan needs a worker for it.

Outside the owned paths, avoid specific file paths or code snippets: they go stale fast. Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it and note briefly that it came from a prototype. Trim to the decision-rich parts, not a working demo, just the important bits.
