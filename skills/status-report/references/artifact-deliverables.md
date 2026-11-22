# Artifact deliverables — Codex adapter

This is an internal dependency of `status-report`. It is not an operator command and must not be
advertised as one.

## Authority order

1. Finish and verify the status Markdown first.
2. Create the Operating Review presentation only when the operator explicitly requests that artifact
   type or names the template.
3. Treat the presentation as a derivative view. It never becomes the repository's source of truth and
   never silently changes the report's conclusions.
4. Read only the template's `SKILL.md`, completely, and then follow its retained template assets and
   the relevant Presentations skill. Do not preload other templates.

## Exact retained template

The retained OpenAI Default Templates root is:

`~/.codex/plugins/cache/openai-curated-remote/openai-templates/0.1.1/skills`

The Operating Review presentation uses `artifact-template-operating-review`. Append `/SKILL.md` to
that directory and read that exact file before creating the artifact. If the root or the file is
missing, stop and run `/skills templates` to refresh this adapter. Never guess a newer cache version,
select a template by recency, edit the plugin cache, or silently substitute a different template.

## Provenance and maintenance

This reference points into the installed `openai-templates` plugin; it does not copy its proprietary
instructions or assets. A plugin version change makes this root stale by design: `/skills audit` must
detect that state, and `/skills templates` must update it after inspecting the new package.
