# 0013. `RuntimeBackend` owns per-trial RPC methods — collapse `DockerRunnerAdapter`

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`DockerRunnerAdapter` (`tolokaforge/core/docker_adapter.py`) is a per-trial wrapper the `Conductor` constructs inside `run()`. It exposes seven methods — `register_trial`, `execute_tool`, `grade_trial`, `get_state`, `reset_trial`, `cleanup_trial`, `register_tools` — and six of the seven are pure trial_id-currying: they delegate straight to `RunnerClient` with `trial_id` bound. The seventh (`register_tools`) is deprecated.

That layer was pragmatic when it landed, but with ADR-0007 (`RuntimeBackend` Protocol), ADR-0008 (`Conductor` Protocol), and ADR-0010 (provisioning contract) in place, the seam picture is:

- `RuntimeBackend` owns run-level lifecycle + per-trial *provisioning* (bring up / tear down the environment for a trial) + a `cleanup_trial(trial_id)` method that already takes trial_id explicitly.
- `Conductor` owns the per-trial execution *body* (agent loop, grading, artifact write).
- Everything else — `register_trial`, `execute_tool`, `grade_trial`, `get_state`, `reset_trial` — is per-trial RPC work with no state of its own, currently hidden behind a wrapper class that exists purely to bind `trial_id`.

The seam question is: **which of the two established seams should own these five methods?** The ticket that surfaced this (TECHDEL-428) lists three options:

- **Option A** — `Conductor` owns them. Natural in the sense that `Conductor` is per-trial by construction.
- **Option B** — `RuntimeBackend` takes `trial_id` explicitly. The Runner-RPC surface becomes visible on the Protocol; no intermediate wrapper.
- **Option C** — Keep the adapter; just don't construct it inside `Conductor.run()`. Doesn't actually delete anything.

## Decision

**Option B, with a tool-executor carve-out.**

The five RPC methods land on `RuntimeBackend`:

- `register_trial(trial_id, trial_spec_json, default_tool_timeout_s) -> dict`
- `execute_tool(trial_id, tool_name, arguments, timeout_seconds, executor) -> ToolResult`
- `grade_trial(trial_id, llm_messages_json, grading_components) -> dict`
- `get_state(trial_id, include_unstable, tables) -> dict`
- `reset_trial(trial_id, execute_init_actions) -> dict`

`cleanup_trial(trial_id)` is already on `RuntimeBackend`; no change.

`DockerRunnerAdapter` does **not** disappear entirely. It shrinks to a pure `ToolExecutor` — the interface `TrialRunner` speaks — retaining only:

- `execute(tool_name, arguments, ...)` — the `ToolExecutor.execute` contract; forwards to `RuntimeBackend.execute_tool(trial_id, ...)`.
- `tool_logs` list + `get_logs()` / `clear_logs()` — the per-trial bookkeeping `TrialRunner.tool_executor.get_logs()` reads for the stuck-detector and per-trial metrics.

Every other adapter method (`register_trial`, `grade_trial`, `get_state`, `reset_trial`, `cleanup_trial`, `register_tools`) is deleted. Callers in `Conductor.run()` and the orchestrator switch to `runtime.method(trial_id, ...)` directly.

## Why not the alternatives

### Not Option A (Conductor)

`Conductor` is a Protocol whose implementations vary the *body* of a trial (`InProcessConductor`, `InMemoryConductor`, a future `RemoteConductor`). Requiring every `Conductor` implementation to also expose `register_trial` / `execute_tool` / etc. as public methods forces test doubles (`InMemoryConductor`) to fake the RPC surface just to satisfy the Protocol shape — even when the fake never registers a trial. This is exactly the "concrete leaks into the seam" smell ADR-0011 codifies against.

### Not Option C (adapter as-is)

Doesn't delete the class. Doesn't reduce the surface. Just moves the constructor call — the six delegate methods stay.

### Why "with a tool-executor carve-out" and not pure Option B

`DockerRunnerAdapter.execute()` is not a pure `RunnerClient.execute_tool` delegate. It also:

- Binds `executor` identity (`"agent"` vs `"user"`) — a per-instance property, not per-call.
- Appends to `self.tool_logs`, a per-trial list that `TrialRunner` reads via the `ToolExecutor.get_logs()` protocol for metrics and stuck-detection.

Moving both into `RuntimeBackend` would make the Protocol grow per-trial state (which `RuntimeBackend` doesn't have — it's a run-level object). Keeping a slim per-trial `ToolExecutor` that owns `tool_logs` and delegates `execute_tool` to `RuntimeBackend` is the honest split.

The `ToolExecutor` surface is already established (`tolokaforge.tools.registry`, `tolokaforge.tools.user_tools`) — this is just one more implementation of that established shape, not a new pattern.

## Consequences

- **`RuntimeBackend` Protocol grows by five methods.** All take `trial_id: str` as their first positional argument. Every backend implements them.
- **`DockerRunnerAdapter` shrinks to `execute()` + `tool_logs` bookkeeping.** Its docstring and class name-purpose narrow to "per-trial `ToolExecutor` for the docker runtime path." A rename could follow in a later PR; kept out of scope for this ADR to minimise churn.
- **`InMemoryRuntimeBackend` gains the five methods.** They raise `NotImplementedError` matching the existing `_UnusableExecutorClient` pattern — the in-memory backend is for lifecycle-and-provisioning testing, not RPC testing.
- **`RuntimeBackend.executor_client` remains** for now — `DockerRunnerAdapter.execute()` still needs an object to route `execute_tool` through. A follow-up ADR can retire it once every call site of `.execute()` on `DockerRunnerAdapter` is proven safe to switch to `runtime.execute_tool(trial_id, ...)` directly.
- **`Conductor.run()` bodies get simpler.** Instead of `DockerRunnerAdapter(runner_client=runtime.executor_client, trial_id=…).register_trial(spec_json)`, the body reads `runtime.register_trial(trial_id, spec_json)`. One less object; the seam is visible at the call site.
- **Contract tests widen.** `tests/canonical/test_runtime_backend_contract.py` pins the five new methods; `tests/canonical/test_runner_client_contract.py` (from ADR-0011 landing) still pins the `RunnerClient` seven-method surface — the two Protocols are now genuinely different.

## Follow-ups

- Remove `executor_client` from `RuntimeBackend` once `DockerRunnerAdapter.execute()` is proven safe to route through `runtime.execute_tool` directly (a small dedicated ticket — needs to verify no test or code path still reaches for adapter attributes tied to the underlying client).
- Consider renaming `DockerRunnerAdapter` → `DockerToolExecutor` in a documentation-only PR, now that its purpose is narrower.
- Delete `tests/unit/test_docker_adapter_cleanup_trial.py` in this PR (already dead once `cleanup_trial` disappears from the adapter's surface).

## Status transition

- **Proposed** on 2026-07-02 alongside the implementation PR that widens `RuntimeBackend` and shrinks `DockerRunnerAdapter`.
- **Accepted** once the implementation ships and one release cycle passes without a fresh test breakage traceable to the new Protocol surface.

## Links

- Ticket: internal (TECHDEL-428) — GitHub #106.
- Related ADRs: 0007 (`RuntimeBackend` Protocol), 0008 (`Conductor` Protocol), 0010 (provisioning contract), 0011 (seam-definition + data-declaration conventions).
- Predecessor PR: #135 (`RunnerClient` promoted to Protocol) — made this ADR expressible without a per-backend `RunnerClient` type.
