---
name: ui-style
description: >-
  Use this skill when a UI needs grounded picks for its style, color palette, font pairing, chart
  type, icons or UX rules for its product type and stack: "pick a palette", "which fonts", "which
  chart fits this", "what style suits an ops dashboard". It searches a bundled design database. Not
  for building the screen (ui-dashboard, ui-direction) or judging one (ui-critique).
metadata:
  source: "https://github.com/nextlevelbuilder/ui-ux-pro-max-skill"
  source_path: ".claude/skills/ui-ux-pro-max"
  source_version: "6ae86f61f520d6ca229d2f40e070410638c908c5"
  license: "MIT"
  exposed: "true"
---

# ui-style

Read the nearest AGENTS.md and the project's design file first; where they differ from the files below, they win.

Follow [ui-ux-pro-max](references/ui-ux-pro-max/ui-ux-pro-max.md). Run its search as
`python3 ~/.agents/skills/ui-style/references/ui-ux-pro-max/scripts/search.py`, in place of the
`${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/scripts/search.py` path it shows. The bundled
stack data covers `react`, `shadcn` and `html-tailwind`.
