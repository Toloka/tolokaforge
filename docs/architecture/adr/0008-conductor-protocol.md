# 0008. `Conductor` Protocol — per-trial executor seam

- **Status:** Proposed
- **Date:** 2026-06-26
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The Phase 1 seam-definition arc landed five seams in 0.3.0 + the early 0.4.0 window: control↔trial wire format (`TrialSpec`/`TrialResult`, ADR-0003), per-trial and run-level data planes (`TrialArtifactWriter` ADR-0004, `RunAggregateWriter` ADR-0005), typed `EnvEndpoints` on `TrialSpec` (ADR-0006), and the orchestrator's execution surface (`RuntimeBackend` ADR-0007). One Phase-1 seam closes the arc and unlocks Phase 2: the abstraction the orchestrator uses to *run* an individual trial.

Today `Orchestrator._run_trial` is a 621-line instance method that does everything between "you have a trial to run" and "here's the `TrialResult`": environment setup, runner registration, agent-loop invocation, grading, artifact writing. The orchestrator owns it. The orchestrator both *schedules* trials and *executes* them.

After this PR, the orchestrator becomes a **scheduler** — it owns the run queue, the retry policy, the result aggregation, and the lease loop. A **`Conductor`** becomes the **executor** — it owns the per-trial work. The orchestrator calls `conductor.run(task, trial_idx, ...)` and gets a `TrialResult` back; everything inside that call is the Conductor's concern.

Why this matters: Phase 2 (out-of-process trials, remote conductor, distributed execution) is impossible to reach while `_run_trial` is a method on the orchestrator. The structural split this PR ships is the precondition. After it lands, shipping a `RemoteConductor` is "implement the Protocol against a gRPC client" rather than "rewrite half the orchestrator."

ADR-0003 and ADR-0007's Follow-ups both name this as the closer of the seam-definition arc.

## Decision Drivers

- **Closes the Phase-1 seam-definition arc.** Every plane in the engine has a typed Protocol with at least two implementations after this PR.
- **Symmetry with the other seams.** Same pattern: `@runtime_checkable` Protocol + concrete production impl + in-memory test fixture + canonical contract test.
- **Lean code in the orchestrator.** A 621-line method becomes a one-line delegation. The orchestrator file shrinks by ~870 lines net.
- **Precondition for distributed execution.** `Conductor` is the abstraction a `RemoteConductor` will satisfy in Phase 2.
- **Cut-and-paste discipline.** The body of `_run_trial` has been touched by many PRs (env_endpoints, judge config, runtime backend); its internal structure is well-exercised. The right move is to lift it verbatim into `InProcessConductor.run()` so the diff reads as "this block moved" rather than "this block was refactored." Body refactoring is its own follow-up.

## Considered Options

1. **Extract `_run_trial` verbatim into `InProcessConductor`.** Cut-and-paste, no body changes; move the three private helpers (`_build_system_prompt`, `_build_judge_messages_json`, `_serialize_model_config`) plus the module-level `_build_resolved_block` helper alongside. **This PR.**
2. **Refactor `_run_trial`'s internals while extracting.** The 621-line body has clear seams (environment setup, runner registration, agent loop, grading, artifact writing). Tempting, but mixes two changes — extraction risk + refactor risk — into one diff. Defer.
3. **Wrap `_run_trial` in a class without lifting the helpers.** Half-measure. The helpers are only called by `_run_trial`; leaving them on `Orchestrator` makes the orchestrator continue to look like an executor.
4. **Defer until Phase 2 actually starts.** Each later seam definition (Migration Bench v0 on the new contracts, `RemoteConductor`, distributed execution) re-litigates the executor shape under deadline pressure. Settle the shape now while the seam pattern is fresh.

## Decision

We adopt **Option 1**.

- Add `tolokaforge/core/conductor.py` with `@runtime_checkable` `Conductor` Protocol declaring `run(task, trial_idx, agent_client, user_config, output_dir, docker_runtime, request_limiter, *, attempt_id, worker_id, env_endpoints, judge_config) -> TrialResult`.
- `InProcessConductor` captures the orchestrator's per-run dependencies (`adapter`, `artifact_writer`, `config`, `logger`, `verbose`, `strict`) in its constructor. The body of its `run()` method is the verbatim body of the old `_run_trial`; the three private helpers travel with it. The module-level `_build_resolved_block` helper (only consumer was `_serialize_model_config`) moves to `conductor.py`.
- `InMemoryConductor` is the test fixture. Records every `run()` call on a `ConductorCallLog`; returns a configurable `TrialResult` via a `trajectory_factory: Callable[[str, int], Trajectory] | None` constructor arg (defaults to a synthetic success trajectory). Two purposes: (a) prove the seam is swappable; (b) let orchestrator-level tests assert scheduling / retry behaviour without spinning up Docker or an LLM.
- `Orchestrator.__init__` accepts an optional `conductor_factory: Callable[..., Conductor] | None = None` kwarg. The orchestrator constructs the Conductor inside `run()` / `run_worker()` (after the adapter is set + per-run dependencies resolved) via a new `_build_conductor()` helper. Default factory builds an `InProcessConductor`.
- The two `_run_trial` call sites become `conductor.run(...)`. `_run_trial` and the three helper methods are **deleted** from `Orchestrator`.

## Consequences

### Positive

- Phase 1's seam-definition arc is complete. Every plane has a typed contract with at least two implementations.
- The orchestrator becomes meaningfully smaller (~870 lines of net deletion). The scheduling vs. execution split is now explicit in the code structure.
- Orchestrator-level tests can use `InMemoryConductor` to assert scheduling / retry behaviour without spinning up Docker or an LLM. Validated by `test_run_worker_aborts_before_any_trial_dispatch`, which now patches the conductor factory cleanly.
- The shape of a `RemoteConductor` (or any out-of-process executor) is now defined and ready to be implemented as Phase 2 starts.

### Negative / Trade-offs

- The Protocol's `run()` signature is long (12 keyword-style arguments). Justified because the existing `_run_trial` already had this surface — the extraction preserves it verbatim. A follow-up could collapse it into a per-call `TrialSpec`-shaped input (similar to the control↔trial seam ADR-0003 established for the runner-side wire), but that's a separate refactor with its own breaking-change-risk considerations.
- `InProcessConductor.run()` is still 621 lines. Cleanup of the body's internal structure is queued as a follow-up — see Follow-ups below.
- Mocking the conductor in tests is mildly more involved than mocking `_run_trial` was (need to provide a factory rather than a method patch). Mitigation: `InMemoryConductor` is provided as the canonical replacement, and it's strictly more useful (call log, configurable trajectory factory).

### Follow-ups

- **Refactor `_run_trial`'s internals.** The 621-line body has clear seams (environment setup, runner registration, agent loop, grading, artifact writing). Separate PR — cut-and-paste discipline first; refactor after.
- **`RemoteConductor` concrete implementation.** When Phase 2 starts and the first out-of-process / distributed-execution work begins.
- **Collapse `DockerRunnerAdapter` into per-trial Protocol methods.** Same as ADR-0007's follow-up.
- **Promote `RunnerClient` to its own Protocol.** Same as ADR-0007's follow-up.
- **Collapse the 12-arg `run()` signature into a `TrialSpec`-shaped input.** The control↔trial seam (ADR-0003) already uses a single `TrialSpec` to capture per-trial state for the gRPC wire; the same pattern could simplify the Conductor's surface.

## Rejected alternatives

- **Option 2 — refactor while extracting.** Two risks at once. Defer the body cleanup.
- **Option 3 — wrap without lifting helpers.** Leaves the orchestrator looking like an executor; defeats the architectural goal.
- **Option 4 — defer.** Each later seam ends up re-litigating the executor shape simultaneously with whatever it's doing. Cheaper to settle the shape now.

## Scope notes

- **`TrialRunner` is a separate concept.** `tolokaforge.core.runner.TrialRunner` is the *agent ↔ user simulator loop* that runs *inside* a trial. The `Conductor` wraps one trial end-to-end (the loop is one part of its body). No rename; ADR-0008 flags the naming proximity so future readers don't conflate them.
- **The conductor factory pattern, not an instance kwarg.** The orchestrator can't construct a `Conductor` at `__init__` time — `self.adapter` is set during `run()` via `load_tasks()`, not at `__init__`. A factory lets the orchestrator construct at the right moment while keeping the injection point clean. Mirrors how `DockerRuntime` is constructed once per `run()` after `runner_address` is resolved.
- **No new gRPC `.proto` change.** This ADR types the orchestrator-side execution surface, not the gRPC wire.
- **Migration of `test_run_worker_aborts_before_any_trial_dispatch`.** The pre-extraction test patched `Orchestrator._run_trial` to assert it wasn't called. After extraction, it uses the `conductor_factory` kwarg to inject an `InMemoryConductor` and asserts `call_log.runs == []`. Test intent preserved; mocking surface moved from a method patch to a dependency injection — strictly cleaner.
