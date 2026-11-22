---
name: ui-states
description: >-
  Use this skill when the user wants to work on every state of one component: loading, empty,
  error, partial, disabled, success and the rest, each rendered with mock data on a throwaway page
  with a switcher that is removed in one step afterwards. Use it for "show me all the states of X"
  or "design the empty and error states". Not for worst-case data (ui-stress), design variants (ui-mock) or reviews (ui-critique).
metadata:
  source: "https://github.com/jakubkrehel/skills"
  source_path: "skills/state-machine"
  source_version: "8b254eb75dadf3615a6abce87aed28e1b6e4d77c"
  license: "MIT"
  exposed: "true"
---

# ui-states

Read the nearest AGENTS.md and the project's design file first; where they differ from the files below, they win.

Follow [state-machine](references/state-machine/state-machine.md).

Other skills it names: `break` is ui-stress, `variant` is ui-mock,
`interface-review` is ui-review, `better-interface` is ui-critique.
