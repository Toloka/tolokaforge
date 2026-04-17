# eval-orchestrator

Split tolokaforge run configs into parallel shards and merge results.
Distributes evaluation across multiple machines for parallel execution.

## Install

Part of the tolokaforge uv workspace:

```bash
uv sync
```

Or standalone:

```bash
cd tools/eval-orchestrator && uv sync
```

## Usage

### Split a config into shards

```bash
uv run eval-orchestrator split \
  --config examples/distributed_run/run_config.yaml \
  --shards 4 \
  --workers-per-shard 5 \
  --output-dir .ci/shards
```

This will:
1. Parse the run config and resolve `tasks_glob` to discover all tasks
2. Distribute tasks across 4 shards (round-robin)
3. Write `shard_0.yaml` ... `shard_3.yaml` with symlinked task directories
4. Write `matrix.json` for GitHub Actions matrix strategy

### Merge shard outputs

```bash
uv run eval-orchestrator merge \
  --input-dirs output/shard-0,output/shard-1,output/shard-2,output/shard-3 \
  --output-dir output/merged
```

This will:
1. Copy all `trials/` directories from each shard
2. Merge `run_state.json` files
3. Re-aggregate metrics from all trial `grade.yaml` and `metrics.yaml` files
4. Write combined `aggregate.json`, `run_state.json`, and `summary.md`

## CI Integration

The split command outputs a `matrix.json` suitable for use with CI matrix strategies
(e.g., GitHub Actions `fromJson()`).
