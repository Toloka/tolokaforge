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
│  ┌────────┴────────┬──────────────────┬──────────────────┐     │
│  ▼                 ▼                  ▼                   │     │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │     │
│  │NativeAdapter │  │ TauAdapter   │  │TlkMcpCoreAdptr │  │     │
│  │ (task.yaml)  │  │ (env.py)     │  │(testcase.json) │  │     │
│  │  [built-in]  │  │  [plugin]    │  │   [plugin]     │  │     │
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
# In external_adapters/tolokaforge-adapter-tau/pyproject.toml
[project.entry-points."tolokaforge.adapters"]
tau = "tolokaforge_adapter_tau:TauAdapter"
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
uv sync --extra tau
uv sync --extra tlk_mcp_core

# Install all adapters
uv sync --extra adapters
```

## File Structure

```
tolokaforge/adapters/
├── __init__.py        # Entry-point discovery, get_adapter(), register_adapter()
├── base.py            # BaseAdapter abstract class, AdapterEnvironment
└── native.py          # NativeAdapter for file-based tasks (built-in)

external_adapters/
├── tolokaforge-adapter-tau/           # Tau-bench adapter plugin
│   ├── pyproject.toml                 # Entry-point: tau
│   └── src/tolokaforge_adapter_tau/
│       ├── __init__.py
│       ├── adapter.py                 # TauAdapter
│       └── import_hook.py            # TauBenchImportFinder
└── tolokaforge-adapter-tlk-mcp-core/  # MCP Core adapter plugin
    ├── pyproject.toml                 # Entry-point: tlk_mcp_core
    └── src/tolokaforge_adapter_tlk_mcp_core/
        ├── __init__.py
        ├── adapter.py                 # TlkMcpCoreAdapter
        └── testcase.py               # TlkMcpCoreTestCase
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

Adapters also carry overridable declarations with behaviour-preserving
defaults:

| Declaration | Default | Description |
|--------|--------|-------------|
| `trial_grader_name` (class attr) | `"runner_rpc"` | Name of the `TrialGrader` the orchestrator loads for this adapter's runs (from the `tolokaforge.trial_graders` entry-point group). Override to ship a custom grader. |

## Adapter-Specific Details

### NativeAdapter (built-in)

- **Detection**: Glob pattern matching `**/task.yaml`
- **Tools**: Loaded via `mcp_server` Python module specified in task config
- **Grading**: Uses `grading.yaml` with state checks, transcript rules, LLM judge
- **Package**: Built into `tolokaforge` core

### TauAdapter (plugin: `tolokaforge-adapter-tau`)

- **Detection**: Directory containing `env.py`
- **Tools**: Loaded from `tools/__init__.py` with `ALL_TOOLS` list
- **Data**: Loaded from `data/__init__.py` with `load_data()` function
- **Grading**: Hash-based comparison after executing `golden_actions`
- **Install**: `uv sync --extra tau`

### TlkMcpCoreAdapter (plugin: `tolokaforge-adapter-tlk-mcp-core`)

- **Detection**: Glob pattern matching `testcases/*.json`
- **Tools**: Loaded from `mcp-tools-library` package
- **Data**: `mcp_core.InMemoryDatabase` with `data_patch` overlays
- **Grading**: Stable hash comparison (excludes `UnstableField` annotations)
- **TypeSense**: Automatic indexing of `docindex/*.md` for knowledge base search
- **Install**: `uv sync --extra tlk_mcp_core`

## Configuration Examples

### Native Tasks (default)

```yaml
evaluation:
  tasks_glob: "tasks/food_delivery_2/tasks/**/task.yaml"
  output_dir: "output/food_delivery"

# No harness_adapter = uses NativeAdapter automatically
```

### Tau-format Environment

```yaml
evaluation:
  tasks_glob: "tasks/tau/food_delivery"
  output_dir: "output/tau_food_delivery"

harness_adapter:
  type: "tau"
  params:
    task_split: "test"
```

### TLK MCP Core Environment

```yaml
evaluation:
  tasks_glob: "contrib/project-m-copilot-mock-tools/mcp_servers/*/src/domains/*/testcases/*.json"
  output_dir: "output/tlk_mcp_core_retail"

harness_adapter:
  type: "tlk_mcp_core"
  params:
    tools_library: "contrib/project-m-copilot-mock-tools/mcp-tools-library"
    use_full_instruction: false
```

## Benefits

1. **Unified Code Path**: Orchestrator always works through adapter interface
2. **Plugin Architecture**: Install only the adapters you need
3. **No sys.path Hacks**: External adapters use proper entry-point registration
4. **Single Source of Truth**: Uses original benchmark's data/tools/grading
5. **Extensible**: Same pattern works for SWE-bench, GAIA, etc.
6. **Backward Compatible**: Existing native tasks work via built-in NativeAdapter
