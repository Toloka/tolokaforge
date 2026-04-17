# Harness Adapter Architecture

Adapters provide a unified interface for loading tasks and environments from different sources.

For contributor-facing contract details, see `docs/ADAPTER_INTERFACE.md`.

## Design Principles

1. **Unified Interface**: All task/environment loading goes through adapters
2. **Default Native Support**: File-based YAML tasks use `NativeAdapter` by default
3. **Plugin Discovery**: External adapters are discovered via `importlib.metadata` entry-points
4. **Same Config Pattern**: All adapters use `evaluation.tasks_glob` to locate tasks
5. **Deterministic Precedence**: With `evaluation.task_packs`, root list order defines precedence (`first-wins` on duplicates with warnings)

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      Run Configuration                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  evaluation:                                             │   │
│  │    tasks_glob: "tasks/tau/food_delivery"                  │   │
│  │  harness_adapter:                                        │   │
│  │    type: "tau"  # or "native" (default) or "tlk_mcp_core"│   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Entry-Point Discovery                        │
│  importlib.metadata.entry_points(group="tolokaforge.adapters") │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐     │
│  │   native     │  │     tau      │  │   tlk_mcp_core    │     │
│  │  (built-in)  │  │  (plugin)    │  │    (plugin)       │     │
│  └──────────────┘  └──────────────┘  └───────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Adapter Layer                             │
│  ┌──────────────────┐                                          │
│  │   BaseAdapter    │                                          │
│  │   (abstract)     │                                          │
│  └────────┬─────────┘                                          │
│           │                                                     │
│  ┌────────┴────────┬──────────────────┐                   │     │
│  ▼                 ▼                  │                    │     │
│  ┌──────────────┐  ┌────────────────┐ │                   │     │
│  │NativeAdapter │  │FrozenMcpCore  │ │                   │     │
│  │ (task.yaml)  │  │ (_domain/)    │ │                   │     │
│  │  [built-in]  │  │  [built-in]   │ │                   │     │
│  └──────────────┘  └────────────────┘ │                   │     │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    TolokaForge Core                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │
│  │ Orchestrator│──│ TrialRunner │──│ GradingEngine           │ │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## Plugin Architecture

External adapters are separate Python packages that register themselves via
`importlib.metadata` entry-points. The core `tolokaforge` package discovers
them automatically at import time.

### Entry-Point Registration

Each adapter package declares an entry-point in its `pyproject.toml`:

```toml
# In external_adapters/tolokaforge-adapter-terminal-bench/pyproject.toml
[project.entry-points."tolokaforge.adapters"]
terminal_bench = "tolokaforge_adapter_terminal_bench:TerminalBenchAdapter"
```

### Discovery Mechanism

```python
# tolokaforge/adapters/__init__.py
import importlib.metadata

def _discover_adapters() -> dict[str, type]:
    adapters = {"native": NativeAdapter}  # Always built-in
    for ep in importlib.metadata.entry_points(group="tolokaforge.adapters"):
        try:
            adapters[ep.name] = ep.load()
        except Exception as e:
            logger.debug("Adapter %s not available: %s", ep.name, e)
    return adapters
```

### Installation

```bash
# Install an external adapter
uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench
```

## File Structure

```
tolokaforge/adapters/
├── __init__.py          # Entry-point discovery, get_adapter(), register_adapter()
├── base.py              # BaseAdapter abstract class, AdapterEnvironment
├── native.py            # NativeAdapter for file-based tasks (built-in)
├── frozen_mcp_core.py   # FrozenMcpCoreAdapter for converted tasks (built-in)
└── bundle_writer.py     # Bundle writer for task artifacts

external_adapters/
└── tolokaforge-adapter-terminal-bench/  # Terminal-bench adapter plugin
    ├── pyproject.toml                   # Entry-point: terminal_bench
    └── src/tolokaforge_adapter_terminal_bench/
        ├── __init__.py
        ├── adapter.py                   # TerminalBenchAdapter
        ├── compose_env.py
        └── task_parser.py
```

## BaseAdapter Interface

All adapters implement these core methods:

| Method | Description |
|--------|-------------|
| `get_task_ids()` | List available task IDs |
| `get_task(task_id)` | Load task as `TaskConfig` |
| `get_task_dir(task_id)` | Get task directory path |
| `create_environment(task_id)` | Create `AdapterEnvironment` with data, tools, wiki |
| `get_tools(task_id)` | Get raw tool classes |
| `get_registry_tools(task_id, env)` | Get wrapped `Tool` instances for registry |
| `get_system_prompt(task_id)` | Get system prompt/wiki content |
| `get_grading_config(task_id)` | Get `GradingConfig` |
| `grade(task_id, trajectory, final_state, env)` | Grade trajectory (default uses `GradingEngine`) |
| `reset_environment(env)` | Reset environment to initial state |
| `compute_golden_hash(task_id, env)` | Compute expected state hash |

## Adapter-Specific Details

### NativeAdapter (built-in)

- **Detection**: Glob pattern matching `**/task.yaml`
- **Tools**: Loaded via `mcp_server` Python module specified in task config
- **Grading**: Uses `grading.yaml` with state checks, transcript rules, LLM judge
- **Package**: Built into `tolokaforge` core

### FrozenMcpCoreAdapter (built-in)

- **Detection**: Task directories with `_domain/` bundle
- **Tools**: Loaded from bundled `_domain/` directory via `tool_artifacts`
- **Data**: Converted task data, DB creation and tool wrapping
- **Grading**: Stable hash comparison (excludes unstable fields)

### TerminalBenchAdapter (plugin: `tolokaforge-adapter-terminal-bench`)

- **Detection**: Docker Compose task definitions
- **Tools**: Docker Compose environment
- **Install**: `uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench`

## Configuration Examples

### Native Tasks (default)

```yaml
evaluation:
  tasks_glob: "examples/**/task.yaml"
  output_dir: "output/examples"

# No harness_adapter = uses NativeAdapter automatically
```

### Frozen MCP Core Tasks

```yaml
evaluation:
  task_packs:
    - "/path/to/frozen-pack"
  tasks_glob: "**/task.yaml"
  output_dir: "output/frozen_mcp_core"

harness_adapter:
  type: "frozen_mcp_core"
  params: {}
```

## Benefits

1. **Unified Code Path**: Orchestrator always works through adapter interface
2. **Plugin Architecture**: Install only the adapters you need
3. **No sys.path Hacks**: External adapters use proper entry-point registration
4. **Single Source of Truth**: Uses original benchmark's data/tools/grading
5. **Extensible**: Same pattern works for SWE-bench, GAIA, etc.
6. **Backward Compatible**: Existing native tasks work via built-in NativeAdapter
