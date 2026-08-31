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

12. `grading_combine_layer() -> CombineLayer`

    What your projects supply beneath a task's own `combine` block.  The
    pre-run authoring gate resolves a task's *effective* combine from this and
    the task's own block, so an adapter that reads a project tree reports its
    defaults here.  The default is `CombineLayer.unresolvable()`: an adapter
    that synthesises grading config rather than reading a project tree cannot
    say what a project supplies, and reporting "no defaults" instead would
    refuse a task whose weights are inherited.

13. `grading_hash_source_layer(task: TaskConfig, task_dir: Path) -> HashSourceLayer`
    — a **classmethod**

    What you supply beneath a task's authored `state_checks.hash` block.
    Report facts, not verdicts: the source you compute the comparison from and
    whether it is usable, missing or empty; the gates decide what is fatal.

    ```python
    @classmethod
    def grading_hash_source_layer(cls, task, task_dir):
        golden = task_dir / "fixtures" / "golden_actions.json"
        if not golden.exists():
            return HashSourceLayer(
                supplied=AdapterHashSource(
                    where="fixtures/golden_actions.json",
                    state=SuppliedSourceState.MISSING,
                )
            )
        ...
    ```

    Three answers, deciding three different things:

    - `HashSourceLayer.unresolvable()` — you cannot say.  The default, and the
      answer that leaves an enabled hash block declaring no source reported but
      never refused.
    - `HashSourceLayer()` — nothing beneath the block, so the authored keys are
      the whole layer and a block enabling the hash with no source declared is
      an authoring defect the gates refuse.
    - `HashSourceLayer(supplied=AdapterHashSource(where=…, state=…))` — you
      supply the source named by `where`, a path in your own vocabulary
      relative to the task directory, since that is what a refusal shows the
      author.  A `USABLE` source makes the bare block a clean pass; `MISSING`
      or `EMPTY` is refused before any trial is paid for.

    A classmethod, matching the family of hooks in items 14 - 16: `tolokaforge
    validate` is a static gate that constructs no adapters and keeps validating
    packs whose adapter package is not installed, so every fact reported here
    must be a function of the task and its directory alone.  See
    [ADR-0042](adr/0042-adapter-blind-authoring-gate.md).

14. `grading_tool_inventory(task: TaskConfig, task_dir: Path) -> ToolInventory`
    — a **classmethod**

    The tool set this adapter presents at runtime, for the authoring gate to
    hold `present` / `absent` matchers and trace-check tool references against.
    An adapter whose runtime tool set is not the native reading of
    `tools.agent.enabled` — one that resolves tools from its own registry or a
    fixture the pack does not name — reports its own set here so the same rules
    protect its packs.

    Two answers, deciding two different things:

    - `ToolInventory.unresolvable()` — you cannot say.  The default, and the
      answer that leaves every tool-aware rule reported rather than refused for
      adapters that cannot answer.  Reported as `SkipKind.ADAPTER_DECLARED`,
      promotable to fatal by an author naming your adapter under
      `tolokaforge validate --strict-authoring`.
    - A concrete `ToolInventory` — the names your runtime resolves, plus
      whichever JSON-schema parameters the pack's own fixture or registry
      resolves for each.  See `NativeAdapter.grading_tool_inventory` for the
      native reading — the shape any adapter's return here must satisfy.

15. `grading_replay_world(task: TaskConfig, task_dir: Path) -> ReplayWorld`
    — a **classmethod**

    What this adapter gives a golden-action replay to be executed against.  Two
    facts, neither of them readable from `grading.yaml`: what the state a
    replay loads is (a JSON file, an inline mapping, or nothing), and whether
    the pack ships the tool module those actions call.  The native reading is
    `initial_state.json_db` and `tools.agent.mcp_server`; an adapter that
    builds a world from its own fixtures reports through this hook instead.

    Two answers:

    - `ReplayWorld.unresolvable()` — you cannot say.  The default; reported as
      `SkipKind.ADAPTER_DECLARED`.
    - `ReplayWorld(initial_state=…, mcp_server=…)` — the two task-level facts
      the golden-action rule reads.

16. `grading_seeded_tables(task: TaskConfig, task_dir: Path) -> SeededTablesLayer`
    — a **classmethod**

    The tables this adapter seeds, which the pack's `state_checks.id_fields`
    declaration keys.  Reports a view of the state a trial starts on so a
    declared primary key held against a table the adapter does not seed is
    caught before the trial is paid for rather than raising during grading.

    Two answers:

    - `SeededTablesLayer.unresolvable()` — you cannot say.  The default;
      reported as `SkipKind.ADAPTER_DECLARED`.
    - `SeededTablesLayer(tables=<name>: <records>)` — the tables and their
      records the trial's starting state carries.  See
      `NativeAdapter.grading_seeded_tables` for the native reading.

17. `fingerprint() -> dict[str, Any] | None`

    Report what this adapter resolved for the run.  The engine writes the
    returned payload verbatim under `adapter_fingerprints[<adapter type>]` on
    `engine_run_state.json` and neither validates nor interprets it, so it
    must be JSON-safe — a payload that is not raises at run start.  The
    default returns `None` and contributes no namespace; an adapter overrides
    it once it has resolved inputs worth naming.  See
    [Output Format](OUTPUT_FORMAT.md) § `engine_run_state.json`.

18. `grading_source(task: TaskConfig, task_dir: Path) -> GradingSource`

    The grading block one run of *task* reads, resolved against this adapter.
    Where a task naming a file that is on disk is `GradingSourceKind.ON_DISK`
    whatever the adapter, an absence is answered off the adapter: an adapter
    that grades from the task file (native) refuses one it has no block to
    read; an adapter that grades from a source the pack does not carry
    (external) pronounces on the absence through its own registered kind.

    The default answers `GradingSourceKind.UNINTERROGABLE` with `path=None`
    and a non-empty reason — the honest "the adapter has not declared its
    grading source". An adapter that resolves the block against its own run
    configuration overrides. See `NativeAdapter.grading_source` for the
    native reading — the shape any adapter's override here can consult.

    An instance method, unlike the four readers in items 13 - 16: `grading_source`
    resolves the source an adapter *actually reads* against its instance state
    (pack roots, project defaults), which a classmethod cannot see. The static
    gate `tolokaforge validate` reaches the source through a dispatch helper
    that handles the not-installed arm without needing an instance.

19. `emit_runner_grading_payload(task_id: str) -> dict[str, Any]`

    The payload this adapter emits for the runner's `RunnerGradingConfig`
    construction. The default is `{}` — the runner falls through to the
    historical dispatch. Adapters that grade by their own registered kind
    (`test_execution`, `preference_pair`, …) override to return the payload
    their grader reads at trial time.

20. `preferred_grader_kind() -> str`

    The registered `tolokaforge.grader_kinds` key this adapter prefers.  The
    default is `"composite"`, the shipped default kind. Adapters shipping
    their own kind override to return its registered name.

### Capability flags

Three `ClassVar[bool]` slots declare adapter-level runtime facts callers
otherwise infer from adapter identity. Each default is `False` on
`BaseAdapter`; an adapter that answers `True` for one flips it on the class:

- `requires_docker_cli_in_runner: ClassVar[bool] = False` — the runner needs
  the host Docker CLI to grade this adapter's trials. Adapters whose grading
  runs commands against Docker daemons (build/exec) flip this to `True`; the
  harness reads the flag to decide whether the runner stack must carry a
  Docker socket bind.
- `grades_from_task_grading_file: ClassVar[bool] = False` — this adapter
  grades from the `grading:` block the task names on disk. Native-shaped
  adapters flip this to `True`; external adapters that synthesise grading
  config from their own fixtures leave it `False`. The `grading:` presence
  gate reads the flag to decide whether an absent block is a refusal or an
  unchecked pronouncement.
- `syncs_adapter_env_to_state: ClassVar[bool] = False` — the conductor
  should sync `AdapterEnvironment` into runner state. Adapters whose
  environment holds runtime facts the runner reads back into `TrialState`
  (Tau-family) flip this to `True`; adapters whose runner owns the state
  end-to-end leave it `False`.

### AdapterGradingContract

The Protocol at `tolokaforge.adapters.AdapterGradingContract` names the six
grading methods (items 13 - 16, 18 - 20) plus the three capability flags as
one addressable contract. `BaseAdapter` satisfies it structurally through
the defaults documented above, so every adapter shipping today matches
without changing shape. External adapters can import it for a
`runtime_checkable` `isinstance` check or for a mypy contract; adoption is
optional — the harness reaches through the slots directly, not through the
Protocol.

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
never from the adapter's identity. Four capabilities matter to adapter authors:

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

### `SearchConfig.plane`

Which plane serves the task's `documents_path`:

- **`"typesense"`** — the runner registers a search client for the domain, and
  `search_policy` tools reach the collection the host-side indexer built.
- **`"rag_service"`** — rag-service indexes the corpus bundled in
  `tool_artifacts`, per trial. Set `enabled: true` alongside it; that flag is
  what gates the indexing.
- **`None`** *(default)* — the adapter did not declare one, and the runner
  derives `typesense` from a task that carries `host` / `port` / `api_key`.

The address is the stack's, not the task's: the runner container is created
knowing `TYPESENSE_HOST` / `TYPESENSE_PORT`, so an adapter has no Docker
networking to reason about and should emit no connection details at all.

**Migrate in this order: declare `plane` first, drop the connection details
second.** Reversed, an adapter emits a task with a `documents_path`, no address
to derive a plane from, and no declaration — which the runner refuses at
`RegisterTrial` naming `search.plane`, because registering it would leave
`search_policy` with nothing behind it. Emitting both during the transition is
fine: a declared plane outranks anything derivable from an address.

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
