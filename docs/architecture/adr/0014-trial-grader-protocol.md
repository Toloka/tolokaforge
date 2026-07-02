# 0014. `TrialGrader` Protocol — swappable trial-grading strategy

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`InProcessConductor.run()` used to carry the grading path inline: a ~100-line block that (a) checked the trajectory's status and short-circuited with an auto-fail `Grade` on `TrialStatus.ERROR` / `TrialStatus.TIMEOUT`, (b) short-circuited again on `TerminationReason.STUCK_DETECTED`, (c) otherwise built a judge-messages transcript, dispatched to `RuntimeBackend.grade_trial` over gRPC, and parsed the response dict into a `Grade` (with all of `state_diff_json`, `criterion_results`, `judge_report`, `judge_status` sub-payload handling).

That's a strategy pattern in disguise — three independent grading strategies living inside one conditional. `CLOUD_RUNTIME_ARCHITECTURE.md` §6.3 also places grading as a *triggered* concern of the Conductor: "the per-trial control flow — assemble prompts, call the LLM, dispatch tool calls, **trigger grading**, emit the `TrialResult`." "Trigger" implies a delegated call, not an owned responsibility.

The seam question: **which layer owns the grading strategy — Conductor internals, or a first-class Protocol sibling to `Conductor`?**

## Decision

Introduce a `TrialGrader` Protocol as a trial-plane seam, sibling to `Conductor`. Single method:

```python
class TrialGrader(Protocol):
    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade: ...
```

The concrete `RunnerRPCTrialGrader(runtime_backend, logger)` implementation carries all three current strategies internally — the two auto-fail branches plus the gRPC `grade_trial` dispatch and `Grade` materialisation. Constructor takes the `RuntimeBackend` it needs for the RPC branch and the per-run `StructuredLogger` used for per-branch observability.

`InProcessConductor` accepts a `trial_grader: TrialGrader` via constructor injection. The `_grade` phase collapses to one call:

```python
trajectory.grade = self.trial_grader.grade(
    spec, trajectory,
    runner.effective_system_prompt or system_prompt,
)
```

The `Orchestrator._build_conductor` helper constructs `RunnerRPCTrialGrader(runtime_backend, logger)` at conductor-build time and passes it in.

## Consequences

**Immediate.** Grading is a named, swappable component. The three strategies that were hidden inside a conditional are now separately reachable inside `RunnerRPCTrialGrader.grade`. Contract tests pin the `Grade` shape at the Protocol boundary rather than relying on `InProcessConductor` internals.

**Testability.** Unit tests exercise each strategy branch with a stub `RuntimeBackend` that captures `grade_trial` calls (no gRPC required). Auto-fail branches assert that `grade_trial` is *not* called; the gRPC branch asserts on the dispatch shape.

**Forward-looking.** The Protocol is what future variants slot into:

- **Judge Protocol lift (GH #131 / ADR-0011 Follow-ups).** The rubric judge is currently coupled to the runner-side `grade_trial` implementation; extracting it as a first-class Judge component means a `JudgeBackedTrialGrader` becomes a natural `TrialGrader` variant.
- **Multi-container / remote grading (CLOUD_RUNTIME §6.4).** When grading moves into the runner sandbox pod, a `RemoteTrialGrader` (gRPC client to a pod-hosted grader service) replaces `RunnerRPCTrialGrader` behind the same Protocol; Conductor is unchanged.
- **Rule-only / custom / hybrid graders.** Task packs that skip the LLM judge entirely (deterministic state-check tasks) can inject a lighter `TrialGrader` and save the cost.

**Narrower `runtime_backend` dependency in Conductor.** Conductor still calls `runtime_backend.register_trial` and `runtime_backend.get_state`; it no longer calls `runtime_backend.grade_trial` directly. When we later extract `TrialRegistrar` / `TrialStateReader` seams (not this PR), the Conductor's dependency on the concrete runtime backend shrinks further.

## Alternatives considered

**A. Keep grading inline in `Conductor._grade`.** Rejected: the three-branch conditional is a strategy pattern, and the Cloud Runtime target explicitly separates "trigger grading" (Conductor) from "own grading" (a swappable component). Also runs against the project pattern of extracting new responsibilities into new seams rather than adding to either monolith.

**B. Extract grading as private methods on `InProcessConductor`.** Rejected: keeps the coupling inside the Conductor class. A future `RemoteConductor` would either inherit or copy the same logic. The Protocol seam separates the responsibility from the trial-body implementation.

**C. Extract grading with a functional API (not a Protocol).** Rejected: constructor injection of a `TrialGrader` instance is what makes the seam swappable. A module-level function couples to the current `RuntimeBackend` implicitly.

**D. Also extract `_build_judge_messages_json` as its own component.** Rejected as premature — it's ~20 LoC of pure serialisation that has no substitution story today. Lives as a module-level helper in `tolokaforge/core/trial_grader.py`.

## Refs

- `docs/CLOUD_RUNTIME_ARCHITECTURE.md` §6.3 (Trial plane — conductor; "trigger grading").
- ADR-0008 (Conductor Protocol) — parent seam.
- ADR-0011 (Seam-definition + data-declaration conventions) — Follow-ups notes the Judge lift.
- ADR-0013 (`RuntimeBackend` owns per-trial RPC methods) — `grade_trial` still lives there; `TrialGrader` is the caller.
- GH #103 — Conductor decomposition (parent PR).
- GH #160 — Design ticket for this ADR.
- GH #131 — Judge Protocol lift (future `TrialGrader` variant).
