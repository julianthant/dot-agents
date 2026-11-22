---
name: research
description: "Use this skill when the answer lives outside the codebase: how a library, API, CLI, standard or hosted service actually behaves, what changed between versions, or how tools compare, answered from official docs, specs and source code and saved as a cited note. Use it whenever a ticket depends on an external fact the agent would otherwise guess from memory. Not for how this repo's own code works (use `how`) or why it was built that way (use `why`)."
metadata:
  source: "https://github.com/mattpocock/skills"
  source_path: "skills/engineering/research"
  source_version: "4ae2317ddb22b046e26455e4f9c9b3aaaf62212d"
  license: "MIT"
  exposed: "true"
---

Spin up a **background agent** to do the research, so you keep working while it reads. Start it with a fresh context window.

Its job:

1. Investigate the question against **primary sources** (official docs, source code, specs, first-party APIs), not a secondary write-up of them. Follow every claim back to the source that owns it.
2. Write the findings to a single Markdown file, citing each claim's source.
3. Save it where the repo already keeps such notes; match the existing convention, and if there is none, put it somewhere sensible and say where.
