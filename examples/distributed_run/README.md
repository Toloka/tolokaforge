# Distributed Run Example

This example shows two approaches for distributed benchmark execution.

## Self-Contained Config

`run_config.yaml` is included and uses the `package_api` minimal dataset — no
external dependencies needed.

## Approach 1: Config-Driven Sharding (GitHub Actions)

The recommended approach for CI. Uses `eval-orchestrator` to split a run config
across multiple parallel GitHub Actions jobs.

### Run via GitHub Actions

Trigger the **Benchmark Evaluation** workflow (`workflow_dispatch`) with:

- **config**: Path to run config (e.g., `examples/distributed_run/run_config.yaml`)
- **shards**: Number of parallel jobs (default: 4)
- **workers_per_shard**: Concurrency per job (default: 5)

The workflow:
1. Resolves tasks from the config's `tasks_glob`
2. Splits them into N shard configs using `eval-orchestrator split`
3. Runs each shard in parallel on Depot runners
4. Merges all shard outputs into a single result using `eval-orchestrator merge`
5. Uploads combined artifacts with aggregate metrics

### Run locally

```bash
# Split a config into shards
uv run eval-orchestrator split \
  --config examples/distributed_run/run_config.yaml \
  --shards 4 \
  --workers-per-shard 5 \
  --output-dir .ci/shards

# Run each shard (in separate terminals)
scripts/with_env.sh uv run tolokaforge run --config .ci/shards/shard_0.yaml --verbose
scripts/with_env.sh uv run tolokaforge run --config .ci/shards/shard_1.yaml --verbose
# ...

# Merge results
uv run eval-orchestrator merge \
  --input-dirs output/shard-0,output/shard-1,output/shard-2,output/shard-3 \
  --output-dir output/merged
```

## Approach 2: Queue-Backed Workers (Postgres)

For long-running distributed runs across many machines sharing a Postgres queue.

### 1) Configure Postgres Queue Backend

Edit `run_config.yaml` (or create a copy) with a shared Postgres DSN:

```yaml
orchestrator:
  queue_backend: "postgres"
  queue_postgres_dsn: "postgresql://<postgres-host>:5432/tolokaforge"
  repeats: 5
  max_attempt_retries: 1
  max_requests_per_second: 2.0
  max_budget_usd: 100.0
```

`evaluation.output_dir` is ignored by queue workers in this flow; use `--run-dir`.

### 2) Prepare Queue Once

```bash
uv run tolokaforge prepare \
  --config examples/distributed_run/run_config.yaml \
  --run-dir results/distributed_example \
  --reset-queue
```

### 3) Start Multiple Workers

Run on one machine (multiple terminals/processes), or on multiple machines that share the same Postgres DSN:

```bash
uv run tolokaforge worker \
  --config examples/distributed_run/run_config.yaml \
  --run-dir results/distributed_example
```

### 4) Monitor Progress

```bash
uv run tolokaforge status \
  --run-dir results/distributed_example \
  --config examples/distributed_run/run_config.yaml
```

You will see queue counts, ETA estimate, and accumulated tokens/cost from completed attempts.

### 5) Analyze Results

```bash
uv run python examples/analyze_results/analyze_run.py --run-dir results/distributed_example
```
