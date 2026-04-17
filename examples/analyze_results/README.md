# Analyze Results Example

Load and analyze benchmark run artifacts using `tolokaforge` metrics and failure attribution APIs.

## What It Demonstrates

- Loading trajectories, grades, and metrics from trial directories
- Computing aggregate metrics (pass@k, cost, latency percentiles)
- Attributing failures to root causes (model errors, tool failures, stuck loops)
- Printing a summary report

## Run

```bash
# After a benchmark finishes (results in results/my_run_*)
uv run python examples/analyze_results/analyze_run.py --run-dir results/my_run_20260417_121145

# Or use the built-in CLI
uv run tolokaforge status --run-dir results/my_run_20260417_121145
```

## APIs Used

- `tolokaforge.Trajectory` — trial result model
- `tolokaforge.compute_pass_at_k` — pass@k calculation
- `tolokaforge.calculate_task_metrics` / `calculate_aggregate_metrics` — aggregation
- `tolokaforge.attribute_failure` / `summarize_failure_attributions` — root cause analysis
