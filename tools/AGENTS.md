# AGENTS.md — tools/

## Overview

Each subdirectory is a **uv workspace member** — an independent Python package runnable via `uv run <tool-name>`. All tools are registered in the root `pyproject.toml` under `[tool.uv.workspace]`.

## Current Tools

| Tool | Entry Point | Purpose |
|------|-------------|---------|
| `demo-recorder` | `demo-recorder` | Generate demo videos from trajectories |
| `dev-mcp` | `dev-mcp` | Dev MCP server for AI agent interaction (tests, lint, format, etc.) |
| `eval-orchestrator` | `eval-orchestrator` | Split/merge eval configs for parallel CI |
| `pricing-updater` | `pricing-updater` | Fetch and update LLM pricing data |

## Adding a New Tool

1. Create `tools/<tool-name>/` with standard Python package structure:
   - `pyproject.toml` with `[project.scripts]` entry
   - `src/<package_name>/` with `__init__.py` and `cli.py`
   - `README.md`
   - `tests/` directory
2. Register in root `pyproject.toml` → `[tool.uv.workspace]` → `members` list.
3. Run `uv sync` to install.
4. Verify: `uv run <tool-name> --help` must work.

## Rules

1. **DO NOT** use `[project.optional-dependencies]` in tool packages.
2. **Dev dependencies** go in root `pyproject.toml` under `[dependency-groups]` → `dev` (PEP 735), not in the tool's `pyproject.toml`.
3. Every tool **must** be independently runnable via `uv run <tool-name> --help`.
4. Every tool **must** have its own `README.md`.
5. Every tool **should** have tests in `tools/<tool-name>/tests/`.
6. Use `typer` for CLI, `hatchling` for build backend — follow existing tools as reference.
7. Use `[project.scripts]` to define the CLI entry point (e.g., `<tool-name> = "<package>.cli:app"`).
