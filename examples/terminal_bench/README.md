# Terminal-bench example tasks

Ported terminal-bench tasks that run through the `TerminalBenchAdapter`
plugin. Each task ships a Docker Compose stack (`docker-compose.yaml` +
`environment/Dockerfile`) with all dependencies pre-seeded (PostgreSQL,
application code). The agent is given a single `bash` tool scoped to the task
container; grading runs `tests/test.sh`, which writes a reward float to
`/logs/verifier/reward.txt`.

| Task | Difficulty | Stack | Bugs |
|---|---|---|---|
| [`fix-billing-holds`](fix-billing-holds/) | medium | Python 3.11 FastAPI, PostgreSQL 15 | fee calculation + data migration |
| [`fix-airline-segmentation`](fix-airline-segmentation/) | medium | Python 3.11 (pandas/scikit-learn), PostgreSQL 15 | RFM K-means pipeline correctness |

## Prerequisites

1. **Docker daemon** running locally.
2. **Adapter plugin** installed into the tolokaforge workspace:
   ```bash
   uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench
   ```
3. **LLM API key** in `.env` (at least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).

## Run all tasks

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/terminal_bench/run_config.yaml
```

## Run one task

Each task ships its own config:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/terminal_bench/run_billing_holds.yaml
scripts/with_env.sh uv run tolokaforge run --config examples/terminal_bench/run_airline_segmentation.yaml
```

## How it works

- **Discovery** — `TerminalBenchAdapter` scans this directory for subfolders
  that contain both `docker-compose.yaml` and `task.yaml` / `task.toml`.
- **Execution** — for each trial the Runner starts the compose stack with a
  trial-unique project name (`tbench_<trial_id>`), copies `tests/` and
  `run-tests.sh` into `/tests/`, and exposes a single `bash` tool pointed at
  the `main` service.
- **Grading** — after the agent finishes, the Runner executes
  `cd /tests && bash test.sh`, then reads `/logs/verifier/reward.txt`. The
  float value (0.0–1.0) is the final score; `binary_pass = reward >= 0.5`.

## Smoke-check the environment

To verify your setup without spending API calls, build one task image and run
its test suite on the unsolved baseline:

```bash
cd examples/terminal_bench/fix-billing-holds

docker build -t tbench_fix-billing-holds:smoke ./environment

T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME=tbench_fix-billing-holds:smoke \
T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME=billing_smoke_main \
T_BENCH_TASK_LOGS_PATH=/tmp/tbench/logs \
T_BENCH_TASK_AGENT_LOGS_PATH=/tmp/tbench/agent_logs \
T_BENCH_CONTAINER_LOGS_PATH=/logs \
T_BENCH_CONTAINER_AGENT_LOGS_PATH=/logs/agent \
T_BENCH_TEST_DIR=/tests \
docker compose -p billing_smoke up -d --wait

docker compose -p billing_smoke cp tests/. main:/tests/
docker compose -p billing_smoke cp run-tests.sh main:/tests/test.sh
docker compose -p billing_smoke exec -T main bash -c \
  "mkdir -p /logs/verifier /logs/agent && cd /tests && bash test.sh"

docker compose -p billing_smoke down -v --remove-orphans
```

You should see some tests pass (baseline health + pre-bug assertions) and a
reward printed to stdout.

## Runtime compatibility

Terminal-bench tasks run under `--runtime shared` only. `TerminalBenchAdapter` synthesises `TaskConfig` from `TerminalBenchTask` metadata and leaves `environment_manifest` unset by design — the per-task compose stack lives on `adapter_settings.compose_file` and is materialised through the `bash` tool's `DOCKER_COMPOSE_EXEC` invocation style, not through `PerTrialRuntimeBackend`. Pointing `--runtime per_trial` at terminal-bench tasks raises `ProvisionError` at provision time (fail-loud, no silent fallback). See `docs/architecture/RUNTIME_BACKENDS.md` "Adapter compatibility with `per_trial`" for the boundary rationale and the follow-up to unify the two paths.

## Related docs

- `docs/ADAPTER_ARCHITECTURE.md` — how adapters plug into the orchestrator
- `docs/architecture/RUNTIME_BACKENDS.md` — runtime backends + adapter compatibility
- `external_adapters/tolokaforge-adapter-terminal-bench/` — the adapter source
