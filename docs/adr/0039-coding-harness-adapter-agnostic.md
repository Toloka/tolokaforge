# 0039. Coding-harness as an adapter-agnostic run-config concept

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Related:**
  - [ADR-0033](0033-external-harness-registry.md) — the `HarnessSpec` field
    list and the operator overlay. Unchanged here; this ADR consumes it.
  - [ADR-0034](0034-external-harness-plugin-discovery.md) — the entry-point
    plug-in discovery contract. Unchanged; the mixin threads
    `plugin_discovery` through so opt-in adapters compose the same three
    layers.
  - [ADR-0036](0036-tolokaforge-coding-harnesses-split.md) — why the mixin
    lives in `tolokaforge_coding_harnesses/` rather than in the engine.
  - [ADR-0037](0037-runtime-gateway-as-harness-data.md) — the two paths
    (`gateway_route` on the spec, `harness_presets_file` overlay) both
    survive the lift because they live on the spec, not on the entry
    point.

## Context and Problem Statement

The coding-harness surface — `HarnessSpec` registry, `install-harness.sh`,
`middleware_proxy.py`, `container_injection.py` — ships as a general
component in [`tolokaforge_coding_harnesses/`](../../tolokaforge_coding_harnesses/)
([ADR-0036](0036-tolokaforge-coding-harnesses-split.md)). But its **entry
point** — how a task-pack author declares "run this trial under
`claude-code`" — was a bespoke field on one adapter's params bag:
`evaluation.harness_adapter.params.agent_harness` on the terminal-bench
adapter. A run-config author declaring the model in one place had to
declare the harness somewhere else, on a knob whose address was owned
by a specific adapter.

Three forces made the address wrong:

- **The engine's dispatch is already adapter-agnostic.**
  `Conductor._run_agent_loop` branches on
  `spec.task.metadata["agent_harness_command"]` with no adapter-identity
  check. `DockerComposeExecToolWrapper` reads `service` and
  `compose_project_prefix` off `ToolSource.extra`; it knows nothing
  about terminal-bench. `RunnerGradingConfig.grading_method` is a
  `Literal` the runner dispatches on, again without adapter identity.
  The engine treats "run a coding-harness CLI" as a shape a trial can
  arrive in; only the *entry* to that shape was single-adapter.
- **A second adopter had no address.** A native task pack that wanted
  its trial driven by `claude-code` had no way to say so — the harness
  selector lived on terminal-bench's params bag, so declaring it on a
  native run either got ignored or emitted a "unknown param" error,
  depending on how strictly the run config's schema was parsed.
- **The model choice and the harness choice belong together.** A
  benchmark result depends on *which model ran under which scaffold*;
  the run config already declares the model on `models.agent.name`, and
  the harness is a co-equal choice recorded on the same artifact key
  (`agent_harness_version` alongside `agent_harness_model`). Two
  addresses for one decision leaked the coupling into every consumer.

## Decision Drivers

- **The task author's mental model.** A run declares "which model on
  which scaffold" in one place, alongside provider and temperature.
- **Adapter authors compose, don't fork.** An adapter opts into
  harness mode by inheriting one mixin — no engine edit, no branch on
  adapter identity anywhere in the engine.
- **Package-boundary invariant unchanged.** The mixin lives in
  `tolokaforge_coding_harnesses/`, which imports no engine module
  (`tests/unit/test_package_boundary.py` refuses regressions). An
  external runtime that reads the registry can also inherit the mixin
  without pulling the engine in.
- **Backward compatibility for one release.** Existing run configs
  under `evaluation.harness_adapter.params.{agent_harness,agent_model}`
  keep working; the lift happens at parse time with a
  `DeprecationWarning` per key naming the canonical location.
- **Fail-loud gate.** A run declaring `models.agent.coding_harness` against an
  adapter that has not opted in refuses before any container work,
  naming both sides of the mismatch and the accepted set — the
  operator's next action is a one-line config edit.

## Considered Options

1. **Add `harness` to `ModelConfig` + extract a mixin any adapter can
   inherit.** Selected. Task authors write the harness alongside the
   model; adapters opt in with one inheritance edit and inherit six
   helpers plus the capability flag. Byte-identical wire output for the
   shipped terminal-bench path is provable via canonical replay.

2. **Keep the field on `harness_adapter.params` and register aliases
   per adapter.** Rejected. The knob's home would still be
   adapter-specific — each opting-in adapter would have to re-declare
   the field, and a task pack switching adapters would edit two
   locations. This is precisely the coupling the ADR retires.

3. **A peer top-level block — e.g. `agent.harness` alongside
   `agent.model`, `agent.temperature`.** Rejected. `ModelConfig` already
   carries provider, name, temperature, and `max_tokens` — the fields
   that together describe *how the LLM turn runs*. `harness` is
   another such field; splitting it into a peer block would leave two
   half-populated addresses.

4. **A capability enum instead of a boolean.** Rejected. There is one
   shape of opt-in today: an adapter either provides the four harness
   wire artefacts (metadata handshake, bash tool schema,
   `test_execution` grading payload, image layer) or it does not. A
   future adapter with a partial subset can add a second flag when the
   case exists — the boolean stays valid.

## Decision

Adopt **Option 1** — `ModelConfig.harness` as the canonical home, and
`CodingHarnessAdapterMixin` in `tolokaforge_coding_harnesses/` as the
adapter-side opt-in.

### `ModelConfig.harness`

`harness: str | None = None` on
[`tolokaforge/core/models/model_config.py`](../../tolokaforge/core/models/model_config.py).
`None` (the default) runs the engine's own LLM turn loop; a non-empty
string is a coding-harness name resolved against the effective registry
([ADR-0033](0033-external-harness-registry.md)) at adapter time.
`ModelConfig.name` is the same field the engine loop reads for the
model — the CLI consumes exactly that string, with the vendor-prefix
handling declared per-harness in
[`data/harnesses.yaml`](../../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml).

### `CodingHarnessAdapterMixin`

Lives at
[`tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/adapter_support.py`](../../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/adapter_support.py).
Six helpers, plus the capability flag:

| Helper | Contract |
|---|---|
| `supports_coding_harness: ClassVar[bool] = True` | Adapter capability flag. The orchestrator's config gate refuses `models.agent.coding_harness` against any adapter whose flag reads `False` (the `BaseAdapter` default). |
| `resolve_harness_spec(agent_harness, agent_model, provider_env=None, presets_file=None, plugin_discovery=True) -> HarnessSpec` | Composes the shipped catalog + operator overlay + installed plug-in bundles ([ADR-0033](0033-external-harness-registry.md) § "Registry composition"), then validates. Refuses unknown harness names and empty models. |
| `build_harness_command(agent_harness, spec, instruction, model, provider_env=None, *, path_resolver=None) -> str` | Wraps `harness_command()`. Assembles the argv, model routing, provider-env, and (if declared) middleware-proxy preamble into one `bash -c`-shaped string. |
| `emit_harness_metadata(agent_harness, spec, command, model) -> dict` | The four-key handshake the conductor branches on: `agent_harness`, `agent_harness_version`, `agent_harness_model`, `agent_harness_command`. |
| `emit_harness_tool_schema(*, service, compose_project_prefix, timeout_s, toolset="coding_harness") -> dict` | Payload for `ToolSchema(**payload)`: one `bash` tool routed at the trial container via `InvocationStyle.DOCKER_COMPOSE_EXEC`. `timeout_s` must cover the whole trial — the CLI runs to completion inside a single exec. |
| `emit_test_execution_grading() -> dict` | Payload for `RunnerGradingConfig(**payload)`: `grading_method="test_execution"`, `weights={"custom_checks": 1.0}`, `pass_threshold=0.5`. The runner reads reward from `/logs/verifier/reward.txt`. |
| `write_install_script_layer(context_dir, base_image, spec, middleware_proxy=False) -> str` | Materialises a standalone Dockerfile snippet + copies the shipped `install-harness.sh` (and the middleware proxy when the spec declares one) into `context_dir`. Returns the Dockerfile's relative path. Self-contained — the caller wraps compose-specific plumbing (`.dockerignore`, nested contexts) around it. |

### Payload dicts, not engine types

`emit_harness_tool_schema` and `emit_test_execution_grading` return
**payload dicts** rather than `ToolSchema` / `RunnerGradingConfig`
instances. The adapter constructs the engine types at its own call site
(`ToolSchema(**payload)`). This preserves the package-boundary invariant
— `tolokaforge_coding_harnesses/` imports nothing under `tolokaforge/`,
so `tests/unit/test_package_boundary.py` stays green — and localises a
future engine-model schema change to the adapter's construction line,
not inside the mixin. Payload shapes mirror the pydantic fields
one-for-one; pydantic v2 reconstructs nested types (`ToolSource` from
`source={…}`) transparently.

### Config-validation gate at run start

`Orchestrator.load_tasks` reads `agent_model_config.harness`. When set,
it reads `getattr(self.adapter, "supports_coding_harness", False)` on
the resolved adapter and refuses the run with a message naming both
sides of the mismatch and the currently-opted-in set (today: `native`,
`terminal_bench`). The refusal happens before any container work is
started, so no image build or compose bring-up ever runs against a
mismatched pair.

### State-checks composability

Coding-harness trials compose with **any** grading method. Two paths:

- **`test_execution`** (the mixin's default). The runner short-circuits
  at `service.py:1802` — reads the reward float from
  `/logs/verifier/reward.txt` and returns before assembling any
  jsonpath state. Terminal-bench's byte-identical replay is naturally
  preserved: no filesystem snapshot runs.
- **`state_checks` / `transcript` / `rubric`** (or any combination
  through the standard combiner). The runner assembles the trial's
  jsonpath state via `_assemble_jsonpath_state`. When the trial's
  metadata carries both `agent_harness_command` and `agent_visible_dir`
  AND a `DockerComposeExecToolWrapper` is registered for the trial,
  `_read_filesystem_for_state` snapshots the container's agent-visible
  directory into `state["filesystem"]` via
  [`tolokaforge/runner/harness_state.py`](../../tolokaforge/runner/harness_state.py).
  A `state_checks` assertion against `$.filesystem['/work/factorial.py']`
  resolves against what the CLI actually left behind.

The shipped adopters set the agent-visible dir as an adapter convention:
`/work` for the native adapter, `/app` for terminal-bench.

### Backward compatibility

The pre-lift shape —
`evaluation.harness_adapter.params.{agent_harness,agent_model}` —
still parses. `RunConfig._lift_harness_adapter_params_aliases` runs at
parse time, lifts each key into `models.agent.{harness,name}`, emits
one `DeprecationWarning` per key naming the canonical home, and drops
the legacy entry from `params` so downstream reads route through one
address. A collision between an equal value on both sides warns once;
differing values raise `ValueError` naming both keys.

Removal target: the next scheduled major-version bump that ships after
this ADR lands. The `DeprecationWarning` is the operator's advance
notice; the git log for `RunConfig._lift_harness_adapter_params_aliases`
is the removal record when it lands.

## Consequences

### Positive

- **One address per decision.** A task pack switching harnesses (or
  matrixing across them) edits `models.agent.coding_harness` in one file. A
  task pack switching adapters keeps `models.agent.coding_harness` untouched.
- **Adapter opt-in is one inheritance line.** Any adapter that inherits
  `CodingHarnessAdapterMixin` alongside `BaseAdapter` gets the six
  helpers and the capability flag; no engine edit is required to
  register a new opt-in.
- **State-checks composability shipped.** A native pack with a JSONPath
  assertion against `$.filesystem['/work/…']` grades correctly under
  harness mode today — `test_execution` is the default but not the
  only option.
- **Wire output is byte-identical for the terminal-bench path.** The
  canonical replay at
  `tests/canonical/snapshots/tbench_echo_hello_harness/` proves it.
- **Boundary invariant unchanged.** The mixin's payload-dict return
  convention means `tolokaforge_coding_harnesses/` still imports no
  engine module, so an external runtime that reads the registry can
  inherit the mixin without pulling the engine in.

### Negative / Trade-offs

- **A second address exists for one release** — the legacy
  `harness_adapter.params.{agent_harness,agent_model}` still parses.
  The trade-off is a deliberate deprecation window; a run using the
  old shape sees one `DeprecationWarning` per key naming the new home.
- **`agent_visible_dir` is an adapter convention, not a task-declared
  field.** Native pins `/work`, terminal-bench pins `/app`. A task pack
  cannot yet override it. If a future harness adapter surfaces where
  the trial's agent-visible dir varies per task, this becomes a
  task-level field — but the current shape carries no evidence of that
  need.
- **The capability flag is a boolean.** A future adapter that opts into
  a subset of harness mode (e.g. gateway-provisioning only, no image
  layer) needs a richer signal. The boolean is honest today and
  extends cleanly when the case surfaces.

### Follow-ups

- **`migration-bench` adopter.** The private-repo migration-bench
  adapter is the natural next opt-in; adopting the mixin there is a
  one-PR change in `tolokaforge-tools`.
- **Operator UX polish.** The `tolokaforge adapters list` (or
  equivalent) subcommand could grow a `supports_coding_harness`
  column; the gate's error message could then cite it. Deferred as UX,
  not a correctness gap.
- **Rename the harness entry-point group** — the group name is still
  `tolokaforge_adapter_terminal_bench.harness_registries` for the same
  reason [ADR-0036 § "The entry-point group name is a retained
  compatibility artefact"](0036-tolokaforge-coding-harnesses-split.md#the-entry-point-group-name-is-a-retained-compatibility-artefact)
  documents. Renaming it becomes cleaner once the adapter-agnostic
  entry-point is the only visible surface; not gated on this ADR.

## Links

- Related ADRs:
  - [ADR-0033](0033-external-harness-registry.md) — `HarnessSpec` and
    the operator overlay; unchanged.
  - [ADR-0034](0034-external-harness-plugin-discovery.md) — plug-in
    entry-point discovery; unchanged.
  - [ADR-0036](0036-tolokaforge-coding-harnesses-split.md) — the
    package hoist that made the mixin's address possible.
  - [ADR-0037](0037-runtime-gateway-as-harness-data.md) — gateway
    routing on the spec, orthogonal to this ADR.
- Related code:
  - [`tolokaforge/core/models/model_config.py`](../../tolokaforge/core/models/model_config.py)
    — `ModelConfig.harness`.
  - [`tolokaforge/core/models/run_config.py`](../../tolokaforge/core/models/run_config.py)
    — `_lift_harness_adapter_params_aliases`.
  - [`tolokaforge/core/orchestrator.py`](../../tolokaforge/core/orchestrator.py)
    — the config-validation gate in `load_tasks`.
  - [`tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/adapter_support.py`](../../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/adapter_support.py)
    — the mixin.
  - [`tolokaforge/adapters/native.py`](../../tolokaforge/adapters/native.py),
    [`external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py`](../../external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py)
    — the two shipped adopters.
  - [`tolokaforge/runner/harness_state.py`](../../tolokaforge/runner/harness_state.py)
    — the container-filesystem snapshot for non-`test_execution`
    grading composability.
  - [`examples/native/coding_harness/`](../../examples/native/coding_harness/),
    [`examples/terminal_bench/`](../../examples/terminal_bench/) — the
    two shipped example packs.
