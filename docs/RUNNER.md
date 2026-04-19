# Runner Guide

This guide covers Tolokaforge's queue-backed runner for local and distributed execution.

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

## Local Queue Run (SQLite)

```bash
uv run tolokaforge prepare --config my_run_config.yaml --run-dir results/queue_run --reset-queue
uv run tolokaforge worker --config my_run_config.yaml --run-dir results/queue_run
uv run tolokaforge status --run-dir results/queue_run
```

Run multiple local workers on one machine:

```bash
uv run tolokaforge worker --config my_run_config.yaml --run-dir results/queue_run &
uv run tolokaforge worker --config my_run_config.yaml --run-dir results/queue_run &
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
uv run tolokaforge prepare --config my_run_config.yaml --run-dir results/distributed_run --reset-queue
```

3. Start N workers (on any machines with access to the same Postgres):

```bash
uv run tolokaforge worker --config my_run_config.yaml --run-dir results/distributed_run
```

4. Monitor:

```bash
uv run tolokaforge status --run-dir results/distributed_run --config my_run_config.yaml
```

## Retries, Rate Limits, and Budget

Execution controls interact as follows:

1. `max_requests_per_second` throttles request throughput across worker threads in a process.
2. `max_attempt_retries` requeues retryable failures (timeouts/rate-limit/API/resource failures).
3. `max_budget_usd` stops new work when estimated cumulative cost reaches the cap.

Practical guidance:
- Start with low `max_requests_per_second` when provider limits are unknown.
- Keep `max_attempt_retries` small (`1-2`) to avoid infinite churn on invalid tasks.
- Always set `max_budget_usd` for long runs.

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

## LLM Judge Evaluation

The Runner evaluates `llm_judge` grading inline during trial execution. API keys required by the judge model are injected into the Runner container via `ServiceDefinition.secret_keys`.

**How secrets flow:**

1. Orchestrator reads `grading.yaml` → extracts `llm_judge.model_ref`
2. `model_ref` is mapped to the required API key (e.g., `openrouter/...` → `OPENROUTER_API_KEY`)
3. `SecretManager.to_env_dict()` resolves the key values from `.env` / environment
4. Keys are passed to the Runner container as environment variables during `Container.create()`
5. Runner calls litellm with the judge model to score the agent's transcript

Only the specific keys needed for the judge model are injected — not all available secrets. See [SECURITY.md](SECURITY.md) for the security model.

## Output Artifacts

Queue state + per-attempt artifacts are written under `run_dir`:

- `run_queue.sqlite` (sqlite backend only)
- `trials/<task_id>/<trial>/trajectory.yaml`
- `trials/<task_id>/<trial>/metrics.yaml`
- `trials/<task_id>/<trial>/grade.yaml`
- `aggregate.json`
- `per_task_metrics.json`
- `metadata_slices.json`
- `failure_attribution.json`

