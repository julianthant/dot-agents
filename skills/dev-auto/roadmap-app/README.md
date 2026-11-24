# hr-relay roadmap

The Vite and React app polls `/data.json`, which `../scripts/roadmap.py`
generates. For local development, put sample data in `public/data.json` and
run `pnpm dev`. The sample file is ignored and the production build fetches
the live feed.

From this directory, use Node.js 22.18+ and pnpm to install and verify:

```sh
pnpm install --frozen-lockfile
pnpm check
pnpm lint
pnpm build
```

On the Mac that serves the roadmap, run `pnpm run publish` from this directory
after installing dependencies. It builds the app and deploys its HTML and
assets to `~/.orca/bots/subaru-2/roadmap/`.

Use the `com.relay.roadmap-serve.plist` template in the parent directory.
Set `ROADMAP_SCRIPT` to the absolute path to the deployed `scripts/roadmap.py`,
`ROADMAP_DIRECTORY` to the absolute directory containing the published app,
and `ROADMAP_TAILNET_IP` to the Mac's Tailscale IPv4 address. Set the log
placeholder to an absolute path outside the served directory. The server also
listens on loopback. It uses the Mac's authenticated `gh` CLI.

Boot out the old serve and render LaunchAgents. Bootstrap the updated serve
job in the user GUI domain and confirm `/status` shows fresh source ages
before enabling central reads in the tick and workers.

The board polls `/data.json` every five seconds. The API also serves
`GET /status`, `/issues`, `/project`, `/prs`, `/heads`,
`/runs?repo=hr-relay&sha=...` (or `branch`, or `id`), and
`/jobs?repo=hr-relay&id=...&attempt=...`. The GitHub endpoints report
`ageSeconds` and `stale`. A lookup may return `data: null` while pending.
The server sends conditional GitHub REST GETs where an ETag is available.
GitHub GraphQL queries use POST and branch-head reads use Git.

On Mac, run `~/.agents/skills/dev-auto/scripts/ci-wait <sha-or-branch>` from
the task checkout. Set `ROADMAP_URL=http://<mac-tailnet-ip>:8790` in the WSL
worker environment and run the same command there. Use the full SHA after a
push. HR Relay tasks wait for the push workflow and dot-agents tasks wait for
the PR workflow.

After the shared API is live, merge the PreToolUse entry from
`../../../hooks/ci-wait.json` into the Claude user settings and the Codex
account's `hooks.json`, preserving other entries. Reload the agent sessions
and trust the hook in Codex.
