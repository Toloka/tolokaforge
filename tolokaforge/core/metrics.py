"""Metrics calculation including pass^k"""

from math import comb

from tolokaforge.core.models import Trajectory


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

    Args:
        trajectories: List of trajectories for a task
        k_values: List of k values to calculate (default: [1, 5, 10])

    Returns:
        Dictionary with pass@k for each k value
    """
    # Count successful trials
    if k_values is None:
        k_values = [1, 5, 10]
    n_total = len(trajectories)
    n_success = sum(1 for t in trajectories if t.grade and t.grade.binary_pass)

    results = {}
    for k in k_values:
        if k > n_total:
            # Not enough trials for this k
            results[f"pass@{k}"] = None
            results[f"pass_hat@{k}"] = None
        else:
            # Use correct pass@k formula
            pass_k = compute_pass_at_k(n=n_total, c=n_success, k=k)
            results[f"pass@{k}"] = pass_k
            # Alias for pass-hat@k naming (same Chen et al. estimator).
            results[f"pass_hat@{k}"] = pass_k

    return results


def calculate_task_metrics(trajectories: list[Trajectory]) -> dict[str, any]:
    """
    Calculate aggregate metrics for a task across all trials

    Args:
        trajectories: List of trajectories for a task

    Returns:
        Dictionary with aggregate metrics
    """
    if not trajectories:
        return {}

    n_total = len(trajectories)
    n_success = sum(1 for t in trajectories if t.grade and t.grade.binary_pass)

    # Basic success metrics
    metrics = {
        "total_trials": n_total,
        "successful_trials": n_success,
        "success_rate": n_success / n_total if n_total > 0 else 0.0,
    }

    # pass@k metrics
    pass_k_results = calculate_pass_k(trajectories)
    metrics.update(pass_k_results)

    # Average metrics
    metrics["avg_score"] = (
        sum(t.grade.score for t in trajectories if t.grade) / n_total if n_total > 0 else 0.0
    )
    metrics["avg_latency_s"] = (
        sum(t.metrics.latency_total_s for t in trajectories) / n_total if n_total > 0 else 0.0
    )
    metrics["avg_turns"] = (
        sum(t.metrics.turns for t in trajectories) / n_total if n_total > 0 else 0.0
    )
    metrics["avg_tool_calls"] = (
        sum(t.metrics.tool_calls for t in trajectories) / n_total if n_total > 0 else 0.0
    )
    # Per-task token/cache/reasoning averages. Pulled from the full Usage
    # object on each trajectory so Anthropic cache counters + reasoning
    # budgets are first-class citizens (Stage 5 / P7).
    _usage_fields = (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    for _field in _usage_fields:
        metrics[f"avg_{_field}"] = (
            sum(getattr(t.metrics.usage, _field) for t in trajectories) / n_total
            if n_total > 0
            else 0.0
        )
    known_costs = [t.metrics.cost_usd for t in trajectories if t.metrics.cost_usd is not None]
    metrics["total_cost_usd"] = sum(known_costs) if known_costs else None
    metrics["avg_cost_usd"] = (
        metrics["total_cost_usd"] / n_total
        if metrics["total_cost_usd"] is not None and n_total > 0
        else None
    )
    # Judge spend is separate from the agent trajectory cost: the rubric judge
    # runs its own LLM in the Runner, so its cost lives on ``grade.judge_usage``,
    # not ``trajectory.metrics``. ``total_cost_usd`` above is agent-only; surface
    # judge cost on its own and a combined total so a run's true spend is visible
    # without parsing every ``grade.yaml`` (issue: run-level judge cost).
    _judge_costs = [
        t.grade.judge_usage.cost_usd
        for t in trajectories
        if t.grade is not None and t.grade.judge_usage is not None
    ]
    metrics["judge_cost_usd"] = sum(_judge_costs) if _judge_costs else None
    if metrics["total_cost_usd"] is None and metrics["judge_cost_usd"] is None:
        metrics["total_cost_incl_judge_usd"] = None
    else:
        metrics["total_cost_incl_judge_usd"] = (metrics["total_cost_usd"] or 0.0) + (
            metrics["judge_cost_usd"] or 0.0
        )

    metrics.update(calculate_latency_percentiles([t.metrics.latency_total_s for t in trajectories]))

    # Per-API-call latency percentiles aggregated across every call in
    # every trial. Useful for diagnosing upstream tail latency separately
    # from per-trial wall time, which also includes tool execution.
    api_call_latencies = [call.latency_s for t in trajectories for call in t.metrics.usage.calls]
    api_call_percentiles = calculate_latency_percentiles(api_call_latencies)
    for key, value in api_call_percentiles.items():
        metrics[f"api_call_{key}"] = value

    # Stuck detection rate
    metrics["stuck_rate"] = (
        sum(1 for t in trajectories if t.metrics.stuck_detected) / n_total if n_total > 0 else 0.0
    )

    return metrics


def calculate_aggregate_metrics(
    task_metrics: list[dict[str, any]], weighted: bool = True
) -> dict[str, any]:
    """
    Calculate aggregate metrics across all tasks

    Args:
        task_metrics: List of metrics dictionaries for each task
        weighted: Whether to weight by number of trials per task

    Returns:
        Dictionary with aggregate metrics
    """
    if not task_metrics:
        return {}

    n_tasks = len(task_metrics)
    total_trials = sum(m["total_trials"] for m in task_metrics)

    agg = {
        "total_tasks": n_tasks,
        "total_trials": total_trials,
    }

    if weighted:
        # Weighted average (micro-average)
        agg["success_rate_micro"] = (
            sum(m["successful_trials"] for m in task_metrics) / total_trials
            if total_trials > 0
            else 0.0
        )
        agg["avg_score_micro"] = (
            sum(m["avg_score"] * m["total_trials"] for m in task_metrics) / total_trials
            if total_trials > 0
            else 0.0
        )
    else:
        # Unweighted average (macro-average)
        agg["success_rate_macro"] = (
            sum(m["success_rate"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
        )
        agg["avg_score_macro"] = (
            sum(m["avg_score"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
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

    # Other averages
    agg["avg_latency_s"] = (
        sum(m["avg_latency_s"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
    )
    agg["avg_turns"] = sum(m["avg_turns"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
    agg["avg_tool_calls"] = (
        sum(m["avg_tool_calls"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
    )
    agg["stuck_rate"] = sum(m["stuck_rate"] for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0

    # Token / cache / reasoning aggregates (Stage 5 / P7 — the flat
    # tokens_input / tokens_output were removed; every field of the Usage
    # dataclass is exposed here so analytics can audit cache hit-rate and
    # reasoning-budget spend.)
    _agg_usage_fields = (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    for _field in _agg_usage_fields:
        agg[f"total_{_field}"] = sum(
            m.get(f"avg_{_field}", 0) * m["total_trials"] for m in task_metrics
        )
        agg[f"avg_{_field}"] = (
            sum(m.get(f"avg_{_field}", 0) for m in task_metrics) / n_tasks if n_tasks > 0 else 0.0
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
