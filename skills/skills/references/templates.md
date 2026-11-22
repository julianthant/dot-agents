## Templates

Distinguish three cases:

1. Official artifact capabilities such as Documents, Presentations, Spreadsheets, PDF, and Excel
   Live Control are tool-bearing workflows. Keep them unless the user accepts losing the capability.
2. A default-template plugin may contribute many gallery templates without providing the underlying
   artifact tools. Audit the templates for actual usefulness before disabling the bundle or exact
   entries.
3. A user-created artifact template is intentionally one skill per retained reference. Combining
   several into one router loses independent Template Gallery entries.

The maintained OpenAI Default Templates route is `status-report` → Operating Review. Its authority
and capability boundary lives at
`~/.agents/skills/status-report/references/artifact-deliverables.md`. The owning skill's
repository-native Markdown is authoritative; the artifact is opt-in and derivative. Other default
templates remain installed but may be hidden as standalone entries.

For creation or an exact update of a personal artifact template, locate and read the active
`template-creator` plugin skill. Do not hand-author or merge generated template packages.
