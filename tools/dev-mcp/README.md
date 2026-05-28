# dev-mcp

Dev MCP server for AI agent interaction with the tolokaforge repository.

## Overview

This is a [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes development tools as MCP tools. It gives AI agents (Roo Code, Claude Code, Cursor, etc.) convenient access to repository operations: running Python, launching tests, linting/formatting, regenerating canonical snapshots, and more.

## Usage

```bash
# Run the MCP server (stdio transport)
uv run --package dev-mcp dev-mcp
```

> **Note:** The `--package dev-mcp` flag is required because `uv sync` (default) only installs the root package. The `--package` flag ensures the workspace member is installed before running.

The server is automatically configured for Roo Code (`.roo/mcp.json`) and Claude Code (`.mcp.json`).

## Tools

| Tool | Description |
|------|-------------|
| `run_python` | Execute Python code or a script via `uv run python` |
| `run_tests` | Run pytest with optional markers (unit/canonical/integration), paths, keywords |
| `update_canonical_snapshots` | Regenerate canonical test snapshots (`--update-canon`) |
| `lint_check` | Check linting issues without fixing (ruff check) |
| `lint_fix` | Auto-fix linting issues (ruff check --fix) |
| `format_code` | Format code with black + ruff format |
| `format_check` | Check formatting without changes |
| `validate_tasks` | Validate task YAML definitions |
| `uv_sync` | Install/sync project dependencies |
| `make_clean` | Clean build artifacts |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `DEV_MCP_MAX_OUTPUT` | `50000` | Max chars returned to agent. Full output always saved to log file. |

## Output

Each tool returns a structured text block with exit code, log file path, and output. Full output is always dumped to `/tmp/dev_mcp_<tool>_<timestamp>.log`. If output exceeds `DEV_MCP_MAX_OUTPUT`, it is truncated with a pointer to the log file.

## Environment

The server auto-loads `.env` on every tool invocation. For tools requiring API keys (e.g., integration tests), pre-flight checks verify required env vars and return clear error messages if missing.
