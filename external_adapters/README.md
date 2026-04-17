# External Adapters

This directory contains adapter packages that are installed as separate Python packages
with entry-point registration for `tolokaforge.adapters` discovery.

## Packages

| Package | Entry-Point Name | Adapter Class | Source |
|---------|-----------------|---------------|--------|
| `tolokaforge-adapter-terminal-bench` | `terminal_bench` | `TerminalBenchAdapter` | Terminal-bench Docker Compose tasks |

## How It Works

Each package declares an entry-point in its `pyproject.toml`:

```toml
[project.entry-points."tolokaforge.adapters"]
terminal_bench = "tolokaforge_adapter_terminal_bench:TerminalBenchAdapter"
```

The core `tolokaforge` package discovers these at runtime via `importlib.metadata.entry_points()`.

## Installation

Install individually:

```bash
uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench
```

## Creating a New Adapter

1. Create a new directory: `external_adapters/tolokaforge-adapter-{name}/`
2. Add `pyproject.toml` with entry-point under `tolokaforge.adapters`
3. Implement `BaseAdapter` subclass
4. Add to root `pyproject.toml` workspace members and optional deps
5. Run `uv sync` to install
