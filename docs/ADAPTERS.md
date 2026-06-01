# Adapter Known Issues & Audit Log

This document tracks known issues, bugs found during evaluation runs, and their
resolution status.  Organised by adapter; cross-cutting harness issues are in
their own section.

For adapter architecture and interface contracts, see
[ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) and
[ADAPTER_INTERFACE.md](ADAPTER_INTERFACE.md).

---

## `native` — NativeAdapter

Built-in adapter for file-based YAML tasks (`task.yaml` + `grading.yaml`).

### Open Issues

No issues recorded.

---

## `terminal_bench` — TerminalBenchAdapter

Built-in plugin for Docker-compose terminal tasks.

### Open Issues

No issues recorded.

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
| **Root cause** | The Runner's `GradeTrial` RPC returns `state_diff_json` in the Grade proto, and `docker_runtime.grade_trial()` extracts it to `g["state_diff_json"]`. But the orchestrator at `_run_trial()` never parsed it — the `Grade(...)` constructor was not passed `state_diff`. |
| **Fix** | Parse `g["state_diff_json"]` via `json.loads()` and pass it as `state_diff=state_diff_parsed` to the `Grade` constructor. Now `grade.yaml` contains the full per-table diff (missing, extra, different records with field details). |

#### gRPC Runner health check takes ~20s on startup

| Field       | Value |
|-------------|-------|
| **Status**  | ⚠️ Open (minor) |
| **Symptom** | 20 consecutive `Health check failed: UNAVAILABLE: ipv4:127.0.0.1:37643: Socket closed` messages before the runner becomes ready. |
| **Analysis** | The Runner container takes 20 seconds to start the gRPC server.  The health check retries every ~1s with no backoff.  Not a bug, but noisy. |
| **Recommendation** | Add exponential backoff or increase initial delay for Runner health checks. |
