# 0041. Zero-coverage exit signal on `run_state.json`

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Related:**
  - [ADR-0005](0005-run-aggregate-writer-seam.md) — `RunAggregateWriter`
    and `AggregateMetrics`. The two booleans this ADR adds are *derived
    from* aggregate fields but do not live *on* the aggregate; the
    seam this ADR extends is `run_state.json`, not `aggregate.json`.
  - [ADR-0039](0039-coding-harness-adapter-agnostic.md) — the closest
    architectural sibling: another opt-in signal whose default
    preserves shipped behaviour and whose CLI flag overrides a
    run-config field with the same precedence rule this ADR reuses.
  - [ADR-0040](0040-standalone-grader.md) — the most recent
    orchestrator-plane ADR; unrelated in scope but the sibling in the
    numbering sequence.

## Context and Problem Statement

A run whose trials all die before the agent is measured — every trial
hits `PROVISION_ERROR`, `API_TIMEOUT`, or `RATE_LIMIT` — writes
`aggregate.json {measured_trials: 0}`, prints `✓ Run complete`, and
exits `0` when neither opt-in completion gate fires. The chain is deliberate:
[`tolokaforge/core/failure_attribution.py:60-66`](../../tolokaforge/core/failure_attribution.py)'s
`EXCLUDED_TYPED_REASONS` classifies those trials as
`INFRASTRUCTURE_ABORT` rather than `UNGRADEABLE`, so
[`tolokaforge/dx/cli/main.py`](../../tolokaforge/dx/cli/main.py)'s
`_fail_on_completeness_gates` ungradeable branch does not fire
without the `--fail-on-zero-coverage` opt-in. The design intent
this preserves is documented at
[`docs/CLI.md`](../CLI.md) § "Run and worker exit codes":
"a trial the provider or the substrate killed … does not trigger the
gate". That contract is load-bearing for existing CI pipelines.

Issue #1063 names a second shape of the same slogan. A run whose LLM
judge sub-component errors on every trial classifies every trial as
`MEASURED`, records a `Grade` with `judge_status == JudgeStatus.ERRORED`
and `score == 0.0`, and exits `0`. The judge sub-component does not
raise `GradingFailedError`; the only writer of
`trajectory.grading_error` is
[`conductor.py:980`](../../tolokaforge/core/orchestrator/conductor.py)
inside `except GradingFailedError`, so trials where the judge errored
never reach the ungradeable gate. The `aggregate.judge_cost_usd == 0.0`
tell is buried inside the aggregate JSON — invisible to CI unless a
parser opens the file and knows to look.

CI parsers already parse shell exit codes; they should not have to
open `aggregate.json` to distinguish "nothing was measured" or "no
grade succeeded" from a clean run.

## Decision Drivers

- **CI parsers read shell exit codes, not JSON.** The signal has to
  reach the operator on the process's exit channel.
- **The signal must be opt-in.** Existing pipelines rely on
  "infrastructure aborts do not fail the run"
  ([`docs/CLI.md`](../CLI.md) § "Run and worker exit codes"). Flipping
  that default silently would break them.
- **The signal must be inspectable even when the flag is off.** A
  triage dashboard reading `run_state.json` should distinguish the two
  failure modes without re-parsing `aggregate.json`.
- **One derivation site.** `scored_trials` is already defined at
  [`tolokaforge/core/metrics.py:319-336`](../../tolokaforge/core/metrics.py)
  (`len([t.grade.score for t in measured if t.grade is not None])`)
  and carried on
  [`AggregateMetrics.scored_trials`](../../tolokaforge/core/output/aggregate_models.py).
  A second definition would drift.
- **Both #1301 and #1063 in one PR.** The two share the exit-0
  slogan; one design record covers both.

## Considered Options

1. **Booleans on `run_state.json` + opt-in CLI/config flags → exit 2.**
   Selected. Adds two `RunState` booleans, two mirrored CLI flags
   plus run-config keys, and a new exit code `2`. Preserves the shipped
   exit-code contract for every consumer that ignores the flags.

2. **Field-only (booleans on `run_state.json`, no flags).** Rejected.
   #1063's CI could parse the boolean via `jq -e`, but the signal is
   less discoverable than a flag+exit-code pair, and a run whose judge
   errored on every trial would still exit `0` on every existing
   pipeline. The point is a distinguishable exit code.

3. **Flag-only (widen the meaning of exit 1).** Rejected. Exit `1`
   already means "the run completed and any trial is `ungradeable`".
   Overloading it removes the CI branch
   `case $? in 0) ok;; 1) partial;; 2) empty;; esac` and breaks
   pipelines that treat `1` as "partial-verdict, act on it".

4. **A new `run_summary.json` for CI consumers.** Rejected. Adds a
   second artifact CI must open. `run_state.json` is already the
   CI-facing completion record — it carries `status`, `start_time`,
   `end_time`, and the fields a CI script parses to answer "did the
   run complete cleanly?". Booleans that summarise a completion
   condition belong there.

5. **Booleans on `AggregateMetrics` (`aggregate.json`).** Rejected.
   `AggregateMetrics` is a pure metrics record (numeric counts,
   ratios) with `extra="forbid"` — every field is a number a
   dashboard plots. Derived completion booleans are not a metric;
   they are a CI-facing summary of what the run finished as, and
   `run_state.json` is the record that carries that summary today.

6. **Version `run_state.json`'s schema.** Rejected. The two new fields
   are additive `bool` with `default=False`. A state file written by
   older code loads unchanged; a state file written by this code
   loads on older code with the two fields silently dropped under
   Pydantic's default permissive `extra` policy. No schema bump earns
   its keep here.

7. **Derive the second gate from classification
   (`zero_scored = count(class == MEASURED and grade is not None) == 0`).**
   Rejected. #1063's failure mode is a judge-only error that leaves
   `trajectory.grading_error is None` (the sole writer of that field
   is [`conductor.py:980`](../../tolokaforge/core/orchestrator/conductor.py)
   inside `except GradingFailedError`, and the judge sub-component
   does not raise that class), so those trials classify as `MEASURED`
   and carry a `Grade` record with `judge_status == ERRORED`. A
   classification-based gate never fires for #1063 by construction.
   The chosen derivation reads `t.grade.judge_status` directly.

## Decision

Adopt **Option 1**.

### `RunState` grows two booleans

Two fields on
[`RunState`](../../tolokaforge/core/resume.py):

```python
zero_coverage: bool = False
zero_judge_graded: bool = False
```

Both default `False` so a state file written by older code loads
unchanged and a run that never opts in reads `False`/`False` after
completion. Populated only on the completion write in the `run`
process; the worker path never writes `run_state.json` (shards share a
run-dir and a race-write is a footgun — the worker surfaces the same
signal through its own exit code).

### `OrchestratorConfig` grows two flags

Two fields on
[`OrchestratorConfig`](../../tolokaforge/core/models/run_config.py):

```python
fail_on_zero_coverage: bool = False
fail_on_zero_judge_graded: bool = False
```

Mirrored as two Click options on `tolokaforge run` and `tolokaforge
worker`: `--fail-on-zero-coverage` and `--fail-on-zero-judge-graded`.
When a flag is set at the CLI, it overrides the run-config value with
the same precedence rule
[`tolokaforge/dx/cli/main.py`](../../tolokaforge/dx/cli/main.py)'s
`--runtime` uses today: flag beats YAML.

### Field derivations

Computed at completion inside
[`Orchestrator._publish_grading_completeness`](../../tolokaforge/core/orchestrator.py)
from `self.results`:

- `measured_trials = sum(1 for t in self.results if classify_trial_outcome(t) == TrialOutcomeClass.MEASURED)`
  — the classifier at
  [`failure_attribution.py:99-112`](../../tolokaforge/core/failure_attribution.py).
- `scored_trials = sum(1 for t in self.results if t.grade is not None)`
  — the same predicate `_measured_averages` uses at
  [`metrics.py:319-336`](../../tolokaforge/core/metrics.py); the
  quantity `AggregateMetrics.scored_trials` records. This is the
  single derivation site.
- `judge_errored_trials = sum(1 for t in self.results if t.grade is not None and t.grade.judge_status == JudgeStatus.ERRORED)`
  — the judge-level observable at
  [`grade.py:152`](../../tolokaforge/core/models/grade.py).
- `synthesized_trials = sum(1 for t in self.results if t.grade is not None and t.grade.synthesized_by_termination_reason is not None)`
  — trials whose `Grade` was synthesised by a `TrialGrader` auto-fail
  branch (`ERROR` / `TIMEOUT` / `STUCK_DETECTED` / `EMPTY_COMPLETION`).
  No evaluator ran on these trials; the marker is on
  [`grade.py`](../../tolokaforge/core/models/grade.py).
- `zero_coverage = total_trials > 0 and (measured_trials == 0 or synthesized_trials == measured_trials)`.
- `zero_judge_graded = judge_errored_trials > 0 and judge_errored_trials == scored_trials`.

`zero_coverage` fires on two triggers, both fail-loud when
`--fail-on-zero-coverage` is set. The first — `measured_trials == 0` —
covers a run whose every attempt classified as an infrastructure abort.
The second — `synthesized_trials == measured_trials` — covers a run
whose every measured trial was harness-synthesised from a
non-agent-observation termination reason: the run reached its
`TrialGrader` at least once but the grader never ran any evaluator,
so no measurement describes agent behaviour. Both cases exit `2` with
the same signal because both answer the same operator question
("did the run measure anything?") with the same answer.

The `judge_errored_trials > 0` guard is what makes
`zero_judge_graded` read judge-status, not grade-presence: a run whose
trials classify `MEASURED` with `t.grade is None` on every trial
(`scored_trials == 0`, `judge_errored_trials == 0`) has produced no
grades to reason about and does not fire the second gate.

### Exit-code precedence

When a flag is set and its condition holds, the CLI exits `2`.
Precedence, in evaluation order:

1. `zero_coverage` — most specific opt-in.
2. `zero_judge_graded` — second opt-in.
3. `ungradeable` — the shipped exit-`1` gate, unchanged.

An opted-in specific gate outranks the generic ungradeable gate; the
more specific exit-`2` signal outranks exit-`1` in the overlap
(operator declared "fail on this", the specific signal wins).

## Consequences

### Positive

- **CI distinguishes four outcomes at the shell.** Zero-coverage,
  zero-judge-graded, ungradeable, and clean each carry a distinct
  exit code (`2`, `2`, `1`, `0` respectively — the two exit-`2` cases
  are distinguished by which flag was set and by the error line).
  Neither #1301 nor #1063 needs a JSON parser to reach the operator.
- **Existing consumers unaffected.** Both flags default `False` and
  the two booleans are additive with `default=False`; a caller that
  ignores the flags sees the shipped exit codes with unchanged
  semantics.
- **Single derivation site.** `scored_trials` reads exactly one
  predicate (`t.grade is not None`) and is defined in one place
  (`_publish_grading_completeness`) — the same quantity
  `AggregateMetrics.scored_trials` carries. No drift risk.
- **Closes both issues in one PR.** #1301 (zero-coverage) and #1063
  (zero-judge-graded) share the exit-0 slogan and one design record.

### Negative / Trade-offs

- **`run_state.json` grows two derived fields.** A mild duplication of
  what `aggregate.json` already lets a reader compute. Accepted: the
  readers of the two files are different (CI parser vs metrics
  consumer), and each should have local, cheap access to the answer
  it needs.
- **A new exit code enters the operator's vocabulary.** Exit `2` is
  distinct from the shipped `0`/`1` pair; runbooks and CI scripts
  that switch on the exit code need to know about it. Mitigated by
  the shipped exit codes staying unchanged for callers that ignore
  the flags — the new code appears only when an operator opts in.
- **`mark_run_completed` signature widens.** The method grows two
  keyword-only booleans and moves later in `Orchestrator.run()` so it
  can carry the derived values. The reorder is safe (no in-run reader
  of `run_state.status` between the completeness publish and the
  state-file write) but the invariant is now guarded by a `finally`
  clause so a report-generation exception cannot leave the file at
  `status: "running"`.

### Follow-ups

- **`docs/CLI.md` § "Run and worker exit codes"** grows a row for
  exit `2` and cites this ADR — landed in the CLI-flag stage of the
  implementing PR.
- **A third completion-condition gate.** If one is ever proposed,
  factor the three into an enum on the widened gate function rather
  than adding a fourth `elif`. Not gated on this ADR.

## Non-goals

- **Not versioning `run_state.json`.** Two additive `bool` fields with
  `default=False` load in both directions under Pydantic's default
  `extra` policy.
- **Not adding the booleans to `AggregateMetrics`.** `AggregateMetrics`
  is `extra="forbid"` and a pure metrics record — the derived
  CI-signal booleans belong on `RunState`.
- **Not changing `EXCLUDED_TYPED_REASONS`.** The
  [`failure_attribution.py:60-66`](../../tolokaforge/core/failure_attribution.py)
  set — and the `docs/CLI.md` § "Run and worker exit codes" contract
  that "a trial the provider or the substrate killed … does not
  trigger the gate" — is preserved verbatim.
- **Not making zero-coverage a default fail.** Both flags default
  `False`; the shipped exit-`0` semantics for infrastructure-abort
  runs stand until an operator opts in.

## Prior art

- [`docs/CLI.md`](../CLI.md) § "Run and worker exit codes" — the
  contract that "a trial the provider or the substrate killed … does
  not trigger the gate" this ADR preserves via the flag defaults.
- [`_fail_on_completeness_gates`](../../tolokaforge/dx/cli/main.py)
  — the widened gate function whose three branches (zero-coverage,
  zero-judge-graded, ungradeable) implement the precedence documented
  here; the console error line names which channel fired.

## Links

- Related ADRs:
  - [ADR-0005](0005-run-aggregate-writer-seam.md) —
    `RunAggregateWriter` seam; the aggregate this signal derives
    from and deliberately does not extend.
  - [ADR-0039](0039-coding-harness-adapter-agnostic.md) — the
    architectural sibling: opt-in signal, flag-overrides-YAML
    precedence, default preserves shipped behaviour.
  - [ADR-0040](0040-standalone-grader.md) — sibling in numbering.
- Related code:
  - [`tolokaforge/core/resume.py`](../../tolokaforge/core/resume.py)
    — `RunState` and `RunStateManager.mark_run_completed`.
  - [`tolokaforge/core/models/run_config.py`](../../tolokaforge/core/models/run_config.py)
    — `OrchestratorConfig`.
  - [`tolokaforge/core/orchestrator.py`](../../tolokaforge/core/orchestrator.py)
    — `GradingCompleteness` and `_publish_grading_completeness`.
  - [`tolokaforge/core/metrics.py`](../../tolokaforge/core/metrics.py)
    — the `scored_trials` predicate.
  - [`tolokaforge/core/output/aggregate_models.py`](../../tolokaforge/core/output/aggregate_models.py)
    — `AggregateMetrics.scored_trials`.
  - [`tolokaforge/dx/cli/main.py`](../../tolokaforge/dx/cli/main.py)
    — the CLI flags and the widened completeness gate.
  - [`docs/CLI.md`](../CLI.md) § "Run and worker exit codes" — the
    documented exit-code contract.
- Issues:
  - #1301 — zero-coverage runs exit `0`.
  - #1063 — every-grade-judge-errored runs exit `0`.
