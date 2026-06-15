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

## Runtime Capabilities (declarative, opt-in)

The runner selects per-trial behaviour from **data on the `TaskDescription`**,
never from the adapter's identity. Two capabilities matter to adapter authors:

### `GradingConfig.grading_method`

A declarative selector on the grading config that tells the runner *how* to grade:

- **`None`** *(default)* — standard grading: combines state checks / transcript
  rules / LLM judge per `weights` + `pass_threshold`. Most adapters want this and
  need do nothing.
- **`"test_execution"`** — the runner runs a reference test suite inside the
  trial's env container via an exec-capable lifecycle tool (today:
  `DockerComposeExecToolWrapper`) and scores by reading a reward float written
  by the suite. Requires such a tool in `TaskDescription.agent_tools`; otherwise
  the runner returns a clear error at `GradeTrial` time. Used by the
  `terminal_bench` adapter as a worked example.
- `"hash"` / `"transcript"` / `"llm"` are reserved names for future
  single-method dispatch and currently behave as part of the default path.

Typos in this value are caught at validation by the `Literal[...]` field type;
an "unknown grading method" cannot reach the runner silently.

### `ToolWrapper.has_lifecycle`

Tools may own per-trial resources (e.g. a compose stack started for the trial).
The base `ToolWrapper` declares `has_lifecycle = False` and ships no-op
`start(ctx)` / `stop()`. A tool that needs lifecycle management overrides:

```python
class MyTool(ToolWrapper):
    has_lifecycle = True

    def start(self, ctx: ToolLifecycleContext) -> None:
        ...  # provision per-trial resources; ctx carries trial_id + artifacts_dir

    def stop(self) -> None:
        ...  # tear down what start() created
```

The runner calls `start` / `stop` generically on every tool whose
`has_lifecycle` is set — it doesn't need to know your tool by name.

#### Current limitations of the lifecycle contract

The lifecycle contract is deliberately minimal today — sufficient for the one
lifecycle tool that ships built-in, but shallow on purpose. Known gaps for
custom lifecycle tools:

- `ToolLifecycleContext` carries only `trial_id` and `artifacts_dir`. Tools
  needing env-service endpoints, resource limits, secrets, or task metadata
  must source them from elsewhere.
- `start(ctx)` is synchronous, with no timeout or retry policy.
- Only `start` / `stop` hooks exist (no `suspend` / `snapshot` /
  `reset-without-teardown`).
- No ordering or dependency declaration between multiple lifecycle tools in
  the same trial — they're started in dict-iteration order.
- No explicit readiness signal; `start()` returning is treated as "ready".

The shape is forward-compatible: `ToolLifecycleContext` is a dataclass and the
runner dispatches off the capability (not adapter identity), so the contract
will grow additively when a real second user surfaces a need. Until then,
custom lifecycle tools must either fit within the current shape or vendor the
missing pieces internally. See the open tracking issue for the planned
direction.

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
2. `TauAdapter` (`tolokaforge_adapter_tau`): Tau-bench format. Plugin package.
3. `TlkMcpCoreAdapter` (`tolokaforge_adapter_tlk_mcp_core`): MCP Core JSON format. Plugin package.

See also: `docs/ADAPTER_ARCHITECTURE.md`.
