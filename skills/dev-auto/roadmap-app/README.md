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

Use the two `com.relay.roadmap-*.plist` templates in the parent directory.
Set `ROADMAP_SCRIPT` to the absolute path to the deployed `scripts/roadmap.py`,
`ROADMAP_DIRECTORY` to the absolute directory containing the published app,
and `ROADMAP_BIND` to the existing server's bind address. Set both log
placeholders to absolute paths outside the served directory. Use the same
Python, GitHub CLI and Orca environment for both jobs. Replace the two
LaunchAgents and reload them with `launchctl bootout` and `launchctl bootstrap`
in the user GUI domain.

The render job reads the tick's saved issue data and live Orca and tick state
every 30 seconds. **Refresh** fetches GitHub, recalculates readiness using
current remote refs, and reloads the board. The GitHub timestamp records the
last successful manual fetch. The serve job uses the existing bind and port.
