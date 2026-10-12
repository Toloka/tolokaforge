# Scripts

Utility scripts for developing with Tolokaforge.

    scripts/
    ├── common.sh                              # Shared bash utilities (logging, env loading)
    ├── with_env.sh                            # Load .env + run a command
    ├── with_profile.sh                        # Load profile (no .env) + run a command
    ├── generate_task_pack_compose_override.py  # Generate Docker compose overrides for task packs
    ├── analysis/
    │   ├── audit_preset_fallthrough.py         # Read-only audit: which models resolve to a shared preset that carries none of the budget knobs a sibling carries
    │   └── calibrate_rubric.sh                 # Calibrate a rubric judge against golden fixtures + apply the trust gate
    ├── docker/
    │   └── prune-docker.sh                     # Reclaim Docker disk between many-trial runs (keeps layers held by running containers)
    ├── hatch/
    │   └── hatch_runner_subset_builder.py     # Custom hatchling builder for the runner-subset wheel (Docker-only; hatch build --target custom)
    ├── setup/
    │   ├── cbm-onboard.sh                     # codebase-memory-mcp + Claude Code hooks into ~/.claude/ (make cbm-onboard)
    │   ├── cbm-offboard.sh                    # Reverse of cbm-onboard (make cbm-offboard)
    │   └── setup_env.sh                       # Interactive .env setup (API keys)
    └── tests/
        ├── smoke.sh                           # Multi-tier pytest runner (unit → integration)
        └── task_pack_docker_smoke.sh          # Docker task-pack mount integration test

## Quick reference

    # Load .env and run the harness
    scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml

    # Interactive .env setup (first time)
    scripts/setup/setup_env.sh

    # Generate Docker Compose override for task-pack mounts
    uv run python scripts/generate_task_pack_compose_override.py \
      --config examples/native/coding/run_configs/dev.yaml \
      --output docker-compose.taskpacks.override.yaml

    # Run the smoke test suite
    scripts/tests/smoke.sh

    # Audit model → preset resolution before a multi-model sweep
    uv run python scripts/analysis/audit_preset_fallthrough.py
    uv run python scripts/analysis/audit_preset_fallthrough.py moonshotai/kimi-k2.6 moonshotai/kimi-k3
    uv run python scripts/analysis/audit_preset_fallthrough.py --json --fail-on-suspect

## Preset fall-through audit

`scripts/analysis/audit_preset_fallthrough.py` reports, per model slug, the
preset `tolokaforge.core.llm.presets` resolves it to, which budget knobs
(`default_max_turns`, `empty_retry_count`, `max_context_tokens` +
`context_watermark`, `tool_output_max_chars`, `message_assembly_policy`,
`parser_error_retry_count`, `output_length_retry_count`) that preset declares,
and the model's OpenRouter context window. It flags two structural hazards:

- **SUSPECT** — the slug resolves to a preset whose match globs span more than
  one vendor, while a same-family slug resolves to a different preset carrying
  budget knobs this one does not.
- **CONTEXT CEILING** — a preset's `max_context_tokens + context_watermark`
  sits above (overshoot) or far below (undershoot) the smallest real window
  among the slugs its globs match.

With no arguments it audits every concrete slug named by a preset match glob
plus the current sweep lineup. `--offline` skips the OpenRouter fetch and
falls back to the cache at `.cache/openrouter_models.json`; the preset half of
the audit runs either way. The script writes nothing outside that cache.

## Formatting and linting

Use the Makefile targets — no shell wrappers needed:

    make lint          # ruff check (no fix)
    make lint-fix      # ruff check --fix
    make format        # black + ruff format
    make format-check  # check only (for CI)

## codebase-memory-mcp (cbm) — per-engineer Claude Code setup

Opt-in: nothing here runs for engineers who don't onboard.

- `make cbm-onboard` — installs the `codebase-memory-mcp` binary (via the
  official installer, pinned to a release tag, prompted; the installer also
  registers the MCP server with your coding agents), symlinks the four
  `.claude/hooks/cbm-*` files into `~/.claude/hooks/`, patches
  `~/.claude/settings.json` with 7 hook entries (SessionStart × 4,
  UserPromptSubmit, PostToolUse:Bash, PostToolUse:ExitWorktree), and indexes
  this repo (`index_repository`, mode `full` — the filtered modes exclude
  `scripts/`, `docs/`, `.github/`). Idempotent — safe to re-run after every
  `git pull`. Symlinks (not copies) mean hook updates land via `git pull`
  with no re-run.
- `make cbm-offboard` — reverses the on-disk changes. Leaves the cbm
  binary in place.
- Flags via direct invocation: `bash scripts/setup/cbm-onboard.sh
  --dry-run` (preview), `--no-binary`, `--no-index`, `--yes`; offboard
  takes `--dry-run`.

Backups of `~/.claude/settings.json` are written to
`~/.claude/settings.json.bak.cbm-*.<timestamp>` on every write.

What the hooks do:

- `cbm-repo-context` (SessionStart) — emits the repo's cbm project key,
  the nearest `AGENTS.md` chain, a loud warning if the repo has no cbm
  index yet (first call must be `index_repository`), and the cbm-first
  protocol reminder (use `search_graph` / `trace_path` / `search_code`,
  don't grep the repo).
- `cbm-prompt-reinject` (UserPromptSubmit) — re-injects a ~70-token
  cbm-first rule on every prompt so it survives context compaction.
- `cbm-cleanup-on-bash-worktree-remove` / `cbm-cleanup-on-exit-worktree`
  (PostToolUse) — drop the matching cbm index DB when a git worktree is
  removed, so per-worktree indexes don't accumulate.
