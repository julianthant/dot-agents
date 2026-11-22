## Validation

Completion requires evidence for every item:

- every `SKILL.md` parses and its `name` equals its folder;
- every file in a skill's `references/` is reachable from that skill or is reported as orphaned;
- every `~/.agents/...` path and relative resource resolves, and no relative link leaves its skill;
- `expose` lists every skill marked `metadata.exposed: "true"`, each description within 1,024 characters;
- `scripts/link_skills.py` reports `in sync`, and `scripts/build_instructions.py --check` passes;
- each agent in [links](links.md) sees the shared operator set: Codex via `codex debug prompt-input`,
  OpenCode via `opencode debug skill`, Claude through the links, with no `shadowed` entries;
- `audit.py` reports no `platform_terms` (model names, effort levels, native role names or host tool names) outside the
  documented launcher exceptions (see [platform adapters](platform-adapters.md));
- explicit-only skills carry both invocation flags;
- new or changed scripts and their tests execute successfully;
- changes in `~/.agents` are committed by logical category with explicit paths; nothing is pushed.

Return a compact table with scope, added, updated, retired, unchanged, blocked, validation, backup path
and commit.

Syntax, path and link checks are structural validation, not a demonstrated behavioral improvement.
Label manual scenario walkthroughs separately from independent model runs. For substantial instruction
changes use [evaluation](evaluation.md); do not report prepared fixtures as passing runs.
