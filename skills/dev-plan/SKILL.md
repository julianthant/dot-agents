---
name: dev-plan
description: >-
  Use this skill when the user types /dev-plan to take a feature idea all the way to approved,
  published tickets: it chains brainstorm, grill, spec and tickets, adding architect for hard work.
  Not for one planning step on its own (load that skill directly) or for building (cook).
disable-model-invocation: true
---

# dev-plan

Load each skill in order; stop when the request is met.

1. `brainstorm`
2. `grill`
3. `spec`
4. Hard work only: `architect`, up to its agreed sketch; implementation waits for the tickets.
5. `tickets`
