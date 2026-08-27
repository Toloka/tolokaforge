# Adapter Known Issues & Audit Log

This document tracks known issues, bugs found during evaluation runs, and their
resolution status.  Organised by adapter; cross-cutting harness issues are in
their own section.

For adapter architecture and interface contracts, see
[ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) and
[ADAPTER_INTERFACE.md](ADAPTER_INTERFACE.md).

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
