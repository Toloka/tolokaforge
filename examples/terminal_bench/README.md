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
- **Environment synthesis** — for each discovered task the adapter
  materialises a staging directory holding a copy of the task pack, a
  `tests/test.sh` script, empty `_logs/` mountpoints, and a synthesised
  `docker-compose.tolokaforge.yaml` that resolves the terminal-bench
  variable set at synthesis time and injects engine `runner` +
  `db-service` services. The adapter emits an
  `EnvironmentPatch(stack.compose_file=…, stack.runner_service="runner")`
  on `TaskConfig`; every compose service resolves to
  `ServiceSpec(isolation="ephemeral")`, so the orchestrator selects
  `PerTrialRuntimeBackend` automatically.
- **Image pre-build** — the adapter declares one
  `ComposeImageBuild` per task on `docker_stack_requirements()`; the
  orchestrator runs `docker compose -f <synthesised> build <agent-service>`
  once per run, before any trial provisions.
- **Provision** — `PerTrialRuntimeBackend.provision` copies the staging
  directory into a per-trial context, writes a `.env` with
  `TOLOKAFORGE_TRIAL_SLUG=<sanitised trial-id>`, and runs
  `docker compose up -d --wait`. The synthesised compose file pins the
  agent container as `tbench_${TOLOKAFORGE_TRIAL_SLUG}_<agent-service>`.
- **Execution** — the runner-side `bash` tool `docker exec`s into that
  container by name; no compose lifecycle runs inside the tool.
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

Terminal-bench tasks run under `PerTrialRuntimeBackend`. Backend selection is task-driven: the manifest the adapter emits declares every compose service as `ephemeral`, so `Orchestrator._select_backend_from_tasks()` returns `per_trial` and every trial gets its own compose project — no config change is required. `TrialExecutor`'s `provision → await_ready → endpoints → teardown` bracket, per-trial network isolation, and `PROVISION_ERROR` attribution all apply. See `docs/RUNTIME_BACKENDS.md` § "Adapter compatibility with `per_trial`".

## Related docs

- `docs/ADAPTER_ARCHITECTURE.md` — how adapters plug into the orchestrator
- `docs/RUNTIME_BACKENDS.md` — runtime backends + adapter compatibility
- `external_adapters/tolokaforge-adapter-terminal-bench/` — the adapter source
