#!/usr/bin/env bash
# Onboard codebase-memory-mcp + the four cbm-* hooks into the current
# engineer's ~/.claude/. Idempotent — safe to re-run after every
# `git pull`.
#
# What this script changes on disk:
#   1. ~/.local/bin/codebase-memory-mcp        — installed via the official
#                                                installer if missing
#                                                (skippable: --no-binary).
#                                                The installer also registers
#                                                the MCP server with your
#                                                coding agents.
#   2. ~/.claude/hooks/cbm-repo-context        — symlink → repo file
#      ~/.claude/hooks/cbm-prompt-reinject     — symlink → repo file
#      ~/.claude/hooks/cbm-cleanup-on-bash-worktree-remove — symlink → repo file
#      ~/.claude/hooks/cbm-cleanup-on-exit-worktree        — symlink → repo file
#   3. ~/.claude/settings.json                 — 7 hook entries (event+matcher+
#                                                command) added in-place. Existing
#                                                entries untouched. Backup at
#                                                ~/.claude/settings.json.bak.cbm-onboard.<ts>.
#   4. cbm index of this repo                  — built via `cli index_repository`
#                                                so the first agent session doesn't
#                                                start against an empty graph
#                                                (skippable: --no-index).
#
# Symlinks (not copies) mean every `git pull` of this repo updates the hook
# behavior automatically. No re-run required.
#
# Usage:
#   make cbm-onboard                                 # interactive defaults
#   bash scripts/setup/cbm-onboard.sh --dry-run      # show diff, don't write
#   bash scripts/setup/cbm-onboard.sh --no-binary    # skip the binary install
#   bash scripts/setup/cbm-onboard.sh --no-index     # skip indexing this repo
#   bash scripts/setup/cbm-onboard.sh --yes          # don't prompt for the binary install

set -euo pipefail

DRY_RUN=0
INSTALL_BINARY=1
INDEX_REPO=1
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-binary) INSTALL_BINARY=0 ;;
    --no-index) INDEX_REPO=0 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help)
      sed -n '2,34p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

# Pinned installer ref — bump deliberately after checking the upstream diff;
# never point at `main` (would execute whatever is there at run time).
CBM_INSTALLER_URL="https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/v0.9.0/install.sh"

# ── prerequisites ───────────────────────────────────────────────────
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required (apt-get install jq / brew install jq)" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_HOOKS="$REPO_ROOT/.claude/hooks"
USER_HOOKS="$HOME/.claude/hooks"
USER_SETTINGS="$HOME/.claude/settings.json"

[ -d "$REPO_HOOKS" ] || { echo "ERROR: $REPO_HOOKS missing — wrong directory?" >&2; exit 1; }

# ── hook inventory (single source of truth, shared with offboard) ───
# Format: "event|matcher|hook_basename|timeout"  (timeout 0 = omit)
HOOK_ENTRIES=(
  "SessionStart|startup|cbm-repo-context|0"
  "SessionStart|resume|cbm-repo-context|0"
  "SessionStart|clear|cbm-repo-context|0"
  "SessionStart|compact|cbm-repo-context|0"
  "UserPromptSubmit||cbm-prompt-reinject|0"
  "PostToolUse|Bash|cbm-cleanup-on-bash-worktree-remove|10"
  "PostToolUse|ExitWorktree|cbm-cleanup-on-exit-worktree|10"
)

# Files this script symlinks (paired with HOOK_ENTRIES targets).
HOOK_FILES=(
  cbm-repo-context
  cbm-prompt-reinject
  cbm-cleanup-on-bash-worktree-remove
  cbm-cleanup-on-exit-worktree
)

# Dry-run reporter. Each side-effectful call below decides for itself what to
# print + what to execute — no eval, no shell-string interpolation.
say_dry() { printf '  [dry-run] %s\n' "$*"; }

# ── 1. install the binary ───────────────────────────────────────────
echo "==> codebase-memory-mcp binary"
if command -v codebase-memory-mcp >/dev/null 2>&1; then
  echo "  already installed at $(command -v codebase-memory-mcp)"
elif [ "$INSTALL_BINARY" -eq 0 ]; then
  echo "  skipping per --no-binary (cbm will not work until you install it)"
else
  echo "  not installed. Official one-line installer (pinned):"
  echo "    curl -fsSL $CBM_INSTALLER_URL | bash"
  install_now=0
  if [ "$ASSUME_YES" -eq 1 ]; then
    install_now=1
  elif [ "$DRY_RUN" -eq 0 ]; then
    # `read` can fail under non-TTY stdin; don't let that abort the script.
    printf "  run it now? [y/N] "
    reply=""
    read -r reply || reply=""
    case "$reply" in
      y|Y|yes|YES) install_now=1 ;;
      *) echo "  skipped. Install manually before using cbm." ;;
    esac
  fi
  if [ "$install_now" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      say_dry 'curl -fsSL <installer> | bash'
    else
      curl -fsSL "$CBM_INSTALLER_URL" | bash
    fi
  fi
fi

# ── 2. symlink hooks into ~/.claude/hooks/ ──────────────────────────
echo
echo "==> hook symlinks → $USER_HOOKS"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$USER_HOOKS"
fi
for hook in "${HOOK_FILES[@]}"; do
  src="$REPO_HOOKS/$hook"
  dst="$USER_HOOKS/$hook"

  if [ ! -f "$src" ]; then
    echo "  WARN: source missing: $src — skipping"
    continue
  fi

  if [ -L "$dst" ]; then
    current_target="$(readlink "$dst")"
    # already pointing where we want? no-op.
    if [ "$current_target" = "$src" ] || [ "$(cd "$(dirname "$dst")" && readlink -f "$dst" 2>/dev/null || echo "$current_target")" = "$src" ]; then
      echo "  $hook: already symlinked → $current_target"
      continue
    fi
    echo "  $hook: existing symlink → $current_target — replacing with $src"
    if [ "$DRY_RUN" -eq 1 ]; then
      say_dry "ln -sfn $(printf %q "$src") $(printf %q "$dst")"
    else
      ln -sfn "$src" "$dst"
    fi
  elif [ -f "$dst" ]; then
    # regular file. Replace only if content matches (= safe upgrade from a
    # copy-based setup); otherwise refuse so we never stomp customizations.
    if cmp -s "$src" "$dst"; then
      echo "  $hook: identical regular file — converting to symlink"
      if [ "$DRY_RUN" -eq 1 ]; then
        say_dry "rm -f $(printf %q "$dst") && ln -sfn $(printf %q "$src") $(printf %q "$dst")"
      else
        rm -f "$dst"
        ln -sfn "$src" "$dst"
      fi
    else
      echo "  $hook: REGULAR FILE differs from repo version. Refusing to overwrite."
      echo "         Inspect: diff $dst $src"
      echo "         Once you've reconciled, re-run this script."
    fi
  else
    echo "  $hook: creating symlink → $src"
    if [ "$DRY_RUN" -eq 1 ]; then
      say_dry "ln -sfn $(printf %q "$src") $(printf %q "$dst")"
    else
      ln -sfn "$src" "$dst"
    fi
  fi
done

# ── 3. patch ~/.claude/settings.json ─────────────────────────────────
echo
echo "==> ~/.claude/settings.json"
if [ ! -f "$USER_SETTINGS" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would create $USER_SETTINGS with {\"hooks\":{}}"
  else
    mkdir -p "$(dirname "$USER_SETTINGS")"
    echo '{"hooks":{}}' > "$USER_SETTINGS"
    echo "  created empty $USER_SETTINGS"
  fi
fi

# Build a single jq program that applies all 7 add-if-missing operations.
# Each operation is keyed by (event, matcher, command_basename) so re-runs
# produce zero diff. Marker entry — exact command path — is the identity
# offboard uses to remove these later.
jq_program='
def hook_obj($cmd; $tm):
  if $tm > 0 then {type:"command", command:$cmd, timeout:$tm}
  else {type:"command", command:$cmd} end;

def upsert($event; $matcher; $cmd; $tm):
  .hooks //= {} |
  .hooks[$event] //= [] |
  .hooks[$event] = (
    .hooks[$event] as $blocks |
    # locate an existing block with the same matcher (null-aware)
    ($blocks | map(.matcher == $matcher) | index(true)) as $bi |
    if $bi == null then
      $blocks + [
        if $matcher == null then
          {hooks: [hook_obj($cmd; $tm)]}
        else
          {matcher: $matcher, hooks: [hook_obj($cmd; $tm)]}
        end
      ]
    else
      $blocks
      | (.[$bi].hooks // []) as $current
      | if ($current | any(.command == $cmd)) then $blocks
        else .[$bi].hooks = $current + [hook_obj($cmd; $tm)] end
    end
  );

# start the chain with the input doc; each upsert below extends it
.'
for entry in "${HOOK_ENTRIES[@]}"; do
  IFS='|' read -r event matcher hook tm <<<"$entry"
  cmd="~/.claude/hooks/$hook"
  if [ -z "$matcher" ]; then
    jq_program+=" | upsert(\"$event\"; null; \"$cmd\"; $tm)"
  else
    jq_program+=" | upsert(\"$event\"; \"$matcher\"; \"$cmd\"; $tm)"
  fi
done

# Apply.
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] computing diff…"
  patched=$(jq "$jq_program" "$USER_SETTINGS")
  printf '%s\n' "$patched" | diff -u "$USER_SETTINGS" - | sed 's/^/    /' || true
else
  ts=$(date +%Y%m%d-%H%M%S)
  cp "$USER_SETTINGS" "$USER_SETTINGS.bak.cbm-onboard.$ts"
  echo "  backup: $USER_SETTINGS.bak.cbm-onboard.$ts"
  jq "$jq_program" "$USER_SETTINGS" > "$USER_SETTINGS.tmp"
  mv "$USER_SETTINGS.tmp" "$USER_SETTINGS"
  echo "  patched (7 hook entries upserted, idempotent on re-run)"
fi

# ── 4. index this repo ───────────────────────────────────────────────
echo
echo "==> cbm index for $REPO_ROOT"
if [ "$INDEX_REPO" -eq 0 ]; then
  echo "  skipping per --no-index"
elif ! command -v codebase-memory-mcp >/dev/null 2>&1; then
  echo "  binary not installed — skipping. Index later with:"
  echo "    codebase-memory-mcp cli index_repository --repo-path \"$REPO_ROOT\" --mode full"
elif [ "$DRY_RUN" -eq 1 ]; then
  say_dry "codebase-memory-mcp cli index_repository --repo-path $(printf %q "$REPO_ROOT") --mode full"
else
  # mode full, not fast/moderate: the filtered modes exclude scripts/, docs/,
  # .github/ — exactly the files this repo's stale-reference sweeps rely on —
  # and a zero-hit search_code against a filtered index reads as "no
  # references" when the truth is "never looked".
  echo "  indexing (mode full — takes a few minutes on first run)…"
  codebase-memory-mcp cli index_repository --repo-path "$REPO_ROOT" --mode full
fi

# ── 5. summary ───────────────────────────────────────────────────────
echo
echo "==> done"
echo "  Verify hooks with: jq '.hooks' '$USER_SETTINGS'"
echo "  Restart Claude Code, then check /mcp lists codebase-memory-mcp."
echo "  If the MCP server is missing there, run: codebase-memory-mcp install"
echo "  Uninstall with: make cbm-offboard"
