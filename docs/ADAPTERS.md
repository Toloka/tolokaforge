# Adapter Known Issues & Audit Log

This document tracks known issues, bugs found during evaluation runs, and their
resolution status.  Organised by adapter; cross-cutting harness issues are in
their own section.

For adapter architecture and interface contracts, see
[ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) and
[ADAPTER_INTERFACE.md](ADAPTER_INTERFACE.md). The grading-side contract every
adapter satisfies — the four readers, two emit seams, and three capability
flags — is `AdapterGradingContract` in
[ADAPTER_INTERFACE.md § AdapterGradingContract](ADAPTER_INTERFACE.md#adaptergradingcontract).

---

## Installing adapters — opt-in, never by default

The default install is the engine loop only:

```bash
pip install tolokaforge          # engine loop; no other-harness dependencies
```

Adapters for other harnesses ship as separate out-of-tree packages, installed
only through extras. Installing one never changes the engine; it just makes that
adapter discoverable (via the `tolokaforge.adapters` entry point):

```bash
pip install "tolokaforge[terminal_bench]"   # Terminal-Bench tasks
pip install "tolokaforge[inspect_ai]"       # Inspect AI tasks
pip install "tolokaforge[harbor]"           # Harbor harness (harbor run + Terminus 2)
pip install "tolokaforge[adapters]"         # all shipped adapters
```

`pip install tolokaforge` must never pull in an adapter package or a third-party
harness distribution — that boundary is enforced by
`tests/canonical/test_default_install_opt_in_boundary.py`. We do not overflow the
engine's dependencies with other harnesses by default.

---

## Execution modes

Every trial runs in one of two shapes, named by `ExecutionMode` in
`tolokaforge/core/execution_mode.py`:

- **`ENGINE_LOOP`** — the engine's own LLM turn loop drives the agent. This
  is the default mode an adapter declares, not one every adapter runs: a
  delegated-only adapter replaces it.
- **`DELEGATED`** — the task brings its own agent (a coding-harness CLI named
  on `TaskDescription.metadata["agent_harness_command"]`); the engine
  provisions and grades the trial but does not run the turn loop.

`select_execution_mode(metadata)` classifies a trial at dispatch time: a
non-blank `agent_harness_command` selects `DELEGATED`, its absence selects
`ENGINE_LOOP`, and a present-but-blank or non-string command is a broken
adapter and raises. The mode is classified, never stored — it is written into
no metadata or wire artefact, so canonical snapshots are unaffected.

An adapter declares which modes it runs through the
`supported_execution_modes: ClassVar[frozenset[ExecutionMode]]` capability on
`BaseAdapter` (default `{ENGINE_LOOP}`). Before any container work, the
orchestrator refuses a run that selects `DELEGATED` against an adapter whose
capability does not include it, naming both sides and the adapter's accepted
modes. `supports_coding_harness = True` is retained as a back-compat surface
for one release: an external adapter that sets only that legacy flag is
treated as also running `DELEGATED` (see
`adapter_supported_modes()` in `tolokaforge/core/orchestrator.py`).

### How to add a harness / delegated adapter

Two shapes exist. An adapter that runs a vendor coding-agent CLI from the
registry inherits the mixin (step 1). An adapter that delegates to an external
harness by emitting its own command skips the mixin and declares its mode
directly (step 2), supplying its own command assembly and grading.

1. **Registry CLI: inherit the mixin.** Add `CodingHarnessAdapterMixin`
   (`tolokaforge_coding_harnesses.adapter_support`) alongside `BaseAdapter`.
   It supplies the six wire-artefact helpers — registry resolution, command
   assembly, the four-key metadata handshake, the `bash` tool schema, the
   `test_execution` grading payload, and the install-script Dockerfile layer —
   and keeps `supports_coding_harness = True`. The mixin imports no engine
   module, so the package boundary stays intact. A delegated adapter that owns
   its environment and emits its own command does not use the mixin.
2. **Declare the mode.** Override `supported_execution_modes` on the
   engine-facing adapter class so the orchestrator gate lets the run through —
   `frozenset({ExecutionMode.ENGINE_LOOP, ExecutionMode.DELEGATED})` for a
   registry adapter, or `frozenset({ExecutionMode.DELEGATED})` for a
   delegated-only adapter. (`ExecutionMode` lives engine-side only — never
   import it into the coding-harnesses package.)
3. **Emit the handshake.** In `to_task_description`, when a harness is
   selected, emit `agent_harness_command` (and the sibling `agent_harness*`
   keys) via the mixin helpers, register the single `bash` agent tool, and
   route grading through `emit_test_execution_grading` (or compose with any
   grading method — see [ADR-0039](adr/0039-coding-harness-adapter-agnostic.md)
   § "State-checks composability").
4. **Lock it.** Subclass `AdapterGradingContractSuite`
   (`tolokaforge.testing.adapters`) and set
   `expected_supported_execution_modes` to the set your adapter declares.

`NativeAdapter` (the engine's own loop) and `TerminalBenchAdapter` ship in
the tree; additional delegated-harness adapters install through their extras.

---

## `harbor` — HarborAdapter (delegated, external plugin)

Opt-in plugin from the `tolokaforge-adapter-harbor` distribution
(`pip install "tolokaforge[harbor]"`); selected via
`evaluation.harness_adapter.type: harbor`. The **delegated** kind (step 2 above,
not the registry mixin): it declares `supported_execution_modes =
{DELEGATED}` directly, owns its own environment, and emits its own command.

Runs Terminal-Bench 2.0 task packs by delegating execution to the real Harbor
harness — the trial's agent step is a single `harbor run -p /app/task -a
terminus-2 -m <model> -e docker --jobs-dir <host-identity-path> --job-name
tf-<trial> --agent-setup-timeout-multiplier 10 -k 1 -y`, and Harbor drives its
own Terminus 2 agent + verifier inside a sandbox it builds via
Docker-out-of-Docker. The job name is unique per trial, so concurrent trials of
one task do not collide. Grading is `test_execution`: a generated `tests/test.sh`
reads `verifier_result.rewards.reward` from this trial's exact Harbor
`result.json`, and — because `harbor run` exits 0 even when the agent never
started — surfaces an infra-failure (recorded exception, missing/off-schema
result, no verifier result) as a **grading error** rather than a `0.0` score.
This is **delegation, not task-reuse** — to run the same TB2 pack on
tolokaforge's own loop, use the `terminal_bench` adapter instead.

Because Harbor owns the sandbox, tolokaforge's per-trial `TaskIsolation`,
spend-cap, and crash-restart guarantees do **not** apply inside Harbor's run, and
the DooD sibling containers Harbor spins up are cleaned up by Harbor (a Harbor
crash can orphan them on the host). See the package
[`README.md`](../external_adapters/tolokaforge-adapter-harbor/README.md) §
Forfeitures and the examples under [`examples/harbor/`](../examples/harbor/).

### Open Issues

No issues found. The keyless `oracle` end-to-end path (agent image build → `harbor
run -a oracle` over the mounted socket → `result.json` → `test_execution`) is
covered by the adapter's integration test.

---

## `frozen_mcp_core` — FrozenMcpCoreAdapter

Entry-point plugin registered by the `tolokaforge-tools` distribution (not
present in this repository); selected via
`evaluation.harness_adapter.type: frozen_mcp_core`. Serves converted/frozen
`tlk_mcp_core` task packs: loads tools from a bundled `_domain/` directory
and handles DB creation, tool wrapping, and stable hash grading.

### Fixed Issues

#### 1. Empty tool schemas sent to the LLM (CRITICAL)

| Field       | Value |
|-------------|-------|
| **Status**  | ✅ Fixed |
| **File**    | `tolokaforge/adapters/frozen_mcp_core.py` — `to_task_description()` |
| **Symptom** | Agent calls tools with `arguments: {}` repeatedly, gets `"Field required"` errors. 33% pass rate instead of expected ~100%. |
| **Root cause** | `to_task_description()` built `ToolSchema` objects with hardcoded empty parameters (`{"type": "object", "properties": {}}`) and generic descriptions (`"Frozen tool: {name}"`). The converted `fixtures/tools.json` — which contains correct parameter schemas — was never loaded. The empty schemas propagated through gRPC `RegisterTrial` → `RegisterTrialResponse.tool_schemas` → orchestrator `tool_schemas` → LLM. |
| **Fix** | Load `fixtures/tools.json` from the task directory and use the actual `description` and `parameters` for each tool. Falls back to empty schema only when the file is absent. |

#### 2. Stale diagnostic state in `env.yaml` (SIGNIFICANT)

| Field       | Value |
|-------------|-------|
| **Status**  | ✅ Fixed |
| **File**    | `tolokaforge/core/orchestrator.py` — `_run_trial()` post-trial state sync |
| **Symptom** | All trials write identical `env.yaml` showing only the initial state. Actual tool-induced changes are invisible in post-mortem diagnostics. |
| **Root cause** | After trial execution, the orchestrator synced `adapter_env.data` — a snapshot taken during `create_environment()`. In Docker mode, tool execution happens through the Runner's DB service, so the adapter's local `InMemoryDatabase` never reflects actual changes. Additionally, `create_environment()` stores its DB at `self._db_instances[task_id]` (keyed by task ID, not trial ID), so concurrent trials on the same task overwrite each other's DB reference. |
| **Fix** | After trial execution in Docker mode, fetch the actual post-trial state from the Runner's DB service via `shared_stack_runtime.get_state(trial_id)`. Falls back to adapter data if the RPC fails. Non-Docker mode still uses adapter data directly. |

### Open Issues (Not Fixed)

#### 3. Unstable fields may be incomplete for some tasks

| Field       | Value |
|-------------|-------|
| **Status**  | ⚠️ Open (task-level, not harness) |
| **Symptom** | Trial 2 grade shows `"zendesk_tickets: 0 missing, 0 extra, 1 different"` — the ticket was created but a field differs. |
| **Analysis** | `fixtures/unstable_fields.json` marks `zendesk_tickets.subject` and `zendesk_tickets.description` as unstable (llm_generated), plus various timestamp fields. However, auto-generated IDs and other LLM-influenced fields may not be fully covered. This is a task authoring concern, not a harness bug — each task pack should ensure its unstable fields list is comprehensive. |
| **Recommendation** | The conversion pipeline (`tolokaforge adapter convert`) should surface a warning when grading fails due to differences in fields that look auto-generated (e.g., match `id` patterns). |

#### 4. TypeSense stub warning in orchestrator process

| Field       | Value |
|-------------|-------|
| **Status**  | ⚠️ Open (cosmetic) |
| **Symptom** | `mcp_core not available - TypeSense will use stub implementation` warning appears twice (once per worker) during adapter initialization. |
| **Analysis** | The orchestrator process cannot import `mcp_core` because it's only available inside the Runner container via bundled artifacts. TypeSense search works correctly inside the Runner. The warning is harmless but confusing. |
| **Recommendation** | Suppress or downgrade the warning when TypeSense will be used via the Runner, not locally. |

#### 5. `"Failed to initialize json-db service"` + `"Failed to sync json-db state"` warnings

| Field       | Value |
|-------------|-------|
| **Status**  | ⚠️ Open (cosmetic noise) |
| **File**    | `tolokaforge/core/orchestrator.py` — json-db init/sync blocks |
| **Symptom** | Warnings appear for every trial of frozen_mcp_core tasks. |
| **Analysis** | The orchestrator attempts to connect to `http://localhost:8000` for json-db, but the Docker-exposed DB service port is auto-allocated (e.g., 45033). The Runner correctly connects via Docker networking (`db-service:8000`). For frozen tasks, DB initialization actually happens through the Runner's `RegisterTrial` → `init_trial()`. These warnings are noise for Docker-mode adapter tasks. |
| **Recommendation** | Skip the legacy json-db init/sync code path when the runtime is Docker and the adapter is not `NativeAdapter`. |

---

## `native` — NativeAdapter

Built-in adapter for file-based YAML tasks (`task.yaml` + `grading.yaml`);
the harness's default when the run config selects no other adapter.

### Source-less non-builtin tool guard

NativeAdapter emits `ToolSchema` objects with `source=None` for every enabled
tool name unless the actor block declares `tools.<actor>.mcp_server`. A
source-less schema is only resolvable at the runner when the tool name is in
the builtin registry, so the harness raises in two layers:

- **Emit time** (`NativeAdapter._actor_tool_schemas`) raises
  `NativeAdapterMisconfigurationError` (a `ValueError` subclass) before the
  schema leaves the harness. The message names the offending tool and the
  pack root, points at `evaluation.harness_adapter.type` as the run-config
  key to override, and enumerates the registered non-native adapters.
- **Runner-side fallback** (`ToolFactory._create_wrapper`) raises
  `ToolConfigurationError` for any schema that still reaches the runner with
  `source=None` and a non-builtin name — e.g. from a plugin adapter that
  also emits source-less schemas. The message points at
  `tools.<actor>.mcp_server` and enumerates the registered adapter names.

When the pack root or an ancestor within three directory levels contains a
`_domain/tools/<name>` directory, the emit-time message appends a
`detected shape: _domain/tools/<name>` clause. That layout is the common
shape of a converted MCP task pack whose run config forgot to override the
harness adapter; the clause is a generic filesystem-pattern hint, never a
hardcoded pack-name check.

### Open Issues

No issues found during this evaluation run.  The `native` adapter was not
exercised in the frozen retail evaluation.

---

## `tau` — TauAdapter (external plugin)

External adapter for TAU environment tasks.  Registered via entry-point
`tolokaforge_adapter_tau`.

### Open Issues

No issues found during this evaluation run.  The `tau` adapter was not
exercised in the frozen retail evaluation.

---

## `tlk_mcp_core` — TlkMcpCoreAdapter (external plugin)

External adapter that runs live `mcp_core` tools against the source
`mcp-tools-library`.  Registered via entry-point
`tolokaforge_adapter_tlk_mcp_core`.

### Open Issues

No issues found during this evaluation run.  The live `tlk_mcp_core` adapter
was not exercised — the frozen variant (`frozen_mcp_core`) was used instead.

---

## Cross-Cutting Harness Issues

Issues that affect all adapters or the harness infrastructure.

### Fixed Issues

#### Docker container cleanup crash

| Field       | Value |
|-------------|-------|
| **Status**  | ✅ Fixed |
| **File**    | `tolokaforge/docker/container.py` — `Container.destroy()` |
| **Symptom** | `Failed to destroy container for 'db-service': Container.destroy() got an unexpected keyword argument 'remove_volumes'` |
| **Root cause** | `ServiceStack.destroy()` in `stack.py` called `container.destroy(remove_volumes=remove_volumes)`, but `Container.destroy()` accepted no keyword arguments. |
| **Fix** | Added `remove_volumes: bool = False` keyword argument to `Container.destroy()` and passes it as `v=remove_volumes` to the Docker SDK's `docker_container.remove()`. |

#### Docker network cleanup race

| Field       | Value |
|-------------|-------|
| **Status**  | ✅ Fixed |
| **File**    | `tolokaforge/core/orchestrator.py` — cleanup section of `run()` |
| **Symptom** | `Failed to remove network 'runner-net': network runner-net has active endpoints` |
| **Root cause** | Cleanup order was: `service_stack.destroy()` (tries to remove `runner-net`) → `_typesense_server.stop()` (removes TypeSense from `runner-net`). Since TypeSense was still attached to `runner-net` when the stack tried to remove it, removal failed. |
| **Fix** | Swapped cleanup order: stop TypeSense server first (disconnects it from `runner-net`), then destroy the service stack. |

### Open Issues

#### `state_diff` not propagated to `grade.yaml`

| Field       | Value |
|-------------|-------|
| **Status**  | ✅ Fixed |
| **File**    | `tolokaforge/core/orchestrator.py` — grade construction in `_run_trial()` |
| **Symptom** | `grade.yaml` always shows `state_diff: null` even when the grading RPC computes a detailed diff (e.g., "1 different in table X"). Makes post-mortem debugging of grading mismatches impossible without re-running. |
| **Root cause** | The Runner's `GradeTrial` RPC returns `state_diff_json` in the Grade proto, and `shared_stack_runtime.grade_trial()` extracts it to `g["state_diff_json"]`. But the orchestrator at `_run_trial()` line 1262 never parsed it — the `Grade(...)` constructor was not passed `state_diff`. |
| **Fix** | Parse `g["state_diff_json"]` via `json.loads()` and pass it as `state_diff=state_diff_parsed` to the `Grade` constructor. Now `grade.yaml` contains the full per-table diff (missing, extra, different records with field details). |

#### gRPC Runner health check takes ~20s on startup

| Field       | Value |
|-------------|-------|
| **Status**  | ⚠️ Open (minor) |
| **Symptom** | 20 consecutive `Health check failed: UNAVAILABLE: ipv4:127.0.0.1:37643: Socket closed` messages before the runner becomes ready. |
| **Analysis** | The Runner container takes 20 seconds to start the gRPC server.  The health check retries every ~1s with no backoff.  Not a bug, but noisy. |
| **Recommendation** | Add exponential backoff or increase initial delay for Runner health checks. |

### Judge preflight

Adapters with host-side judging override `requires_judge_model(task_id)`. The
orchestrator checks every selected task before scheduling any trials and refuses
a missing `models.judge`. The default implementation checks the task description
for the built-in `llm_judge` component.
