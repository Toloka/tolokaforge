# Adapter Interface Contract

This document defines the extension contract for adding new adapter backends.

## Plugin Registration

Adapters register as entry-points in the `tolokaforge.adapters` group:

```toml
# In your adapter package's pyproject.toml
[project.entry-points."tolokaforge.adapters"]
my_adapter = "my_adapter_package:MyAdapter"
```

The adapter class is discovered automatically by `tolokaforge` when the package
is installed.

## Required Methods

Each adapter must subclass `BaseAdapter` and implement:

1. `get_task_ids() -> list[str]`
2. `get_task(task_id: str) -> TaskConfig`
3. `get_task_dir(task_id: str) -> Path`
4. `create_environment(task_id: str) -> AdapterEnvironment`
5. `get_tools(task_id: str) -> list[Any]`
6. `get_registry_tools(task_id: str, env: AdapterEnvironment) -> list[Any]`
7. `get_system_prompt(task_id: str) -> str`
8. `get_grading_config(task_id: str) -> GradingConfig`
9. `reset_environment(env: AdapterEnvironment) -> None`
10. `compute_golden_hash(task_id: str, env: AdapterEnvironment) -> str | None`

## Optional Methods

11. `convert_to_native(task_id: str) -> NativeTaskBundle`

    Convert an external task to native TolokaForge format (task.yaml,
    grading.yaml, etc.) for disk serialisation.  The default implementation
    raises `NotImplementedError`; only external adapters need to override.
    See [Conversion Layer](CONVERSION_LAYER.md) for details.

## Lifecycle Expectations

1. Discovery: enumerate tasks deterministically.
2. Load: convert source format into canonical `TaskConfig`.
3. Environment: create deterministic initial state per task.
4. Tools: register tools with stable names and schemas.
5. Execution: run through orchestrator/trial runner.
6. Grading: produce canonical `Grade` object.
7. Reset: cleanly reset state between trials.
8. *(Optional)* Conversion: emit native format bundle via `convert_to_native()`.

## Constructor Contract

Adapters receive a `params: dict[str, Any]` in their constructor. Common params:

- `tasks_glob`: Path pattern for task discovery
- `base_dir`: Base directory for resolving paths
- `task_packs`: List of root directories to search

Adapter-specific params should be documented in the adapter's docstring.

## Determinism and Conflict Policy

1. When task packs are configured, root list order defines precedence.
2. Duplicate task IDs are `first-wins` with warning diagnostics.
3. Errors must be actionable and include task path/context.

## Error Contract

Adapters should fail fast with specific errors for:
1. Missing required source files.
2. Invalid task schema conversion.
3. Invalid grading configuration.
4. Unresolvable environment/tool dependencies.

## Reference Implementations

1. `NativeAdapter` (`tolokaforge.adapters.native`): canonical `task.yaml` + `grading.yaml` path. Built-in.
2. `FrozenMcpCoreAdapter` (`tolokaforge.adapters.frozen_mcp_core`): converted tasks with `_domain/` bundle. Built-in.
3. `TerminalBenchAdapter` (`tolokaforge_adapter_terminal_bench`): Docker Compose terminal tasks. Plugin package.

See also: `docs/ADAPTER_ARCHITECTURE.md`.
