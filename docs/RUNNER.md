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

When a task declares its own `environment_manifest`, the runner runs
alongside those task-declared services for the duration of each trial,
provisioning them per the manifest's isolation rules. See
[architecture/RUNTIME_BACKENDS.md](architecture/RUNTIME_BACKENDS.md) for
the backend lifecycle.

## Local Queue Run (SQLite)

```bash
uv run tolokaforge prepare --config examples/native/coding/run_config.yaml --run-dir results/queue_run --reset-queue
uv run tolokaforge worker --config examples/native/coding/run_config.yaml --run-dir results/queue_run
uv run tolokaforge status --run-dir results/queue_run
```

Run multiple local workers on one machine:

```bash
uv run tolokaforge worker --config examples/native/coding/run_config.yaml --run-dir results/queue_run &
uv run tolokaforge worker --config examples/native/coding/run_config.yaml --run-dir results/queue_run &
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
uv run tolokaforge prepare --config examples/native/coding/run_config.yaml --run-dir results/distributed_run --reset-queue
```

3. Start N workers (on any machines with access to the same Postgres):

```bash
uv run tolokaforge worker --config examples/native/coding/run_config.yaml --run-dir results/distributed_run
```

4. Monitor:

```bash
uv run tolokaforge status --run-dir results/distributed_run --config examples/native/coding/run_config.yaml
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
- `trials/<task_id>/<trial>/grade.yaml`
- `aggregate.json`
- `per_task_metrics.json`
- `metadata_slices.json`
- `failure_attribution.json`

See [ANALYTICS.md](ANALYTICS.md) for interpretation.
