# Scripts

Utility scripts for developing with Tolokaforge.

    scripts/
    ├── common.sh                              # Shared bash utilities (logging, env loading)
    ├── with_env.sh                            # Load .env + run a command
    ├── with_profile.sh                        # Load profile (no .env) + run a command
    ├── generate_task_pack_compose_override.py  # Generate Docker compose overrides for task packs
    ├── setup/
    │   ├── cbm-onboard.sh                     # codebase-memory-mcp + Claude Code hooks into ~/.claude/ (make cbm-onboard)
    │   ├── cbm-offboard.sh                    # Reverse of cbm-onboard (make cbm-offboard)
    │   └── setup_env.sh                       # Interactive .env setup (API keys)
    └── tests/
        ├── smoke.sh                           # Multi-tier pytest runner (unit → integration)
        └── task_pack_docker_smoke.sh          # Docker task-pack mount integration test

## Quick reference

    # Load .env and run the harness
    scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_config.yaml

    # Interactive .env setup (first time)
    scripts/setup/setup_env.sh

    # Generate Docker Compose override for task-pack mounts
    uv run python scripts/generate_task_pack_compose_override.py \
      --config examples/native/coding/run_config.yaml \
      --output docker-compose.taskpacks.override.yaml

    # Run the smoke test suite
    scripts/tests/smoke.sh

## Formatting and linting

Use the Makefile targets — no shell wrappers needed:

    make lint          # ruff check (no fix)
    make lint-fix      # ruff check --fix
    make format        # black + ruff format
    make format-check  # check only (for CI)

## codebase-memory-mcp (cbm) — per-engineer Claude Code setup

Opt-in: nothing here runs for engineers who don't onboard.

- `make cbm-onboard` — installs the `codebase-memory-mcp` binary (via the
  official installer, prompted; the installer also registers the MCP server
  with your coding agents), symlinks the four `.claude/hooks/cbm-*` files
  into `~/.claude/hooks/`, and patches `~/.claude/settings.json` with 7
  hook entries (SessionStart × 4, UserPromptSubmit, PostToolUse:Bash,
  PostToolUse:ExitWorktree). Idempotent — safe to re-run after every
  `git pull`. Symlinks (not copies) mean hook updates land via `git pull`
  with no re-run.
- `make cbm-offboard` — reverses the on-disk changes. Leaves the cbm
  binary in place.
- Flags via direct invocation: `bash scripts/setup/cbm-onboard.sh
  --dry-run` (preview), `--no-binary`, `--yes`; offboard takes `--dry-run`.

Backups of `~/.claude/settings.json` are written to
`~/.claude/settings.json.bak.cbm-*.<timestamp>` on every write.

What the hooks do:

- `cbm-repo-context` (SessionStart) — emits the repo's cbm project key,
  the nearest `AGENTS.md` chain, and the cbm-first protocol reminder
  (use `search_graph` / `trace_path` / `search_code`, don't grep the repo).
- `cbm-prompt-reinject` (UserPromptSubmit) — re-injects a ~70-token
  cbm-first rule on every prompt so it survives context compaction.
- `cbm-cleanup-on-bash-worktree-remove` / `cbm-cleanup-on-exit-worktree`
  (PostToolUse) — drop the matching cbm index DB when a git worktree is
  removed, so per-worktree indexes don't accumulate.
