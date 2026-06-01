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
│  │    tasks_glob: "tasks/**/task.yaml"                       │   │
│  │  harness_adapter:                                        │   │
│  │    type: "terminal_bench"  # or "native" (default)        │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Entry-Point Discovery                        │
│  importlib.metadata.entry_points(group="tolokaforge.adapters") │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐     │
│  │   native     │  │terminal_bench│  │     example       │     │
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
│  ┌────────┴────────┬──────────────────┬──────────────────┐     │
│  ▼                 ▼                  ▼                   │     │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │     │
│  │NativeAdapter │  │TerminalBench │  │ ExampleAdapter │  │     │
│  │ (task.yaml)  │  │  Adapter     │  │  (plugin)      │  │     │
│  │  [built-in]  │  │  [plugin]    │  │                │  │     │
│  └──────────────┘  └──────────────┘  └────────────────┘  │     │
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
# In tolokaforge-adapter-example/pyproject.toml
[project.entry-points."tolokaforge.adapters"]
example = "tolokaforge_adapter_example.adapter:ExampleAdapter"
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
# Install specific adapter
uv sync --extra terminal_bench
uv sync --extra example

# Install all adapters
uv sync --extra adapters
```

## File Structure

```
tolokaforge/adapters/
├── __init__.py        # Entry-point discovery, get_adapter(), register_adapter()
├── base.py            # BaseAdapter abstract class, AdapterEnvironment
└── native.py          # NativeAdapter for file-based tasks (built-in)

# External adapter plugins live in their own packages, for example:
tolokaforge-adapter-example/           # Example external adapter plugin
├── pyproject.toml                     # Entry-point: example
└── src/tolokaforge_adapter_example/
    ├── __init__.py
    ├── adapter.py                     # ExampleAdapter
    └── data.py                        # Plugin-specific data/tool loading
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

### TerminalBenchAdapter (plugin: `terminal_bench`)

- **Detection**: Terminal Bench task layout discovered via `tasks_glob`
- **Tools**: Provided by the plugin package for the Terminal Bench environment
- **Data**: Loaded from the benchmark's own task definitions
- **Grading**: Uses the benchmark's verification/grading logic
- **Install**: `uv sync --extra terminal_bench`

### Example external adapter (plugin: `tolokaforge-adapter-example`)

Any third-party adapter follows the same contract. A hypothetical
`example` plugin illustrates the concepts a plugin typically wires up:

- **Detection**: Glob pattern matching the plugin's task files
- **Tools**: Loaded from the plugin package and exposed as `Tool` instances
- **Data**: Loaded from the plugin's own data definitions, optionally with
  per-task overlays
- **Grading**: Hash-based or rule-based comparison defined by the plugin
- **Install**: `uv sync --extra example`

## Configuration Examples

### Native Tasks (default)

```yaml
evaluation:
  tasks_glob: "tasks/food_delivery_2/tasks/**/task.yaml"
  output_dir: "output/food_delivery"

# No harness_adapter = uses NativeAdapter automatically
```

### Terminal Bench Environment

```yaml
evaluation:
  tasks_glob: "tasks/terminal_bench/**/task.yaml"
  output_dir: "output/terminal_bench"

harness_adapter:
  type: "terminal_bench"
```

### External Plugin Adapter

```yaml
evaluation:
  tasks_glob: "path/to/tasks/**/testcases/*.json"
  output_dir: "output/example"

harness_adapter:
  type: "example"  # or "native"
  params:
    tools_library: "path/to/tools-library"
    use_full_instruction: false
```

## Benefits

1. **Unified Code Path**: Orchestrator always works through adapter interface
2. **Plugin Architecture**: Install only the adapters you need
3. **No sys.path Hacks**: External adapters use proper entry-point registration
4. **Single Source of Truth**: Uses original benchmark's data/tools/grading
5. **Extensible**: Same pattern works for SWE-bench, GAIA, etc.
6. **Backward Compatible**: Existing native tasks work via built-in NativeAdapter
