---
name: docs
description: Write and simplify technical documentation and agent instructions — tutorials, how-tos, reference, explanations, READMEs, RFCs, PR and commit text — reconcile duplicate or stale guidance, and keep AGENTS.md and CLAUDE.md identical. Use for /docs, writing or rewriting technical docs, documentation refactoring, or requests to reduce agent context. Not for Word/PDF artifacts or changing product requirements.
disable-model-invocation: true
---

# Docs

Make the next agent able to find the right rule quickly. Keep one authoritative statement of each
rule, concise instruction entry points, and identical `AGENTS.md` / `CLAUDE.md` twins. Optimize for
correct decisions and retrieval, not a word-count target.

`/docs audit [path]` is read-only. `/docs [path]` refactors the named repository or subtree; without
a path, use the current repository. An ordinary request to inspect or explain docs stays read-only.
Follow the repository's gates and commit policy. Do not dispatch agents, alter product behavior,
change permissions, or reorganize unrelated personal settings as part of documentation work.

For writing or rewriting a document's text, follow [technical writing](references/technical-writing.md): pick
the Diátaxis mode first, then its sentence rules.

## Find the useful change

Run `python3 <this-skill>/scripts/audit.py <repo>` for the file inventory, instruction-pair drift,
and project instruction sizes. Read the actual files and their callers; size alone is not a defect.
The script's 32,768-byte default is one agent's project-instruction load limit, not a universal one; supply
`--project-doc-max-bytes N` when configuration establishes a different limit. It does not model
global instructions, fallback filenames, or the app's current prompt.

Find the repository's declared plan, contracts, generated docs, and historical records before
editing. Do not assume another project's hierarchy applies. Prioritize conflicting instructions,
missing read triggers, duplicated authority, stale state, and oversized automatically loaded files.
Check both instruction names before choosing wording; resolve disagreements from user intent,
current owners, and code. Never choose by modification time or silently pick one side.

## Refactor the information

For each substantive block, decide:

- **Keep:** permissions, non-obvious invariants, commands needed for the task, mandatory validation,
  and a compact map of where the work belongs.
- **Point to its owner:** content already stated accurately in a contract, source declaration,
  schema, registry, or generated catalog. Remove the duplicate and name when to read the owner.
- **Move:** useful detail with no existing home. Put it in one focused reference or lesson ledger,
  with a short contents list when useful. A pointer must name the exact file and the work that
  requires it. Search/read matching sections rather than loading a whole historical ledger.
- **Remove:** proven repetition, stale status obtainable from live authority, generic advice that
  does not change behavior, or superseded guidance with a verified replacement. Preserve unique
  decisions, evidence, provenance, and exceptions; record the survivor for consequential removals.

Do not merely rename a giant instruction file and require every task to read the whole thing.
Do not copy a complete source inventory that the agent can inspect directly. Preserve normative
language and its exceptions when shortening; an approved design is not proof of implementation,
and an old successful live test is not current availability. Historical evidence keeps its date.
Generated documents are regenerated through their owner, never hand-edited. Do not rewrite frozen
history to look current. Escalate only unresolved consequential judgments; continue independent work.

Write each codebase instruction pair from one shared text buffer and verify byte equality. If only
one name exists, retain it unless the user/repository authorizes creating the missing twin. Put any
necessary platform distinction in a small labeled clause in the SAME text, with shared references;
do not create separate per-agent versions of the domain guidance. When recording later
lessons, follow the module's ledger pointer instead of growing its instruction file again.

## Retain only useful lessons

When lesson capture is requested, preserve reasoning only when it is non-obvious, durable and
material to avoiding recurrence or rediscovery. Prefer a test/type for enforceable behavior, a
local comment for local rationale, and the existing linked ledger for cross-cutting reasoning.
Update an overlapping lesson rather than creating a duplicate. A nontrivial fix alone does not
justify a new document, and this criterion does not authorize unrequested memory collection.

For moved instructions, test retrieval with a realistic task: the entrypoint should lead to the
needed rule without loading unrelated procedures. A valid path alone does not prove a good trigger.

Provenance: original local adaptations of
[writing-for-agents](https://github.com/mattpocock/skills/blob/HEAD/skills/productivity/writing-for-agents/SKILL.md)
and [Compound Engineering capture](https://github.com/EveryInc/compound-engineering-plugin/blob/HEAD/docs/guides/ce-compound.md),
reviewed 2026-09-05. Existing document owners and persistence authorization remain unchanged.

## Prove the result

Run `python3 <this-skill>/scripts/audit.py <repo> --check`. This checks existing pairs and project
load sizes; it does not verify semantics or links. Verify moved bodies by comparison/hash, and
check shortened rules against their authoritative survivors. Resolve links, anchors, read triggers,
and generated-doc callers touched by the change. Recheck that mandatory gates and safety boundaries
remain reachable in full. Respect current overrides and report single-name instruction directories.

Run the repository's required gates. No new runtime tests are needed merely to move prose. Do not
weaken checks or rewrite unrelated code to make a documentation refactor green. Preserve unrelated
working-tree changes and existing commits; never push without authorization.

Report a compact before/after table, the important moves/removals and their owners, validation,
commits, and remaining decisions. Use the repository's existing evidence location if one is needed;
do not create another plan, permanent cleanup tracker, or report for a trivial wording edit.
