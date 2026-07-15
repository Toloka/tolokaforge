# 0013. `RuntimeBackend` owns per-trial RPC methods — collapse `DockerRunnerAdapter`

- **Status:** Accepted
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

The seam question is: **which of the two established seams should own these five methods?** Three options were on the table:

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

- **`RuntimeBackend` Protocol grows by five methods.** All take `trial_id: str` as their first positional argument. `register_trial`'s `default_tool_timeout_s` uses the explicit `DEFAULT_TOOL_TIMEOUT_S` default at every layer — no None-signals-use-default trick — so the two-layer defaults can't drift silently.
- **`DockerRunnerAdapter` shrinks to `execute()` + `tool_logs` bookkeeping and now depends on `RuntimeBackend` (not `RunnerClient`).** Its constructor takes `runtime: RuntimeBackend` and `execute()` calls `runtime.execute_tool(trial_id, ...)`. The adapter no longer touches `RunnerClient` — every path from `TrialRunner` down flows through the `RuntimeBackend` seam. A rename to `DockerToolExecutor` could follow in a later PR; kept out of scope for this ADR to minimise churn.
- **`InMemoryRuntimeBackend` gains the five methods.** They raise `NotImplementedError` matching the existing `_UnusableExecutorClient` pattern — the in-memory backend is for lifecycle-and-provisioning testing, not RPC testing.
- **`RuntimeBackend.executor_client` is removed.** No production caller reads it after this PR — `DockerRunnerAdapter` routes tool execution through `RuntimeBackend.execute_tool` — so the field, the `_UnusableExecutorClient` stub in `runtime.py`, and the `SharedStackRuntimeBackend.executor_client` alias are all deleted. The two tests that asserted on the stub's `NotImplementedError` shape go with them; the equivalent guarantee is now pinned by `test_per_trial_rpc_methods_raise_not_implemented` on `InMemoryRuntimeBackend` directly.
- **`Conductor.run()` bodies get simpler.** Instead of `DockerRunnerAdapter(runner_client=runtime.executor_client, trial_id=…).register_trial(spec_json)`, the body reads `runtime.register_trial(trial_id, spec_json)`. One less object; the seam is visible at the call site.
- **Contract tests widen.** `tests/canonical/test_runtime_backend_contract.py` pins the five new methods; `tests/canonical/test_runner_client_contract.py` (from ADR-0011 landing) still pins the `RunnerClient` seven-method surface — the two Protocols are now genuinely different.

## Follow-ups

- Consider renaming `DockerRunnerAdapter` → `DockerToolExecutor` in a documentation-only PR, now that its purpose is narrower.
- Tighten the pre-existing broad `except Exception` in `InProcessConductor.run()`'s `get_state` fallback (`conductor.py:568`) to specific exception types once we have evidence of what the RPC path actually raises under load. Out of scope for this ADR — pre-existing defensive shape.

## Status transition

- **Proposed** on 2026-07-02 alongside the implementation PR that widens `RuntimeBackend` and shrinks `DockerRunnerAdapter`.
- **Accepted** on 2026-07-15 — the implementation shipped in #141 (the five per-trial RPC methods moved onto `RuntimeBackend`) and #148 (`PerTrialRuntimeBackend`), and a release cycle passed with no fresh test breakage traceable to the new Protocol surface.

## Links

- Public issue: GitHub #106.
- Related ADRs: 0007 (`RuntimeBackend` Protocol), 0008 (`Conductor` Protocol), 0010 (provisioning contract), 0011 (seam-definition + data-declaration conventions).
- Predecessor PR: #135 (`RunnerClient` promoted to Protocol) — made this ADR expressible without a per-backend `RunnerClient` type.
