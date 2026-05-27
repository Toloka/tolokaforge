# External Adapters

This directory contains adapter packages that are installed as separate Python packages
with entry-point registration for `tolokaforge.adapters` discovery.

## Packages

| Package | Entry-Point Name | Adapter Class | Source |
|---------|-----------------|---------------|--------|
| `tolokaforge-adapter-tau` | `tau` | `TauAdapter` | Tau-bench environments |
| `tolokaforge-adapter-tlk-mcp-core` | `tlk_mcp_core` | `TlkMcpCoreAdapter` | MCP-core based environments |
| `tolokaforge-adapter-terminal-bench` | `terminal_bench` | `TerminalBenchAdapter` | Terminal-bench Docker Compose tasks |

## How It Works

Each package declares an entry-point in its `pyproject.toml`:

```toml
[project.entry-points."tolokaforge.adapters"]
tau = "tolokaforge_adapter_tau:TauAdapter"
```

The core `tolokaforge` package discovers these at runtime via `importlib.metadata.entry_points()`.

## Installation

Via the root `pyproject.toml` optional dependencies:

```bash
# Install specific adapter
uv sync --extra tau
uv sync --extra tlk_mcp_core

# Install all adapters
uv sync --extra adapters
```

Or install individually:

```bash
uv pip install -e external_adapters/tolokaforge-adapter-tau
uv pip install -e external_adapters/tolokaforge-adapter-tlk-mcp-core
uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench
```

## Creating a New Adapter

1. Create a new directory: `external_adapters/tolokaforge-adapter-{name}/`
2. Add `pyproject.toml` with entry-point under `tolokaforge.adapters`
3. Implement `BaseAdapter` subclass
4. Add to root `pyproject.toml` workspace members and optional deps
5. Run `uv sync` to install
