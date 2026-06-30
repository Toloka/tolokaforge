# 0008. `Conductor` Protocol — per-trial executor seam

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The orchestrator's architecture is built around a set of typed Protocols, each defining a swappable boundary in the engine: the control↔trial wire format (`TrialSpec`/`TrialResult`, ADR-0003), the per-trial and run-level data planes (`TrialArtifactWriter` ADR-0004, `RunAggregateWriter` ADR-0005), typed `EnvEndpoints` on `TrialSpec` (ADR-0006), and the execution surface (`RuntimeBackend` ADR-0007). One boundary remains untyped: the abstraction the orchestrator uses to *run* an individual trial.

Today `Orchestrator._run_trial` is a 621-line instance method that does everything between "you have a trial to run" and "here's the `TrialResult`": environment setup, runner registration, agent-loop invocation, grading, artifact writing. The orchestrator owns it. The orchestrator both *schedules* trials and *executes* them.

After this PR, the orchestrator becomes a **scheduler** — it owns the run queue, the retry policy, the result aggregation, and the lease loop. A **`Conductor`** becomes the **executor** — it owns the per-trial work. The orchestrator calls `conductor.run(spec, task_config)` and gets a `TrialResult` back; everything inside that call is the Conductor's concern.

Why this matters: out-of-process trial execution (remote conductor, distributed execution) is impossible to reach while `_run_trial` is a method on the orchestrator. The structural split this PR ships is the precondition. After it lands, shipping a `RemoteConductor` is "implement the Protocol against a gRPC client" rather than "rewrite half the orchestrator."

ADR-0003 and ADR-0007's Follow-ups both name this as the closing seam.

## Architecture context — picture before prose

### Before vs after: orchestrator responsibility split

```
BEFORE (pre-PR #101):                  AFTER (PR #101):

┌──────────────────────────┐           ┌──────────────────────┐
│      Orchestrator         │          │     Orchestrator     │
│   ┌────────────────────┐  │          │  (scheduler only)    │
│   │ run queue          │  │          │                      │
│   │ retry policy       │  │          │  • run queue         │
│   │ result aggregation │  │          │  • retry policy      │
│   │ lease loop         │  │          │  • result aggregation│
│   │ ────────────────── │  │          │  • lease loop        │
│   │ _run_trial:        │  │          │                      │
│   │  • env setup       │  │          │       │              │
│   │  • registration    │  │          │       │ conductor.run()
│   │  • agent loop      │  │          │       ▼              │
│   │  • grading         │  │          └───────┬──────────────┘
│   │  • artifact write  │  │                  │
│   └────────────────────┘  │          ┌───────▼──────────────┐
│   (~2188 LoC)             │          │   Conductor  (NEW)   │
└──────────────────────────┘           │  (per-trial executor) │
                                       │                      │
                                       │  • env setup         │
                                       │  • registration      │
                                       │  • agent loop        │
                                       │  • grading           │
                                       │  • artifact write    │
                                       │   (~621 LoC body)    │
                                       └──────────────────────┘
                                       Orchestrator: ~1289 LoC
                                       (~870 LoC net delete)
```

The orchestrator becomes a scheduler; the per-trial work becomes a swappable executor.

### How `Conductor` fits with the other Protocol seams

```
                   Orchestrator (scheduler)
                           │
                           │  conductor.run(spec, task_config)
                           │  ─── returns TrialResult ───
                           ▼
        ┌──────────────────────────────────────────┐
        │              Conductor                   │
        │   per-trial executor                     │
        │   (InProcess today; out-of-process       │
        │    impls as the design admits them)      │
        └──┬──────────┬──────────┬─────────────────┘
           │          │          │
       carries   delegates   delegates
           │          │          │
           ▼          ▼          ▼
       TrialSpec   Runtime-     TrialArtifact-
       (ADR-0003)  Backend      Writer
                   (ADR-0007)   (ADR-0004)
           │
           │  contains:
           ▼
       EnvEndpoints              RunAggregateWriter
       (ADR-0006)                (ADR-0005, run-level — orchestrator owns)
```

Every plane in the engine now has a typed Protocol seam:

| Plane | Seam | Status |
|---|---|---|
| Control ↔ Trial (wire) | `TrialSpec` / `TrialResult` | ADR-0003 |
| Trial → Data (per-trial) | `TrialArtifactWriter` | ADR-0004 |
| Trial → Data (run-level) | `RunAggregateWriter` | ADR-0005 |
| Control ↔ Trial (endpoints) | `EnvEndpoints` | ADR-0006 |
| Execution surface | `RuntimeBackend` | ADR-0007 |
| **Per-trial executor** | **`Conductor`** | **This ADR** |

### What landing this seam unlocks

```
TODAY:                              ONCE A REMOTE BACKEND EXISTS:

┌─────────────┐                     ┌─────────────┐
│ Orchestrator│                     │ Orchestrator│
└──────┬──────┘                     └──────┬──────┘
       │ conductor.run()                   │ conductor.run()
       ▼                                   ▼
┌─────────────┐                     ┌─────────────┐  gRPC   ┌──────────────┐
│ InProcess   │                     │ Remote      │ ──────▶ │ remote trial │
│ Conductor   │                     │ Conductor   │         │ executor     │
│ (same proc) │                     │             │         │ (any process,│
└─────────────┘                     └─────────────┘         │  any host)   │
                                                            └──────────────┘
                                    Same Orchestrator code — just
                                    a different Conductor injected.
                                    "Rewrite half the orchestrator"
                                    becomes "implement the Protocol."
```

That last picture is the architectural payoff. Distributed execution is no longer "redesign the orchestrator"; it's "write a new class that implements the `Conductor` Protocol."

## Decision Drivers

- **Closes the typed-seam arc.** Every plane in the engine has a typed Protocol with at least two implementations after this PR.
- **Symmetry with the other seams.** Same pattern: `@runtime_checkable` Protocol + concrete production impl + in-memory test fixture + canonical contract test.
- **Lean code in the orchestrator.** A 621-line method becomes a one-line delegation. The orchestrator file shrinks by ~870 lines net.
- **Precondition for distributed execution.** `Conductor` is the abstraction a `RemoteConductor` will satisfy when an out-of-process backend lands.
- **Cut-and-paste discipline.** The body of `_run_trial` has been touched by many PRs (env_endpoints, judge config, runtime backend); its internal structure is well-exercised. The right move is to lift it verbatim into `InProcessConductor.run()` so the diff reads as "this block moved" rather than "this block was refactored." Body refactoring is its own follow-up.

## Considered Options

1. **Extract `_run_trial` verbatim into `InProcessConductor`.** Cut-and-paste, no body changes; move the three private helpers (`_build_system_prompt`, `_build_judge_messages_json`, `_serialize_model_config`) plus the module-level `_build_resolved_block` helper alongside. **This PR.**
2. **Refactor `_run_trial`'s internals while extracting.** The 621-line body has clear seams (environment setup, runner registration, agent loop, grading, artifact writing). Tempting, but mixes two changes — extraction risk + refactor risk — into one diff. Defer.
3. **Wrap `_run_trial` in a class without lifting the helpers.** Half-measure. The helpers are only called by `_run_trial`; leaving them on `Orchestrator` makes the orchestrator continue to look like an executor.
4. **Defer until a remote backend actually ships.** Each later seam definition would re-litigate the executor shape under deadline pressure. Settle the shape now while the seam pattern is fresh.

## Decision

We adopt **Option 1**.

- Add `tolokaforge/core/conductor.py` with `@runtime_checkable` `Conductor` Protocol declaring `run(spec: TrialSpec, task_config: TaskConfig) -> TrialResult`. `spec` is the same wire-format input the runner consumes (ADR-0003); `task_config` is the orchestrator-side rich `TaskConfig` (initial state, tool configs, user-simulator mode) the in-process executor still reads from while the body is verbatim.
- `InProcessConductor` captures the orchestrator's per-run dependencies (`adapter`, `artifact_writer`, `config`, `logger`, `verbose`, `strict`, `agent_client`, `docker_runtime`, `output_dir`, `request_limiter`) in its constructor. Its `run()` method runs the trial end-to-end; three private helpers travel with it. The module-level `_build_resolved_block` helper (only consumer was `_serialize_model_config`) moves to `conductor.py`.
- `InMemoryConductor` is the test fixture. Records every `run()` call on a `ConductorCallLog`; returns a configurable `TrialResult` via a `trajectory_factory: Callable[[str, int], Trajectory] | None` constructor arg (defaults to a synthetic success trajectory). Two purposes: (a) prove the seam is swappable; (b) let orchestrator-level tests assert scheduling / retry behaviour without spinning up Docker or an LLM.
- `Orchestrator.__init__` accepts an optional `conductor_factory: Callable[..., Conductor] | None = None` kwarg. The orchestrator constructs the Conductor inside `run()` / `run_worker()` (after the adapter is set + per-run dependencies resolved) via a new `_build_conductor()` helper. Default factory builds an `InProcessConductor`.
- The two `_run_trial` call sites become `conductor.run(...)`. `_run_trial` and the three helper methods are **deleted** from `Orchestrator`.

## Consequences

### Positive

- Every plane in the engine now has a typed contract with at least two implementations.
- The orchestrator becomes meaningfully smaller (~870 lines of net deletion). The scheduling vs. execution split is now explicit in the code structure.
- Orchestrator-level tests can use `InMemoryConductor` to assert scheduling / retry behaviour without spinning up Docker or an LLM. Validated by `test_run_worker_aborts_before_any_trial_dispatch`, which now patches the conductor factory cleanly.
- The shape of a `RemoteConductor` (or any out-of-process executor) is now defined and ready to be implemented.

### Negative / Trade-offs

- The Protocol's `run()` takes two arguments: `spec: TrialSpec` and `task_config: TaskConfig`. The orchestrator builds the `TrialSpec` once per trial (with `_build_trial_spec`, which also runs the adapter-registry guard) and passes the same orchestrator-side `TaskConfig` it has been carrying through scheduling. `InProcessConductor.run()` reads from `spec` / `task_config` / `self` directly; the body lift preserves the existing execution surface.
- `InProcessConductor.run()` is still 621 lines. Cleanup of the body's internal structure is queued as a follow-up — see Follow-ups below.
- Mocking the conductor in tests is mildly more involved than mocking `_run_trial` was (need to provide a factory rather than a method patch). Mitigation: `InMemoryConductor` is provided as the canonical replacement, and it's strictly more useful (call log, configurable trajectory factory).

### Follow-ups

- **Decompose `InProcessConductor.run()`'s internals.** The 621-line body has clear seams (environment setup, runner registration, agent loop, grading, artifact writing). Separate PR — cut-and-paste discipline first; refactor after.
- **`RemoteConductor` concrete implementation.** When the first out-of-process / distributed-execution work begins.
- **Collapse `DockerRunnerAdapter` into per-trial Protocol methods.** Same as ADR-0007's follow-up.
- **Promote `RunnerClient` to its own Protocol.** Same as ADR-0007's follow-up.

## Rejected alternatives

- **Option 2 — refactor while extracting.** Two risks at once. Defer the body cleanup.
- **Option 3 — wrap without lifting helpers.** Leaves the orchestrator looking like an executor; defeats the architectural goal.
- **Option 4 — defer.** Each later seam ends up re-litigating the executor shape simultaneously with whatever it's doing. Cheaper to settle the shape now.

## Scope notes

- **`TrialRunner` is a separate concept.** `tolokaforge.core.runner.TrialRunner` is the *agent ↔ user simulator loop* that runs *inside* a trial. The `Conductor` wraps one trial end-to-end (the loop is one part of its body). No rename; ADR-0008 flags the naming proximity so future readers don't conflate them.
- **The conductor factory pattern, not an instance kwarg.** The orchestrator can't construct a `Conductor` at `__init__` time — `self.adapter` is set during `run()` via `load_tasks()`, not at `__init__`. A factory lets the orchestrator construct at the right moment while keeping the injection point clean. Mirrors how `DockerRuntime` is constructed once per `run()` after `runner_address` is resolved.
- **No new gRPC `.proto` change.** This ADR types the orchestrator-side execution surface, not the gRPC wire.
- **Test fixture migration.** Tests that previously patched `Orchestrator._run_trial` now inject an `InMemoryConductor` through the `conductor_factory` kwarg and assert on `call_log.runs`. The mocking surface moves from a method patch to a dependency injection.
