/goal Implement julianthant/hr-relay#{number} ({title}).

Read the current issue with
`curl -fsS "${{ROADMAP_URL:-http://127.0.0.1:8790}}/issues?number={number}" | jq -e 'select(.stale == false) | .data'`
and follow the repository instructions.
Work only on {branch} in this worktree, within the issue's owned paths and assigned coder route.
Exclude the launcher's worktree-local Codex notice setting from commits. Commit by logical change. Push only this task branch. Run the issue's checks and wait for
branch or PR CI with `~/.agents/skills/dev-auto/scripts/ci-wait "$(git rev-parse HEAD)"`. Fix failures you caused.
Report the exact SHA, CI URL, changes, checks and unrun obligations in worker_done.
Ask the coordinator about unresolved choices, permissions and scope changes.
On a usage or context handoff, run /handoff, report its path and stop.
