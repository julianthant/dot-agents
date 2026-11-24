---
name: tickets
description: >-
  Use this skill when a spec, plan or conversation has to be split into work for other agents:
  tracer-bullet vertical slices with blocking edges, each published as a GitHub issue that
  carries its brief, the skills its worker loads, the coder and reviewer routes and the tier. Use it
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

Publish one unassigned GitHub issue per ticket in the target repository and track it in
`julianthant`'s HR Relay Roadmap (Project 5). Subaru is the coordinator for the published work.

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

For each ticket also choose the skills its worker needs, the coder and reviewer routes, and the
tier. Owned paths of tickets that can run at the same time must not overlap. Group tickets into
waves based on their blockers and assign each wave Start and Target dates.

### 5. Quiz the user

Present the proposed breakdown as a numbered list. For each ticket, show:

- **Title**: `<feature> / <lane>`
- **Blocked by**: which other tickets (if any) must complete first
- **What it delivers**: the end-to-end behaviour this ticket makes work
- **Skills, coder, reviewer, tier**
- **Area and wave**: the Project Area and the wave's Start and Target dates

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the blocking edges correct: does each ticket only depend on tickets that genuinely gate it?
- Should any tickets be merged or split further?

Iterate until the user approves the breakdown.

### 6. Publish the GitHub issues

Read the Project ID with `gh project view 5 --owner julianthant --format json` and the live field
and Area option IDs with `gh project field-list 5 --owner julianthant --format json`. Use the
approved Area and wave dates for each ticket.

1. Create the approved issues in the target repo, starting with blockers. Use `gh issue create --repo "$repo" --title
   "$title" --body-file "$body_file"`, with the body below. Keep each issue's URL and number.
2. Add each issue to the Project using `gh project item-add 5 --owner julianthant --url
   "$issue_url" --format json --jq .id`. Set each field in a separate `gh project item-edit`
   call with `--project-id`, `--id` (the returned item ID), and `--field-id`. Use
   `--single-select-option-id` for Area and `--date YYYY-MM-DD` for Start and Target.
3. For each blocker, read its numeric REST ID with `gh api
   "repos/$blocker_repo/issues/$blocker_number" --jq .id`. Set the dependency on the
   **dependent** issue with `gh api -X POST "repos/$repo/issues/$issue_number/dependencies/blocked_by"
   -F "issue_id=$blocker_id"`.
4. Read back Area, Start, and Target in the Project and the dependencies from each issue's
   `dependencies/blocked_by` endpoint. Report issue URLs and the verified blocking edges. If
   publishing fails partway through, report what succeeded and resume from the existing issue
   URLs after checking their state.

For example, if `Import / Preview` is issue `#42` in wave 1 and `Import / Save` is issue `#43`
in wave 2, set each item's Area and its wave's dates in Project 5. Post to
`issues/43/dependencies/blocked_by` with the REST `.id` from `issues/42`. The resulting link
shows `#43` as blocked by `#42`.

Subaru can schedule any ticket whose blockers are all complete. For a linear chain that means top to bottom.

Do NOT close or modify any parent issue.

<issue-body-template>

Outcome: the end-to-end behaviour this ticket makes work, from the user's perspective, not a layer-by-layer implementation list.
Stop when: the point at which the worker is done (for example: checks pass and the work is committed).
Owned paths: the repo-relative paths this worker may change.
Branch: `task/<feature>/<lane>`
Checks: the commands that prove the outcome.
Do not touch: paths, systems and decisions outside this ticket.
Acceptance:
- [ ] Criterion 1
- [ ] Criterion 2

Skills: a, b, c
Coder: <approved route>
Reviewer: <approved route, or none>
Tier: <tier>

</issue-body-template>

A feature's last ticket, or a separate ticket, covers opening the feature's pull request only when the plan needs a worker for it.

Outside the owned paths, avoid specific file paths or code snippets: they go stale fast. Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it and note briefly that it came from a prototype. Trim to the decision-rich parts, not a working demo, just the important bits.
