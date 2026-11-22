## Create or improve

Create or update only the named skill or explicitly authorized set of existing skills, in
`~/.agents/skills/<name>/`, with every file it needs inside that folder. Read
[writing for agents](writing-for-agents.md) and [platform adapters](platform-adapters.md), add
per-agent metadata only where needed, then follow [links](links.md) so every agent sees the change.

Do not broaden a focused skill into a catchall merely to reduce the menu. Use a router only when its
branches share one operator intent and each branch has a precise selection condition.

When authoring: keep only prose that changes a decision; say what to do and give a reason only when
the rule is confusing without one; point at structural sources (types, READMEs, config) instead of
restating them; delegate to other skills by name. A workflow you keep hitting that no skill captures is
a proposal for a new skill. Validate frontmatter (`name`, `description`), referenced files and links,
and add test scenarios when the change is structural.
