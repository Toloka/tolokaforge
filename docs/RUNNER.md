# Runner Guide

This guide covers Tolokaforge's queue-backed runner for local and distributed
execution — the `prepare` / `worker` / `status` batch flow.

> **Looking to consume the runner as an independent component** — from your own
> agent loop, from a non-Python control plane, via
> `tolokaforge.runner.run_trial(...)`, or via the `tolokaforge run-trial`
> subprocess CLI — see [STANDALONE_RUNNER.md](STANDALONE_RUNNER.md). This guide
> is the different tool for the different job of running a whole batch.

## Execution Modes

Tolokaforge supports two queue backends:

| Backend | Use case | Shared across machines | Extra infra |
| --- | --- | --- | --- |
| `sqlite` | Local runs, CI smoke, single machine | No | None |
| `postgres` | Distributed workers across machines/runners | Yes | Postgres |

## Lifecycle

1. `prepare`: discovers tasks and enqueues `(task_id, trial_index)` attempts.
2. `worker`: leases attempts, executes them, and marks `completed`/`failed`/`requeued`.
3. `status`: shows queue counts, ETA, estimated cost, and token totals from artifacts.

### The pre-run gate

Before a single trial is scheduled — by `run`, and by `prepare` so a distributed
enqueue is rejected once rather than by every worker identically — the run makes one
pass over every selected task and checks two things it must not discover later:

- **The judge model.** A task grading with `llm_judge` when the run config carries no
  `models.judge` aborts the run, naming the offending tasks.
- **The grading block.** Each task's grading config goes through the same predicate
  `tolokaforge validate` applies: the migration rejections the typed grading blocks
  carry, and the authoring rules checked against the tools that task gives its actors
  (see [GRADING.md § What is validated before a run](GRADING.md#what-is-validated-before-a-run)).
  Every offending task is named in one abort — an author fixing a run's packs wants
  the list, not the first entry. `evaluation.grading_validation.fail_on` names the
  least severe class that is fatal; what the gate could not check is logged and fails
  nothing.

The pass resolves each task's wire description once and keeps it, so the trials that
follow reuse it rather than rebuilding it. A task naming an adapter the host has not
installed is rejected in the same pass.

Queue attempt states:
- `pending`
- `leased`
- `running`
- `completed`
- `failed`
- `cancelled`

## Key Config Fields

`orchestrator` fields used by runner behavior:

```yaml
orchestrator:
  repeats: 5
  queue_backend: "sqlite"         # "sqlite" or "postgres"
  queue_postgres_dsn: null         # required for postgres backend
  max_attempt_retries: 1           # retry transient failures
  max_requests_per_second: 1.0     # global request throttle across workers
  max_budget_usd: 50.0             # hard spend cap for the run
  runtime: "docker"               # Docker-based execution (required)
  auto_start_services: true       # auto-start Docker services via ServiceStack (default)
```

## Docker Service Management

Docker services are managed via the `tolokaforge docker` CLI commands (replacing docker-compose):

```bash
# Build Docker images (with content-hash caching)
uv run tolokaforge docker build          # Build all images
uv run tolokaforge docker build --core   # Build core images only

# Start/stop service stacks
uv run tolokaforge docker up --profile core   # Start core stack
uv run tolokaforge docker down --volumes      # Stop and cleanup

# Check service status
uv run tolokaforge docker status
```

When a task declares its own `environment_manifest`, the runner runs
alongside those task-declared services for the duration of each trial,
provisioning them per the manifest's isolation rules. See
[RUNTIME_BACKENDS.md](RUNTIME_BACKENDS.md) for
the backend lifecycle.

### Image resolution — pull vs build

At `tolokaforge run` time the four first-party service images
(`runner`, `db-service`, `rag-service`, `mock-web`) resolve through a
`docker.image_source` policy with three values. Design rationale,
alternatives considered, and the failure-mode contract are captured in
[ADR-0031](adr/0031-pull-vs-build-default-for-service-images.md).

| `docker.image_source` | Behavior |
| --- | --- |
| `auto` (default) | Pull the published `tolokasoft1/tolokaforge-<svc>:<engine_version>` image from Docker Hub when the engine is a wheel install (`pip install tolokaforge`); build locally from source when running from a repo checkout. Falls back to a local build on any pull failure with a `logger.warning` naming the exact reason. |
| `pull` | Always pull. On pull failure — 404, 429, unreachable — this is a hard failure with no fallback. Use when you need a strong "pull or die" guarantee (e.g. arena runs where a silent rebuild would waste minutes per fresh host). |
| `build` | Always build locally. Skip pull entirely. Use when editing Dockerfiles or service code, or in air-gapped environments that never touch Docker Hub. |

Override the config value with the `--image-source` flag on
`tolokaforge run` or the `TOLOKAFORGE_IMAGE_SOURCE` environment
variable. Precedence: flag > env > `docker.image_source` in the run
config > default (`auto`). The engine version used for the pull tag
comes from `tolokaforge.__version__` (`importlib.metadata.version(
"tolokaforge")`); a source checkout without a `pip install` reports a
sentinel and always resolves `auto` to `build`.

For the Docker Hub tag axis (`:X.Y.Z` immutable, `:X.Y` moving,
`:latest`), see the published-images section of
[STANDALONE_RUNNER.md](STANDALONE_RUNNER.md#published-images).

Docker Hub rate-limits anonymous pulls to 100 per 6 h per IP. Shared
CI runners or arena hosts that drive many `tolokaforge run` invocations
should either configure authenticated pulls via the daemon's standard
`~/.docker/config.json` (`docker login`), or set `docker.image_source:
build` to skip pull entirely. A 429 in `auto` mode surfaces as a
`WARNING` line naming `rate_limited` before the fallback build starts.

### Runner readiness contract

The runner is gated for readiness at two independent layers, and they answer
different questions:

- **Container-internal `HEALTHCHECK`** (`runner.Dockerfile`) opens a gRPC channel
  to `localhost:50051` *inside the container*. It answers "has the gRPC server
  bound its port?" and is what `docker compose up --wait` blocks on — but a
  loopback probe cannot tell whether the port is reachable from outside the
  container.
- **Host-side readiness gate** (`PerTrialRuntimeBackend.provision`, per-trial
  runs) opens a gRPC channel to the runner's *published host port* from the
  orchestrator process. It answers "can the engine actually invoke this runner?"
  — the guarantee the container-loopback healthcheck cannot give. The runner is
  always probed with the `grpc` kind; provisioning fails fast with an actionable
  `ProvisionError` (naming the resolved `host:port` and the container's listen
  addresses) rather than surfacing as a downstream client-connect timeout. See
  [RUNTIME_BACKENDS.md § Readiness gate](RUNTIME_BACKENDS.md#readiness-gate).
- **RC-image parity exercise** — every published RC runner + grader image
  pair is driven through the 10-pack per-component parity gate over the real
  gRPC wire before promotion; see
  [GRADER_SERVICE.md § Parity gate — RC-smoke guarantees](GRADER_SERVICE.md#parity-gate).

## Runner Image Contents

`runner.Dockerfile` is a multi-stage build on a `python:3.12-slim` base
(≈390 MB uncompressed). A `builder` stage installs the `tolokaforge` wheel and
its `runner` extra into an isolated `/opt/venv` with the build-only apt
toolchain (`curl`, `git`); the `runtime` stage copies only that venv, so the
build toolchain never ships.

- **Dependency surface** — the image installs `tolokaforge[runner]`. The
  `runner` extra (declared in `pyproject.toml`) is the single source of truth
  for the runner's domain-tool runtime drivers (`asyncpg`, `psycopg2-binary`,
  `alembic`, `python-jose`, `fastapi`, `uvicorn`, `sqlalchemy`, `odata-query`)
  that task tool code extracted from `tool_artifacts` needs at grade time. The
  harness's own imports come with the base wheel — including `litellm`, run
  in-container for LLM-as-judge grading.
- **Not in the default image** — the pip/setuptools/wheel toolchain and `*.pyc`
  bytecode are stripped, and the **docker CLI + compose plugin are absent**.
- **Opt-in build args** — `INSTALL_DOCKER_CLI=true` adds the docker CLI +
  compose plugin (for terminal-bench tasks, which shell out to the host Docker
  daemon via the mounted socket); `INSTALL_PLAYWRIGHT=true` adds Playwright +
  Chromium (for browser/mobile tasks). Both are set automatically by the
  orchestrator when it detects a run that needs them — a terminal-bench adapter
  for the docker CLI, a browser/mobile tool for Playwright — so
  `tolokaforge-runner:local` is built with exactly what a run requires and stays
  slim otherwise.

### Engine / image version lock

The image and the engine that drives it speak one wire protocol, and the pairing
is enforced. `ENGINE_PROTOCOL_VERSION` in
[`tolokaforge/runner/protocol.py`](../tolokaforge/runner/protocol.py) is the single
source of that number; the engine declares it on every `RegisterTrial`, and the
runner **refuses to register a trial from an engine below its own version**,
naming the skew in `RegisterTrialResponse.error`. The orchestrator already treats a
registration failure as fatal, so a skewed pair fails before any tokens are spent.

**This gate's bound is one-sided, and the unprotected direction is the quieter one.**
An **older** engine against a newer image fails every trial at registration, loudly —
for the field it cannot send (`call_id`) and, from protocol version 2 on, for the two
`user_simulator` keys it still emits into the trial spec that the current image no
longer declares. A **newer** engine against an older image passes *this* gate — the
older runner does not know the `engine_protocol_version` field, and proto3 drops
unknown fields on a proto message rather than erroring — so the version skew itself
surfaces later and less clearly: that engine sends a `call_id` on every `ExecuteTool`
which the older runner also ignores, so calls are recorded without the id grading
joins on. Each version and what it first changed is listed in
[`GRPC_PROTOCOL.md`](GRPC_PROTOCOL.md#version-lock) § Version lock; refusing an
engine below the bound at the gate, rather than at model validation, is what a bump
buys.

**That reasoning covers the proto message only, and registration also carries a
JSON payload where it does not hold.** The trial spec crosses as `trial_spec_json`,
parsed by `extra="forbid"` Pydantic models, so a field the older image does not
declare is a validation error rather than a dropped byte. Several grading keys are
emitted on **every** pack, so a newer engine against an older image is rejected at
`RegisterTrial` for any pack at all, with a Pydantic `extra_forbidden` error naming
the field. Which keys bite, from which release, and in which direction is one table:
[`GRADING.md`](GRADING.md#runner-engine-version-lock) § Runner-engine version lock.

**So the order matters: rebuild the image before rolling the engine.** Upgrading
the engine first leaves you inside the one window this gate cannot close, and — for
any pack bearing `state_checks` or `transcript_rules` — inside a registration failure
the JSON payload will raise anyway.

**The image's own dependency resolution is a second, unversioned skew, and no gate sees it.**
The image installs the wheel with **pip**, which resolves each declared range itself rather
than reading `uv.lock`, so a range loose enough to admit two majors gives the container a
different library than the host venv — with no protocol version to disagree about. The failure
lands at run time inside the container: see
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#every-tool-call-fails-mcp-server-closed-connection)
§ Every Tool Call Fails. `make docker-build-core` is the fix, and an upper bound on the major
is what keeps the resolution honest.

That is what an engine upgrade needs: rebuild the image from the same tree
(`make docker-build-core`) or pin an image tag that matches. The gate sits at
registration rather than per call deliberately — an old engine sends no `call_id`,
and rejecting each `ExecuteTool` would reach the agent as an ordinary tool failure,
so it would retry until its turn budget was gone and report a completed trial
scoring near zero, with the skew visible only inside the transcript.

### Runner subset — what the runner image ships

The published PyPI wheel carries every `tolokaforge/**` file; the runner Docker
image installs a Docker-only *subset* build of that same tree so the image
contains only code the runner actually runs
([ADR-0025](adr/0025-runner-wheel-split.md) § "The module partition"). The
canonical enumeration lives in
[`tolokaforge/core/_runner_subset.py`](../tolokaforge/core/_runner_subset.py) —
consumed verbatim by the hatch build target and locked against the runner
container's actual runtime import closure by
[`tests/canonical/test_runner_subset_partition.py`](../tests/canonical/test_runner_subset_partition.py).

**Building the subset wheel.** The `[tool.hatch.build.targets.custom]`
section of [`pyproject.toml`](../pyproject.toml) declares the subset build
target; the custom builder at
[`scripts/hatch/hatch_runner_subset_builder.py`](../scripts/hatch/hatch_runner_subset_builder.py)
renames the distribution to `tolokaforge-runner-subset`, replaces the base
wheel's dependency list with the runner-runtime deps, and binds the
subset-native CLI shim
([ADR-0027](adr/0027-subset-native-cli-shim.md)) — `tolokaforge =
tolokaforge.runner._cli:main` — as the subset wheel's `[console_scripts]`
entry, and carries every runner-reachable seam group verbatim from
`pyproject.toml`: `tolokaforge.custom_check_executors`,
`tolokaforge.judge_model_providers`, `tolokaforge.rubric_evaluators`,
`tolokaforge.transcript_rule_matchers`, `tolokaforge.state_check_backends`,
and `tolokaforge.trace_check_operators`. Without these, the runner boots
then crashes at first seam load with "Unknown implementation …". The
canonical enumeration lives at
`scripts/hatch/hatch_runner_subset_builder.py::RUNNER_REACHABLE_ENTRY_POINT_GROUPS`
and two drift-locks guard it: `test_subset_wheel_carries_runner_reachable_entry_point_groups`
asserts each group's rows target modules the subset ships, and
`test_subset_partition_load_calls_are_in_the_allowlist` walks every
`load_*` seam call reachable from the subset partition and asserts its
group is in the allowlist. The other groups (`runtime_backends`,
`trial_graders`, `conductors`, `service_readiness_probes`,
`turn_policies`, `grading_substrates`) point at modules the subset does
not ship — their loaders are called from `tolokaforge.core.runner` /
`tolokaforge.grader.composite_dispatch`, which live outside the subset
partition — and are deliberately excluded from the subset wheel.

```bash
uv run hatch build --target custom
# → dist/tolokaforge_runner_subset-<version>-py3-none-any.whl
```

The base `tolokaforge` wheel — `hatch build --target wheel` /
`uv build --wheel` — is unchanged, still published to PyPI, and still installs
via `pip install tolokaforge` / `pip install tolokaforge[runner]`. The subset
wheel is a Docker-only artifact and is never uploaded to PyPI.

**Whole subpackages in the subset:**

| Path | Rationale |
|---|---|
| `tolokaforge/runner/` | The runner service, gRPC glue, DB / RAG clients, tool factory, runner-side grading. |
| `tolokaforge/secrets/` | Single-abstraction secret manager reconstructed from `TOLOKAFORGE_SECRETS_JSON`. |
| `tolokaforge/tools/` | Tool registry + built-in tool drivers the tool factory dispatches by name at `RegisterTrial`. |
| `tolokaforge/core/actors/` | Actor seams the turn loop dispatches on. (One file excluded — see below.) |
| `tolokaforge/core/models/` | Wire types the gRPC surface serialises, plus the run-config blocks `RunConfig` is typed by — `docker_config.py` rides along because `RunConfig` carries it; the runner does not read it. |
| `tolokaforge/core/llm/` | LLM client + policies; the runner runs LLM-as-judge in-container. (One file excluded — see below.) |
| `tolokaforge/core/grading/` | Grading substrate — check runner, checks helpers, judge, key manifest, state composition, state diff, trace timeline, transcript wire. (Eleven files excluded — see below.) |

**Loose files in the subset:**

- `tolokaforge/__init__.py` — package init; lazy `__getattr__` symbols that
  resolve to orchestrator modules (`Orchestrator`, metrics, run queue) are
  present but raise `AttributeError` on the slim image, and the runner never
  reads them.
- `tolokaforge/core/__init__.py`, `tolokaforge/core/_runner_subset.py` —
  the subset's own audit artifact and the `core/` package init.
- `tolokaforge/core/deprecations.py`, `hash.py`, `logging.py`, `loop.py`,
  `netpolicy_constants.py`, `pricing.py`, `run_display_events.py`, `trial.py`
  — the shared-spine files at the root of `core/` the runner closure reaches
  directly.

**Data files in the subset:**

- `tolokaforge_models/data/pricing.json`, `tolokaforge_models/data/model_presets.yaml`
  — non-Python payloads `tolokaforge.core.pricing` and
  `tolokaforge.core.llm.presets` read at import time via
  `importlib.resources`. Shipped as `RUNNER_SUBSET_DATA_FILES` inside
  `_runner_subset.py`; without them the runner image would boot with an
  empty pricing table (cost telemetry silently zero) and the preset
  registry would raise on first grading-model resolution.
- `tolokaforge/_python_version.txt` — the pinned Python minor version;
  landed via a `force-include` remap of the repo-root `.python-version`
  dotfile, mirroring the base wheel's identical remap.

**Subset-native CLI shim ([ADR-0027](adr/0027-subset-native-cli-shim.md)):**

- `tolokaforge/runner/_cli.py` — the module bound as the subset wheel's
  `tolokaforge` console script. Preserves the ADR-0024 committed exec
  surface (`tolokaforge --version` / `tolokaforge run-trial`) inside the
  slim image without dragging `tolokaforge/_entry.py` or `dx/cli/*`
  (base-wheel only) into the subset. The shim's `run-trial` orchestrates
  in-process against the local runner gRPC service and cannot exercise
  adapter-specific setup — see
  [STANDALONE_RUNNER.md § Command surface of the published runner image](STANDALONE_RUNNER.md#command-surface-of-the-published-runner-image)
  for the narrower semantics.

**Excluded — orchestrator-only files under a subpackage otherwise in the subset:**

The enumeration below is the one in `RUNNER_SUBSET_EXCLUDED_FILES`; the
canonical test rejects drift between them and the pyproject mirror.

| Path | Excluded because |
|---|---|
| `tolokaforge/core/actors/turn_policy.py` | Reaches `core.plugin_registry` (orchestrator-only) for `TurnPolicyContext`. |
| `tolokaforge/core/grading/agreement.py` | Shared-spine imports only; consumed by the offline rubric-migration commands. |
| `tolokaforge/core/grading/combine.py` | Imports `core.grading.state_checks`, itself orchestrator-only. |
| `tolokaforge/core/grading/config_validation.py` | Shared-spine imports only; consumed by the pre-run authoring gate. |
| `tolokaforge/core/grading/corpus_curation.py` | Imports `core.output.artifacts` and `core.output_writer` (orchestrator-only). |
| `tolokaforge/core/grading/migration_declaration.py` | Reaches the same two through its `corpus_curation` import. |
| `tolokaforge/core/grading/replay.py` | Imports `core.output.artifacts` (orchestrator-only). |
| `tolokaforge/core/grading/replay_layout.py` | Standard library only; consumed by the offline replay commands. |
| `tolokaforge/core/grading/rubric_migration.py` | Imports the adapters' private task loader (orchestrator-only). |
| `tolokaforge/core/grading/state_checks.py` | Imports `core.utils.diff` (orchestrator-only). |
| `tolokaforge/core/grading/trace_replay.py` | Imports `core.output.artifacts` (orchestrator-only). |
| `tolokaforge/core/grading/unknown_keys.py` | Shared-spine imports only; consumed by the pre-run authoring gate. |
| `tolokaforge/core/llm/fallback_client.py` | Consumed only by `dx/cli/main.py`. |

**Not in the subset:** everything at the `tolokaforge/core/` root not listed above
(the `Orchestrator` class, dry-run, output writer, config validator, compose
materialisation, engine run state, backend capabilities, the `RuntimeBackend` /
`Conductor` / `TrialGrader` Protocol definitions and their factories, the
`run_trial` library entry, run queue, resume, project loader, plugin registry,
metrics, budgets, and the remaining utility modules);
`tolokaforge/core/output/`; `tolokaforge/core/search/`; `tolokaforge/core/utils/`;
`tolokaforge/core/schema/`; `tolokaforge/adapters/`; `tolokaforge/dx/`;
`tolokaforge/docker/`; `tolokaforge/env/`; `tolokaforge/runtime/`;
`tolokaforge/_entry.py`.

## Tool Lifecycle

Some tools own per-trial resources — a compose stack, a long-lived
subprocess — that must be provisioned when a trial starts and torn down when
it resets. The runner manages this generically off a single capability, never
off adapter identity: a `ToolWrapper` sets `has_lifecycle = True`, and the
runner calls `start()` on `RegisterTrial` and `stop()` on `ResetTrial` for
every tool that declares it. Tools without the capability are untouched.

`start()` receives a `ToolLifecycleContext`:

- `trial_id` — the trial the resource belongs to (used for per-trial naming).
- `artifacts_dir` — the extracted task-artifacts path, or `None`.
- `work_dir` — the per-trial session working root a tool seeds its
  shell/subprocess `cwd` from, or `None`.

The context is a frozen value object; tools read it, never mutate it.

A session-lifetime tool follows one pattern: open its resource in `start()`
(seeding `cwd` from `work_dir`), hold it across every `execute()` call, and
tear it down in `stop()`. The resource — and its identity, e.g. a subprocess
PID — is stable for the life of the trial and gone once `stop()` returns.

## Local Queue Run (SQLite)

```bash
uv run tolokaforge prepare --config examples/native/coding/run_configs/dev.yaml --run-dir results/queue_run --reset-queue
uv run tolokaforge worker --config examples/native/coding/run_configs/dev.yaml --run-dir results/queue_run
uv run tolokaforge status --run-dir results/queue_run
```

Run multiple local workers on one machine:

```bash
uv run tolokaforge worker --config examples/native/coding/run_configs/dev.yaml --run-dir results/queue_run &
uv run tolokaforge worker --config examples/native/coding/run_configs/dev.yaml --run-dir results/queue_run &
wait
```

## Distributed Run (Postgres)

Use Postgres when workers run on different hosts/containers/runners.

1. Set backend config:

```yaml
orchestrator:
  queue_backend: "postgres"
  queue_postgres_dsn: "postgresql://<postgres-host>:5432/tolokaforge"
```

2. Prepare queue once:

```bash
uv run tolokaforge prepare --config examples/native/coding/run_configs/dev.yaml --run-dir results/distributed_run --reset-queue
```

3. Start N workers (on any machines with access to the same Postgres):

```bash
uv run tolokaforge worker --config examples/native/coding/run_configs/dev.yaml --run-dir results/distributed_run
```

4. Monitor:

```bash
uv run tolokaforge status --run-dir results/distributed_run --config examples/native/coding/run_configs/dev.yaml
```

### GitHub Actions

For multi-runner GitHub Actions, use `.github/workflows/distributed-workers.yml`.
It requires a shared Postgres DSN via either:
- workflow input `queue_postgres_dsn`, or
- repo secret `TOLOKAFORGE_QUEUE_POSTGRES_DSN`.

## Resuming a queue-worker run

Workers restart into a resumable state without a flag: the durable queue is the source of truth for pending / leased / completed / failed attempts, and `worker --run-dir <existing>` leases only `pending` items. Restart the same command against the same run directory (SQLite) or the same Postgres DSN (distributed) and the worker picks up whatever wasn't finished.

```bash
uv run tolokaforge worker --config path/to/run.yaml --run-dir results/queue_run
```

On reattach to a run directory whose queue holds completed attempts, the worker emits an INFO log line naming the run and the queue state:

```
14:32:07.918 | INFO | completed=42 pending=8 run_id=queue_run total=50 | Reattaching to run dir queue_run: 42/50 completed, 8 pending in queue.
```

Completions and behavioural failures already in the queue stay marked as such — the worker never re-executes them. Retryable in-flight leases whose lease expired are recovered on reattach (logged as `Worker recovered stale in-flight attempts`) and returned to `pending` so a fresh lease attempt runs them.

To restart a queue from scratch, pass `--reset-queue` to `prepare`:

```bash
uv run tolokaforge prepare --config path/to/run.yaml --run-dir results/queue_run --reset-queue
```

`--reset-queue` clears every attempt row before re-enqueueing. Without it, `prepare` refuses to re-enqueue over a populated queue (logged as `Queue already populated; skipping enqueue`) so a re-`prepare` on a working directory is a safe no-op.

## Retries, Rate Limits, and Budget

Execution controls interact as follows:

1. `max_requests_per_second` throttles request throughput across worker threads in a process.
2. `max_attempt_retries` requeues retryable failures (timeouts/rate-limit/API/resource failures).
3. `max_budget_usd` stops new work when estimated cumulative cost reaches the cap.

Practical guidance:
- Start with low `max_requests_per_second` when provider limits are unknown.
- Keep `max_attempt_retries` small (`1-2`) to avoid infinite churn on invalid tasks.
- Always set `max_budget_usd` for long runs.

### Retryability and countability are two questions

One classification of a finished trial answers both, and the answers are
independent by design:

- **Is another attempt worth making?** `Orchestrator._is_retryable_trajectory`.
  Anything transient — a rate limit, an API error, a timeout, a bare error, a
  `trial_lost` registration — is requeued until `max_attempt_retries` is spent. A
  deterministic fault (`provision_error`, an auth-shaped `api_error`) is not: the
  next attempt fails the same way.
- **Did the attempt measure the agent?** `classify_trial_outcome`. Only a trial
  killed by a *typed* infrastructure condition — `rate_limit`, `api_timeout`,
  `provision_error` — leaves the rate denominators.

They disagree, and the disagreements are the point. A wall-clock `timeout`, an
`api_error`, a bare `error` and a `trial_lost` are all retried *and* counted:
repeating them may help, and the trial they describe is either one the agent was
measured on or one whose fault is ours to carry. Deriving either answer from the
other would silently either stop retrying transient failures or start excusing
agent failures from the benchmark.

`trial_lost` is the one of those the agent had no part in: the runner no longer
holds the trial the engine is running — a restarted shared-stack runner, a
shutdown sweep, or the deregistration before a retry — so the tool call that hit
it reached no tool. The trial ends there rather than spending its turn budget on
refusals, classifies as `harness_error`, and is **not graded**: the runner that
would compute the verdict is the one that lost the trial, so no fabricated score
enters `avg_score`. Re-registering is exactly the repair, which is why it is
retried.

Retries are exhausted before any of this is recorded, so a trial that eventually
succeeded contributes one measured result, not one per attempt. See
[`docs/GRADING.md`](GRADING.md:1) § Infrastructure aborts produce no grade.

Engine-loop trials on presets that declare `context_watermark` +
`max_context_tokens` emit a `role: system` message shaped
`"Context summarized before turn N (...); wire history reset."` when the
loop's context-window seam fires (see
[`docs/LLM_LAYER.md`](LLM_LAYER.md:1) § Context-window handoff). Grading
reads through it — it is a `role: system` message and per G3/N3 in
[`docs/GRADING.md`](GRADING.md:1) is not an event. `Trajectory.messages`
still carries the full pre-summarize view, so the grader's timeline
builder is unaffected; only the wire prompt on subsequent turns sees the
compacted view. A trial that hits the context wall with no recovery
terminates with `TerminationReason.CONTEXT_WINDOW_EXCEEDED` and
`TrialStatus.FAILED`; `TrialGrader` auto-fails it without dispatching
to the runner grader.

## Programmatic Queue Access

```python
from pathlib import Path

from tolokaforge import create_run_queue

queue = create_run_queue(
    "sqlite",
    sqlite_path=Path("results/my_run/run_queue.sqlite"),
    max_retries=1,
)

counts = queue.get_counts()
print(counts)
```

For Postgres:

```python
queue = create_run_queue(
    "postgres",
    sqlite_path=Path("results/my_run/run_queue.sqlite"),
    max_retries=1,
    postgres_dsn="postgresql://<postgres-host>:5432/tolokaforge",
)
```

## Output Artifacts

Queue state + per-attempt artifacts are written under `run_dir`:

- `run_queue.sqlite` (sqlite backend only)
- `trials/<task_id>/<trial>/trajectory.yaml`
- `trials/<task_id>/<trial>/metrics.yaml`
- `trials/<task_id>/<trial>/grade.yaml` — **only when the trial produced a
  grade.** A trial the infrastructure aborted has no verdict to write, so a reader
  must not assume the file is there
- `aggregate.json`
- `per_task_metrics.json`
- `metadata_slices.json`
- `failure_attribution.json`

See [ANALYTICS.md](ANALYTICS.md) for interpretation.
