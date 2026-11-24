# hr-relay roadmap

The Vite and React app polls `/data.json`, which `../scripts/roadmap.py`
generates. For local development, put sample data in `public/data.json` and
run `pnpm dev`. The sample file is ignored and the production build fetches
the live feed.

The `roadmap-app` CI job installs from the frozen lockfile, runs lint and the
model check, and uploads the compiled site as the `roadmap-app` artifact.

## API

The board polls `/data.json` every five seconds. The API also serves
`GET /status`, `/issues` (or `/issues?number=...`), `/project`, `/prs`, `/heads`,
`/runs?repo=hr-relay&sha=...` (or `branch`, or `id`), and
`/jobs?repo=hr-relay&id=...&attempt=...`. The GitHub endpoints report
`ageSeconds` and `stale`. A lookup may return `data: null` while pending.
The server sends conditional GitHub REST GETs where an ETag is available.
GitHub GraphQL queries use POST and branch-head reads use Git. Use
`127.0.0.1:8790` or `localhost:8790` for the loopback listener and the configured
Tailscale IPv4 address and port for the tailnet listener.

`/status` reports each source's age, REST requests and quota usage, and a
separate `graphql` budget in primary points. The board shows stale GitHub data
alongside the latest local Orca and tick state. The release pipeline has its
own freshness indicator.

## Mac cutover

Use the reviewed full SHA and its successful Actions run ID. Set
`ROADMAP_RELEASE_SHA` and `ROADMAP_CI_RUN`, then stage the release from a plain
shell. Keep the existing checkout available for rollback.

```sh
release="$HOME/.local/share/dev-auto/roadmap-$ROADMAP_RELEASE_SHA"
agents="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"
mkdir -p "$(dirname "$release")"
git -C "$HOME/.agents" fetch origin "$ROADMAP_RELEASE_SHA"
git -C "$HOME/.agents" worktree add --detach "$release" "$ROADMAP_RELEASE_SHA"
gh run view "$ROADMAP_CI_RUN" -R julianthant/dot-agents --json headSha,conclusion \
  | jq -e --arg sha "$ROADMAP_RELEASE_SHA" '.headSha == $sha and .conclusion == "success"'
gh run download "$ROADMAP_CI_RUN" -R julianthant/dot-agents -n roadmap-app -D "$release/board"

backup=$(mktemp -d "$HOME/.local/share/dev-auto/roadmap-backup.XXXXXX")
for label in com.relay.roadmap-serve com.relay.roadmap-render com.relay.dev-auto; do
  cp -p "$agents/$label.plist" "$backup/$label.plist"
done
printf 'Keep this backup for rollback: %s\n' "$backup"

serve="$backup/new-roadmap-serve.plist"
cp "$release/skills/dev-auto/com.relay.roadmap-serve.plist" "$serve"
plutil -replace ProgramArguments.2 -string "$release/skills/dev-auto/scripts/roadmap.py" "$serve"
plutil -replace ProgramArguments.5 -string "$release/board" "$serve"
plutil -replace ProgramArguments.7 -string "$(tailscale ip -4)" "$serve"
plutil -replace StandardErrorPath -string "$backup/roadmap.log" "$serve"

tick="$backup/new-dev-auto.plist"
cp "$backup/com.relay.dev-auto.plist" "$tick"
plutil -replace ProgramArguments.1 -string "$release/skills/dev-auto/scripts/tick.py" "$tick"
```

Pause manual ticks and worker starts. Unload the three old jobs and take the
render plist out of the LaunchAgents directory so it stays unloaded after
login. Keep the existing Run, anchor, account settings and state directory.

```sh
launchctl bootout "$domain/com.relay.dev-auto"
launchctl bootout "$domain/com.relay.roadmap-render"
launchctl bootout "$domain/com.relay.roadmap-serve"
mv "$agents/com.relay.roadmap-render.plist" "$backup/unloaded-roadmap-render.plist"
cp "$serve" "$agents/com.relay.roadmap-serve.plist"
launchctl bootstrap "$domain" "$agents/com.relay.roadmap-serve.plist"
```

Wait for both loopback and the tailnet address to serve the API. Use the same
Mac session and authenticated `gh` CLI as the previous service.

```sh
curl -fsS http://127.0.0.1:8790/status | jq -e 'all(.sources[]; .stale == false)'
curl -fsS "http://$(tailscale ip -4):8790/data.json" | jq -e '.githubFetchedAt != null'
```

After the worker checks below pass, install the updated tick plist and resume
manual operation. Check the next tick and the board before resuming worker starts.

```sh
cp "$tick" "$agents/com.relay.dev-auto.plist"
launchctl bootstrap "$domain" "$agents/com.relay.dev-auto.plist"
```

## Worker install and proof

On WSL, set the same `ROADMAP_RELEASE_SHA` and stage the code.

```sh
release="$HOME/.local/share/dev-auto/roadmap-$ROADMAP_RELEASE_SHA"
mkdir -p "$(dirname "$release")"
git -C "$HOME/.agents" fetch origin "$ROADMAP_RELEASE_SHA"
git -C "$HOME/.agents" worktree add --detach "$release" "$ROADMAP_RELEASE_SHA"
```

On both hosts, wait for the release to land on `main` and update the tracked
installation with a fast-forward pull. Move aside any untracked files left by
a previous installation attempt before running the pull. This keeps future
pulls from colliding with copied files.

```sh
checkout="$HOME/.agents"
backup_files=$(mktemp -d)
for path in skills/dev-auto/scripts/ci-wait skills/dev-auto/scripts/github_poller.py hooks/ci-wait.py; do
  if { test -e "$checkout/$path" || test -L "$checkout/$path"; } &&
    ! git -C "$checkout" ls-files --error-unmatch -- "$path" >/dev/null 2>&1; then
    mkdir -p "$backup_files/$(dirname "$path")"
    mv "$checkout/$path" "$backup_files/$path"
  fi
done
printf 'Previous untracked client files, if any, are in %s\n' "$backup_files"
git -C "$checkout" pull --ff-only origin main
for path in skills/dev-auto/scripts/ci-wait skills/dev-auto/scripts/github_poller.py hooks/ci-wait.py; do
  cmp "$release/$path" "$checkout/$path" || break
done
```

Confirm all three comparisons succeed before enabling the hooks.
`ci-wait` uses the standard library and needs Python 3.10 or newer. Check
`python3 --version` in the WSL worker shell. The executable uses the worker's
`python3` from `PATH`. If the worker uses a different default interpreter,
invoke the script explicitly with `python3.10` or a newer Python. The Mac
server and tick have their own Python requirements.

On WSL, set `ROADMAP_URL=http://<mac-tailnet-ip>:8790` in the Orca worker
environment and open a worker shell that inherits it. Check `$ROADMAP_URL/status`
and run the client against the deployed dot-agents SHA. Record the output and
exit status from WSL. Mac workers use loopback by default.

```sh
curl -fsS "$ROADMAP_URL/status" | jq -e 'all(.sources[]; .stale == false)'
"$HOME/.agents/skills/dev-auto/scripts/ci-wait" --repo dot-agents "$ROADMAP_RELEASE_SHA"
```

Use full SHAs after pushes. HR Relay waits for the push workflow and dot-agents
waits for the PR workflow.

Once the service is reachable, add the PreToolUse entry from
[`hooks/ci-wait.json`](../../../hooks/ci-wait.json) to the `hooks.PreToolUse` array
in each Claude user `settings.json` and each active Codex account's `hooks.json`.
Keep existing entries. Reload the sessions and trust the new hook in Codex.
In each active Codex account, ask Codex to make a shell tool call for
`gh run watch --help`. It should be denied before execution and show the
`ci-wait` instruction. The help command makes no GitHub request if the hook
is inactive. Check that `gh run view --help` still executes. This exercises the
live `PreToolUse` event, including Codex's `Bash` matcher and
`tool_input.command` field. [Codex hook payload](https://developers.openai.com/codex/hooks#pretooluse)

For a separate check of the handler, pipe this
representative payload into the installed script and check for
`hookSpecificOutput.permissionDecision == "deny"`.

```sh
printf '%s\n' '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"gh run watch --help"}}' |
  python3 "$HOME/.agents/hooks/ci-wait.py" |
  jq -e '.hookSpecificOutput.permissionDecision == "deny"'
```

## Rollback

In the same Mac shell, use the saved `backup`, `agents` and `domain` values.
Pause worker starts and unload any new tick or serve job. Restore the saved
plists before bootstrapping the original serve, render and tick jobs in order.
The saved plists point at the original scripts and board files.

```sh
for label in com.relay.dev-auto com.relay.roadmap-serve; do
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
  fi
done
for label in com.relay.roadmap-serve com.relay.roadmap-render com.relay.dev-auto; do
  cp -p "$backup/$label.plist" "$agents/$label.plist"
  launchctl bootstrap "$domain" "$agents/$label.plist"
done
```

On both clients, remove the added PreToolUse entry and reload the sessions.
Remove the WSL `ROADMAP_URL` setting. Confirm the old board loads and its
Refresh works before resuming worker starts with the prior workflow.
