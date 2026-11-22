# Writing for agents

> Internal reference for `skills`, skill-producing workflows, and edits to agent instruction
> documents. Adapted from Matt Pocock’s MIT-licensed `writing-for-agents` material.

## Context pointers

A pointer must state what it reaches and the distinct conditions that require it. Front-load the
capability, use one trigger per real branch, and remove identity already carried by the target.
Weak pointers make good references unreachable.

## Information hierarchy

1. Keep the ordered actions every run needs in the entrypoint.
2. Co-locate definitions, rules, and caveats consulted together.
3. Move branch-specific detail behind an exact pointer to a reference.

Each step needs a checkable, demanding completion criterion. Sharpen the criterion before adding
more steps. Split by sequence only when visible later steps cause premature completion; split by
invocation only when the extracted behavior needs independent discovery.

## Exposure and invocation

- A skill belongs in `~/.agents/skills/`, the one copy every agent reads. A reference it needs
  belongs in its own `references/` and is reached by relative path. Do not hide helpers inside
  `skills/`.
- Explicit-only is a separate axis, expressed for every agent at once: `disable-model-invocation:
  true` in the frontmatter and `policy.allow_implicit_invocation: false` in `agents/openai.yaml`.
  It keeps human invocation.
- Skill text names no models, effort levels, native role names or host tool names; see
  [platform adapters](platform-adapters.md).

## Editing discipline

Keep one source of truth for each meaning. Treat discoverable environment facts as lookups, not
documentation to cache. Remove stale branches, repeated meaning, generic advice, and sentences that
do not change agent behavior. Preserve hard authorization, safety, completion, and evidence rules.
