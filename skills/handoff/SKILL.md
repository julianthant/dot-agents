---
name: handoff
description: >-
  Use this skill when this session must stop and another agent will continue the work: the context
  is nearly full, you were told to hand off, or the task is pausing mid-way. It compacts the
  conversation into a handoff on the session's Concord ticket, or a temp file when there is no
  ticket; not for final reports, PR bodies or specs.
argument-hint: "What will the next session be used for?"
metadata:
  source: "https://github.com/mattpocock/skills"
  source_path: "skills/productivity/handoff"
  source_version: "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d"
  license: "MIT"
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work.

Where it goes:

- **The session has a ticket** (`CONCORD_AGENT_ID` is set): record the handoff on that Concord ticket with `update_work` (`task_id` of the ticket, `kind: "progress"`, content starting `Handoff:`). Do not write a temp file. If the call fails with `database is locked`, retry it unchanged after a short pause.
- **Otherwise:** save to the temporary directory of the user's OS (`$TMPDIR`, else `/tmp`; `%TEMP%` on Windows) - not the current workspace.

Include a "suggested skills" section in the document, naming which skills the next agent should call the Skill tool for.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
