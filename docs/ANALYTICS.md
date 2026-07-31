# Analytics Guide

This guide explains Tolokaforge metrics outputs, failure attribution, and programmatic analysis patterns.

## Generated Result Files

After a run, Tolokaforge writes analytics artifacts in `evaluation.output_dir`:

- `aggregate.json`: run-level aggregate metrics (`schema_version: 2`)
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

The roll-up is produced during report generation, at the end of a run, by
scanning the run-output tree on disk — it reads the capture artifacts other
stages already wrote, and does not itself capture anything. Discovery walks the
three surfaces above: `trials/*/*/services/_capture.yaml` (provision-failure
manifests), `trials/*/*/metrics.yaml` (trial-body byte maps under the
`captured_service_logs` key), and `<output_dir>/services/_capture.yaml` (the
run-level shared-stack manifest). Entries are ordered deterministically by
`(task_id, trial_index, source)`; `per_service_bytes` sums each service across
every entry and `total_bytes` is the grand total.

Reading is fail-safe: a missing file, malformed YAML, a non-mapping payload, a
mistyped byte count, or a non-integer trial-index directory is logged and that
surface is skipped. A corrupt capture artifact never breaks report generation —
the roll-up simply omits what it cannot parse.

## Core Metrics

### The denominator: measured trials

Every rate is computed over the trials that **measured the agent**. A trial the
provider or the substrate killed produced no grade and no performance to
describe, so it is not in any rate; the counts that say so sit in the same row:

- `total_trials`: every attempt the run made
- `measured_trials`: the attempts that measured the agent — **the denominator of
  every rate below except `avg_score`**
- `scored_trials`: the measured attempts that produced a grade — **`avg_score`'s
  denominator**, and the weight `avg_score_micro` uses. A `harness_error` trial is
  measured and never reaches grading, so `scored_trials < measured_trials` on any
  run that hit one
- `infrastructure_aborts`: per reason, the attempts excluded from that
  denominator (`{"api_timeout": 0, "provision_error": 0, "rate_limit": 3}`). All
  three keys are always present, so a zero is distinguishable from a missing key
- `harness_errors`: attempts that failed on a defect of ours. **Inside**
  `measured_trials`, not excluded from it — our own bugs stay in the denominator.
  A non-zero value is a run-health signal
- `outcomes_by_reason`: every termination reason the run observed, with the class
  it was counted as: `{"max_turns": {"class": "measured", "count": 7}}`

`measured_trials + sum(infrastructure_aborts.values()) == total_trials`, and
`0 <= scored_trials <= measured_trials`, and
`0 <= harness_errors <= measured_trials`.

**Which direction the numbers move, if you are comparing against older figures.**
Every rate here is **weakly higher** than the same run's figures under the previous
convention, because an aborted trial used to enter the denominator carrying a
fabricated `0.0`. The gap is exactly the abort count: a run with no aborts reports
identical numbers, and a run where half the trials were rate-limited can double.
So a dashboard that appears to improve on the day this lands has not improved —
it stopped counting trials the provider killed as trials the model failed. Read
`infrastructure_aborts` alongside any rate you are comparing, and use the
`schema_version` stamp to tell which convention produced a given file.

`outcomes_by_reason` is what makes a classification call auditable without a
rerun: it carries the counts needed to recompute the numbers under a different
convention (say, counting wall-clock timeouts as aborts) from the aggregate
alone, with no need to re-read a single `trajectory.yaml`.

A trial is an infrastructure abort only when its termination reason was produced
from an exception **type** — `rate_limit`, `api_timeout`, `provision_error`. A
message that merely looks like one of those conditions terminates as a counted
reason instead. Exclusion has to be earned, because a trial wrongly excluded
raises every published rate with nothing in the output to show it.

### Success and Quality

- `success_rate`: passing attempts / measured attempts
- `avg_score`: mean continuous score over `scored_trials` — the measured attempts
  that were graded
- `pass@k`: probability at least one pass appears in `k` draws
- `pass_hat@k`: alias using the same Chen et al. estimator as `pass@k`
- `stuck_rate`: measured attempts that tripped stuck detection / measured attempts

When `measured_trials` is `0`, every one of these is `null` — never `0.0`, which
would read as a task the model failed at — and the task drops out of the
run-level macro averages. `avg_score` is `null` whenever `scored_trials` is `0`,
which a task can reach with measured trials to its name.

At run level each micro-average weighs by the denominator of the per-task figure
it averages: `success_rate_micro` by `measured_trials`, `avg_score_micro` by
`scored_trials`. Weighing the score by the measured count would rebuild a
numerator no trial produced, so a single ungraded trial would move the run's
headline score.

Estimator used in code:

`pass@k = 1 - C(n-c, k) / C(n, k)`

where:
- `n`: measured attempts
- `c`: passing attempts

`pass@k` is `null` when `k > n`. Read it next to `measured_trials` and
`infrastructure_aborts`: `pass@5: null` with `measured_trials: 4` and one abort
means coverage was **lost**, while the same `null` with `total_trials: 4` means
it never existed. The run logs a warning naming the affected tasks and the `k`
values that lost coverage whenever a run records any abort.

### Latency, Cost, and Tokens

Spend covers **every attempt**: an aborted trial bought whatever tokens it burned
before it died, and a cost total that hid them would under-report the run.
Latency percentiles likewise describe every attempt — they say what the harness
executed, not how the agent performed. `avg_latency_s` is the exception: it is a
performance average, so it follows the measured denominator.

- `avg_latency_s` (measured attempts), `latency_p50_s`, `latency_p90_s`, `latency_p99_s`
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
- `harness_errors` and `infrastructure_aborts` per task and run-wide
- retry-related run behavior (visible through queue counts and failed/completed totals)

Retryability and countability are two independent questions over one
classification. A wall-clock timeout is retried *and* counted: repeating it may
help, and the agent that burned the budget was measured doing so. See
[`docs/RUNNER.md`](RUNNER.md:1) § Retries, Rate Limits, and Budget.

## Failure Attribution

Deterministic classes currently emitted:

- `tool_arguments`
- `tool_execution`
- `grader_contract`
- `timeout_or_resource`

Fallback class:

- `model_reasoning`

Every attribution record also carries `outcome_class` (`measured` /
`harness_error` / `infrastructure_abort`), so a reader of a single record can see
whether the attempt counted, and the summary carries `by_outcome_class` for the
same split run-wide.

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
