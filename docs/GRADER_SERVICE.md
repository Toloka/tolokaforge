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

## SubstrateService (runner-side, read-only)

`SubstrateService` is a read-only gRPC surface the runner exposes when
`RunConfig.grader.expose_substrate: true` is set. An independent grader
container dials it to answer every read the substrate seam
([ADR-0039](adr/0039-standalone-grader.md)) makes — initial state, RAW
and STABLE final DB state, agent-visible filesystem, and the trial's
per-trial knowledge-base search — without ever asking the runner to
mutate state on the grader's behalf.

Config surface — one optional field on `GraderConfig`:

```yaml
# run_config.yaml (excerpt)
grader:
  expose_substrate: true      # default: false
```

`expose_substrate: false` (the default, and the shape every existing
`run_config.yaml` carries by omission) keeps the surface off. The
orchestrator forwards the flag to the runner container as
`RUNNER_EXPOSE_SUBSTRATE=true`; the runner reads this env var and, when
truthy, registers `add_SubstrateServiceServicer_to_server` on the same
`grpc.Server` and the same listen port that carries `RunnerService`
(default `50051`). No new open port; no new Docker port expose. A runner
started with the flag off returns `UNIMPLEMENTED` for any
`SubstrateService/*` call.

The seven RPCs:

| RPC | What it returns |
| --- | --- |
| `ReadInitialState` | The trial's pre-execution state (`{table: [rows]}` JSON) — same shape `TaskDescription.initial_state.tables` carries. |
| `ReadFinalDBState` | RAW final DB state, mirroring `db_client.get_state`. The shape judge state-diff and custom_checks read; unfiltered rows. |
| `ReadFinalDBStateStable` | STABLE final DB state, mirroring `db_client.get_stable_state`. The shape jsonpath state-checks grading reads; unstable fields filtered server-side by the DB service. |
| `ReadFilesystemPath` | One file under `AGENT_WORK_DIR`; `is_file` + `content_utf8` for text, `is_file` + `content_bytes_b64` for binary. Symlinks / non-files / missing paths return `exists=false`. |
| `ListFilesystemDir` | Relative POSIX paths of every non-symlink UTF-8-decodable file under `AGENT_WORK_DIR`. Same filter `_read_agent_visible_filesystem` ships today — no `node_modules` / `.venv` / `.git` excluder. |
| `KBSearch` | Trial's per-trial KB hits. `kb_available: false` is a first-class "this trial has no KB" signal; the callback substrate returns `None` from `knowledge_search()` when it is false. |
| `SubstrateHealthCheck` | `status: "ready" \| "degraded" \| "unavailable"` and `active_trials` — distinct from `RunnerService.HealthCheck`, which reports RunnerService plumbing. |

The read-only guarantee is structural, not a docstring promise. The
servicer class holds `_READ_ONLY = True` and the canonical test
`test_substrate_service_gated_startup.py` enumerates the public method
set on `SubstrateServicer` (compared against the generated base
`SubstrateServiceServicer`) and refuses any name whose prefix matches a
write verb (`set_` / `insert` / `update` / `write` / `delete` /
`mutate`) — adding a write RPC to `runner.proto` surfaces the offender
in the generated base and fails the test before an implementation
lands.

The sanctioned client is
[`LiveRunnerCallbackGradingSubstrate`](../tolokaforge/core/grading/substrate_live.py).
An independent grader constructs one per trial, pointed at the runner's
substrate address; every substrate read (`initial_state`, `final_state`,
`final_state_stable`, `db_reader`, `knowledge_search`, `filesystem_root`,
`filesystem_state`) issues at most one RPC per grade and caches the result.
`filesystem_root` materialises the agent-visible tree eagerly to a private
temp directory on first use — matching the runner's shipping filter
(non-symlink, UTF-8-decodable) with no path-component excluder. Any
`grpc.RpcError` is wrapped as `SubstrateUnreachableError`, which the grader
translates into `GradingFailedError` so a runner that disappears mid-grade
is booked as ungradeable rather than as an agent failure.
`tolokaforge/core/grading/substrate_client.py::GrpcSubstrateClient` is the
underlying wire adapter — one instance per `(channel, trial_id)` pair.

## Sub-component plug-in seams

Below the composite dispatch, individual grade sub-components are Protocol
seams a downstream package extends by registering an `importlib.metadata`
entry point. Each seam has a shipped reference impl registered under a
default name; the loader lives on `tolokaforge.core.plugin_registry` and
follows the fail-loud shape [ADR-0022](adr/0022-runtime-independence.md)
pins for every seam in the engine.

| Seam | Entry-point group | Default | Contract |
| --- | --- | --- | --- |
| Custom check executor | `tolokaforge.custom_check_executors` | `check_runner` (production), `in_memory` (test fixture) | [`CheckExecutor`](../tolokaforge/core/grading/check_runner.py) — runs the pack's `checks.py` against the trial's evidence and returns a `CheckResultSet`. |
| Judge model provider | `tolokaforge.judge_model_providers` | `litellm` (fronts [`LLMClient`](../tolokaforge/core/llm/client.py)) | [`JudgeModelProvider`](../tolokaforge/core/grading/judge_model_provider.py) — builds a `JudgeModel` (the `.generate` + `.classify_loop_error` surface `LLMJudge` drives) from a `ModelConfig`. |
| Rubric evaluator | `tolokaforge.rubric_evaluators` | `llm_judge` (wraps [`LLMJudge`](../tolokaforge/core/grading/judge.py)) | [`RubricEvaluator`](../tolokaforge/core/grading/rubric_evaluator.py) — grades one trial's rubric evidence into a `JudgeResult`. Constructed from a `RubricEvaluatorContext` carrying the resolved `JudgeModelProvider` + customization flags; receives the per-trial evidence at `.evaluate()`. |
| Transcript rule matcher | `tolokaforge.transcript_rule_matchers` | `default` (wraps [`evaluate_transcript_rules`](../tolokaforge/core/grading/transcript.py)) | [`TranscriptRuleMatcher`](../tolokaforge/core/grading/transcript_rule_matcher.py) — scores the whole `TranscriptRulesConfig` against a `TrialTimeline` and returns one `TranscriptEvaluationResult`. Holistic seam (one matcher per config); the events-less-trial gate and per-key accounting stay in the composite. |
| Trace check operator | `tolokaforge.trace_check_operators` | 15 non-binding names (`equals`, `equals_ci`, `contains`, `contains_ci`, `not_equals`, `regex`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `len_gt`, `len_gte`, `exists`) + 2 binding names (`equals_binding`, `contains_binding`) | [`TraceCheckOperator`](../tolokaforge/core/grading/trace_check_operator.py) — one callable per operator name a `ValuePredicate` may declare, resolved per-call via the registry cache. **Per-operator granularity** (the only seam that is not holistic): a downstream `equals_semver` registration extends the operator vocabulary without a framework PR. Binding operators are identified by the `_binding` suffix on the registered name. |

Register a downstream impl the same way as a `TrialGrader`:

```toml
# downstream package pyproject.toml
[project.entry-points."tolokaforge.judge_model_providers"]
openai_direct = "acme_judge:_openai_direct_provider_factory"

[project.entry-points."tolokaforge.rubric_evaluators"]
deterministic_rules = "acme_grader:_rules_evaluator_factory"
```

The runner resolves the shipping defaults at startup via
`load_custom_check_executor("check_runner")`,
`load_judge_model_provider("litellm")`, and
`load_transcript_rule_matcher("default")`, and caches the resulting instances
on `RunnerServiceImpl`. The check executor is threaded through the
composite `grade_custom_checks` dispatch. The judge model provider is
threaded into the `RubricEvaluatorContext` that the runner constructs at
grade time — `load_rubric_evaluator("llm_judge")(ctx)` — and the composite
`grade_llm_judge` receives the resolved evaluator; no LLM transport ever
appears in composite. The transcript-rule matcher is threaded through the
composite `grade_transcript_rules` dispatch; the events-less-trial gate
(`scored_transcript_rules`) and the per-key accounting stay in the
composite so every deployment topology applies them identically.
Trace-check operators resolve per-call inside
`tolokaforge.core.grading.trace_checks._operator_holds`, which reads
`load_trace_check_operator(name)` — the entry-point discovery cache means
a name only pays the loader cost on its first mention. A
downstream `pip install` of a package that registers under any of these
groups is picked up on the runner's next start with no framework change.

## See also

- [ADR-0014 — TrialGrader Protocol](adr/0014-trial-grader-protocol.md)
- [ADR-0022 — Runtime Independence](adr/0022-runtime-independence.md)
- [ADR-0038 — Grader Detachment](adr/0038-grader-detachment.md)
- [ADR-0039 — Standalone Grader Substrate](adr/0039-standalone-grader.md)
- [STANDALONE_RUNNER.md](STANDALONE_RUNNER.md) — the sibling doc for the
  runner-as-component surface
