# fix-airline-segmentation

**Difficulty:** medium
**Stack:** Python 3.11 (pandas, numpy, scikit-learn), PostgreSQL 15
**Tags:** python, postgresql, data-pipeline, machine-learning, debugging, rfm-analysis

An airline passenger segmentation pipeline runs end-to-end without errors
("Segmentation completed successfully") but produces nonsensical clusters:
high-value frequent flyers end up in the same K-means segment as infrequent
travellers. The agent must trace the RFM (Recency / Frequency / Monetary)
feature engineering across `extract.py`, `features.py`, `cluster.py`, and
`report.py`, locate the bugs, re-run the pipeline (`cd /app && python main.py`),
and regenerate correct entries in `rfm_features`, `segments`,
`segment_report`, and `pipeline_log`.

Hard constraints: schema, CLI, output-table layout, and `k=5` must all be
preserved.

See `task.yaml` for the full brief (schema, table list, entry point, env vars).

## How to run this task

From the repo root, **after** installing the adapter
(`uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench`)
and exporting your LLM key in `.env`:

```bash
scripts/with_env.sh uv run tolokaforge run \
  --config examples/terminal_bench/run_airline_segmentation.yaml
```

Results are written under `results/terminal_bench/`.

## Resource profile

| Setting | Value |
|---|---|
| CPUs | 2 |
| Memory | 4 GB |
| Agent timeout | 1800 s |
| Verifier timeout | 120 s |

The Dockerfile seeds PostgreSQL, generates synthetic passenger data, and
**runs the full pipeline once at build time** so `rfm_features`/`segments`
start populated (with buggy output). Cold build is ~2–3 minutes.

## Verifier scoring

15 pytest checks in `tests/test_segmentation.py` cover recency/frequency/
monetary correctness, cluster size distribution and differentiation, and the
four output tables being populated after re-run. Reward is `passed / 15`;
`binary_pass = reward >= 0.5`.

Unlike the other two tasks, this one has no `run-tests.sh` at task-root:
`tests/test.sh` is used directly by the adapter, which is the standard
terminal-bench convention.

## Manual smoke-check

```bash
cd examples/terminal_bench/fix-airline-segmentation
docker build -t tbench_fix-airline-segmentation:smoke ./environment

T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME=tbench_fix-airline-segmentation:smoke \
T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME=airline_smoke_main \
T_BENCH_TASK_LOGS_PATH=/tmp/tbench/logs \
T_BENCH_TASK_AGENT_LOGS_PATH=/tmp/tbench/agent_logs \
T_BENCH_CONTAINER_LOGS_PATH=/logs \
T_BENCH_CONTAINER_AGENT_LOGS_PATH=/logs/agent \
T_BENCH_TEST_DIR=/tests \
docker compose -p airline_smoke up -d --wait

docker compose -p airline_smoke cp tests/. main:/tests/
docker compose -p airline_smoke exec -T main bash -c \
  "mkdir -p /logs/verifier /logs/agent && cd /tests && bash test.sh"

docker compose -p airline_smoke down -v --remove-orphans
```
