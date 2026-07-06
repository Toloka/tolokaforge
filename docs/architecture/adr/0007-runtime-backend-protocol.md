# 0007. `RuntimeBackend` Protocol — lift `SharedStackRuntimeBackend` behind a typed seam

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The seam-definition arc so far has typed the control↔trial wire format (`TrialSpec`/`TrialResult`, ADR-0003), both halves of the data plane (`TrialArtifactWriter` ADR-0004, `RunAggregateWriter` ADR-0005), and the trial-scoped service URLs on the wire (`EnvEndpoints`, ADR-0006). One seam remains: the abstraction the orchestrator uses to dispatch a trial to its execution environment.

Today `tolokaforge/core/orchestrator.py` imports `SharedStackRuntimeBackend` from `tolokaforge.core.shared_stack_runtime` concretely. `SharedStackRuntimeBackend` is misleadingly named — it isn't a Docker daemon client; it's a thin gRPC client wrapper that talks to a runner gRPC server. It exposes three lifecycle methods (`connect`, `close`, `health_check`) and one attribute (`executor_client: RunnerClient`) the orchestrator passes to `DockerRunnerAdapter` for per-trial operations. The retry-cleanup path also reaches through `executor_client.cleanup_trial(trial_id)` directly — that's the one call the orchestrator makes *before* a per-trial adapter exists.

There is no typed surface that says "this is how the orchestrator talks to its execution environment." A future where the runner is in a different process, a different machine, or a different runtime altogether (Firecracker, local subprocess, remote conductor) has no contract to plug into.

## Decision Drivers

- **Symmetry with the other Phase-1 seams.** Each plane has a `@runtime_checkable` Protocol with at least two implementations. The execution surface should match.
- **Lean code.** The orchestrator's two `SharedStackRuntimeBackend(...)` construction sites and one direct `executor_client.cleanup_trial` call can route through a Protocol without losing any behaviour.
- **Fail-fast.** A backend that doesn't satisfy the Protocol fails at instance creation, not deep in the retry path.
- **The seam is the precondition for the `Conductor` Protocol** (ADR-0008), which reads `RuntimeBackend` to know where to send a trial.

## Considered Options

1. **`RuntimeBackend` Protocol with `executor_client` exposed as the concrete `RunnerClient` type.** Smallest change that types the orchestrator's full dependency surface. Documented leak: the gRPC `RunnerClient` type is part of the seam.
2. **Two separate Protocols — `RuntimeBackend` (lifecycle) and `RunnerClient` (RPC surface).** Cleaner separation but doubles the Protocol-promotion work; the RPC surface has ~7 methods that would all need re-declaration.
3. **Just a thin wrapper class without Protocol.** Doesn't type the seam — the orchestrator still imports a concrete class.
4. **Defer until a second backend lands.** Same argument as ADR-0004/0005/0006: each later seam ends up re-litigating the runtime shape simultaneously with whatever it's actually doing.

## Decision

We will adopt **Option 1**.

- Add `RuntimeBackend` to `tolokaforge/core/runtime.py`. `@runtime_checkable` Pydantic-free `typing.Protocol`. Surface:
  - `connect(timeout, retry_interval) -> None`
  - `close() -> None`
  - `health_check() -> bool`
  - `cleanup_trial(trial_id) -> dict[str, Any]` — the one operation the orchestrator calls before a per-trial adapter exists.
  - `executor_client: RunnerClient` — the per-trial adapter handoff.
- Add `cleanup_trial(trial_id)` to `SharedStackRuntimeBackend`. One-line delegate to `self.runner_client.cleanup_trial(trial_id)`. `SharedStackRuntimeBackend` becomes a structural implementation of `RuntimeBackend` (no `class SharedStackRuntimeBackend(RuntimeBackend)` declaration — Python Protocols are duck-typed).
- Add `InMemoryRuntimeBackend` — a no-network implementation that records lifecycle and cleanup calls on a `RuntimeBackendCallLog` dataclass. Its `executor_client` is a stub whose attribute access raises `NotImplementedError` with a message pointing at `SharedStackRuntimeBackend` or `RunnerClient`-mocking as alternatives. Two purposes: (a) prove the seam is swappable; (b) serve as a test fixture for lifecycle-and-cleanup paths.
- `Orchestrator.__init__` accepts an optional `runtime_backend: RuntimeBackend | None = None` kwarg. When `None`, the orchestrator constructs `SharedStackRuntimeBackend(runner_address=...)` inline (the legacy behaviour). When provided, the orchestrator uses the injected backend.
- The `shared_stack_runtime` local variable in `run()` and `run_worker()` is type-annotated as `RuntimeBackend` (Protocol), not `SharedStackRuntimeBackend` (concrete).
- The one direct `shared_stack_runtime.executor_client.cleanup_trial(trial_id)` call in `_cleanup_runner_state_for_retry` becomes `shared_stack_runtime.cleanup_trial(trial_id)`.

## Consequences

### Positive

- The orchestrator depends on a Protocol, not a concrete class. The execution surface is now swappable without touching the orchestrator.
- The Phase-1 seam-definition arc is complete with this Protocol and the `Conductor` Protocol (ADR-0008). Every architectural seam in the engine has a typed contract with at least two implementations.
- `InMemoryRuntimeBackend` is reusable by any test that needs an orchestrator with no Docker dependency.
- Future plug-in discovery (entry-point-discovered backends) can rely on `isinstance(impl, RuntimeBackend)` for safe injection.

### Negative / Trade-offs

- The Protocol exposes the concrete `RunnerClient` type via `executor_client`. A backend that doesn't speak gRPC would either have to fake a `RunnerClient`-shape object (painful) or live behind a follow-up split into two Protocols (`RuntimeBackend` + `RunnerClient`). Acceptable today — every realistic near-term backend (Docker, remote conductor) speaks the same gRPC contract.
- `@runtime_checkable` for Protocols with attributes can be surprising — it only checks method presence, not attribute presence. A class with the four lifecycle methods but no `executor_client` will still pass `isinstance()`. Mitigation: contract tests assert `hasattr(impl, "executor_client")` separately; the docstring on `RuntimeBackend` flags the requirement.

### Follow-ups

- **Promote `RunnerClient` to its own Protocol** if a non-gRPC backend ever needs to fake the RPC surface without the gRPC stack.
- **Collapse `DockerRunnerAdapter` into per-trial Protocol methods.** The adapter is partial-application of `trial_id` over the runner RPC surface; if `RuntimeBackend` directly took `trial_id` parameters everywhere, the adapter could be deleted.
- **`LocalProcessRuntime` / Firecracker / remote-conductor backends.** Concrete alternative implementations of `RuntimeBackend`. User-driven follow-ons once the seam is in place.
- **Remove the hardcoded `EXECUTOR_ADDRESS` env-var default** in `orchestrator.py`. Belongs to the centralized-config follow-up surfaced by ADR-0006.

## Rejected alternatives

- **Option 2 — two separate Protocols.** Higher mechanical cost (~7 RPC methods on `RunnerClient` would need a parallel Protocol declaration) for marginal additional value today.
- **Option 3 — wrapper class without Protocol.** Doesn't actually type the seam; the orchestrator still depends on a single concrete shape.
- **Option 4 — defer.** Each later contract change (`Conductor`, remote runner, plug-in discovery) re-litigates the runtime shape under deadline pressure. Settle the shape now while there's only one implementation to migrate.

## Scope notes

- **Only `cleanup_trial` lifts to the Protocol** as a direct method (besides lifecycle). Two other `executor_client` calls remain in the orchestrator — `executor_client.get_state(trial_id)` at the final-state-sync point and `executor_client.grade_trial(trial_id, ...)` on the agentic-judge path. Both happen *after* a per-trial adapter exists and could be routed through the adapter; doing so is a separate refactor and out of scope for this ADR.
- **The orchestrator's default factory is unchanged.** When no `runtime_backend` is injected, the orchestrator constructs `SharedStackRuntimeBackend(runner_address=...)` as before. Existing callers see no behavioural difference.
- **No gRPC `.proto` change.** This ADR types the orchestrator-side execution surface, not the gRPC wire.
- **`EXECUTOR_ADDRESS` env-var default** stays at `"executor:50051"` (Docker DNS name). Moving it requires the centralized-config follow-up from ADR-0006.
