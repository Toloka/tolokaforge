# fix-billing-holds

**Difficulty:** medium
**Stack:** Python 3.11 FastAPI, PostgreSQL 15
**Tags:** billing, debugging, python, fastapi, postgresql, data-migration

A billing microservice with a hold/commit/cancel flow has been miscalculating
fees since early 2024. Different clients use different fee types
(`percentage`, `tiered`, `flat_rate`) and not all of them are buggy — the
agent must identify which fee types are broken, fix the calculation logic in
`/app/fee_utils.py`, and then run targeted data migrations against
`holds`, `transactions`, and the append-only `billing_ledger` (which blocks
UPDATE/DELETE via triggers, so corrections must be appended as
`fee_adjustment` rows). Pre-bug historical data must remain untouched, and
aggregated tables (`report_summaries`, `client_fee_totals`) must be rebuilt
via `aggregator.py` after fixing the raw data.

See `task.yaml` for the full service brief, API contract, and constraints.

## How to run this task

From the repo root, **after** installing the adapter
(`uv pip install -e external_adapters/tolokaforge-adapter-terminal-bench`)
and exporting your LLM key in `.env`:

```bash
scripts/with_env.sh uv run tolokaforge run \
  --config examples/terminal_bench/run_billing_holds.yaml
```

Results are written under `results/terminal_bench/`.

## Resource profile

| Setting | Value |
|---|---|
| CPUs | 2 |
| Memory | 4 GB |
| Agent timeout | 1800 s |
| Verifier timeout | 120 s |

The image pre-seeds PostgreSQL with buggy historical data and a baseline
FastAPI service. Build time on a cold cache is ~60–90 s.

## Verifier scoring

30 pytest checks in `tests/test_billing.py` cover: hold/commit API parity,
per-fee-type calculation correctness, per-hold migration correctness, ledger
net-fee invariants, and aggregated-report consistency. Reward is
`passed / 30`; `binary_pass = reward >= 0.5`. On the unsolved baseline
(image as-shipped) you should see ~13/30 passing — those are the
health/pre-bug invariants that hold without any fix.

## Manual smoke-check

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
