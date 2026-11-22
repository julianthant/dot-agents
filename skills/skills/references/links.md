## One source, many agents

Every user-owned skill lives once in `~/.agents/skills/<name>/`, with its references, scripts and
assets inside that folder. Edit only there. Each agent reaches that copy as follows (UI paths
rechecked 2026-11-02):

| Agent | How it finds the shared skills | Upkeep |
|---|---|---|
| Codex | reads `~/.agents/skills/` natively | none |
| Claude Code | one link per skill in `~/.claude/skills/` | run `scripts/link_skills.py --apply` after adding, renaming or removing a skill |
| Cursor | reads `~/.agents/skills/` natively; reads `~/.claude/skills/` only with its Claude-compatibility option | keep that option off to avoid duplicates |
| OpenCode | reads `~/.agents/skills/` natively; `~/.config/opencode/skills/ui` also links to the shared UI entry | `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` in `~/.zshenv` stops it also reading `~/.claude/skills/` and claude.ai's `synced/` copies |
| Gemini / Antigravity | `~/.gemini/skills/ui` links to the shared entry; `~/.gemini/config/skills` and `~/.gemini/antigravity-cli/skills` link to `~/.agents/skills` | link, never copy |
| Orca account homes | each `~/Library/Application Support/orca/codex-accounts/*/home/skills/ui` links to `~/.agents/skills/ui` | link new account homes when created |

After link or text changes, reload skills or start a fresh session to refresh an already-loaded
catalog.

Repository skills follow the same rule: `<repo>/.agents/skills/` is the source, and one committed
folder link `<repo>/.claude/skills -> ../.agents/skills` gives Claude every repository skill,
including new ones. Do not add per-skill links beside it.

Agent-only entries stay outside the shared root: claude.ai's `~/.claude/skills/synced/`, Cursor's
`~/.cursor/skills/`, Codex's `~/.codex/skills/`. Report them; do not copy them in unasked.

## Audit

Run `python3 scripts/audit.py --repo "$PWD"` for the shared inventory with hashes, invocation policy,
and what each agent actually sees. Classify:

- `ok` — reachable by every agent above;
- `missing-link` / `stale-link` — `link_skills.py` would add or repoint it;
- `shadowed` — a real folder in an agent's own skill folder hides the shared skill;
- `agent-only` — an entry outside the shared root;
- `invalid` — missing `SKILL.md`, malformed frontmatter, `name` differs from the folder, or a broken
  `~/.agents/...` reference.

Plugin and system caches are platform-owned; audit them only when a skill references them.

## Change the inventory

1. Back up affected paths into `~/.agents/backups/<timestamp>/` and commit the current state of
   `~/.agents` (a local git repository, no remote) before replacing or retiring anything.
2. Create, edit, rename or retire skills in `~/.agents/skills/` only. Update every router and
   reference that names a changed skill.
3. Run `python3 scripts/link_skills.py` (dry run), then `--apply`. A real folder that shadows a
   shared skill needs `--replace --backup <dir>`.
4. When `~/.agents/AGENTS.md` or `~/.agents/platforms/codex.md` changed, run
   `python3 scripts/build_instructions.py`; Claude imports the shared file directly.
5. Validate, then commit by logical category.
