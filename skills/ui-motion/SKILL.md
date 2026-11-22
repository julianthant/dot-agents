---
name: ui-motion
description: >-
  Use this skill when UI motion is being added, judged or fixed: transitions, enter and exit
  animations, hover and press feedback, easing and duration, or animations that feel off, janky or
  excessive. Use it for "animate this", "review the animations", "make this feel smoother". Not for
  static layout or styling (ui-polish), 3D scenes (ui-3d) or choosing an animation library
  (ui-library).
metadata:
  source: "https://github.com/emilkowalski/skills"
  source_path: "skills/emil-design-eng, skills/review-animations, skills/improve-animations"
  source_version: "4061b67b8d976a186aca118594f54b638c5f718a"
  license: "MIT"
  exposed: "true"
---

# ui-motion

Read the nearest AGENTS.md and the project's design file first; where they differ from the files below, they win.

- Building or tuning motion: [emil-design-eng](references/emil-design-eng/emil-design-eng.md).
- Reviewing motion code: [review-animations](references/review-animations/review-animations.md).
- Auditing a codebase's motion and planning fixes:
  [improve-animations](references/improve-animations/improve-animations.md).

Each skill these files name that sits in `references/<name>/` is read from `references/<name>/<name>.md`; resolve its own links from its folder. Other skills they name: `break-ui` is ui-stress, `pick-ui-library` is ui-library,
`prototype` is ui-mock; any other is not installed, so use emil-design-eng.
