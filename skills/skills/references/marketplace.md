## Marketplace and plugin skills

Marketplace skills are dependencies, not entries in the shared user-owned inventory. Never edit a plugin cache to control visibility; updates will overwrite the change.

When the user wants marketplace skills absent from `/` but retained as implementation knowledge:

1. Inventory which operator skills actually depend on each marketplace skill, including scripts, assets, tools, and license or attribution files.
2. Copy only the reusable instructions those skills need into each dependent skill's own `references/`, recording the upstream in its `metadata` and keeping licence and attribution files. Do not copy a connector or executable capability and pretend it is documentation.
3. Update and validate every dependent skill against the copied reference.
4. Report capabilities that would disappear if the plugin were disabled.
5. Disable or uninstall the plugin only when the user explicitly authorizes that state change.

Codex does not expose a setting that hides a skill from the picker while preserving automatic model
invocation. An exact disabled path removes the skill from both; a router may still load that exact
retained file intentionally. Keep the plugin enabled when its other assets or capabilities remain
useful, and audit path controls after plugin updates because cache-version paths are deliberately
exact.Never patch marketplace cache files to change
visibility.
