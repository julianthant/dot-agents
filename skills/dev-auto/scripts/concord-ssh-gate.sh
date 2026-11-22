#!/bin/sh
# SSH forced command for the ledger key: runs only concord-mcp, against the ledger this
# script is installed in (ledger-init copies it to <ledger>/concord-ssh-gate.sh).
# Client: ssh ... "concord-mcp <kind>:<host>-<name>"; the id becomes CONCORD_AGENT_ID.
# The forced command passes the key's [hosts] prefix from accounts.toml.
set -eu
reject() { echo "concord-ssh-gate: $1" >&2; exit 64; }

root=$(cd "$(dirname "$0")" && pwd -P)
prefix=${1:-}
[ "$#" -eq 1 ] && [ "${#prefix}" -le 47 ] || reject "expected one host prefix"
case "$prefix" in
  '' | [!a-z0-9]* | *[!a-z0-9._-]*) reject "invalid host prefix" ;;
esac
cmd="${SSH_ORIGINAL_COMMAND:-}"
case "$cmd" in
  "concord-mcp "*) id="${cmd#concord-mcp }" ;;
  *) reject "usage: concord-mcp <claude|codex|cursor|script>:$prefix-<name>" ;;
esac
[ "${#id}" -le 64 ] || reject "agent id too long"
case "$id" in
  *[!a-z0-9:._-]*) reject "agent id has characters outside [a-z0-9:._-]" ;;
esac
kind="${id%%:*}"
name="${id#*:}"
case "$kind" in
  claude | codex | cursor | script) ;;
  *) reject "agent kind must be claude, codex, cursor or script" ;;
esac
case "$name" in
  *:*) reject "agent id has more than one colon" ;;
  "$prefix"-?*) ;;
  *) reject "agent id must be <kind>:$prefix-<name>" ;;
esac

exec env -i HOME="$HOME" PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  CONCORD_TELEMETRY_DISABLED=1 CONCORD_NO_UPDATE_CHECK=1 \
  CONCORD_REPO_ROOT="$root" CONCORD_AGENT_KIND="$kind" CONCORD_AGENT_ID="$id" \
  concord-mcp
