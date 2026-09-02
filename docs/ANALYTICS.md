# Analytics Guide

This guide explains Tolokaforge metrics outputs, failure attribution, and programmatic analysis patterns.

## Generated Result Files

After a run, Tolokaforge writes analytics artifacts in `evaluation.output_dir`:

- `aggregate.json`: run-level aggregate metrics (`schema_version: 3`)
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

`measured_trials` is **the denominator we hold ourselves accountable for** — not
"the attempts where we observed the agent". The two readings agree on most runs
and part company on the attempts that are our own fault: a trial our harness
broke, and a trial that ran to a normal end and whose grading then refused to
produce a verdict, are both counted, because a denominator that dropped our
defects would report a cleaner benchmark than the run earned. What genuinely
never happened — the attempts the provider or the substrate killed — is excluded.
The counts that say which is which sit in the same row:

- `total_trials`: every attempt the run made
- `measured_trials`: the attempts inside that denominator — **the denominator of
  every rate below except `avg_score`**
- `scored_trials`: the measured attempts that produced a grade — **`avg_score`'s
  denominator**, and the weight `avg_score_micro` uses. An `ungradeable` trial
  reaches grading and comes back without a verdict, and a `trial_lost` one is not
  graded at all, so `scored_trials < measured_trials` on any run that hit either
- `infrastructure_aborts`: per reason, the attempts excluded from that
  denominator (`{"api_timeout": 0, "provision_error": 0, "rate_limit": 3}`). All
  three keys are always present, so a zero is distinguishable from a missing key
- `harness_errors`: attempts that failed on a defect of ours. **Inside**
  `measured_trials`, not excluded from it — our own bugs stay in the denominator.
  A non-zero value is a run-health signal
- `ungradeable`: attempts whose grading refused. Also **inside**
  `measured_trials`, also a defect of ours, and a non-pass in `success_rate` and
  `pass@k` — so a grading regression deflates the run visibly rather than
  vanishing from it. The reason grading gave is in that trial's
  `trajectory.yaml` under `grading_error`
- `outcomes_by_reason`: every termination reason the run observed, with the class
  it was counted as: `{"max_turns": {"class": "measured", "count": 7}}`. An
  ungradeable attempt terminates the way a graded one does, so its row is keyed
  `ungradeable_<reason>` — `{"ungradeable_agent_done": {"class": "ungradeable",
  "count": 1}}` — which keeps one key mapping to exactly one class. A trial the
  runner lost is keyed `trial_lost` with class `harness_error`; a runner lost
  *after* the trial's last tool call is not that row, because the agent finished
  and it was grading that refused — that attempt lands under
  `ungradeable_<reason>`, which is the honest reading of it

`measured_trials + sum(infrastructure_aborts.values()) == total_trials`, and
`0 <= scored_trials <= measured_trials`, and — a trial being classified once, so
the two diagnostic counts never cover the same attempt —
`0 <= harness_errors + ungradeable <= measured_trials`.

**What a rate on this page is a rate over.** Every one of them has
`measured_trials` underneath it, which means each includes our own defects — a
harness error and an ungradeable trial both weigh against the run — and excludes
only the attempts a **typed** infrastructure abort removed. So a rate read alone
cannot say how much of the run it describes: two tasks both reporting
`success_rate: 0.5` are not comparable if one of them lost half its attempts to
the provider and the other lost none. Read `infrastructure_aborts` alongside any
rate you compare, and the coverage behind the number travels with it.

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
- `harness_errors`, `ungradeable` and `infrastructure_aborts` per task and run-wide
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
- `grading_failure`
- `infrastructure`
- `timeout_or_resource`
- `provision_failure`
- `harness_autofail`

Fallback class:

- `model_reasoning`

`grading_failure` is the attempt whose grading refused. It has its own class
because the fallback would otherwise attribute it to `model_reasoning` — the
agent blamed, in an artifact whose whole purpose is naming the right cause, for a
fault of ours. A `trial_lost` attempt is attributed `infrastructure`
deterministically for the same reason: the call that hit the fault reached no tool
and was never recorded, so there is no failed call for the tool-log scan to find
and the fallback would blame the agent for a substrate fault.

`harness_autofail` catches trials the harness auto-failed on a `TrialGrader`
synth branch whose termination reason is not otherwise enumerated in the
deterministic elif chain — today that is `STUCK_DETECTED` (the `TIMEOUT` /
`EMPTY_COMPLETION` / `ERROR` / `RATE_LIMIT` / `API_ERROR` reasons keep their
`timeout_or_resource` label because the enumerated branch catches them
first). It is forward-compat: any future `TerminationReason` a `TrialGrader`
synthesises from without an enumerated branch lands here rather than falling
through to the tool-log scan and settling on `model_reasoning`. The trial was
never measured by an evaluator, so any fallthrough that blames the model
would misattribute a harness-terminated trial — the misattribution this
class prevents.

Every attribution record also carries `outcome_class` (`measured` /
`harness_error` / `infrastructure_abort` / `ungradeable`), so a reader of a single
record can see whether the attempt counted, and the summary carries
`by_outcome_class` for the same split run-wide.

Evidence payloads include tool name/index, error strings, state-diff keys, termination reasons, and — on a synth trial — a `{kind: "synthesized_grade", termination_reason: <reason.value>}` entry.

Every attribution record also carries `provision_stage` as a top-level field, taking one of `materialise_run` / `provision` / `await_ready` / `reset_recipe` / `register_trial` / `cycle` (the closed
`tolokaforge.core.models.trajectory.ProvisionStage` vocabulary) when the trial's `termination_reason` is `provision_error`, and `null` otherwise. It is a first-class attribution field, not an entry inside `evidence`, because it answers "which point of the provisioning lifecycle failed" — the operator's question — rather than describing the evidence for the classification. Present-but-null off the provision path so a reader can access `record["provision_stage"]` unconditionally.

Every attribution record also carries two first-class marker fields for harness-synthesised auto-fail grades:

- `synthesized: bool` — `True` when the trial's `Grade` was fabricated on a `TrialGrader` auto-fail branch (no evaluator ran on the trial), `False` on every real measured verdict.
- `synthesized_by_termination_reason: str | None` — the `TerminationReason` value name the grader synthesised from (`error` / `stuck_detected` / `empty_completion`), or `null` off the synth path.

Present-but-null off the synth path so a reader can access `record["synthesized"]` unconditionally rather than gate on the presence of a marker key.

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
