# Scripts

Utility scripts for developing with Tolokaforge.

    scripts/
    ├── common.sh                              # Shared bash utilities (logging, env loading)
    ├── with_env.sh                            # Load .env + run a command
    ├── with_profile.sh                        # Load profile (no .env) + run a command
    ├── generate_task_pack_compose_override.py  # Generate Docker compose overrides for task packs
    ├── setup/
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
