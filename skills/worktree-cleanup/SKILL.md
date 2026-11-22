---
name: worktree-cleanup
description: Audit and clean a repo's git worktrees, assigned branches and associated agent sessions — classify first, then merge in order, remove registered and physical worktrees, delete branches, close finished sessions and verify no residue; optionally reclaim disk. Also resolves conflicts hunk by hunk from primary sources. Use for /worktree-cleanup, worktree or branch cleanup, "what's using my disk", conflicts, and the end of parallel work.
disable-model-invocation: true
---

# /worktree-cleanup — audit, then clean

Parallel work leaves worktrees behind; a worktree left behind shadows files and loses work. Two
phases, and the destructive one only after the report. Deletion is irreversible, so every gate below
guards against removing something in use or holding uncommitted work.

## Phase 1 — audit (read-only; always do this first)

1. **Snapshot.** Record `df -h /`.
2. **Run the audit.** `node <this-skill>/scripts/worktree-audit.mjs <repo>` reads paths from
   `git worktree list`, never hand-typed ones, and prints one row per worktree: size, age, merged,
   uncommitted (`wip:N` tracked, `untracked:N` new files), remote, PR state, the last chat that
   worked in it, a suggested bucket and lock reason. The chat scan is slow; run it in the background.
   It reads Claude Code and Codex history only, so a worktree used from another agent shows no chat.
3. **Treat the bucket as advice.** Get the pinned and active chats from the user or the session list
   and cross-check every candidate. A pinned or active chat wins over a `safe` bucket, including
   sibling worktrees its background workers created. A worktree whose chat cannot be established is
   `unknown`.
4. **Confirm the facts the bucket rests on.** Resolve the actual integration target, never a literal
   `master` fallback, and exact assigned branch names. A branch prefix alone is not permission to
   merge or delete every matching branch. For each candidate, check commits unique to it and whether
   its tip is an ancestor of the target. Zero commits ahead does not prove cleanliness. Squash or
   rebase integration needs verified patch/content equivalence when ancestry cannot show it. List
   uncommitted work with `git -C <path> status --porcelain --untracked-files=all
   --ignore-submodules=none` and show it with `git -C <path> diff HEAD --ignore-submodules=none
   --submodule=log`; an empty diff omits untracked files, and commits made inside a submodule live
   in the worktree and leave with it.
5. **Find leftovers.** Inventory physical folders at the configured worktree root and known previous
   locations, and compare them with git registrations. Establish ownership from git pointers or
   recorded task paths; a folder name alone is not deletion authority.

Classify each worktree/branch into ONE bucket and print the table before touching anything:

| path / branch | session id | HEAD | ahead of target | dirty? | bucket |
|---|---|---|---|---|---|
| … | exact recorded id / not-applicable / unknown | … | N commits | clean / wip:N, untracked:N | **merge** (commits ahead, clean, task known) · **already-merged** (verified incorporation, clean) · **stale-empty** (no unique work, clean including untracked/ignored, inactive) · **dirty** (uncommitted work — needs a human or a commit) · **locked** (another tool holds it; reason shown) · **in-use** (a pinned or active chat) · **unknown** (cannot tell what it is — read the log and diffstat, then decide) |

Resolve session identity only from the launch ledger or an exact recorded id. Never associate or
close a session by title or recency.

If a worktree is dirty, locked, in-use or unknown and the user is not present, STOP the destructive
phase for that item and report it. Never `git stash`/`reset`/`clean` someone else's work to make a
cleanup tidy, and never `rm -rf` around a lock.

## Phase 2 — clean (after the report; in this order)

1. **Merge** only candidates authorized for integration, in dependency/approved merge order. Verify
   the assignment's destination, predecessor commits, exact-candidate review and acceptance gates.
   Creation order is not dependency order. Recheck target drift before each merge, use the project's
   merge strategy and run its required integration gates. A failed gate blocks cleanup of that item.
2. **Remove** only incorporated/stale-empty, inactive worktrees after checking tracked, untracked
   and ignored content plus locks. Use ordinary `git worktree remove` with the exact quoted path.
   A refusal is evidence to investigate; do not add `--force` just to finish. When the only blocker
   is ignored build output, verified with `git -C <path> status --ignored`, report it and ask before
   forcing. Removal is complete only when both the registration and physical folder are absent. For
   an unregistered leftover, verify its task identity and inactivity, preserve any unique or unknown
   files, then remove the exact empty folder with `rmdir`. Keep the shared worktree parent.
3. **Delete** only verified incorporated branches using `git branch -d` and exact names. If ancestry
   differs after a squash, report the verified equivalence and remaining branch rather than silently
   forcing deletion. Preserve other sessions' branches and the current checkout.
4. **Verify repository cleanup**: re-list registrations, physical paths and exact refs; record
   `df -h /` again. Report kept or blocked items and why. Prune stale metadata only after
   establishing its paths are inactive and genuinely gone; an unavailable volume is not proof of a
   stale worktree.
5. **Close finished sessions**: after cleanup and gates succeed, close or archive each exact
   associated agent session only when the host supports it, and verify the result. Never close the
   calling session; unknown associations remain visible residue. Do not claim an unavailable
   operation succeeded.

## Reclaim disk (only on an explicit request)

After worktrees, list the next-largest reclaimable items with sizes and ask before each: stale iOS
simulators (`xcrun simctl --set testing delete all`, `xcrun simctl delete unavailable`, old runtimes
via `xcrun simctl runtime list`), Xcode `DerivedData` and `iOS DeviceSupport`, package caches (pnpm,
uv, brew, yarn) and old agent transcripts. Keep recent transcripts that `handoff` or session pickup
may need, and every cache the user has said to keep.

## Conflict resolution (Phase 2 and standalone `/worktree-cleanup conflicts`)

Also the entry point mid-conflict: a merge or rebase already stopped and you need it finished.

1. **See the current state** of the merge/rebase: `git status`, `git log --oneline --left-right --merge`, `git diff --diff-filter=U`, and each conflicting file. Name the two sides — which unit/branch each is.
2. **Find the primary sources** for each conflict. Understand why each change was made and its original intent: the commit message, the decision it cites, the plan or reference that owns the contract, the worker brief, the handoff. Read the code, not just the hunk. If you cannot name a side's intent, say so in the report rather than guessing.
3. **Resolve each hunk.** Preserve both intents where possible — two units usually added different things to one list, import block, table or registry, and both belong. Where incompatible, resolve from the governing requirement and evidence; if that does not decide
   the conflict, leave the item blocked and surface the exact operator choice. Do **not** invent new behaviour: no third variant, no "compromise" value neither side wrote, no silently dropped branch of a conditional. Do not abort/skip an operation or discard a side merely to hide an unresolved conflict; never use `--skip`, `-X ours/theirs`, or a checkout of one whole side to make the conflict go away.
   Lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `uv.lock`, `Cargo.lock` and the like) are regenerated
   with the package manager after resolving the manifest, never hand-merged. Before staging, confirm no
   conflict markers remain (`git diff --check` and a search for `<<<<<<<`).
4. **Run the repo's gate.** Discover the checks from the project instructions, `package.json`, `Makefile` or `pyproject.toml` — typecheck, then lint, then tests. Fix anything the merge broke; a conflict resolved into a red gate is not resolved.
5. **Finish the operation.** Stage the resolved paths explicitly (never `git add -A`) and commit. If rebasing, `git rebase --continue` until all commits are rebased.
6. **Report per hunk**: `file:line` · side A intent (source) · side B intent (source) · what was kept · why. Flag every hunk where an intent was dropped or unknown — that is the list a human has to check.

## Report

`df -h /` before and after with space reclaimed. One table (same buckets → merge SHA → worktree
registration/path → branch → session disposition or `not-applicable` → residue), the gate result,
and every item held back with its reason (in use by which chat, uncommitted work, lock). Never commit
or push anything beyond the merge commits themselves.

Provenance: the audit script, in-use check and disk-reclaim list are adapted from pstack's worktree
cleanup playbook (MIT, `scripts/LICENSE-pstack`).
