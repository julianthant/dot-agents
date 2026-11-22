---
name: expose
description: >-
  Use this skill when planning work for other agents, writing tickets or briefs, or deciding which
  skills a worker should load: it prints every exposed technical skill with its full description and
  path, so none is missed. Run it before naming skills on a ticket; not for loading or running those
  skills yourself.
---

# expose

1. From the project root, run `python3 <this skill's folder>/scripts/expose.py`. It prints the name, full description and `SKILL.md` path of every skill whose `metadata.exposed` is `"true"`, from `~/.agents/skills` and the project's `.agents/skills` by default; `--root <dir>` (repeatable) reads other locations instead.
2. Read all of the output. Do not skim, truncate or summarize it.
3. Pick skills from that list only. Name each by its exact name; a skill missing from the output is not available to workers.
4. If the script exits non-zero, report the files it named on stderr and use the rest of the list.
