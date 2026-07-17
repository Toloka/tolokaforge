# Analytics Guide

This guide explains Tolokaforge metrics outputs, failure attribution, and programmatic analysis patterns.

## Generated Result Files

After a run, Tolokaforge writes analytics artifacts in `evaluation.output_dir`:

- `aggregate.json`: run-level aggregate metrics (`schema_version: 1`)
- `per_task_metrics.json`: per-task metrics across trials
- `metadata_slices.json`: aggregates sliced by benchmark type, complexity, tags, expected failure modes
- `failure_attribution.json`: failed-attempt attribution summary + per-attempt evidence

### `aggregate.json` → `captured_service_logs`

`aggregate.json` carries a run-level roll-up of the per-service compose logs
captured on failure, so an operator sees the full capture signal from one file
without walking the trial tree. The field is always present: a run that captured
nothing rolls up to a zero envelope (`captures: 0`, empty maps/lists), which is
distinct from a pre-feature `aggregate.json` that omits the key.

```yaml
captured_service_logs:
  captures: 2                       # number of capture bundles rolled up
  total_bytes: 8704                 # grand total across all services and bundles
  per_service_bytes:                # per-service byte sum across every bundle
    db: 5120
    runner: 512
    api: 3072
  entries:
    - task_id: task-1               # null for the run-level shared-stack surface
      trial_index: 0                # null for the run-level shared-stack surface
      source: provision_failure     # which failure stage produced the bundle
      capture_reason: provision_error  # manifest reason; null on the trial_body surface
      total_bytes: 5632
      services:
        db: 5120
        runner: 512
    - task_id: null
      trial_index: null
      source: shared_stack_materialise
      capture_reason: materialise_error
      total_bytes: 3072
      services:
        api: 3072
```

`source` is one of a closed set naming the capture surface each bundle came from:

- `provision_failure` — per-trial provision / reset-recipe failure
  (`trials/<task>/<idx>/services/_capture.yaml`).
- `trial_body` — per-trial trial-body or graded-red failure
  (`trials/<task>/<idx>/metrics.yaml` → `captured_service_logs`). `capture_reason`
  is `null` on this surface.
- `shared_stack_materialise` — run-level shared-stack materialise failure
  (`<output_dir>/services/_capture.yaml`). `task_id` and `trial_index` are `null`.

These surfaces are documented per-trial in
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md:1) § `captured_service_logs` and
§ `services/`, and at the backend level in
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md:1) § "Per-service log capture on
failure".

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
