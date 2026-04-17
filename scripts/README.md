# Scripts Directory

Utility scripts for managing the Tolokaforge benchmarking harness.

## Directory Structure

```
scripts/
├── common.sh                    # Shared bash utilities
├── with_env.sh                  # Load .env and execute a command
├── with_profile.sh              # Load shell profile and execute a command
│
├── lint/                        # Code quality scripts
│   ├── run_black.sh             # Run black formatter
│   └── run_ruff.sh              # Run ruff linter and formatter
│
├── setup/                       # Development environment setup
│   ├── create_python_venv.sh    # Full dev environment setup (uv, playwright, docker)
│   ├── init_git_lfs.sh          # Initialize Git LFS for fixture data
│   └── setup_env.sh             # Interactive .env setup for API keys
│
└── tests/                       # Test helper scripts
    └── smoke.sh                 # Smoke test for basic harness functionality
```

---

## Shared Utilities

### `common.sh`
Shared bash utilities used by other scripts:
- `log_info`, `log_error`, `log_warning`, `log_debug`
- `check_installed`, `check_variable`
- `load_env_file`
- `real_path`, `exec_silent`

Include in bash scripts:
```bash
source "${SCRIPT_DIR}/common.sh"
```

### `with_env.sh`
Loads environment variables from `.env` and executes a command.

```bash
# Run a benchmark with environment loaded
scripts/with_env.sh uv run tolokaforge run --config examples/browser_task/run_config.yaml
```

### `with_profile.sh`
Loads the shell profile and executes a command. Used when tools like `uv` are installed in non-standard locations.

---

## Setup Scripts (`setup/`)

### `create_python_venv.sh`
Full development environment setup:
- Syncs Python dependencies with `uv`
- Installs Playwright browsers
- Builds Docker images for integration tests

```bash
./scripts/setup/create_python_venv.sh
```

### `setup_env.sh`
Interactive setup for required environment variables (API keys).
Called automatically by `with_env.sh`.

### `init_git_lfs.sh`
Initialize Git LFS for pulling test fixture data.

---

## Lint Scripts (`lint/`)

### `run_ruff.sh`
Runs ruff linter and formatter on the codebase.

```bash
# Fix issues and format code
./scripts/lint/run_ruff.sh

# Check only (for CI, exits non-zero on issues)
./scripts/lint/run_ruff.sh --check
```

### `run_black.sh`
Runs black formatter on the codebase.

```bash
# Format code
./scripts/lint/run_black.sh

# Check only (for CI, exits non-zero on issues)
./scripts/lint/run_black.sh --check
```

---

## Test Helper Scripts (`tests/`)

### `smoke.sh`
Basic smoke test to verify harness functionality.

---

## Python Tools (`tools/`)

Complex Python tools live in `tools/` as uv workspace members, not in `scripts/`.
Each tool has its own `pyproject.toml` and is runnable via `uv run <tool-name>`.

Current tools:
- **`dev-mcp`** — Dev MCP server for AI agent interaction
- **`demo-recorder`** — Record demo sessions for tasks
- **`eval-orchestrator`** — Split/merge eval configs for parallel CI
- **`pricing-updater`** — Fetch and update LLM pricing data

See each tool's `README.md` for usage details.

---

## Code Quality & Pre-commit Hooks

### Setting Up Pre-commit Hooks

Pre-commit hooks automatically run linting checks before each commit, preventing bad code from entering the repository.

**One-time setup:**

```bash
# Sync dependencies (includes pre-commit)
uv sync --dev

# Install the git hooks
uv run pre-commit install
```

**What the hooks check:**
- **ruff**: Python linting (auto-fixes issues when possible)
- **ruff-format**: Code formatting
- **black**: Python code formatting

**Manual usage:**

```bash
# Run hooks on all files
uv run pre-commit run --all-files

# Run hooks on staged files only
uv run pre-commit run

# Update hook versions
uv run pre-commit autoupdate
```

### Makefile Targets

```bash
# Check linting (no fix) - CI ready
make lint

# Auto-fix linting issues
make lint-fix

# Format code (black + ruff format)
make format

# Check formatting only - CI ready
make format-check
```

---

## Script Template

For new bash scripts, use this template:

```bash
#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/../common.sh"  # adjust path depth as needed
# STANDARD PREAMBLE: END (do not edit)

# Your script here
```

For scripts at the `scripts/` root level, use:
```bash
source "${SCRIPT_DIR}/common.sh"
```
