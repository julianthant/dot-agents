---
name: ui-components
description: >-
  Use this skill when the user wants to add, replace, search, fix, style or compose UI components
  in a project with shadcn/ui or a components.json: running the shadcn CLI, picking registry
  components (shadcn, Magic UI, 21st.dev), building chat interfaces, or applying a design system or
  DESIGN.md to shadcn/ui. Not for choosing a visual direction (ui-dashboard, ui-direction),
  non-component libraries (ui-library) or polishing a screen (ui-polish).
allowed-tools: Bash(npx shadcn@latest *), Bash(pnpm dlx shadcn@latest *), Bash(bunx --bun shadcn@latest *)
metadata:
  source: "https://github.com/shadcn-ui/ui, https://github.com/magicuidesign/magicui, https://github.com/21st-dev/skill"
  source_path: "skills/shadcn, skills/magic-ui, skills/21st-cli-use"
  source_version: "dd41d19e2f80ff6891951f7ced74c2c9a650b065, 1ac985adca3cdfab9b1739253fd14209687c4e9c, 15c7436c41dc88c54094f1be93ce53bda456b114"
  license: "MIT, Apache-2.0"
  modified: "Upstream files copied unchanged into references/; an included upstream SKILL.md is renamed to <folder>.md."
  exposed: "true"
---

# ui-components

Read the nearest AGENTS.md and the project's design file first; where they differ from the files below, they win.

Follow [shadcn](references/shadcn/shadcn.md); where its files link `./SKILL.md`, read `shadcn.md`.
For components outside the shadcn registry:

- animated components from Magic UI (`@magicui/*`): [magic-ui](references/magic-ui/magic-ui.md);
- the 21st.dev catalog through its `21st` CLI: [21st-cli-use](references/21st-cli-use/21st-cli-use.md).
