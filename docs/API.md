# Python API Overview

Key classes and entry points for programmatic usage.

## Driving a run as three composable steps

`run_trial` runs one trial end-to-end. To drive a whole batch from your own
orchestration layer — your own scheduler, your own distributed backend — and
still get the report a single `tolokaforge run` writes, compose three steps:

1. **`plan_trials(run_config)`** — expand the run into a flat list of
   `TrialNodeInputs`, one per `(task, repeat)`. Each is a self-contained,
   serializable description of one trial you can ship to wherever it will run.
   `plan_trials` **does** build and resolve an adapter per task — to describe
   each task, and to load the tasks through the config's adapter when
   `tasks=None` — so the adapter must be installed and node-runnable wherever
   `plan_trials` runs. Only the conductor and runtime backend stay unresolved;
   they travel as data on each trial.
2. **`run_trial_from_inputs(inputs)`** — run one `TrialNodeInputs` to a
   `TrialResult`. Your layer calls this itself, on whatever backend it owns,
   in whatever order and concurrency it likes.
3. **`aggregate_results(results, run_config=cfg, output_dir=out)`** — fold the
   `TrialResult`s (or a run directory) into the four aggregate artifacts a full
   run writes.

```python
from tolokaforge.runner import plan_trials, run_trial_from_inputs, aggregate_results

planned = plan_trials(cfg)                       # list[TrialNodeInputs]
results = [run_trial_from_inputs(i) for i in planned]  # your scheduler drives this
report = aggregate_results(results, run_config=cfg, output_dir="results/my_run")
print(report.aggregate["success_rate_micro"])
```

**Guarantee.** The guarantee is scoped to the four **aggregate** files, not the
whole run directory. The four artifacts `aggregate_results` writes
(`aggregate.json`, `per_task_metrics.json`, `metadata_slices.json`,
`failure_attribution.json`) reproduce the report a single `tolokaforge run`
writes, field for field, except the disk-only non-reproducible stamps
(`tolokaforge_version`, `captured_service_logs`). It does **not** reproduce the
rest of the run directory — notably `run_state.json` (the orchestrator's own
queue/resume ledger) and the tracing/observability receipt — which are products
of the orchestrated run, not of the aggregation step. It works the same for the
native loop and for a delegated harness — the harness identity travels as data
on each planned trial (`trial_spec.task.adapter_type`, the agent model's
`harness`), not as a resolved object. `aggregate_results` renumbers
`trial_index` per task, so independently produced single-trial results (each
`trial_index=0`) count as the repeats they are.

### plan_trials

```python
def plan_trials(
    run_config: RunConfig,
    tasks: Sequence[TaskConfig] | None = None,
    *,
    runtime: str = "auto",
    grader: str | None = None,
    conductor: str = "in_process",
    run_id: str | None = None,
) -> list[TrialNodeInputs]: ...
```

- **`run_config`** — supplies the role → model map (`models.agent` required),
  the repeat count and shuffle flag, the run-level grader block, and the node
  run settings. Tasks load through its declared adapter.
- **`tasks`** — preloaded `TaskConfig`s to plan instead of loading from the
  config; preloading gives exact control over the task set.
- **`runtime` / `grader` / `conductor`** — carried as data on each trial.
  `runtime="auto"` defers per-trial isolation to the task; `grader=None`
  carries the run config's own grader block (which defers to the adapter
  default when unset).

### aggregate_results

```python
def aggregate_results(
    results,                       # Iterable[TrialResult | Trajectory], or a run-dir Path
    *,
    run_config: RunConfig | None = None,
    tasks: Sequence[TaskConfig] | None = None,
    output_dir: Path | str | None = None,
) -> RunReport: ...
```

- **`results`** — the trials to aggregate, as `TrialResult` / `Trajectory`
  objects or a run directory whose `trials/<task_id>/<trial_index>/` bundles are
  read back.
- **`run_config` / `tasks`** — source the per-task metadata (benchmark type,
  complexity, tags, expected failure modes) the slices group by. Omit both to
  fold unclassified (or to read a run directory's per-trial `task.yaml`
  snapshots).
- **`output_dir`** — when given, the four JSONs are written here through the
  same writer a full run uses.
- **Returns** a `RunReport` (`per_task_metrics`, `aggregate`,
  `metadata_slices`, `failure_attribution`).

**Caveat.** The built-in remote dispatch adapter (`grpc_node`) still refuses
coding-harness mode — a transport limit, not a surface limit. Driving a
delegated harness through the three steps works on any backend you run
`run_trial_from_inputs` on yourself.

## run_trial

Run one trial in-process and get its `TrialResult` back, without
constructing the orchestrator or its batch lifecycle (queue, run-state,
worker pool, budget, resume).

```python
from tolokaforge.runner import run_trial

result = run_trial(
    task=task,                          # a TaskConfig
    models={"agent": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"}},
    runtime="auto",                     # or a registered backend name
    grader="runner_rpc",
    conductor="in_process",
    output_dir=None,                    # None → no disk artifacts
    trial_index=0,
)
print(result.trajectory.grade)
```

Keyword-only signature:

```python
def run_trial(
    *,
    task: TaskConfig,
    models: dict[str, ModelConfig | dict[str, Any]],
    runtime: str = "auto",
    grader: str = "runner_rpc",
    conductor: str = "in_process",
    output_dir: Path | str | None = None,
    trial_index: int = 0,
    rate_limit_probe: RateLimitProbeConfig | None = None,
) -> TrialResult: ...
```

- **`models`** — a role → model map. `agent` is required; `user` and
  `judge` are optional (`user` defaults to the engine's default user
  model). Values are `ModelConfig` instances or plain dicts.
- **`runtime="auto"`** — resolves to `per_trial` when the task's
  environment manifest requires per-trial isolation, else `shared`,
  mirroring the CLI's task-driven default. `auto` is a reserved value;
  any other value is a registered runtime-backend name.
- **`runtime` / `grader` / `conductor`** — resolved by name through the
  entry-point registries (`tolokaforge.runtime_backends`,
  `tolokaforge.trial_graders`, `tolokaforge.conductors`).
- **`output_dir=None`** — writes no artifacts to disk; pass a directory
  to persist them.
- **`rate_limit_probe=None`** — runs the standard bounded-exponential
  retry path. Pass a `RateLimitProbeConfig` (`tolokaforge.core.models`)
  to enable rate-limit probe mode for the agent and simulator clients.
  Enabling it raises the composed run config's episode budget to twice
  the probe's per-*turn* 429 wall ceiling (a probe absorbs 429s by
  sleeping, and episode wall-time counts that sleep), and `conductor`
  must name an implementation that supports the mode.

Reading the result:

- **`result.trajectory.grade` is `Grade | None`.** It is `None` in two cases: a
  trial the infrastructure aborted — a provider rate limit, an LLM API timeout, a
  provisioning failure — because the agent never ran and any score would describe
  work that never happened; and a trial whose grading ran and could not produce a
  verdict. The `print` above therefore prints `None` on a rate-limited trial, and
  a consumer that reads `.score` without branching raises `AttributeError` rather
  than reading a fabricated zero. See [ANALYTICS.md](ANALYTICS.md) § The
  denominator: measured trials.
- **`result.trajectory.grading_error` is `str | None`** and tells those two cases
  apart: non-`None` is the reason the grading substrate gave, and the trial keeps
  its own `status` / `termination_reason`. A trajectory carrying both a `grade`
  and a `grading_error` is rejected at construction and at `model_validate`.
- **`result.trajectory.tool_log` is `list[RecordedToolCall]`**, one entry per
  attempted call in trial-wide execution order, carrying `call_id`, `sequence`,
  `tool_name`, `arguments`, `executor`, `status`, untruncated `output`,
  `latency_seconds` and `timestamp`. Read `call.tool_name` (not `log["tool"]`) and
  `call.status is ToolExecutionStatus.SUCCESS` (not `log["success"]`). It is
  persisted as the bundle's `tool_log.yaml` sidecar — see
  [OUTPUT_FORMAT.md](OUTPUT_FORMAT.md) § `tool_log.yaml`.

Errors:

- **`pydantic.ValidationError`** — `models` is missing `agent` or carries
  a malformed / unexpected value, or the composed run config is invalid.
- **`ValueError`** — `rate_limit_probe` is enabled with budgets that
  cannot fit inside the episode budget, or with a `conductor` that does
  not support the mode.
- **`UnknownImplementationError`** (`tolokaforge.core.plugin_registry`) —
  a `runtime` / `grader` / `conductor` name is not registered; the
  message lists the known names.
- **`ProvisionError`** (`tolokaforge.core.runtime`) — the substrate
  failed to provision. Raised, not swallowed.

## load_task

Load a `task.yaml` from disk into a validated `TaskConfig` — the config
`run_trial` takes as its `task` argument.

```python
from tolokaforge.runner import load_task

task = load_task("path/to/task.yaml")
```

Signature:

```python
def load_task(
    path: str | Path,
    *,
    project_task_defaults: dict[str, Any] | None = None,
) -> TaskConfig: ...
```

- **`path`** — the `task.yaml` to load. Any `domain:` ref is resolved and
  merged.
- **`project_task_defaults`** — an optional `project.task_defaults` dict
  layered in below the task's own fields (task fields win on conflict).
- **Returns** a `TaskConfig` whose `.source_dir` is stamped with the
  effective task dir (the domain root for shared-domain tasks, the
  `task.yaml` parent otherwise), so the loaded task resolves its own file
  assets with no second return value.

Errors:

- **`FileNotFoundError`** — `path` does not exist.
- **`RuntimeError`** — a `domain:` ref cannot be resolved, or the YAML is
  not a top-level mapping.
- **`pydantic.ValidationError`** — the merged dict fails `TaskConfig`
  validation.

## tolokaforge run-trial

`tolokaforge run-trial` runs a single trial, in one of two modes:

- **One-shot** (`--inputs <file>`) runs one trial from a serialized
  [`TrialNodeInputs`](#versioned-trial-contract) file and exits with a code an
  out-of-process caller can act on. This is the shape a sandbox-per-trial
  backend uses: it cannot open a connection into the sandbox, so it writes the
  inputs in, runs this command, and reads the result and exit code back out.
- **Pipe** (no `--inputs`) runs one trial as a subprocess a harness in any
  language drives over stdin/stdout — the subprocess wrapper of `run_trial`.

### One-shot mode

```bash
tolokaforge run-trial --inputs trial.json \
  --task-pack-root <dir> --output-dir <dir>
```

Reads a serialized `TrialNodeInputs`, runs one trial through the standalone
entry [`run_trial_from_inputs`](TRIAL_NODE_SERVICE.md), writes the reduced bundle
and a `trial_result.json` (a serialized `TrialResult`) under `--output-dir`, and
exits.

- `--task-pack-root <dir>` and `--output-dir <dir>` override the inputs'
  `task_pack_dir` / `output_dir` with this host's paths; both apply only to
  `--inputs`. With a pack root set, each manifest `compose_file` in the inputs is
  rebased onto `<root>/<task_id>` and the task's files are read from there; the
  content-hash check still applies to whatever is mounted.

**Exit codes** — a backend reads them to decide whether to retry:

| Outcome | exit |
|---|---|
| a `TrialResult` was produced and written (a graded failure is still this — the trial ran) | 0 |
| deterministic failure, do not retry (`ProvisionError` / `TaskPackDrift` / `NotImplemented` / `InvalidConfig`) | 2 |
| transient failure, a retry may clear it (any other, unexpected, internal fault) | 3 |

The full traceback for a failure is written to stderr.

<a id="versioned-trial-contract"></a>
**Versioned JSON contract.** `TrialNodeInputs` and `TrialResult` are the JSON
shapes an out-of-process caller serializes. Their committed JSON schemas under
[`docs/schemas/`](schemas/) are the versioned contract to pin to —
`trial_node_inputs.v1.json` and `trial_result.v1.json`. `TRIAL_CONTRACT_VERSION`
(`tolokaforge.core.trial_contract`) versions those two shapes, independent of the
gRPC `NODE_PROTOCOL_VERSION` and of the package version. Both models forbid
unknown fields. Regenerate the schemas after a deliberate model change and bump
the version:

```bash
uv run python scripts/export_trial_contract_schemas.py
```

A canonical test holds the committed schemas level with the models, so a model
change reds the suite until the schema is regenerated and the version bumped.

### Pipe mode

It reads one request on stdin, runs one trial, and writes one terminal message
on stdout.

**Framing.** UTF-8 JSON Lines: one JSON object per line, `\n`-delimited, on
both stdin and stdout. Every message carries `"v":1` — the wire-protocol
version, independent of the tolokaforge package version. stdout is flushed
after the message and carries *only* the wire; diagnostics and tracebacks go to
stderr.

**stdin — one message, then EOF:**

```json
{"v":1,"type":"start","task":{},"models":{"agent":{}},"runtime":"auto","grader":"runner_rpc","conductor":"in_process"}
{"v":1,"type":"cancel"}
```

- `task` and `models` mirror the `run_trial` arguments (`models.agent` is
  required). `rate_limit_probe` is deliberately not part of the wire —
  the subprocess entry always runs the default retry path.
- `runtime` / `grader` / `conductor` are registered implementation names
  (`tolokaforge.runtime_backends` / `tolokaforge.trial_graders` /
  `tolokaforge.conductors`); omitted fields default to
  `"auto"` / `"runner_rpc"` / `"in_process"`.
- A `cancel` sent as the first line acknowledges and exits without running a
  trial.

**stdout — exactly one terminal message:**

```json
{"v":1,"type":"result","result":{}}
{"v":1,"type":"error","error_type":"ProvisionError","message":"stack failed to become ready","fatal":true}
```

- `result` is `TrialResult.model_dump(mode="json")`.
- `error` carries the named `error_type`, a human-readable `message`, and
  `fatal:true` (a single-trial invocation has no non-terminal errors).

**Error types and exit codes.** Every error path exits `1`; the `error_type`
carries the discriminating detail.

| Outcome | `error_type` | exit |
|---|---|---|
| success (`result` emitted) | — | 0 |
| malformed JSON / bad `v` / unknown `type` | `ProtocolError` | 1 |
| `cancel` message / premature stdin EOF / SIGTERM / SIGINT | `cancelled` | 1 |
| invalid `task` or `models` | `ValidationError` | 1 |
| unknown `runtime` / `grader` / `conductor` name | `UnknownImplementationError` | 1 |
| substrate provisioning failure | `ProvisionError` | 1 |
| any other failure | `InternalError` | 1 |

**Signals (POSIX).** SIGTERM and SIGINT trigger clean teardown (via
`run_trial`'s provisioning `finally` brackets) followed by a `cancelled` error
and a non-zero exit.

**Working directory.** File assets on the task (`grading.yaml`,
`initial_state.json`, tools) resolve against the subprocess working directory,
because a task crossing the wire carries no source directory. Spawn the
subprocess with `cwd` at the task-pack root, or send a fully-inline task that
references no on-disk files.

See `docs/adr/0022-runtime-independence.md` § Surface 3 for the full contract.

## Orchestrator

```python
from tolokaforge.core.orchestrator import Orchestrator

orchestrator = Orchestrator(config, output_dir="results")
run_dir = orchestrator.run()  # returns the resolved Path of the timestamped run dir
```

### `grading_completeness`

`run()` and `run_worker()` each stamp `orchestrator.grading_completeness`, a frozen
`GradingCompleteness` carrying `total_attempts`, `ungradeable_trial_ids`, and the
derived `ungradeable` / `is_complete`. It answers whether the run produced a verdict
for everything it measured — the same question the CLI turns into an exit code
([CLI.md § Run and worker exit codes](CLI.md#run-and-worker-exit-codes)), and the one
an embedder has no exit code to read:

```python
from tolokaforge.core.orchestrator import GradingCompleteness  # for typing

run_dir = orchestrator.run()
if not orchestrator.grading_completeness.is_complete:
    raise RuntimeError(
        f"{orchestrator.grading_completeness.ungradeable} trials could not be graded: "
        f"{orchestrator.grading_completeness.ungradeable_trial_ids}"
    )
```

The attribute is unbound until a run finishes, deliberately: reading it off an
orchestrator that never ran raises `AttributeError` rather than reporting a complete
run. A trial the provider or the substrate killed is an infrastructure abort and is
not counted here — it produced no verdict, but it was never measured.

## TrialRunner

```python
from tolokaforge.core.runner import TrialRunner

runner = TrialRunner(
    task_id="task_id",
    trial_index=0,
    agent_client=agent_client,
    user_simulator=user_simulator,
    tool_executor=tool_executor,
    tool_schemas=tool_schemas,
    max_turns=50,
    turn_timeout_s=60,      # declared, not yet enforced — #1147
    episode_timeout_s=1200,
    trace_id=identity.trace_id,  # optional
)
trajectory = runner.run(system_prompt, initial_message)
```

`trace_id` (the trial attempt's `TrialIdentity.trace_id`) gives the agent's and the
user simulator's calls the conversation ids `<trace_id>-agent` / `<trace_id>-user`
that a model's session header sends ([LLM_LAYER.md § Session header](LLM_LAYER.md#session-header));
without it each call sends a fresh id. A blank `trace_id` is refused with `ValueError`.

## LLMClient

```python
from tolokaforge.core.model_client import LLMClient

client = LLMClient(model_config)
result = client.generate(system="...", messages=[...])
```

## ModelCapabilities

```python
from tolokaforge.core.llm.capabilities import ModelCapabilities

caps = ModelCapabilities.for_model(name="openai/gpt-5.4", provider="openai")
# Returns resolved capabilities with schema/prompt policies
```

`tolokaforge.core.llm.capabilities` — Model capability policies (Strategy Pattern) and YAML preset loader. Presets are defined in `tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml`; the
coding-harness specs a terminal-bench harness run resolves against are defined in
`tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`. Key public symbols:

- `ModelCapabilities` — resolved capability set for a model (schema/prompt policies, feature flags)
- `DictMapParam` — dataclass describing a detected dict-map parameter (tool name, param name, value schema)
- `detect_dict_maps(tools)` — shared utility that scans tool definitions for `additionalProperties`-based dict-map parameters; used by both `StrictSchema` and `DictMapHints` policies
- `StrictSchema` — schema policy that rewrites tool schemas for strict-mode models (GPT-5)
- `DictMapHints` — prompt policy that appends system-prompt hints for dict-map parameters

## CLI

```bash
uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml
uv run tolokaforge validate --tasks "tasks/**/task.yaml"
uv run tolokaforge analyze --trajectory results/.../trajectory.yaml
```

> **Note:** Task packs live outside the engine tree. Point `task_packs`
> in your config at any directory containing tasks, or place them in
> `tasks/`. See `examples/` for the expected layout.

See `docs/REFERENCE.md` for schemas and tool definitions.
