# 0019. `Judge` Protocol — the grading-plane judge seam

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** Tolokaforge maintainers
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The read-only rubric judge grades one trial: it builds an `LLMClient` from the run-level judge `ModelConfig`, assembles a harness-owned read-only toolset (DB reads, `search_kb`, workspace file reads, the rubric-shaped `submit_report`), runs the shared `ToolCallingLoop` with no user simulator, and returns a `JudgeResult` — failing loud (`JudgeStatus.ERRORED`, no score) on its own malfunction.

That judge was a single top-level function with no Protocol seam and no `InMemory*` fixture. Every runtime sibling — `RuntimeBackend` (ADR-0007), `Conductor` (ADR-0008), `TrialGrader` (ADR-0014), `KnowledgeSearch` — already follows the repo's Pattern A (ADR-0011): a `Protocol`, a production implementation, and an `InMemory*` test fixture, with a canonical contract test pinning the boundary. ADR-0011's Follow-ups and ADR-0014's Consequences both named the Judge lift as the next application of the pattern.

Two milestone-24 follow-ups land directly on this surface, and both need a seam:

- **#451 (offline judge replay)** — the umbrella (#468) requires *exactly one* judge implementation reachable from both entry points (the runner's gRPC context and a future CLI-side replay). A replay-specific fork of the judge logic is unacceptable.
- **#465 (per-project/task judge customization)** — tool gating (e.g. disabling knowledge search for a task) needs a place to vary judge behaviour without changing the grading contract.

Adding the seam now, before the second variant exists, is the smaller move. The problem this ADR resolves: **which shape does the judge take so that both follow-ups plug in without forking grading logic or leaking LLM-judge config onto variants that don't need it?**

## Decision Drivers

- **Pattern A (ADR-0011).** Every runtime seam is `Protocol` + production impl + `InMemory*` fixture + canonical contract test. The judge is the last first-class runtime component that pre-dates the pattern.
- **One judge implementation from both entry points (#468 / #451).** The runner and a future CLI replay caller must reach the *same* `run()` — no forked judge logic.
- **A replay judge must not be forced to accept LLM-judge config.** Model, budgets, and injected client are meaningless to a judge that replays a stored `JudgeResult`; they cannot sit on the shared contract.
- **The narrow input surface is an invariant worth pinning.** The judge must never see the deterministic-oracle fields (`golden_actions` / `expected_hash` / `jsonpath_checks` / `grading_config`); a Protocol makes "cannot leak by construction" a contract, not a convention.
- **Behaviour preservation.** The `LLMJudge` output must be byte-for-byte the same as the pre-lift function — a structural lift, not a semantic change.

## Considered Options

1. **`Judge` Protocol with a construction-vs-run split** — model / budgets / injected client / logger on the constructor of the concrete impl; per-trial evidence on `run()`.
2. **Keep the rubric judge as a top-level function** — no seam.
3. **A `Judge` Protocol carrying `model_config` and budgets on `run()`** — a single flat call surface.

## Decision

We adopt **Option 1**: a `Judge` Protocol as the grading-plane seam, with a production `LLMJudge` implementation and an `InMemoryJudge` fixture.

```python
@runtime_checkable
class Judge(Protocol):
    def run(
        self, *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None = None,
        kb_search: KnowledgeSearch | None = None,
        extra_read_tools: list[Tool] | None = None,
        workspace_dir: Path | None = None,
        state_diff: str | None = None,
    ) -> JudgeResult: ...
```

**The construction-vs-run split is the load-bearing choice.** How a judge is *built* — the run-level `model_config`, the turn / wall-time / retry budgets, an optionally injected `LLMClient`, the logger — lives on the concrete implementation's constructor. What a judge *grades* — the per-trial evidence — is the `run()` surface. This mirrors `TrialGrader` (ADR-0014) and `Conductor` (ADR-0008): construction captures the run-level dependencies, the method carries the per-trial inputs.

`LLMJudge.__init__(model_config, *, max_turns, episode_timeout_s, submit_report_retries, llm_client=None, logger=None)` carries the LLM-judge config; its `run()` is the verbatim grading loop (production passes no `llm_client`, so one `LLMClient(model_config)` is built per trial, exactly as before). `InMemoryJudge` is the real (non-mock) fixture: it records each `run()` on a `JudgeCallLog` and returns a configurable `JudgeResult` aggregated through the real `aggregate_rubric`, so orchestrator/grading tests get authentic score math with no inference. The pre-lift `run_rubric_judge` function is deleted — it was internal (not re-exported, not on any documented API), so it refactored cleanly with no shim.

## Consequences

### Positive

- The judge is a named, swappable component, matching the rest of the runtime plane. Tests isolate it with `InMemoryJudge` instead of scripting an `LLMClient`.
- The narrow-input-surface invariant is pinned at the contract: the canonical test asserts `Judge.run` carries none of the deterministic-oracle fields.
- Both #451 and #465 slot in behind the Protocol without touching grading logic (see Forward-looking).

### Negative / Trade-offs

- Callers now build then call (`LLMJudge(model_config, ...).run(...)`) instead of one function call. This is the same construction-vs-run ergonomics the other seams already carry; a small test helper collapses the two steps where the flat shape reads better.

### Forward-looking

The Protocol is what the milestone-24 follow-ups slot into:

- **#451 offline judge replay** — plugs in as a `Judge` implementation constructed from a path to a prior `judge_trajectory.yaml`, replaying the stored `JudgeResult` and ignoring the live-evidence params (as `InMemoryConductor` ignores most of its `task_config`). Because `run()`'s DB / KB / workspace params are already optional, a CLI-side offline caller needs **no DB bridge**. The runner (gRPC context) and a future CLI entry point call the *same* `Judge.run` — satisfying the umbrella's "exactly one judge implementation from both entry points" (no replay-specific fork).
- **#465 per-project/task judge customization** — tool gating (e.g. `disable_knowledge_search`) is a construction-time concern of an `LLMJudge` (or a thin decorator `Judge`), not a change to the Protocol; the evidence surface already carries `kb_search` / `extra_read_tools` a customized judge can withhold.
- **`JudgeBackedTrialGrader`** — ADR-0014 named it; now that `Judge` exists, a `TrialGrader` that delegates to a `Judge` is a natural composition (noted, not built here).

### Follow-ups

- Code changes required: none beyond this lift; #451 and #465 land as new `Judge` implementations.
- Documentation to update: ADR-0011 Follow-ups and ADR-0014 Consequences now reference this ADR as the realized lift (done in this change).
- Tests to add: `tests/canonical/test_judge_contract.py` pins the Protocol boundary (added with the lift).

## Alternatives considered

**Keep the rubric judge as a top-level function (Option 2).** Rejected: no seam for the second variant, and two entry points (runner + replay CLI) would each grow their own judge glue, fighting #451's single-implementation rule.

**Put `model_config` / budgets on `run()` (Option 3).** Rejected: it leaks LLM-judge config onto a Protocol a replay judge must also satisfy — a replay judge has no model and no budgets, so those params would be dead weight it is forced to accept. The construction-vs-run split keeps the shared contract to evidence only.

## Links

- Related ADRs: ADR-0008 (`Conductor` Protocol) and ADR-0014 (`TrialGrader` Protocol) — sibling grading/trial-plane seams with the same construction-vs-run split; ADR-0011 (seam + declaration conventions) — Pattern A, whose Follow-ups named this lift.
- Related code: `tolokaforge/core/grading/judge.py` (`Judge` / `LLMJudge` / `InMemoryJudge` / `JudgeCallLog`); `tests/canonical/test_judge_contract.py`; `docs/RUBRIC_GRADING_DESIGN.md`.
- External references: GH #131 (this lift), #451 (offline judge replay), #465 (per-task judge customization), #468 (milestone-24 umbrella).
