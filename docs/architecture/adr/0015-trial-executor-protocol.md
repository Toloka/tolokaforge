# 0015. `TrialExecutor` Protocol — per-trial substrate-lifecycle seam

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`RuntimeBackend` (ADR-0007, ADR-0010) exposes `provision` / `await_ready` / `endpoints` / `teardown` — the per-trial substrate-lifecycle contract. `Conductor` (ADR-0008) owns the trial body: agent loop, tool dispatch, grading. But nothing yet brackets each conductor call with the substrate contract. As of ADR-0014's merge, the orchestrator dispatches trials directly:

```python
executor.submit(conductor.run, spec, task_config)
```

Which means `--runtime per_trial` selects `PerTrialRuntimeBackend`, the banner prints, and the very first per-trial RPC fails with `PerTrialRuntimeBackend has no runner client for trial_id=… provision() must be called before any per-trial RPC method`. The backend is selectable but non-functional end-to-end.

`docs/CLOUD_RUNTIME_ARCHITECTURE.md` §5.3 places the substrate bracket on the **scheduler-side lifeline**:

```
SCH ->> RB: provision(TrialSpec)
RB -->> C: trial env up
C ->> RN: RegisterTrial → agent loop → GradeTrial → TrialResult
SCH ->> RB: teardown(trial)
```

The seam question: **which layer owns this bracket?**

- **Add to Orchestrator.** Grows the control plane, which we're actively trying to shrink (§5.2 makes the Orchestrator scheduler-only).
- **Add inside `Conductor.run()`.** Grows the trial-plane executor. GH #103 explicitly decomposed the conductor to *reduce* its scope; wrapping every trial body in a substrate bracket rebuilds the monolith.
- **New Protocol seam.** Introduces a third component that owns exactly the bracket. Matches the SCH-side lifeline in §5.3 and the plug-in-first principle (D15).

## Decision

Introduce a `TrialExecutor` Protocol as the scheduler-side seam. Single method:

```python
class TrialExecutor(Protocol):
    def execute(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult: ...
```

The production concrete `ProvisioningTrialExecutor(runtime_backend, conductor, logger)` composes the three collaborators and brackets `conductor.run` with the substrate contract:

```python
handle = runtime_backend.provision(spec)
try:
    runtime_backend.await_ready(handle)
    real_endpoints = runtime_backend.endpoints(handle)
    final_spec = spec.model_copy(update={"env_endpoints": real_endpoints})
    return conductor.run(final_spec, task_config)
except ProvisionError as e:
    return _synthesize_provision_failure_result(spec, e)
finally:
    runtime_backend.teardown(handle)
```

`Orchestrator._build_trial_executor(runtime_backend, conductor) -> TrialExecutor` composes one instance per run; the dispatch loop submits `trial_executor.execute` in place of `conductor.run`. The substrate bracket runs on the worker thread — provisioning parallelism = worker count.

`SharedStackRuntimeBackend.endpoints(handle)` is made honest (in a sibling change): constructor injection of `EnvEndpoints`, sentinel-URL logic deleted, `endpoints(handle)` returns the injected value. Both backends now uniformly source per-trial endpoints from `endpoints(handle)`, so the executor's `spec.model_copy(update={"env_endpoints": real_endpoints})` step is substrate-agnostic — a no-op for the shared-stack path (same value across trials) and load-bearing for the per-trial path (real URLs per trial).

`ProvisionError` at any provisioning stage synthesises a failed `TrialResult` with `TerminationReason.PROVISION_ERROR`; `attribute_failure()` classifies it as `provision_failure` in `DETERMINISTIC_CLASSES`; the orchestrator's existing `_is_retryable_trajectory()` re-enqueues; the next attempt gets a fresh `provision()`.

## Consequences

**Immediate.** `--runtime per_trial` executes real trials end-to-end for any task declaring `environment_manifest`. The shared-stack path is behaviourally unchanged — the executor's provision / await_ready / teardown calls hit the no-ops `SharedStackRuntimeBackend` already ships (ADR-0010 compliance was in place; only wiring was missing).

**Testability.** Unit tests exercise the bracket in isolation using `InMemoryRuntimeBackend` + `InMemoryConductor` — no gRPC, no Docker daemon required. Contract tests pin the Protocol shape independent of the concrete `ProvisioningTrialExecutor`.

**Neither Orchestrator nor Conductor grows.** The orchestrator delegates one dispatch call to the executor. The conductor's `run()` is unchanged in shape — it still receives a fully-resolved `TrialSpec` and returns a `TrialResult`.

**Forward-looking. The Protocol is what future variants slot into:**

- **Remote grading and remote conductor (CLOUD_RUNTIME §6.4).** A `RemoteTrialExecutor` (gRPC client to a trial-plane worker) replaces `ProvisioningTrialExecutor` behind the same Protocol. The orchestrator sees no change; the trial plane is a swap-in.
- **Kubernetes / Modal / hosted-sandbox substrates.** Each ships its own `RuntimeBackend` (per ADR-0010's substrate axis); `ProvisioningTrialExecutor` composes them without modification.
- **Non-provisioning executors.** A stub `TrialExecutor` that skips the bracket entirely (for shared-stack-only smoke tests) or a `CachingTrialExecutor` that memoises `TrialResult` for deterministic replay both slot in without touching the orchestrator.

## Alternatives considered

**A. Bracket inside `Orchestrator.run()`.** Rejected: grows the control plane. The plan doc's `neither Orchestrator nor Conductor grows in this arc` invariant would be broken.

**B. Bracket inside `Conductor.run()`.** Rejected: rebuilds the monolith GH #103 just decomposed. `Conductor` implementations that don't need substrate provisioning (`InMemoryConductor` in tests) would inherit dead code they can't share.

**C. Extend `RuntimeBackend` with an `execute(spec, task_config, conductor)` method.** Rejected: puts trial-body-level concerns (Conductor invocation) on the substrate driver. Cross-cutting; future backends would each have to re-implement the identical bracket.

**D. Ship the wiring inline as a helper function on `Orchestrator`, defer the Protocol.** Rejected: bakes the composition into the orchestrator. A future `RemoteTrialExecutor` couldn't slot in without a Protocol boundary to replace against.

## Refs

- `docs/CLOUD_RUNTIME_ARCHITECTURE.md` §5.3 (Trial, target) — the SCH-side substrate bracket.
- ADR-0007 — `RuntimeBackend` Protocol (foundation).
- ADR-0008 — `Conductor` Protocol (parent seam, unchanged surface).
- ADR-0010 — `RuntimeBackend` provisioning contract.
- ADR-0014 — `TrialGrader` Protocol (sibling seam inside the Conductor).
- GH #119 — Wiring ticket for this PR.
- GH #120 — Follow-up validation gate.
