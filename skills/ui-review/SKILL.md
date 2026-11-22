---
name: ui-review
description: >-
  Use this skill when a branch, pull request, commit range or uncommitted change touches UI and
  needs reviewing before push: it finds the interface problems the change introduced or regressed
  across accessibility, layout, writing, typography, color and UI, with file:line findings. Use it
  for a ticket's UI review step or "review my UI changes". Not for judging a whole screen as it
  stands (ui-critique), planning fixes for an existing screen (ui-audit) or non-UI code (review).
metadata:
  source: "https://github.com/jakubkrehel/skills"
  source_path: "skills/interface-review, skills/better-interface, skills/better-accessibility, skills/better-colors, skills/better-layout, skills/better-typography, skills/better-ui, skills/better-writing"
  source_version: "8b254eb75dadf3615a6abce87aed28e1b6e4d77c"
  license: "MIT"
  exposed: "true"
---

# ui-review

Read the nearest AGENTS.md and the project's design file first; where they differ from the files below, they win.

Follow [interface-review](references/interface-review/interface-review.md). Each skill these files name that sits in `references/<name>/` is read from `references/<name>/<name>.md`; resolve its own links from its folder.

Other skills it names: `state-machine` is ui-states, `break` is ui-stress, `variant` is ui-mock.
