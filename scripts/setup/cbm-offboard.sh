#!/usr/bin/env bash
# Reverse of scripts/setup/cbm-onboard.sh: remove the 7 hook entries from
# ~/.claude/settings.json AND remove the 4 ~/.claude/hooks/cbm-* symlinks
# that point into this repo.
#
# Does NOT uninstall the codebase-memory-mcp binary (you might still want it
# for personal projects). Use the cbm installer's own uninstall instructions
# for that.
#
# Identifies what we own by command path: only entries whose command equals
# one of the 4 known ~/.claude/hooks/cbm-* paths are removed. Other hook
# entries are left untouched.
#
# Usage:
#   make cbm-offboard
#   bash scripts/setup/cbm-offboard.sh --dry-run

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_HOOKS="$REPO_ROOT/.claude/hooks"
USER_HOOKS="$HOME/.claude/hooks"
USER_SETTINGS="$HOME/.claude/settings.json"

# Single source of truth: must match scripts/setup/cbm-onboard.sh
MANAGED_CMDS=(
  "~/.claude/hooks/cbm-repo-context"
  "~/.claude/hooks/cbm-prompt-reinject"
  "~/.claude/hooks/cbm-cleanup-on-bash-worktree-remove"
  "~/.claude/hooks/cbm-cleanup-on-exit-worktree"
)
HOOK_FILES=(
  cbm-repo-context
  cbm-prompt-reinject
  cbm-cleanup-on-bash-worktree-remove
  cbm-cleanup-on-exit-worktree
)

# ── 1. unlink symlinks that point into THIS repo ────────────────────
echo "==> hook symlinks"
for hook in "${HOOK_FILES[@]}"; do
  dst="$USER_HOOKS/$hook"
  src="$REPO_HOOKS/$hook"
  if [ ! -e "$dst" ] && [ ! -L "$dst" ]; then
    echo "  $hook: not present — skipping"
    continue
  fi
  if [ ! -L "$dst" ]; then
    echo "  $hook: REGULAR FILE (not our symlink) — leaving alone"
    continue
  fi
  target=$(readlink "$dst")
  if [ "$target" = "$src" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "  [dry-run] rm $dst (was → $target)"
    else
      rm "$dst"
      echo "  removed $dst (was → $target)"
    fi
  else
    echo "  $hook: symlink → $target — not ours, leaving alone"
  fi
done

# ── 2. strip our entries from ~/.claude/settings.json ───────────────
echo
echo "==> ~/.claude/settings.json"
if [ ! -f "$USER_SETTINGS" ]; then
  echo "  $USER_SETTINGS missing — nothing to do"
  exit 0
fi

# Build a jq array of the commands we manage, for filtering.
managed_json=$(printf '%s\n' "${MANAGED_CMDS[@]}" | jq -R . | jq -s .)

jq_program='
def strip_owned($owned):
  .hooks //= {} |
  (.hooks | to_entries) as $events |
  .hooks = (
    [
      $events[] |
      .key as $evname |
      .value as $blocks |
      {
        key: $evname,
        value: (
          ($blocks // []) | map(
            .hooks = ((.hooks // []) | map(select(.command as $c | $owned | index($c) | not)))
          )
          | map(select(.hooks | length > 0))
        )
      }
    ] | from_entries
  );

strip_owned($owned)
'

if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] computing diff…"
  patched=$(jq --argjson owned "$managed_json" "$jq_program" "$USER_SETTINGS")
  printf '%s\n' "$patched" | diff -u "$USER_SETTINGS" - | sed 's/^/    /' || true
else
  ts=$(date +%Y%m%d-%H%M%S)
  cp "$USER_SETTINGS" "$USER_SETTINGS.bak.cbm-offboard.$ts"
  echo "  backup: $USER_SETTINGS.bak.cbm-offboard.$ts"
  jq --argjson owned "$managed_json" "$jq_program" "$USER_SETTINGS" > "$USER_SETTINGS.tmp"
  mv "$USER_SETTINGS.tmp" "$USER_SETTINGS"
  echo "  patched (cbm-onboard entries removed; other entries untouched)"
fi

echo
echo "==> done"
echo "  Restart Claude Code for the change to take effect."
echo "  The codebase-memory-mcp binary is left in place. Remove manually if desired:"
echo "    rm -f \"\$(command -v codebase-memory-mcp 2>/dev/null)\""
