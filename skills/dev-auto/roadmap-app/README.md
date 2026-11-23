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
assets to `~/.orca/bots/subaru-2/roadmap/`, where the existing job writes
`data.json`.
