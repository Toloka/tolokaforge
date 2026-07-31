"""Metrics calculation including pass^k.

Every performance rate here is computed over the trials that **measured the
agent**. A trial the provider or the substrate killed produced no grade and no
performance to describe, so counting it would report a model failure that never
happened; ``measured_trials`` / ``infrastructure_aborts`` /
``outcomes_by_reason`` ride alongside the rates so a reader always sees which
denominator produced them. Spend (cost, token counters) covers every attempted
trial — those tokens were really bought.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import comb
from typing import Any

from tolokaforge.core.failure_attribution import (
    EXCLUDED_TYPED_REASONS,
    TrialOutcomeClass,
    classify_trial_outcome,
)
from tolokaforge.core.models import Trajectory


@dataclass(frozen=True)
class TrialOutcomePartition:
    """A task's trajectories split by :func:`classify_trial_outcome`.

    ``measured_trials + sum(infrastructure_aborts.values()) == total_trials``.
    ``harness_errors`` overlaps ``measured`` rather than partitioning against
    it: our own defects are counted like any other failure, and the count is a
    run-health signal on its own.
    """

    total_trials: int
    measured: tuple[Trajectory, ...]
    outcomes_by_reason: dict[str, dict[str, Any]]
    infrastructure_aborts: dict[str, int]
    harness_errors: int

    @property
    def measured_trials(self) -> int:
        return len(self.measured)


def _outcome_key(trajectory: Trajectory) -> str:
    """The ``outcomes_by_reason`` key for *trajectory*.

    A termination reason is its own key. A trial that recorded none is keyed by
    its status instead, so every key maps to exactly one outcome class — a
    reason-less trial is ``MEASURED`` when it completed and ``HARNESS_ERROR``
    when it did not.
    """
    reason = trajectory.termination_reason
    if reason is not None:
        return reason.value
    return f"unset_{trajectory.status.value}"


def partition_trial_outcomes(trajectories: Sequence[Trajectory]) -> TrialOutcomePartition:
    """Split *trajectories* into the measured set and the aborted counts.

    Callers must hand this the **whole** trial list. Pre-filtering is what
    produced the numbers this partition exists to fix: a filtered list makes
    every downstream denominator silently agree with the filter.
    """
    measured: list[Trajectory] = []
    outcomes_by_reason: dict[str, dict[str, Any]] = {}
    harness_errors = 0

    for trajectory in trajectories:
        outcome = classify_trial_outcome(trajectory)
        row = outcomes_by_reason.setdefault(
            _outcome_key(trajectory), {"class": outcome.value, "count": 0}
        )
        row["count"] += 1
        if outcome is TrialOutcomeClass.HARNESS_ERROR:
            harness_errors += 1
        if outcome is not TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            measured.append(trajectory)

    # Every excluded reason is reported, present or not: a zero is a fact about
    # the run, while a missing key is only a fact about the writer.
    infrastructure_aborts = {
        reason.value: outcomes_by_reason.get(reason.value, {}).get("count", 0)
        for reason in sorted(EXCLUDED_TYPED_REASONS, key=lambda r: r.value)
    }

    return TrialOutcomePartition(
        total_trials=len(trajectories),
        measured=tuple(measured),
        outcomes_by_reason=outcomes_by_reason,
        infrastructure_aborts=infrastructure_aborts,
        harness_errors=harness_errors,
    )


def _mean_or_none(values: Iterable[float]) -> float | None:
    """The mean of *values*, or ``None`` when there are none to average.

    ``None`` is the honest answer for an empty sample; ``0.0`` reads as a
    measured zero.
    """
    materialised = list(values)
    if not materialised:
        return None
    return sum(materialised) / len(materialised)


def _percentile(sorted_values: list[float], p: float) -> float:
    """Compute percentile using linear interpolation."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = (len(sorted_values) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def calculate_latency_percentiles(latencies_s: list[float]) -> dict[str, float]:
    """Return p50/p90/p99 latency percentiles in seconds."""
    if not latencies_s:
        return {"latency_p50_s": 0.0, "latency_p90_s": 0.0, "latency_p99_s": 0.0}
    sorted_values = sorted(float(v) for v in latencies_s)
    return {
        "latency_p50_s": _percentile(sorted_values, 50),
        "latency_p90_s": _percentile(sorted_values, 90),
        "latency_p99_s": _percentile(sorted_values, 99),
    }


def compute_pass_at_k(n: int, c: int, k: int) -> float:
    """
    Compute pass@k metric (HumanEval/MBPP style)

    pass@k measures the probability that at least 1 out of k samples succeeds.

    Formula: pass@k = 1 - C(n-c, k) / C(n, k)

    where:
    - n = total number of samples
    - c = number of correct/passing samples
    - k = number of samples to draw

    Interpretation: If we randomly select k samples from n total samples
    (where c are correct), what's the probability at least 1 is correct?

    Args:
        n: Total number of samples/trials
        c: Number of correct/passing samples
        k: Number of samples to draw

    Returns:
        pass@k value between 0 and 1

    Raises:
        ValueError: If parameters are invalid
    """
    if n < 0 or c < 0 or k < 0:
        raise ValueError("n, c, and k must be non-negative")
    if c > n:
        raise ValueError(f"c ({c}) cannot be greater than n ({n})")
    if k > n:
        raise ValueError(f"k ({k}) cannot be greater than n ({n})")

    # If no samples to draw, undefined (return 0)
    if k == 0:
        return 0.0

    # If all samples are correct, pass@k = 1
    if c == n:
        return 1.0

    # If no correct samples, pass@k = 0
    if c == 0:
        return 0.0

    # Number of failures
    n_fail = n - c

    # If k > n_fail, we're guaranteed to get at least one pass
    if k > n_fail:
        return 1.0

    # General formula: 1 - C(n-c, k) / C(n, k)
    # This is: 1 - (ways to choose k failures) / (ways to choose k samples)
    pass_k = 1.0 - (comb(n_fail, k) / comb(n, k))
    return pass_k


def calculate_pass_k(
    trajectories: list[Trajectory], k_values: list[int] = None
) -> dict[str, float]:
    """
    Calculate pass@k for a set of trajectories

    pass@k measures the probability that at least 1 out of k attempts succeeds.
    Uses the correct HumanEval formula: pass@k = 1 - C(n-c, k) / C(n, k)

    ``n`` is the number of **measured** trials, so a trial the infrastructure
    aborted neither counts as a failure nor props up the sample size. One lost
    trial can therefore turn ``pass@5`` from a number into ``None``: five
    samples are needed to estimate pass@5 and four cannot do it. Read the
    ``None`` next to ``measured_trials`` / ``infrastructure_aborts`` in the same
    row, which say whether coverage was lost or never existed.

    Args:
        trajectories: The task's full trial list — never a filtered one
        k_values: List of k values to calculate (default: [1, 5, 10])

    Returns:
        Dictionary with pass@k for each k value
    """
    if k_values is None:
        k_values = [1, 5, 10]
    measured = partition_trial_outcomes(trajectories).measured
    n_measured = len(measured)
    n_success = sum(1 for t in measured if t.grade and t.grade.binary_pass)

    results = {}
    for k in k_values:
        # ``compute_pass_at_k`` raises on k > n, so the guard and the sample
        # size have to be the same number.
        if n_measured == 0 or k > n_measured:
            results[f"pass@{k}"] = None
            results[f"pass_hat@{k}"] = None
        else:
            pass_k = compute_pass_at_k(n=n_measured, c=n_success, k=k)
            results[f"pass@{k}"] = pass_k
            # Alias for pass-hat@k naming (same Chen et al. estimator).
            results[f"pass_hat@{k}"] = pass_k

    return results


_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _spend_metrics(trajectories: Sequence[Trajectory]) -> dict[str, Any]:
    """Token and cost accounting over **every** attempted trial.

    An aborted trial still bought whatever tokens it burned before it died, so
    excluding it here would under-report what the run actually cost. Judge spend
    is tracked apart from agent spend: the rubric judge runs its own LLM in the
    Runner, so its cost lives on ``grade.judge_usage``, and a run's true total is
    the sum of both.
    """
    n_total = len(trajectories)
    spend: dict[str, Any] = {}
    for field in _USAGE_FIELDS:
        spend[f"avg_{field}"] = sum(getattr(t.metrics.usage, field) for t in trajectories) / n_total

    known_costs = [t.metrics.cost_usd for t in trajectories if t.metrics.cost_usd is not None]
    spend["total_cost_usd"] = sum(known_costs) if known_costs else None
    spend["avg_cost_usd"] = (
        spend["total_cost_usd"] / n_total if spend["total_cost_usd"] is not None else None
    )

    judge_costs = [
        t.grade.judge_usage.cost_usd
        for t in trajectories
        if t.grade is not None and t.grade.judge_usage is not None
    ]
    spend["judge_cost_usd"] = sum(judge_costs) if judge_costs else None
    if spend["total_cost_usd"] is None and spend["judge_cost_usd"] is None:
        spend["total_cost_incl_judge_usd"] = None
    else:
        spend["total_cost_incl_judge_usd"] = (spend["total_cost_usd"] or 0.0) + (
            spend["judge_cost_usd"] or 0.0
        )
    return spend


def _measured_averages(measured: Sequence[Trajectory]) -> dict[str, Any]:
    """The agent's performance averages, ``None`` when nothing was measured.

    ``avg_score`` averages the scores that exist rather than dividing a filtered
    numerator by an unfiltered count — the arithmetic that made one ungraded
    trial halve a task's average score. ``scored_trials`` is that average's own
    denominator, counted from the same list, so the run-level micro can rebuild
    the numerator instead of assuming every measured trial was graded. A
    ``HARNESS_ERROR`` trial is measured and never reaches grading, so the two
    counts differ on any run that hit one.
    """
    scores = [t.grade.score for t in measured if t.grade is not None]
    return {
        "scored_trials": len(scores),
        "avg_score": _mean_or_none(scores),
        "avg_latency_s": _mean_or_none(t.metrics.latency_total_s for t in measured),
        "avg_turns": _mean_or_none(t.metrics.turns for t in measured),
        "avg_tool_calls": _mean_or_none(t.metrics.tool_calls for t in measured),
        "stuck_rate": _mean_or_none(1.0 if t.metrics.stuck_detected else 0.0 for t in measured),
    }


def calculate_task_metrics(trajectories: list[Trajectory]) -> dict[str, any]:
    """
    Calculate aggregate metrics for a task across all trials

    ``total_trials`` counts every attempt. Every performance rate is over
    ``measured_trials`` and is ``None`` — never ``0.0`` — when that is zero, so a
    task whose every trial was aborted reports no performance instead of a
    perfect failure. ``avg_score`` has a narrower denominator of its own,
    ``scored_trials``, because a measured trial can still carry no grade.
    Latency percentiles cover every attempt: they describe what the harness
    executed, not how the agent performed.

    Args:
        trajectories: The task's full trial list — never a filtered one

    Returns:
        Dictionary with aggregate metrics
    """
    if not trajectories:
        return {}

    partition = partition_trial_outcomes(trajectories)
    measured = partition.measured
    n_success = sum(1 for t in measured if t.grade and t.grade.binary_pass)

    metrics: dict[str, Any] = {
        "total_trials": partition.total_trials,
        "measured_trials": partition.measured_trials,
        "infrastructure_aborts": partition.infrastructure_aborts,
        "harness_errors": partition.harness_errors,
        "outcomes_by_reason": partition.outcomes_by_reason,
        "successful_trials": n_success,
        "success_rate": (n_success / partition.measured_trials) if measured else None,
    }
    metrics.update(calculate_pass_k(trajectories))
    metrics.update(_measured_averages(measured))
    metrics.update(_spend_metrics(trajectories))
    metrics.update(calculate_latency_percentiles([t.metrics.latency_total_s for t in trajectories]))

    # Per-API-call latency percentiles aggregated across every call in
    # every trial. Useful for diagnosing upstream tail latency separately
    # from per-trial wall time, which also includes tool execution.
    api_call_latencies = [call.latency_s for t in trajectories for call in t.metrics.usage.calls]
    for key, value in calculate_latency_percentiles(api_call_latencies).items():
        metrics[f"api_call_{key}"] = value

    return metrics


def _merge_abort_counts(task_metrics: list[dict[str, Any]]) -> dict[str, int]:
    """Sum each excluded reason's abort count across tasks."""
    return {
        reason.value: sum(m["infrastructure_aborts"].get(reason.value, 0) for m in task_metrics)
        for reason in sorted(EXCLUDED_TYPED_REASONS, key=lambda r: r.value)
    }


def _merge_outcomes_by_reason(task_metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Sum the per-reason outcome counts across tasks.

    Every reason the run observed lands here with the class it was counted as,
    which is what makes a classification call reversible from the aggregate
    alone: the counts needed to recompute the other convention are all present
    without re-reading a single ``trajectory.yaml``.
    """
    merged: dict[str, dict[str, Any]] = {}
    for task in task_metrics:
        for key, row in task["outcomes_by_reason"].items():
            entry = merged.setdefault(key, {"class": row["class"], "count": 0})
            entry["count"] += row["count"]
    return merged


def calculate_aggregate_metrics(
    task_metrics: list[dict[str, any]], weighted: bool = True
) -> dict[str, any]:
    """
    Calculate aggregate metrics across all tasks

    Rates aggregate over measured trials only, and the macro-averages skip a task
    that measured nothing rather than averaging in a zero. ``total_trials`` still
    counts every attempt, and token / cost totals still cover every attempt.

    Each micro-average weighs by the denominator of the per-task figure it
    averages: ``success_rate_micro`` by ``measured_trials`` and
    ``avg_score_micro`` by ``scored_trials``. They are the same number only on a
    run where every measured trial was graded.

    Args:
        task_metrics: List of metrics dictionaries for each task
        weighted: Whether to weight by number of measured trials per task

    Returns:
        Dictionary with aggregate metrics
    """
    if not task_metrics:
        return {}

    n_tasks = len(task_metrics)
    total_trials = sum(m["total_trials"] for m in task_metrics)
    measured_trials = sum(m["measured_trials"] for m in task_metrics)
    scored_trials = sum(m["scored_trials"] for m in task_metrics)

    agg = {
        "total_tasks": n_tasks,
        "total_trials": total_trials,
        "measured_trials": measured_trials,
        "scored_trials": scored_trials,
        "harness_errors": sum(m["harness_errors"] for m in task_metrics),
        "infrastructure_aborts": _merge_abort_counts(task_metrics),
        "outcomes_by_reason": _merge_outcomes_by_reason(task_metrics),
    }

    if weighted:
        # Micro-average: every measured trial weighs the same, so a task with
        # more measured trials pulls harder.
        agg["success_rate_micro"] = (
            sum(m["successful_trials"] for m in task_metrics) / measured_trials
            if measured_trials > 0
            else None
        )
        # Weighed by ``scored_trials``, not ``measured_trials``: a measured trial
        # with no grade is not in any task's ``avg_score``, so weighing by the
        # measured count would rebuild a numerator that never existed and let one
        # harness error move the run's headline score.
        agg["avg_score_micro"] = (
            sum(
                m["avg_score"] * m["scored_trials"]
                for m in task_metrics
                if m["avg_score"] is not None
            )
            / scored_trials
            if scored_trials > 0
            else None
        )
    else:
        # Macro-average: every task weighs the same. A task that measured
        # nothing contributes no rate at all rather than a zero.
        agg["success_rate_macro"] = _mean_or_none(
            m["success_rate"] for m in task_metrics if m["success_rate"] is not None
        )
        agg["avg_score_macro"] = _mean_or_none(
            m["avg_score"] for m in task_metrics if m["avg_score"] is not None
        )

    # pass@k aggregates (macro-average)
    for k in [1, 5, 10]:
        pass_k_key = f"pass@{k}"
        valid_values = [m[pass_k_key] for m in task_metrics if m.get(pass_k_key) is not None]
        if valid_values:
            agg[f"{pass_k_key}_macro"] = sum(valid_values) / len(valid_values)
        else:
            agg[f"{pass_k_key}_macro"] = None
        pass_hat_k_key = f"pass_hat@{k}"
        valid_hat_values = [
            m[pass_hat_k_key] for m in task_metrics if m.get(pass_hat_k_key) is not None
        ]
        if valid_hat_values:
            agg[f"{pass_hat_k_key}_macro"] = sum(valid_hat_values) / len(valid_hat_values)
        else:
            agg[f"{pass_hat_k_key}_macro"] = None

    # Other averages — macro over the tasks that have the rate at all.
    for key in ("avg_latency_s", "avg_turns", "avg_tool_calls", "stuck_rate"):
        agg[key] = _mean_or_none(m[key] for m in task_metrics if m[key] is not None)

    # Token / cache / reasoning aggregates: every field of the Usage dataclass
    # is exposed here so analytics can audit cache hit-rate and reasoning-budget
    # spend. Per-task averages are over every attempt, so the run totals
    # reconstruct as ``avg x total_trials``.
    for field in _USAGE_FIELDS:
        agg[f"total_{field}"] = sum(
            m.get(f"avg_{field}", 0) * m["total_trials"] for m in task_metrics
        )
        agg[f"avg_{field}"] = (
            sum(m.get(f"avg_{field}", 0) for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
        )
    _known_total_costs = [
        m.get("total_cost_usd") for m in task_metrics if m.get("total_cost_usd") is not None
    ]
    agg["total_cost_usd"] = sum(_known_total_costs) if _known_total_costs else None
    _known_avg_costs = [
        m.get("avg_cost_usd") for m in task_metrics if m.get("avg_cost_usd") is not None
    ]
    agg["avg_cost_usd"] = (
        sum(_known_avg_costs) / n_tasks if _known_avg_costs and n_tasks > 0 else None
    )
    # Roll up judge spend and the combined total across tasks (agent-only
    # ``total_cost_usd`` above; judge runs its own LLM in the Runner).
    _known_judge_costs = [
        m.get("judge_cost_usd") for m in task_metrics if m.get("judge_cost_usd") is not None
    ]
    agg["judge_cost_usd"] = sum(_known_judge_costs) if _known_judge_costs else None
    _known_total_incl = [
        m.get("total_cost_incl_judge_usd")
        for m in task_metrics
        if m.get("total_cost_incl_judge_usd") is not None
    ]
    agg["total_cost_incl_judge_usd"] = sum(_known_total_incl) if _known_total_incl else None

    for percentile in ("latency_p50_s", "latency_p90_s", "latency_p99_s"):
        agg[f"{percentile}_macro"] = (
            sum(m.get(percentile, 0.0) for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
        )

    return agg
