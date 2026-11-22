---
name: handoff
description: >-
  Use this skill when this session must stop and another agent will continue the work: the context
  is nearly full, you were told to hand off, or the task is pausing mid-way. It compacts the
  conversation into a scratch file and reports its path so the next agent can resume. Final
  reports, PR bodies and specs have their own workflows.
argument-hint: "What will the next session be used for?"
metadata:
  source: "https://github.com/mattpocock/skills"
  source_path: "skills/productivity/handoff"
  source_version: "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d"
  license: "MIT"
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work.

Save a uniquely named Markdown file in an existing, writable worktree scratch area. Before
writing there, verify the file path is already ignored with `git check-ignore -- <relative-path>`.
Use the session scratchpad if no worktree scratch location meets these conditions.

Record the goal, verified state, remaining work, and the next action. Include the GitHub issue and
PR URLs when applicable, plus the branch and commit if the next session needs them. Include a
"Suggested skills" section naming the skills the next agent should load.

Verify the file is readable and report its absolute path. For a dispatched Orca worker, finish
with the dispatch's `worker_done` command and pass the path with `--report-path`. Use
`--outcome failed` if the assignment is unfinished. In other sessions, give the path to the user.

For example, when work on issue #42 pauses in a worktree with no ignored scratch area, save
`handoff-issue-42.md` in the session scratchpad and pass its absolute path with `--report-path`
when sending `worker_done`.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
