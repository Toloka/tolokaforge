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

12. `fingerprint() -> dict[str, Any] | None`

    Report what this adapter resolved for the run.  The engine writes the
    returned payload verbatim under `adapter_fingerprints[<adapter type>]` on
    `engine_run_state.json` and neither validates nor interprets it, so it
    must be JSON-safe — a payload that is not raises at run start.  The
    default returns `None` and contributes no namespace; an adapter overrides
    it once it has resolved inputs worth naming.  See
    [Output Format](OUTPUT_FORMAT.md) § `engine_run_state.json`.

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
never from the adapter's identity. Three capabilities matter to adapter authors:

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

### Docker stack requirements

Adapters that need bind mounts, a docker socket, a DinD sidecar, `rag-service`,
or per-run compose-image pre-builds declare them by overriding
`BaseAdapter.docker_stack_requirements()`:

```python
class MyAdapter(BaseAdapter):
    def docker_stack_requirements(self) -> DockerStackRequirements:
        return DockerStackRequirements(
            task_pack_mounts=[self.task_pack_root],
            image_builds=[
                ComposeImageBuild(compose_file=compose, service="main")
                for compose in self.task_compose_files()
            ],
        )
```

Fields:

- `task_pack_mounts: list[Path]` — host directories bind-mounted into the
  runner at their absolute path. Used when the runner spawns sibling
  containers that must resolve task files at the same path on the host Docker
  daemon.
- `extra_runner_binds: list[tuple[Path, str]]` — additional
  `(host_path, container_path)` binds for the runner (typically a shared log
  directory).
- `mount_docker_socket: bool` — bind-mount `/var/run/docker.sock` into the
  runner. Overridden automatically for terminal-bench runs and compose-variant
  tool routing via the runtime-backend build context, so most adapters leave
  this at its default.
- `enable_dind: bool` — add a Docker-in-Docker sidecar so the runner can
  manage Docker Compose stacks without touching the host daemon.
- `needs_rag_service: bool` — declare that the adapter emits
  `TaskDescription.search.enabled=True`, so the orchestrator selects the
  `full_stack` factory that actually provisions `rag-service`. Not rendered
  into stack kwargs — it selects the factory.
- `image_builds: list[ComposeImageBuild]` — task-declared compose images the
  orchestrator builds once per run, immediately after the engine `:local`
  aliases are in place. Each entry runs
  `docker compose -f <compose_file> build <service>`, skipped when the pinned
  image already resolves locally. A build failure raises and aborts the run.
  This keeps the daemon-free `get_task()` / `to_task_description()` accessor
  contract intact — adapters *declare* what needs building; the orchestrator
  runs the subprocess.

### `ToolWrapper.execute` and `ToolWrapper.execute_call`

A tool call answers two questions: what the tool said, and whether the
substrate considers the call to have failed. `ToolWrapper` exposes one method
per question:

- `execute(arguments) -> str` — the output text. Abstract; every wrapper
  implements it. This is what the agent receives.
- `execute_call(arguments) -> ToolCallOutcome` — that same text plus
  `declared_failure: bool`. Concrete on the base class, which returns
  `declared_failure=False`.

A substrate with no out-of-band failure channel signals a failed call by
**raising**, and the golden-replay loop records the raise, so the inherited
`execute_call` is correct for such a wrapper and there is nothing to override.

Override `execute_call` when your substrate answers a failed call with a flag
*beside* the output instead of an error — MCP's `isError: true`, which arrives
next to the error prose the model needs in order to recover. The golden-replay
loop records a `declared_failure=True` outcome as a *raised* golden-action
failure, exactly as it records a raise, quoting the output as the message:

```python
class MyTool(ToolWrapper):
    async def execute(self, arguments: dict[str, Any]) -> str:
        return (await self.execute_call(arguments)).output

    async def execute_call(self, arguments: dict[str, Any]) -> ToolCallOutcome:
        answer = await self._substrate.call(self.name, arguments)
        return ToolCallOutcome(output=answer.text, declared_failure=answer.failed)
```

Keep one request path, as above: `execute` delegates to `execute_call` and drops
the flag. A wrapper that sends the request twice can answer the agent and the
grade differently. That direction holds only for a wrapper that overrides
`execute_call`; one that inherits it implements `execute` directly, since the
inherited `execute_call` is what calls `execute`.

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
