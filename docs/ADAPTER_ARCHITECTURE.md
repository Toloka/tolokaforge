# Harness Adapter Architecture

Adapters provide a unified interface for loading tasks and environments from different sources.

For contributor-facing contract details, see `docs/ADAPTER_INTERFACE.md`.

## Adapter families

The engine exposes two disjoint families of adapters. Each family is a Protocol with an entry-point group; downstream code depends on the Protocol shape, not the concrete class.

- **Harness adapters** (this document) — task loaders. `BaseAdapter` translates an adapter-specific on-disk format into `TaskConfig` / `AdapterEnvironment`. Discovered through `tolokaforge.adapters`. Consumed by the orchestrator, one adapter per run.
- **Composition-plan adapters** (ADR-0044) — compose-mode runtime seams. Three Protocols shipped in `tolokaforge/core/composition_runtime.py`:
  - **`ComposeMaterialiser`** — brings one `StackDecl` up as a live compose project and tears it down; shipped as `DockerComposeMaterialiser` (`tolokaforge/core/docker_compose_materialiser.py`). Ports to K8s / remote sandbox substrates land as new implementations against the same Protocol.
  - **`ServiceLifecycleDispatcher`** — cycles one service between trials for a `ServiceIsolation` label; three built-in dispatchers (`shared`, `reset`, `ephemeral`) register into `DISPATCHER_REGISTRY` at import in `tolokaforge/core/service_lifecycle_dispatchers.py`.
  - **`SubstrateComposer`** — sequences the two above across the run / task / trial brackets, enforces the composition-plan invariants (INV-12), and delegates between-trial service cycling; shipped as `DefaultSubstrateComposer` (`tolokaforge/core/default_substrate_composer.py`).

  See [`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md#composition-plan-seams) for the seam catalogue in runtime context. Composition-plan adapters do not overlap with harness adapters — a harness adapter emits an `EnvironmentManifest`; the composer consumes the manifest's composition plan at run time.

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
them lazily on the first `available_adapters()` (or `get_adapter(<non-native>)`)
call through the shared fail-loud primitive — see § Discovery Mechanism and
§ Fail-loud registry pattern below.

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
from tolokaforge.core.plugin_registry import discover_entry_points

def _discover_adapters() -> dict[str, type]:
    adapters = {"native": NativeAdapter}  # Always built-in
    for name, ep in discover_entry_points("tolokaforge.adapters").items():
        try:
            adapters[name] = ep.load()
        except Exception as exc:
            _FAILED_ADAPTERS[name] = exc
            logger.warning("Adapter %r failed to load: %s", name, exc, exc_info=True)
    return adapters
```

`discover_entry_points` raises `DuplicateRegistrationError` before any
adapter class is loaded when two installed distributions register the same
adapter name; a broken adapter is stashed in `_FAILED_ADAPTERS` and only
surfaces when `get_adapter(<broken_name>)` asks for that specific adapter,
so one broken plug-in never poisons resolution of a healthy sibling.

### Fail-loud registry pattern

Every engine-side `importlib.metadata` entry-point registry — `tolokaforge.adapters`,
every plugin-registry seam listed in
[`docs/RUNTIME_BACKENDS.md` § "Plug-in extension points"](RUNTIME_BACKENDS.md#plug-in-extension-points),
and any future group added under
[ADR-0030](adr/0030-tolokaforge-models-split.md) —
routes discovery through `tolokaforge.core.plugin_registry.discover_entry_points`.
The primitive splits fail-loud into two shapes:

- **Duplicate name — an unresolvable ambiguity.** Two entry points with the
  same `name` in the same group raise `DuplicateRegistrationError` before any
  class is loaded, before any budget is spent. The message names both
  providing distributions so the operator can uninstall one to resolve.
- **Broken import — a per-name defect.** A target that raises on `.load()`
  propagates its original exception only when its own name is requested; a
  healthy sibling in the same group stays resolvable.

New engine registries reuse this primitive rather than open-coding their own
scan. The [ADR-0030 follow-up 5 / 11 model-data
registry](adr/0030-tolokaforge-models-split.md#follow-ups) is the next
planned consumer.

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

## Hosting coding-harness runs

Coding-harness mode is an [`AgentDriver`](../tolokaforge/core/agent_driver.py)
Strategy — the orchestrator selects `CodingHarnessDriver` from
`models.agent.coding_harness` and applies it around adapter output. The
driver replaces the engine's LLM turn loop with one invocation of a
vendor CLI (`claude-code`, `codex`, `kimi-code`, `opencode`,
`grok-build`, `gemini-cli`) inside the trial container, and adds a
`tolokaforge-llm-gateway` sidecar service alongside it to shield the
real provider credential — the CLI's service sees only a dummy token
and the sidecar's docker-DNS URL. New agent loops (Harbor as an
embedded library, an ACP driver, a custom loop) plug in as a new
`AgentDriver` implementation with no adapter edit.

An adapter opts in by overriding
`BaseAdapter.stage_task(task_id) -> StagedTask | None` to materialise
a per-trial staging directory with a synthesised compose file the
driver layers the CLI install onto. The orchestrator refuses
`models.agent.coding_harness` against an adapter whose `stage_task`
returns `None`, naming the currently opted-in set (today: `native`,
`terminal_bench`). Adapters carry no coding-harness state and never
import driver code. Design records:
[ADR-0039](adr/0039-coding-harness-adapter-agnostic.md) (driver
protocol) and
[ADR-0041](adr/0041-coding-harness-credential-gateway.md) (credential
shield).

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
