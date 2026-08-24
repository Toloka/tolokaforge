# Grader Plug-in Seam

The tolokaforge grader — the piece that turns a completed trial into a
`Grade` — is a plug-in seam. Downstream code selects a grader by name,
and the seam accepts fundamentally different dispatch shapes: a
runner-side gRPC that computes deterministic state / transcript checks
plus an LLM judge, a host-side judge callable that runs the rubric
directly, a dedicated grader-service RPC bound to its own address, or a
queue-backed transport that decouples throughput from grader latency.

This document describes the seam as it stands after the grader-detachment
milestone, points at the four registered built-ins, and shows how a
downstream package can register its own grader without touching engine
code. For the design record, see [ADR-0038](adr/0038-grader-detachment.md).

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
grader: runner_rpc         # runner-side gRPC — the shipping default
# or
grader: judge_only          # host-side judge dispatch, no runner state
# or
grader: grader_rpc          # standalone tolokaforge-grader service
# or
grader: queue               # queue-backed transport (broker + workers)
```

The name resolves through the `tolokaforge.trial_graders` entry-point
registry (backed by `importlib.metadata`), so a downstream package that
registers a grader name becomes selectable via the same field.

## Built-in graders

Four implementations ship with tolokaforge.

### `runner_rpc` — `RunnerRPCTrialGrader`

The shipping default. Owns a `GrpcRunnerClient` bound to the runner's
address (`runner_address` on `TrialGraderContext`), calls
`GradeTrial` on the runner service, and translates the returned proto
into a `Grade`. Short-circuits with an auto-fail on
`TrialStatus.ERROR` / `TrialStatus.TIMEOUT` /
`TerminationReason.STUCK_DETECTED` before the RPC is dialled.

```toml
[project.entry-points."tolokaforge.trial_graders"]
runner_rpc = "tolokaforge.core.trial_grader:runner_rpc_trial_grader_factory"
```

### `judge_only` — `JudgeBackedTrialGrader`

Host-side dispatch to an injected judge callable. No runner state, no
transcript rules, no custom checks — pure rubric evaluation. Auto-fail
branches match `RunnerRPCTrialGrader` so both are drop-in swaps for the
caller.

The factory ships with an unwired default that raises `NotImplementedError`
if selected in production before a real `LLMJudge`-backed dispatch is
wired; direct construction with a real `JudgeGradeFn` works today (for
tests and offline-replay integration).

```toml
[project.entry-points."tolokaforge.trial_graders"]
judge_only = "tolokaforge.core.trial_grader:judge_backed_trial_grader_factory"
```

### `grader_rpc` — `GraderRPCTrialGrader`

Dials the standalone `tolokaforge.grader` service. Same call shape as
`runner_rpc` but bound to the grader service's address instead of the
runner's — the seam consumer that ADR-0038 built to enable independent
deploy. The grader service ships as `tolokasoft1/tolokaforge-grader`
alongside the runner / db-service / rag-service / mock-web images and
runs `python -m tolokaforge.grader` on port 50052 by default.

The factory reads `ctx.grader_address` when the operator has split the
runner and grader onto distinct hosts, and falls back to
`ctx.runner_address` for single-address deployments.

```toml
[project.entry-points."tolokaforge.trial_graders"]
grader_rpc = "tolokaforge.core.trial_grader:grader_rpc_trial_grader_factory"
```

### `queue` — `QueueTrialGrader`

Queue-backed transport. The orchestrator worker publishes a grade job
and blocks on a `concurrent.futures.Future`; grader workers consume the
queue in parallel, so orchestrator worker threads no longer serialise
on grader latency (ADR-0038 Decision 3). The queue backend is a plug-in
behind the `GradeBroker` Protocol; the reference impl is
`InMemoryGradeBroker` (thread-safe, `queue.Queue`-backed).

The registered factory raises `NotImplementedError` today: the
`TrialGraderContext` does not yet carry broker-selection configuration
and no worker pool is provisioned by the engine, so selecting
`grader: queue` without one would publish to a broker nothing listens
to. Direct construction of `QueueTrialGrader` with a custom
`GradeBroker` works today and is exercised by the canonical tests.

```toml
[project.entry-points."tolokaforge.trial_graders"]
queue = "tolokaforge.core.trial_grader:queue_trial_grader_factory"
```

## Deploying the standalone grader service

The `tolokaforge-grader` image ships alongside the other four first-party
images and is wired into `deploy/standalone/docker-compose.yaml`:

```yaml
grader:
  image: tolokasoft1/tolokaforge-grader:${TOLOKAFORGE_IMAGE_TAG:-latest}
  ports:
    - "50052:50052"
```

`docker compose up` from `deploy/standalone/` brings up the runner and
the grader side by side; the runner reaches the grader by service-name
DNS (`grader:50052` on the compose network) or the grader from the host
(`localhost:50052`).

The runtime command is fixed: `python -m tolokaforge.grader`. Reads
`--port` (or `$GRADER_SERVICE_PORT`, default `50052`).

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
required.

## The context — serialisable configuration only

The factory receives a `TrialGraderContext` carrying only serialisable
data:

- `runner_address: str` — the runner service's gRPC address; the
  `runner_rpc` grader dials it.
- `grader_address: str | None` — the standalone grader service's address;
  the `grader_rpc` grader dials it, falling back to `runner_address`
  when the operator has not split the two.
- `logger: StructuredLogger` — the run-scoped logger.

The context does *not* carry the orchestrator's live runtime backend.
A canonical hygiene test
(`tests/canonical/test_trial_grader_context_hygiene.py`) pins that
negative-space contract at the type level.

## Migration from earlier tolokaforge

The plug-in-seam contract changed in this milestone. Callers of the
`TrialGraderContext` constructor need to update:

```python
# Before
TrialGraderContext(runtime_backend=backend, logger=logger)

# After
TrialGraderContext(runner_address=backend.runner_address, logger=logger)
```

Downstream grader factories that read `ctx.runtime_backend` should read
`ctx.runner_address` (or `ctx.grader_address`) and build their own gRPC
client from it.

## See also

- [ADR-0014 — TrialGrader Protocol](adr/0014-trial-grader-protocol.md)
- [ADR-0022 — Runtime Independence](adr/0022-runtime-independence.md)
- [ADR-0038 — Grader Detachment](adr/0038-grader-detachment.md)
- [STANDALONE_RUNNER.md](STANDALONE_RUNNER.md) — the sibling doc for the
  runner-as-component surface
