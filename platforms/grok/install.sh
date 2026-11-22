#!/bin/sh
# Gives Grok the shared rules and one Concord server; idempotent.
# Grok does not follow ~/.claude/CLAUDE.md's @-import, so the shared file is linked as a home rule.
set -e
mkdir -p "$HOME/.grok/rules"
ln -sfn "$HOME/.agents/AGENTS.md" "$HOME/.grok/rules/agents.md"
# No identity here: each session's CONCORD_* env, set by dev-auto's launch line, reaches the server.
grok mcp list | grep -q '^ *concord:' || grok mcp add concord -- "$(command -v concord-mcp)"
