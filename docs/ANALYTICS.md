# Analytics Guide

This guide explains Tolokaforge metrics outputs, failure attribution, and programmatic analysis patterns.

## Generated Result Files

After a run, Tolokaforge writes analytics artifacts in `evaluation.output_dir`:

- `aggregate.json`: run-level aggregate metrics (`schema_version: 1`)
- `per_task_metrics.json`: per-task metrics across trials
- `metadata_slices.json`: aggregates sliced by benchmark type, complexity, tags, expected failure modes
- `failure_attribution.json`: failed-attempt attribution summary + per-attempt evidence

## Core Metrics

### Success and Quality

- `success_rate`: passing attempts / total attempts
- `avg_score`: mean continuous score from grading
- `pass@k`: probability at least one pass appears in `k` draws
- `pass_hat@k`: alias using the same Chen et al. estimator as `pass@k`

Estimator used in code:

`pass@k = 1 - C(n-c, k) / C(n, k)`

where:
- `n`: number of attempts
- `c`: passing attempts

### Latency, Cost, and Tokens

- `avg_latency_s`, `latency_p50_s`, `latency_p90_s`, `latency_p99_s`
- `total_cost_usd`, `avg_cost_usd`
- Full [`Usage`](../tolokaforge/core/llm/usage.py:1) aggregates — one
  `total_<field>` + `avg_<field>` pair per `Usage` field:
  - `total_prompt_tokens` / `avg_prompt_tokens`
  - `total_completion_tokens` / `avg_completion_tokens`
  - `total_reasoning_tokens` / `avg_reasoning_tokens` (thinking budget spend)
  - `total_cached_tokens` / `avg_cached_tokens` (generic cache hit tokens)
  - `total_cache_creation_input_tokens` / `avg_cache_creation_input_tokens`
    (Anthropic cache writes)
  - `total_cache_read_input_tokens` / `avg_cache_read_input_tokens`
    (Anthropic cache reads — Stage 6 caching observability metric)

### Reliability

- `tool_success_rate`
- `stuck_rate`
- retry-related run behavior (visible through queue counts and failed/completed totals)

## Failure Attribution

Deterministic classes currently emitted:

- `tool_arguments`
- `tool_execution`
- `grader_contract`
- `timeout_or_resource`

Fallback class:

- `model_reasoning`

Evidence payloads include tool name/index, error strings, state-diff keys, and termination reasons when available.

## Programmatic Analysis Example

Use the runnable script:

```bash
python examples/analyze_results/analyze_run.py --run-dir results/your_run
```

This script:

1. Loads split trial artifacts (`trajectory.yaml`, `metrics.yaml`, `grade.yaml`)
2. Reconstructs `Trajectory` objects
3. Computes per-task and aggregate metrics
4. Computes failure attributions and summary
5. Writes `analysis_summary.json` by default

## API Usage Snippet

```python
from tolokaforge import (
    calculate_aggregate_metrics,
    calculate_task_metrics,
    compute_pass_at_k,
)

# Example direct pass@k usage
print(compute_pass_at_k(n=10, c=3, k=2))

# Then feed real trajectory groups into calculate_task_metrics(...)
# and aggregate them with calculate_aggregate_metrics(...)
```

## Metadata Slicing

Task metadata fields in `task.yaml` are first-class analytics dimensions:

```yaml
metadata:
  complexity: medium
  tags: ["retrieval", "tool-use"]
  expected_failure_modes: ["tool_selection", "grader_contract"]
```

These power `metadata_slices.json` for leaderboard/debug slices without custom SQL.
