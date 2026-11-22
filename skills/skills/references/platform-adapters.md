# Platform adapters

Read before writing skill text or metadata. One copy serves every agent, so the text must not
assume one of them.

## Portable text

- Name no models, effort levels, context sizes or native agent-role names. Say what kind of help is
  needed ("an independent reviewer", "a read-only researcher"); each agent's own instructions map
  that to its roles.
- Describe actions, not host tool names: "fetch the URL", "ask the user", "view the image".
- Refer to skills by name ("invoke `grill`"), not by one agent's `/x` or `$x` syntax.
- Make host-only capabilities conditional: "when the host offers a design canvas…".
- Link a skill's own files by relative path (`references/x.md`, `scripts/x.py`). Name another skill
  instead of reading its files; a `../` path out of the skill breaks when an agent reaches it through
  a link. Run a script another skill ships by its absolute `~/.agents/skills/<skill>/scripts/...` path.

## Metadata

Each agent reads its own metadata and ignores the rest:

- Frontmatter `name` and `description` for all. Claude also reads `disable-model-invocation`,
  `argument-hint` and `allowed-tools`; Codex 0.161 loads skills carrying those keys (verified
  2026-10-08).
- `agents/openai.yaml` for Codex display text and `policy.allow_implicit_invocation`.
- Explicit-only skills set both `disable-model-invocation: true` and
  `allow_implicit_invocation: false`.
- A helper a skill needs lives in that skill's `references/`. Do not hide helpers inside `skills/`
  with `user-invocable: false`; other agents would show them.
- An imported skill records its upstream under `metadata`: `source` (repository URL), `source_path`,
  `source_version` (commit) and `license`, plus `modified` for Apache-2.0 sources. Its licence file
  sits at the skill root.
- `metadata.exposed: "true"` marks a technical skill for `expose`. Its description starts "Use this
  skill when…", says what the user or ticket asks for, names the sibling that covers near misses, and
  stays within 1,024 characters. Keep each `metadata` value a one-line quoted string.

## Instructions

`~/.agents/AGENTS.md` holds the short shared rules. Claude's `~/.claude/CLAUDE.md` imports it and adds
Claude-only lines. Codex cannot import, so `~/.codex/AGENTS.md` is generated from the shared file plus
`~/.agents/platforms/codex.md`, keeping the codex_workflow managed block. Model routing belongs in
those agent-specific parts only.
