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

<a id="configuration-runconfig-grader"></a>

## Configuration — `RunConfig.grader.*`

`RunConfig.grader` is the run-level block that selects the `TrialGrader`
implementation and carries transport-specific settings. Absent means
"use whatever the adapter's `trial_grader_name` names" — the pre-block
behaviour every existing run keeps. Fields:

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `name` | `str \| None` | `None` | Overrides `adapter.trial_grader_name` for this run when set. Value is the entry-point name of a registered `TrialGraderFactory` — one of the four built-ins (`runner_rpc`, `judge_only`, `grader_rpc`, `queue`) or a downstream-registered name. |
| `expose_substrate` | `bool` | `false` | When true, the runner registers `SubstrateService` on its listen port so an independent grader container can read the trial's substrate. Off by default so a brownfield deploy never accidentally opens the surface. See [SubstrateService (runner-side, read-only)](#substrateservice-runner-side-read-only). |
| `queue` | `QueueGraderConfig \| None` | `None` | Consumed only when `name: queue`. |
| `judge` | `JudgeGraderConfig \| None` | `None` | Consumed only when `name: judge_only`. |

Only the subblock matching the selected `name` is consulted; an
unrelated subblock is data the factory ignores.

`QueueGraderConfig` — transport settings for `name: queue`:

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `workers` | `int` (≥ 1) | `4` | Consumer-pool size — the throughput knob the transport exists to unlock. |
| `backend` | `"in_memory"` | `"in_memory"` | Names the `GradeBroker` implementation. |
| `worker_grader` | `str` | `"grader_rpc"` | Downstream `TrialGrader` each worker dispatches to — a `TrialGraderFactory` name from `tolokaforge.trial_graders`. Layering `queue` on top of another registered grader adds throughput scale without duplicating grading logic. |

`JudgeGraderConfig` — overrides for `name: judge_only`. Absent means
"use the task's own `grading.llm_judge.customization` exactly"; every
field is an optional override, and `None` on a field means the task's
own value wins.

| Field | Type | Default | Purpose |
| --- | --- | --- | --- |
| `disable_knowledge_search` | `bool \| None` | `None` | Optional override of the task-level customization; `None` inherits. |
| `custom_system_prompt` | `str \| None` | `None` | Optional override. Blank or whitespace-only is refused fail-loud (`grader.judge.custom_system_prompt must not be empty or whitespace-only`); omit the field to inherit. |
| `include_agent_system_prompt` | `bool \| None` | `None` | Optional override; `None` inherits. |

Two-state override, not three: the `judge` block cannot express "reset
a task-level customization back to library defaults". A flight that
needs the default judge prompt against a pack whose tasks set a strict
prompt edits the tasks' `grading.yaml`.

Worked `run_config.yaml` excerpt with all four `grader.*` fields
side-by-side:

```yaml
grader:
  name: queue                # selects QueueTrialGrader
  expose_substrate: true     # runner registers SubstrateService on 50051
  queue:                     # consumed because name == queue
    workers: 8
    backend: in_memory
    worker_grader: grader_rpc  # each worker dials the standalone grader
  judge:                     # ignored while name != judge_only
    disable_knowledge_search: true
    custom_system_prompt: "Strict factual judge."
    include_agent_system_prompt: false
```

Shipped source of truth:
[`GraderConfig` / `QueueGraderConfig` / `JudgeGraderConfig`](../tolokaforge/core/models/run_config.py)
in `tolokaforge.core.models.run_config`.

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

The standalone service mounts
`tolokaforge.grader.composite_dispatch.GraderCompositeDispatch` as its
`judge_fn`. Each `Grade` RPC deserialises the wire's
`task_config_json` / `task_description_json` / `judge_model_config_json`,
builds a fresh
`tolokaforge.core.grading.substrate_live.LiveRunnerCallbackGradingSubstrate`
against `runner_substrate_address`, and runs the composite grading
functions (state checks / transcript rules / trace checks / llm judge /
custom checks) mirroring the runner's `_grade_trial_async`. Hash grading
is refused server-side too — the substrate is read-only. See
[`docs/adr/0039-standalone-grader.md`](adr/0039-standalone-grader.md).

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

`deploy/standalone/docker-compose.yaml` is the operator template — no
Kubernetes manifest ships in-tree. Adapting the compose recipe to a
Kubernetes cluster is the operator's job; the follow-up
[#1272](https://github.com/Toloka/tolokaforge/issues/1272) tracks a
first-party K8s example.

The `tolokaforge-grader` image ships alongside the other four first-party
images. `deploy/standalone/docker-compose.yaml` brings the five-service
stack — db-service, rag-service, mock-web, runner, grader — up together
with the wiring the grader-side composite dispatch needs:

```yaml
runner:
  environment:
    RUNNER_EXPOSE_SUBSTRATE: "true"
grader:
  ports:
    - "50052:50052"
  depends_on:
    runner:
      condition: service_healthy
```

`RUNNER_EXPOSE_SUBSTRATE=true` registers the read-only `SubstrateService`
on the runner's existing gRPC listen port (`50051`); the grader dials it
per trial for every substrate read (initial / final state,
agent-visible filesystem, per-trial KB). `grader.depends_on:
{runner: service_healthy}` orders startup so the grader accepts
requests only after the runner's channel-ready HEALTHCHECK passes and
the substrate is answering.

`docker compose up --wait` from `deploy/standalone/` blocks on every
service's HEALTHCHECK, including the grader's channel-ready probe. Once
it returns, the runner reaches the grader by service-name DNS
(`grader:50052`) and the host reaches either service at
`localhost:5005{1,2}`.

The grader-container runtime command is fixed: `python -m
tolokaforge.grader`. Reads `--port` (or `$GRADER_SERVICE_PORT`, default
`50052`). Provider credentials come off the compose file's env —
`OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` /
`GEMINI_API_KEY` / `TOLOKAFORGE_SECRETS_JSON` — populated from
`deploy/standalone/.env`. The grader requires a provider key only when
the task exercises `llm_judge`; the other four grading components run
against the keyless stack.

`deploy/standalone/examples/grader_rpc/` documents the run-config shape
that routes grading through the standalone service
(`grader.name: grader_rpc`, `grader.expose_substrate: false`) and the
`GrpcGraderClient` snippet that verifies the composite dispatcher
produces a `Grade` end-to-end against a running stack.

## The `Grade` wire

`GraderService.Grade` is stateless per call: every field the grader-side
composite dispatcher needs to grade the trial rides on the request. The
wire carries eight named fields — the caller populates whichever ones
its dispatch consumes and leaves the rest empty.

| Field                         | # | Purpose                                                                                   |
| ----------------------------- | - | ----------------------------------------------------------------------------------------- |
| `trial_id`                    | 1 | Canonical `"{task_id}:{trial_index}"` identifier — the join key for logs and future stores. |
| `llm_messages_json`           | 2 | Transcript as an LLM-messages JSON string; the timeline builder decodes it.               |
| `termination_reason`          | 3 | `TerminationReason` value name; empty when the caller reports none.                       |
| `task_config_json`            | 4 | `RunnerGradingConfig` JSON — the whole `grading:` block the composite dispatcher reads.   |
| `judge_model_config_json`     | 5 | Optional `ModelConfig` JSON for the judge; empty when the task declares no `llm_judge`.   |
| `task_description_json`       | 6 | `TaskDescription` JSON — carries `initial_state`, `state_checks.id_fields`, `unstable_fields`, and `tool_artifacts` (checks.py plus every sibling artefact module the pack imports). |
| `runner_substrate_address`    | 7 | gRPC address of the runner's `SubstrateService`; the grader builds a `LiveRunnerCallbackGradingSubstrate` against it per trial. |
| `agent_system_prompt`         | 8 | Post-policy system prompt — authoritative. The grader uses this directly rather than re-splitting the leading system message off `llm_messages_json`. |

Field numbers are stable and additive: a future field lands on number 9
so an existing client's payload never lands in a new slot. Provider +
model-name evidence rides on field 5 so the grader constructs its
`LLMClient` via the [`judge_model_providers` seam](#sub-component-plug-in-seams)
without inferring provider from a model name (AGENTS.md Core Rule 10).

### Client-side snapshot

`GraderRPCTrialGrader` and `QueueTrialGrader` both call
`tolokaforge.grader.wire_snapshot.build_grade_request_fields` to project
a completed trial's `TrialSpec` into the wire fields the grader consumes
above the trajectory-shaped trio. The builder returns a frozen
`GradeRequestFields` dataclass; the caller unpacks each field into
`GrpcGraderClient.grade` (`grader_rpc` transport) or into `GradeJob`
(`queue` transport). Field derivation:

| Wire field                | Source                                          |
| ------------------------- | ----------------------------------------------- |
| `task_config_json`        | `spec.task.grading.model_dump_json()`           |
| `judge_model_config_json` | `spec.judge_model_config.model_dump_json()`, empty when `spec.judge_model_config is None` |
| `task_description_json`   | `spec.task.model_dump_json()` — one field carries `initial_state`, `state_checks.id_fields`, `initial_state.unstable_fields`, and `tool_artifacts` |
| `runner_substrate_address`| Passthrough from the grader's stored context (`ctx.runner_address`) |
| `agent_system_prompt`     | Passthrough from the `TrialGrader.grade` caller |

The builder is a pure projection: it reads only in-memory Pydantic
models, opens no gRPC channel, and never touches the filesystem. Both
graders take `runner_substrate_address` on `__init__` (threaded by
`grader_rpc_trial_grader_factory` and `queue_trial_grader_factory` from
`ctx.runner_address`, since `SubstrateService` shares the runner's listen
port). Empty here fails loud at first `.grade()` with a
`GradingFailedError` naming `runner_substrate_address` — a silent empty
string would land at the grader as a 30 s gRPC connect hang.

### Hash grading refuses on `grader_rpc`

Hash grading depends on the runner's substrate reset / replay path; the
grader-side `LiveRunnerCallbackGradingSubstrate` is read-only and cannot
back it. Both grader-side transports refuse a task whose
`grading.state_checks.hash_enabled` is true with a `GradingFailedError`
naming the operator's actionable branch:

> `grader_rpc cannot execute hash-based grading — the substrate is read-only. Configure grader: runner_rpc for this task, or disable hash_enabled.`

The refusal is client-side (fires before any gRPC round-trip) so the
misconfiguration surfaces without a network hop, and the trial books as
ungradeable rather than as an agent failure.

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
pins for every seam in the engine. Six sub-component seams cover the six
evaluators the composite dispatch reaches. The [`.importlinter`
`composite-sub-component-seams` contract](../.importlinter) enforces the
negative-space of the seam by forbidding
[`composite.py`](../tolokaforge/core/grading/composite.py) from importing
any of the six reference-impl-holding modules — the composite reaches every
sub-component only through its Protocol via a resolved-instance kwarg.

| Group | Protocol module | Reference impls | Granularity |
| --- | --- | --- | --- |
| `tolokaforge.custom_check_executors` | [`check_runner.py::CheckExecutor`](../tolokaforge/core/grading/check_runner.py) | `CheckRunner` (production), `InMemoryCheckExecutor` (test fixture) | holistic |
| `tolokaforge.judge_model_providers` | [`judge_model_provider.py::JudgeModelProvider`](../tolokaforge/core/grading/judge_model_provider.py) | `LiteLLMJudgeModelProvider` (fronts `LLMClient`) | holistic |
| `tolokaforge.rubric_evaluators` | [`rubric_evaluator.py::RubricEvaluator`](../tolokaforge/core/grading/rubric_evaluator.py) | `LLMJudgeRubricEvaluator` (wraps `LLMJudge`) | holistic |
| `tolokaforge.transcript_rule_matchers` | [`transcript_rule_matcher.py::TranscriptRuleMatcher`](../tolokaforge/core/grading/transcript_rule_matcher.py) | `DefaultTranscriptRuleMatcher` (wraps `evaluate_transcript_rules`) | holistic |
| `tolokaforge.trace_check_operators` | [`trace_check_operator.py::TraceCheckOperator`](../tolokaforge/core/grading/trace_check_operator.py) | 17 shipped operator functions — 15 non-binding (`equals`, `equals_ci`, `contains`, `contains_ci`, `not_equals`, `regex`, `gt`, `gte`, `lt`, `lte`, `in_`, `not_in`, `len_gt`, `len_gte`, `exists`) + 2 binding (`equals_binding`, `contains_binding`) | per-operator |
| `tolokaforge.state_check_backends` | [`state_check_backend.py::StateCheckBackend`](../tolokaforge/core/grading/state_check_backend.py) | `JsonpathStateCheckBackend`, `DbProbesStateCheckBackend` (hash is NOT a backend — runner-integrated) | per-source |

Register a downstream impl the same way as a `TrialGrader`:

```toml
# downstream package pyproject.toml
[project.entry-points."tolokaforge.judge_model_providers"]
openai_direct = "acme_judge:_openai_direct_provider_factory"

[project.entry-points."tolokaforge.rubric_evaluators"]
deterministic_rules = "acme_grader:_rules_evaluator_factory"

[project.entry-points."tolokaforge.state_check_backends"]
s3_diff = "acme_grader:_s3_diff_state_check_backend_factory"
```

The runner resolves the shipping defaults at startup via
`load_custom_check_executor("check_runner")`,
`load_judge_model_provider("litellm")`,
`load_transcript_rule_matcher("default")`, and
`load_state_check_backend("jsonpath")` + `load_state_check_backend("db_probes")`,
and caches the resulting instances on `RunnerServiceImpl`. The check
executor is threaded through the composite `grade_custom_checks`
dispatch. The judge model provider is threaded into the
`RubricEvaluatorContext` that the runner constructs at grade time —
`load_rubric_evaluator("llm_judge")(ctx)` — and the composite
`grade_llm_judge` receives the resolved evaluator; no LLM transport ever
appears in composite. The transcript-rule matcher is threaded through the
composite `grade_transcript_rules` dispatch; the events-less-trial gate
(`scored_transcript_rules`) and the per-key accounting stay in the
composite so every deployment topology applies them identically. The
state-check backends dict (`{"jsonpath": ..., "db_probes": ...}`) is
threaded through the composite `grade_state_checks_reads` dispatch; each
backend owns its source's read strategy so the composite dispatches
without knowing either evaluator's internals. Trace-check operators
resolve per-call inside
`tolokaforge.core.grading.trace_checks._operator_holds`, which reads
`load_trace_check_operator(name)` — the entry-point discovery cache means
a name only pays the loader cost on its first mention. A
downstream `pip install` of a package that registers under any of these
groups is picked up on the runner's next start with no framework change.

**Hash grading is deliberately NOT a state-check backend.** The
`state_checks.hash` component has state-mutation semantics (snapshot →
reset → replay → snapshot → restore) that the read-only substrate cannot
serve; hash grading stays runner-integrated on
`RunnerServiceImpl._execute_hash_grading`, called by `_grade_trial_async`
above the composite dispatch.

## Wire cost per grade component

On `runner_rpc` / `InProcessGradingSubstrate`, every entry on this
table is a direct object access — no wire, no materialisation. The
`grader_rpc` column names the substrate reads each component triggers
on `LiveRunnerCallbackGradingSubstrate`; each RPC is documented in the
[SubstrateService section](#substrateservice-runner-side-read-only).

| Component | `runner_rpc` (`InProcessGradingSubstrate`) | `grader_rpc` (`LiveRunnerCallbackGradingSubstrate`) |
| --- | --- | --- |
| `state_checks.jsonpath` | direct access | 1 × `ReadFinalDBStateStable` (cached); when a jsonpath references the filesystem view, 1 × `ListFilesystemDir` + N × `ReadFilesystemPath` via the first `filesystem_state` read (cached). |
| `state_checks.db_probes` | direct access | No substrate RPC — probes open their task-declared DSN directly, same as InProcess. |
| `transcript_rules` | direct access | No substrate RPC — evaluated over the in-memory transcript. |
| `trace_checks` | direct access | No substrate RPC — evaluated over the in-memory event timeline. |
| `custom_checks` | direct access | Eager materialisation of `filesystem_root` on first read: 1 × `ListFilesystemDir` + N × `ReadFilesystemPath` (one per non-symlink, UTF-8-decodable file under `AGENT_WORK_DIR`). Plus 1 × `ReadFinalDBState` when a check touches `db_reader`. |
| `llm_judge` | direct access | 1 × `ReadInitialState` + 1 × `ReadFinalDBState` for the state-diff; per `search_kb` tool call, 1 × `KBSearch`; per `read_file` tool call, the same eager `filesystem_root` materialisation as `custom_checks` on first use (subsequent reads served from the private temp tree). |

Substrate accessors cache: once
`LiveRunnerCallbackGradingSubstrate.final_state()` /
`.final_state_stable()` / `.initial_state()` / `.filesystem_state()` /
`.filesystem_root()` has returned once, a second call in the same trial
pays no RPC.

`filesystem_root` is the notable asymmetry: on first read,
`LiveRunnerCallbackGradingSubstrate` walks the runner's agent-visible
tree via `ListFilesystemDir` and eagerly downloads every file to a
private temp directory — matching the runner's shipping filter
(non-symlink, UTF-8-decodable) with no path-component excluder. A large
workspace pays a one-off wire cost proportional to the tree size on the
first component that reads it.

**When to prefer `runner_rpc` for coding-task deploys.** Coding tasks
that leave large workspaces — Python-workspace or terminal-bench packs
where the agent produces hundreds of megabytes under `AGENT_WORK_DIR` —
pay the `filesystem_root` materialisation cost on `grader_rpc` and gain
nothing from the split, since the grader-side composite dispatch has no
throughput advantage over the runner-side one for a single trial. For
those packs, keep `grader.name: runner_rpc` (the shipping default): the
grader runs in-process against the trial's live substrate and skips the
wire.

**Reserved future: shared-mount cheap substrate.** The single-host
topology where an independent grader container reads the substrate off
a shared filesystem/DB mount is the reserved `SharedMountGradingSubstrate`
recipe — see [ADR-0039](adr/0039-standalone-grader.md), reserved-future
substrate SWE-bench pattern. Not shipped today; the two shipping
substrates are `InProcess` and `LiveCallback`.

<a id="extension-points-the-seven-plug-in-groups"></a>

## Extension points — the seven plug-in groups

Seven `importlib.metadata` entry-point groups let a downstream package
extend the grader without a framework change: one substrate group and
six sub-component seams. Each group has a matching loader on
[`tolokaforge.core.plugin_registry`](../tolokaforge/core/plugin_registry.py):

- `tolokaforge.grading_substrates` — `load_grading_substrate(name)` returns the `GradingSubstrate` **class** (the caller instantiates it with per-trial arguments).
- `tolokaforge.custom_check_executors` — `load_custom_check_executor(name)` returns a factory.
- `tolokaforge.judge_model_providers` — `load_judge_model_provider(name)` returns a factory.
- `tolokaforge.rubric_evaluators` — `load_rubric_evaluator(name)` returns a factory.
- `tolokaforge.transcript_rule_matchers` — `load_transcript_rule_matcher(name)` returns a factory.
- `tolokaforge.state_check_backends` — `load_state_check_backend(name)` returns a factory.
- `tolokaforge.trace_check_operators` — `load_trace_check_operator(name)` returns the **operator callable** directly (no factory wrapper; the callable itself is the contract).

Copy-paste block for a downstream `pyproject.toml`:

```toml
[project.entry-points."tolokaforge.grading_substrates"]
my_substrate = "my_package:my_substrate_class"

[project.entry-points."tolokaforge.custom_check_executors"]
my_check_executor = "my_package:my_check_executor_factory"

[project.entry-points."tolokaforge.judge_model_providers"]
my_judge = "my_package:my_judge_provider_factory"

[project.entry-points."tolokaforge.rubric_evaluators"]
my_rubric = "my_package:my_rubric_evaluator_factory"

[project.entry-points."tolokaforge.transcript_rule_matchers"]
my_matcher = "my_package:my_matcher_factory"

[project.entry-points."tolokaforge.state_check_backends"]
my_state_backend = "my_package:my_state_backend_factory"

[project.entry-points."tolokaforge.trace_check_operators"]
my_operator = "my_package:my_operator"
```

`tolokaforge.trial_graders` is the eighth registration point — the
top-level grader-name seam ADR-0038 shipped, already documented in
[Registering a downstream grader](#registering-a-downstream-grader).
A downstream package registering a new grader name lands there, not
in any of the seven groups above.

## Parity gate

The `runner_rpc` and `grader_rpc` legs must produce byte-identical
`Grade` output for every combination of grading components a task
declares — except the two accepted, documented divergences (KB
passthrough for the judge; hash grading refused). A canonical parity
gate at `tests/canonical/test_grader_parity_reference.py` locks that
invariant against a growing corpus of reference packs.

**Harness.** `tests/utils/grader_parity_harness.py` boots an in-process
`RunnerServiceImpl` + `SubstrateServicer` and drives each leg against
the same trial context:

- `run_via_runner_rpc(pack, monkeypatch=...)` — calls
  `RunnerServiceImpl.GradeTrial`; returns a `runner_pb2.Grade`.
- `run_via_grader_rpc(pack, monkeypatch=...)` — constructs
  `GraderCompositeDispatch`, builds a `GradeDispatch` from the pack's
  wire fields, calls `.grade(...)`, and translates the resulting Python
  `Grade` back to `grader_pb2.Grade` via the service's own
  `_grade_to_wire` helper.
- `assert_grader_rpc_refuses(pack, expected_error_fragment, monkeypatch=...)`
  — asserts the grader leg raises `GradingFailedError` with the
  expected message fragment (the refusal-contract branch a
  hash-enabled pack lands on).
- `serialise_grade(g)` — canonical JSON via
  `MessageToDict(preserving_proto_field_name=True,
  always_print_fields_with_no_presence=True)` followed by a `%.6g`
  float normaliser. Both proto types (`runner_pb2.Grade`,
  `grader_pb2.Grade`) project to the same dict shape; the two legs
  converge at the canonical-dict layer, not at the proto-type layer.
  The `trace_checks` optional-presence field materialises as `-1.0`
  when the wire carries no presence — the runner leg's sentinel and
  the grader leg's absent both mean "not evaluated" per the
  `runner.proto` semantics.

**Baselines.** Each pack under
`tests/canonical/grader_parity_baselines/<name>/` ships a committed
`expected_grade.json` captured via the harness on green
`feat/standalone-grader`. The reference test asserts every leg
matches the baseline byte-for-byte.

**Pack shape.** One directory per pack, with:

- `task.yaml` — `TaskDescription` fields (task_id, name, category,
  description, adapter_type, system_prompt, initial_state, agent/user
  tools). The harness folds `grading.yaml` onto its `grading`
  attribute at load time.
- `grading.yaml` — `RunnerGradingConfig` (weights, state_checks,
  transcript_rules, trace_checks, llm_judge, custom_checks).
- `trial.yaml` — wire dispatch fields: `trial_id`,
  `termination_reason`, `agent_system_prompt`, `llm_messages`,
  optional `judge_model_config`.
- `parity.yaml` — declared `accepted_divergences: [...]`, optional
  `judge_script: [...]` for packs exercising `llm_judge` (a
  deterministic scripted-client stand-in that keeps the canonical
  lane keyless), optional `db_probe_rows: {probe_name: [row, ...]}`
  for packs exercising `state_checks.db_probes` (scripted rows the
  harness serves from `_fetch_probe_rows` so neither leg dials a
  live postgres), and optional `refusal_mode` /
  `expected_error_fragment` for the hash-refusal contract.
- `expected_grade.json` — the committed baseline.

**Refresh.** `uv run pytest tests/canonical/test_grader_parity_reference.py
--refresh-baselines` rewrites every pack's baseline from the runner
leg's output and skips the equality assertions. The refresh is
idempotent — a second run against the freshly-written baseline shows
no diff. The resulting change belongs in the same commit as the code
change that motivated it; `git diff` catches accidents during review.

**Isolation packs.** Six packs each populate exactly one non-trivial
grading block so a scoring divergence at one plug-in seam surfaces at
that pack alone. The state-check subseams (`jsonpath_checks`,
`db_probes`) collapse into one wire slot (`GradeComponents.state_checks`)
per `runner.proto`, so they ship as two packs whose `grading.yaml` shape
distinguishes them at the config surface. The
`test_isolation_pack_config_is_single_seam` parametrisation locks the
invariant.

| Pack directory | Seam isolated | Config invariant |
|---|---|---|
| `state_checks_jsonpath_only/` | `state_check_backends[jsonpath]` | `state_checks.jsonpath_checks` non-empty, `db_probes` empty |
| `state_checks_db_probes_only/` | `state_check_backends[db_probes]` | `state_checks.db_probes` non-empty, `jsonpath_checks` empty |
| `transcript_rules_only/` | `transcript_rule_matchers` | `transcript_rules` populated |
| `trace_checks_heavy/` | `trace_check_operators` (bind + before + within) | `trace_checks.constraints` non-empty |
| `custom_checks_only/` | `custom_check_executors` | `custom_checks.enabled: true` |
| `rubric_only/` | `rubric_evaluators` + `judge_model_providers` | `llm_judge.rubric` populated |

The `state_checks_db_probes_only/` pack ships scripted probe rows on
`parity.yaml`'s `db_probe_rows` field (keyed by probe name); the
harness monkeypatches `_fetch_probe_rows` symmetrically on both legs so
neither dials a live postgres. The `custom_checks_only/` pack ships
`checks.py` alongside its YAML — the loader base64-encodes it onto
`TaskDescription.tool_artifacts` for the grader leg and seeds
`runner._artifact_dirs[trial_id]` at the pack directory for the runner
leg, so both legs load the same source under the same relative path.

**Composite packs.** Four packs compose two or more seams so a
divergence in cross-seam folding — the combined `state_checks` slot, the
`weighted` combine method, or the ``reasons`` composition across
components — surfaces where seams meet. `hash_and_all_four/` is the one
pack whose grader leg refuses rather than grades: `state_checks.hash_enabled`
reaches the grader-side `GraderCompositeDispatch.grade` refusal branch
(`grader_rpc cannot execute hash-based grading`) — its `parity.yaml`
declares `refusal_mode: true` and its committed `expected_grade.json`
records the runner leg's Grade only. The refusal fragment is the entire
grader-side contract.

| Pack directory | Composition | Divergence handling |
|---|---|---|
| `state_plus_transcript/` | `state_checks.jsonpath_checks` + `transcript_rules` | pure parity; `accepted_divergences: []` |
| `state_plus_judge/` | `state_checks.jsonpath_checks` + scripted `llm_judge` | pure parity; `accepted_divergences: []` |
| `all_four_no_hash/` | jsonpath + transcript + trace + scripted judge + `custom_checks` | pure parity; `hash_enabled` off |
| `hash_and_all_four/` | hash + jsonpath + transcript + trace + scripted judge + `custom_checks` | refusal: grader raises `GradingFailedError` matching `cannot execute hash-based grading`; only the runner leg produces a Grade |

`test_composite_pack_parity` runs both legs for the first three; the
refusal case is covered by `test_composite_pack_parity_hash_refusal`,
which asserts the runner leg matches the committed baseline and
`assert_grader_rpc_refuses` accepts the declared fragment.

**Regression-detection lock.** Two canonical tests prove the parity
gate would name the seam that regressed rather than only "the baseline
diverged":

- `test_regression_sim_baseline_flip_names_the_component` mutates one
  `components[<name>]` entry in a temp-copy baseline. The failure
  message emitted by `refresh_or_assert_baseline` names the mutated
  component and no other — the committed baseline is never touched.
- `test_regression_sim_leg_divergence_names_the_component` runs the
  runner leg with production `grade_trace_checks`, then monkeypatches
  the composite module's binding and runs the grader leg against the
  divergent scoring. The two serialised Grades differ only on the
  `components.trace_checks` slot per `components_diff` — proving the
  parity gate would surface a real between-legs code divergence at
  that seam.

**RC-smoke guarantees.** The publish-images workflow's `smoke:` job runs
the same corpus against the freshly-pulled RC runner + grader images over
the real gRPC wire — one step alongside the image-level rc-smoke, no new
job (`tests/integration/deploy/test_rc_smoke_parity_reference.py`). Two
assertion tiers, honestly split by whether the pack exercises `llm_judge`
— canonical parity through the harness is what backs the tighter guarantee
on the judge packs, since the container lane has no scripted-client seat.

| Tier | Packs | RC-smoke assertion |
|---|---|---|
| Deterministic | `state_checks_jsonpath_only`, `transcript_rules_only`, `trace_checks_heavy`, `custom_checks_only`, `state_plus_transcript` | Byte-identical `Grade` against the committed baseline via the same `serialise_grade` canonical projection. This is the shipped byte-parity guarantee. |
| Deterministic (excluded) | `state_checks_db_probes_only` | Skipped in RC-smoke: the pack's `db_probes.dsn` points at an `app-db` postgres absent from the standalone compose stack. Canonical parity via the monkeypatched `_fetch_probe_rows` covers it. |
| Wire-shape | `rubric_only`, `state_plus_judge`, `all_four_no_hash` | Grader dispatched keylessly; the missing LLM key surfaces as `judge_status=JUDGE_STATUS_ERRORED` with a `JUDGE ERRORED` segment in `reasons`. Non-judge components (state / transcript / trace / custom) still byte-match the baseline's non-judge components. A `success=false` outcome on a judge-using pack is refused as a regression. |
| Refusal | `hash_and_all_four` | Grader returns `GradeResponse(success=false)` whose `error` carries the ADR-0039 "cannot execute hash-based grading" fragment. Refusal precedes any judge dial, so the assertion is keyless regardless of tier. |

The pack loader reads the SAME `tests/canonical/grader_parity_baselines/`
directory the canonical parity test reads — no corpus fork, so a baseline
regeneration on one lane surfaces on the other automatically. An
import-time `assert _BASELINES_ROOT.is_dir()` in the integration module
fails collection if the shared corpus goes missing.

Follow-up scoped for after this gate stabilises: an in-image scripted
judge provider (`ScriptedJudgeModelProvider` under
`tolokaforge.judge_model_providers`, driven by a JSON script mount) would
promote the wire-shape tier to deterministic — the four judge packs would
byte-check their full baseline in RC-smoke without an LLM key. Deliberately
out of scope here to keep the parity gate landable now.

## See also

- [ADR-0014 — TrialGrader Protocol](adr/0014-trial-grader-protocol.md)
- [ADR-0022 — Runtime Independence](adr/0022-runtime-independence.md)
- [ADR-0038 — Grader Detachment](adr/0038-grader-detachment.md)
- [ADR-0039 — Standalone Grader Substrate](adr/0039-standalone-grader.md)
- [STANDALONE_RUNNER.md](STANDALONE_RUNNER.md) — the sibling doc for the
  runner-as-component surface
