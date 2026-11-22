---
name: deslop
description: "Use this skill when a branch or diff needs AI slop removed before review or commit: needless or out-of-style comments, abnormal defensive checks and try/catch, casts that dodge types, deep nesting and code that does not match the surrounding file, with behaviour unchanged. Use it for 'clean this up', 'deslop', 'remove the comments', or a final tidy pass on agent-written code. Not for judging whether a change meets its spec or the repo's standards (use `review`)."
metadata:
  source: "https://github.com/kdoroszewicz/cursor-plugins-claude"
  source_path: "cursor-team-kit/skills/deslop"
  source_version: "37e5b44be6861c9228c718145cbfcda98cae8e8c"
  license: "MIT"
  exposed: "true"
---

# Remove AI code slop

Check the diff against main and remove AI-generated slop introduced in the branch.

## Focus Areas

- Extra comments that are unnecessary or inconsistent with local style
- Defensive checks or try/catch blocks that are abnormal for trusted code paths
- Casts to `any` used only to bypass type issues
- Deeply nested code that should be simplified with early returns
- Other patterns inconsistent with the file and surrounding codebase

## Guardrails

- Keep behavior unchanged unless fixing a clear bug.
- Prefer minimal, focused edits over broad rewrites.
- Keep the final summary concise (1-3 sentences).
