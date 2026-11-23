# PR rules

## Branches

- A ticket titled `<feature> / <lane>` works on branch `task/<feature>/<lane>`, created from
  `feature/<feature>`. No pull request is opened for a ticket.
- A ticket with a reviewer merges only the SHA its latest `REVIEW VERDICT: PUSH <sha>` cleared
  (`duo`). A merge or fix after that needs a new verdict.
- On that verdict the coder merges the SHA into `feature/<feature>` with `git merge --no-ff`,
  records the merge SHA on the ticket and closes it.
- A ticket with `Reviewer: none` runs its focused checks, typecheck and lint, then merges its branch
  into `feature/<feature>` the same way.
- Fetch origin first. When a branch exists on the remote, `git pull --no-rebase origin <branch>`.
  Merge the latest base; never rebase or force-push. `git rev-list --left-right --count
  origin/<base>...HEAD` must show zero behind.
- Workers never push. Reach a protected branch only through a PR; never
  push to it. Releases happen only on the operator's go.

## The PR

- A feature has one PR: `feature/<feature>` into the integration branch named in the project's
  `AGENTS.md`. The orchestrator pushes the feature branch and opens it once the feature's tickets are
  merged into it, or earlier when the project asks for an early PR. Keep its title and description
  current on every push or merge.
- `pr`, `babysit` and `fix-ci` apply to the feature's PR, not to tickets.
- Once checks are green, `gh pr merge <N> --auto --merge --match-head-commit <sha>`. Green required
  checks merge that SHA. When the project holds a PR for an operator step, the orchestrator enables
  its auto-merge. Never merge a PR by hand.
- Batch fixes. While a CI run is in progress, push again only after it fails. Fix the root cause,
  with bounded cleanup of the fixtures or slow setup it touches; no large rewrites.
- A failure that the base also has needs evidence and one separate repair PR into the base, not a
  copy in every branch.
- Let the project's merge automation bring open PRs up to date. Resolve returned conflicts on the
  PR branch, rerun the affected checks, and get the resolution reviewed. A conflict with another
  live ticket's paths means overlapping scope: record it on the ticket.
- A repair that needs CI or workflow configuration edits, a weakened guard or work beyond the
  ticket goes to the operator as `Waiting on: operator`; the project lists its own such cases.
- A rejected change is reverted on the base through a PR.
