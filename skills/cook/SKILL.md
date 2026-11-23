---
name: cook
description: >-
  Use this skill when the user types /cook to take a bug, feature or refactor from start to a
  verified, mergeable change by chaining the matching skills. Not for planning only (dev-plan),
  interface work (ui) or finishing a change that is already written (ship-pr).
disable-model-invocation: true
---

# cook

Pick the chain that matches the task. Load each skill in order; stop when the request is met.

- Bug: `diagnose` → `tdd` → ship-pr
- Feature: `brainstorm` → `grill` → `spec` → `architect` (hard work only) → `tickets` → `tdd` → ship-pr
- Refactor: `codebase-design` → ship-pr

Ship: `deslop` → `review` → `interrogate` (hard work only) → `done-check` → `pr` → `babysit`; load `fix-ci` when checks fail.

Last step: the project's own verify skill on the real surface, if the project has one.
