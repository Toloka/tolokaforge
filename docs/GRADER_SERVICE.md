# Grader Plug-in Seam

The tolokaforge grader — the piece that turns a completed trial into a
`Grade` — is a plug-in seam. Downstream code selects a grader by name,
and the seam accepts fundamentally different dispatch shapes: a
runner-side gRPC that computes deterministic state / transcript checks
plus an LLM judge, or a host-side judge callable that runs the rubric
directly with no runner state at all.

This document describes the seam as it stands after the *grader detachment*
milestone, points at the two registered built-ins, and shows how a
downstream package can register its own grader without touching engine
code. For the design record — the decisions behind the seam and the
work still ahead — see [ADR-0035](adr/0035-grader-detachment.md).

## The seam

`tolokaforge.core.trial_grader.TrialGrader` is a `@runtime_checkable`
Protocol with one method:

```python
def grade(
    self,
    spec: TrialSpec,
    trajectory: Trajectory,
    agent_system_prompt: str,
) -> Grade | None: ...
```

- Returns `Grade` on a completed trial the grader could measure.
- Returns `None` on a trial the agent never got to run
  (infrastructure abort — no measurement to score).
- Raises `GradingFailedError` when the trial *was* measured but the
  grading substrate could not produce a verdict.

The conductor holds a `TrialGrader` and delegates the grading phase to
it. Callers do not need to know which implementation is behind the
Protocol.

## Selecting a grader

Per-task configuration names the grader by its plug-in name:

```yaml
# task.yaml (excerpt)
grader: runner_rpc         # the default — grades on the runner
# or
grader: judge_only          # rubric-only judge dispatch, no runner state
```

The name resolves through the `tolokaforge.trial_graders` entry-point
registry (backed by `importlib.metadata`), so a downstream package that
registers a grader name becomes selectable via the same field.

## Built-in graders

Two implementations ship with tolokaforge today.

### `runner_rpc` — `RunnerRPCTrialGrader`

The production grader. Owns a `GrpcRunnerClient` bound to the runner's
address (`runner_address` on `TrialGraderContext`), calls
`GradeTrial` on the runner service, and translates the returned proto
into a `Grade`. Short-circuits with an auto-fail on
`TrialStatus.ERROR` / `TrialStatus.TIMEOUT` /
`TerminationReason.STUCK_DETECTED` before the RPC is dialled.

Registered as:

```toml
[project.entry-points."tolokaforge.trial_graders"]
runner_rpc = "tolokaforge.core.trial_grader:runner_rpc_trial_grader_factory"
```

### `judge_only` — `JudgeBackedTrialGrader`

A second impl whose dispatch shape is fundamentally different: instead of
a runner-side RPC that runs the deterministic state / transcript /
custom-check pipeline plus the LLM judge, `JudgeBackedTrialGrader`
invokes an injected judge callable directly. Auto-fail branches match
`RunnerRPCTrialGrader` so both are drop-in swaps for the caller.

Registered as:

```toml
[project.entry-points."tolokaforge.trial_graders"]
judge_only = "tolokaforge.core.trial_grader:judge_backed_trial_grader_factory"
```

The default factory dispatch is unwired (raises `NotImplementedError`
with a clear pointer to the follow-up) — until a production
`LLMJudge`-backed dispatch lands on the umbrella, `judge_only` is
useful primarily to prove that the seam accepts more than one shape,
and to be constructed directly by tests that inject their own
`JudgeGradeFn`.

## Registering a downstream grader

A downstream `pip install` adds a new grader by:

1. Implementing the Protocol (any object with the right `grade` method
   satisfies `isinstance(obj, TrialGrader)`).
2. Providing a factory `Callable[[TrialGraderContext], TrialGrader]`.
3. Declaring the entry point in the downstream package's
   `pyproject.toml`:

```toml
[project.entry-points."tolokaforge.trial_graders"]
my_grader = "my_package.my_module:my_grader_factory"
```

The engine discovers the entry point on next install and makes
`grader: my_grader` a valid task-config choice. No engine-side change
required. See [ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) for the
broader fail-loud registry pattern the seam follows.

## The context — serialisable configuration only

The factory receives a `TrialGraderContext` carrying only serialisable
data:

- `runner_address: str` — the address the built-in `runner_rpc` grader
  dials against. Downstream graders that need a different endpoint (a
  remote grader service, a queue broker) may ignore this field or build
  their own transport from it.
- `logger: StructuredLogger` — the run-scoped logger.

The context deliberately does *not* carry the orchestrator's live
runtime backend. A live gRPC channel would couple the grader to a
specific runner instance chosen by the orchestrator — precisely the
coupling this milestone breaks so a grader can run on a different
machine. A canonical hygiene test
(`tests/canonical/test_trial_grader_context_hygiene.py`) pins that
negative-space contract at the type level.

## Where the seam is going

ADR-0035 records the milestone's five load-bearing decisions. Two
extensions land after this milestone:

- **Standalone grader service.** A new `tolokaforge grader-service`
  binary + a `grader.proto` contract, so the grader can be deployed on
  a different machine from the runner. `GraderRPCTrialGrader` becomes
  a third built-in that dials the standalone service.
- **Queue-backed variant.** A `QueueTrialGrader` that publishes grade
  jobs to a broker (Redis Streams as the reference backend) so
  orchestrator throughput on judge-heavy runs is decoupled from
  grader latency.

Both fit behind the current `TrialGrader` Protocol — no caller-facing
API change — and both plug in through the same entry-point registry.

## See also

- [ADR-0014 — TrialGrader Protocol](adr/0014-trial-grader-protocol.md)
- [ADR-0022 — Runtime Independence](adr/0022-runtime-independence.md)
- [ADR-0035 — Grader Detachment](adr/0035-grader-detachment.md)
- [STANDALONE_RUNNER.md](STANDALONE_RUNNER.md) — the sibling doc for the
  runner-as-component surface
- [ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) — fail-loud
  registry pattern the plug-in seams share
