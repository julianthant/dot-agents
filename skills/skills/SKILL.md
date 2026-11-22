---
name: skills
description: Audit, improve, evaluate, link or manage personal skills and skill-backed plugins kept once in ~/.agents for every agent. Use for /skills audit, eval, links, create, install, plugin, templates, visibility, /reflect, "learn from my chats" or "capture my working style". Inspection alone does not authorize changes.
---

# Skills

Manage the user-owned skill surface while keeping platform-owned tools and plugins native.
Choose the requested mode, inspect the affected owners, apply only authorized changes and validate.
For broad improvement, compare each existing skill's full references/scripts/evals against upstream
methods before adding material. Follow [evaluation](references/evaluation.md) for gap classification,
provenance, baseline preservation and honest evidence of improvement.

## Select a mode

| Request | Read only the relevant procedure |
| --- | --- |
| Inventory, compare, explain what each agent sees | Audit in [links](references/links.md); read-only |
| Add, rename or retire skills; refresh agent links | [links](references/links.md) |
| Write or review skill text and metadata | [platform adapters](references/platform-adapters.md) |
| Create or improve a skill/set | [create](references/create.md); substantial changes also use evaluation above |
| `/skills eval` | [evaluation](references/evaluation.md); distinguish fixture preparation from actual runs |
| Reflect, learn from chats, capture my working style | [learn](references/learn.md) |
| Install an explicitly named source | [install](references/install.md) |
| Create/update a plugin | [plugin](references/plugin.md) |
| Template selection or management | [templates](references/templates.md) |
| Hide/remove entries | [visibility](references/visibility.md) and [marketplace](references/marketplace.md) for plugin dependencies |

Run `python3 <this-skill>/scripts/audit.py --repo <selected-repo>` for hashes, invocation policy and
per-agent visibility. Hashes show byte drift, not semantic quality or runtime context usage.

## Ownership and exposure

Every skill lives once in `~/.agents/skills/<name>/`, and every agent reads that copy (see
[links](references/links.md)). A skill carries what it needs in its own folder (`references/`,
`scripts/`, `assets/`, licence files) and names other skills instead of reading their files. A skill
taken from upstream records its origin in frontmatter `metadata` (`source`, `source_path`,
`source_version`, `license`); compare against that revision before changing it. Preserve precise
triggers and small entrypoints, with conditional procedures behind exact reachable references. Do not
combine distinct operator intents merely to lower the menu count. Do not edit system/plugin caches or
confuse copied instructions with executable capabilities.

Technical skills a worker may need carry `metadata.exposed: "true"`. The `expose` skill prints their
name, description and path for the planner, and each ticket's `Skills:` line names the skills its
worker loads, so the description decides whether a skill gets picked. Operator-only skills are
typed-only (see [platform adapters](references/platform-adapters.md)). Before exposing, retiring or
merging a skill, count its use with `python3 <this-skill>/scripts/usage.py --days 30`.

For authorized mutations, state scope, back up affected originals, commit `~/.agents` first and check
for concurrent edits before replacing files. Resolve conflicts from the user's policy, never by
modification time. Plugin installation or enabled-state changes require their own explicit
authorization.

## Finish

Read [validation](references/validation.md). Report added/updated/retired and unchanged skills,
meaningful method improvements and sources, checks run, unrun behavioral evaluations, remaining
limitations and backup path. Never describe reduced file size as measured token savings.
