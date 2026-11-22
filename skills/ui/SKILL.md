---
name: ui
description: >-
  Use this skill for interface work when no ticket names the ui-* skills to load: it picks the chain
  of ui-* skills for the job. For one UI job on its own, load that ui-* skill directly.
metadata:
  exposed: "true"
---

# ui

Read the nearest AGENTS.md and the project's design file first; they win over every skill's
defaults. Load each skill of the matching chain in order; stop when the request is met.

| Job | Chain |
|---|---|
| New product screen (dashboard, admin, tool) | ui-dashboard → ui-mock → ui-components → ui-states → ui-stress → ui-polish |
| New site, landing page or brand look | ui-direction → ui-style → ui-components → ui-motion → ui-polish |
| Change an existing screen | ui-mock → ui-polish |
| Looks generic, loud or cluttered | ui-critique → ui-redesign → ui-polish |
| Small visual fix | ui-polish |
| Motion | ui-motion |
| New UI dependency | ui-library → ui-components |
| 3D | ui-3d |
| Plan fixes for an existing screen | ui-audit |
| No design file yet | ui-design-md, then the matching chain |

Before push: ui-review; add ui-accessibility when the change adds controls, forms or dialogs. End on
the project's screenshot or verify check.
