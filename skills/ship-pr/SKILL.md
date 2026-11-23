---
name: ship-pr
description: >-
  Use this skill to take a written change from cleanup to a mergeable PR:
  it chains deslop, review, done-check, pr and babysit, adding interrogate for hard work and fix-ci
  when checks fail. Not for writing the change (cook) or for reviewing a diff on its own (review).
---

# ship-pr

Load each skill in order; stop when the request is met. Follow [the PR rules](references/pr-rules.md)
at every step. A ticket's coder runs steps 1-4, then merges into the feature branch; steps 5-6 and
`fix-ci` belong to the feature's pull request.

1. `deslop`
2. `review`; on a ticket with a reviewer, the `duo` loop
3. Hard work only: `interrogate`
4. `done-check`
5. `pr`
6. `babysit`

When checks fail at any step, load `fix-ci`, then continue from that step.
