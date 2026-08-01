"""Tests for metrics calculation, especially pass@k"""

from datetime import datetime, timezone

import pytest

from tolokaforge.core.llm.usage import ProviderRawCall, Usage
from tolokaforge.core.metrics import (
    calculate_aggregate_metrics,
    calculate_latency_percentiles,
    calculate_pass_k,
    calculate_task_metrics,
    compute_pass_at_k,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeStatus,
    JudgeUsage,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)

pytestmark = pytest.mark.unit


def _trial(
    trial_idx: int,
    *,
    score: float | None = None,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = TerminationReason.AGENT_DONE,
    latency_s: float = 1.0,
    turns: int = 3,
) -> Trajectory:
    """One trial. ``score=None`` means no grade at all — the shape a trial the
    infrastructure aborted now has."""
    grade = (
        None
        if score is None
        else Grade(binary_pass=score >= 0.5, score=score, components=GradeComponents())
    )
    return Trajectory(
        task_id="task_outcomes",
        trial_index=trial_idx,
        start_ts=datetime.now(tz=timezone.utc),
        end_ts=datetime.now(tz=timezone.utc),
        status=status,
        termination_reason=termination_reason,
        messages=[],
        metrics=Metrics(latency_total_s=latency_s, turns=turns),
        grade=grade,
    )


def _rate_limited(trial_idx: int) -> Trajectory:
    """A trial a provider 429 killed: no grade, and not the agent's failure."""
    return _trial(
        trial_idx,
        score=None,
        status=TrialStatus.ERROR,
        termination_reason=TerminationReason.RATE_LIMIT,
    )


def _harness_error(trial_idx: int) -> Trajectory:
    """A trial our own defect killed: counted in the rates, and never graded."""
    return _trial(
        trial_idx,
        score=None,
        status=TrialStatus.ERROR,
        termination_reason=TerminationReason.ERROR,
    )


@pytest.mark.unit
class TestPassAtK:
    """Test pass@k calculation"""

    def test_pass_at_1_all_pass(self):
        """Test pass@1 when all trials pass"""
        result = compute_pass_at_k(n=5, c=5, k=1)
        assert result == 1.0

    def test_pass_at_1_all_fail(self):
        """Test pass@1 when all trials fail"""
        result = compute_pass_at_k(n=5, c=0, k=1)
        assert result == 0.0

    def test_pass_at_1_partial(self):
        """Test pass@1 with partial success"""
        result = compute_pass_at_k(n=8, c=5, k=1)
        assert result == pytest.approx(0.625)

    def test_pass_at_4_not_enough_passes(self):
        """Test pass@4 when there aren't enough passes"""
        result = compute_pass_at_k(n=8, c=2, k=4)
        assert result == pytest.approx(0.7857, abs=0.001)

    def test_edge_case_one_trial_pass(self):
        """Test edge case with single passing trial"""
        result = compute_pass_at_k(n=1, c=1, k=1)
        assert result == 1.0

    def test_edge_case_one_trial_fail(self):
        """Test edge case with single failing trial"""
        result = compute_pass_at_k(n=1, c=0, k=1)
        assert result == 0.0

    def test_invalid_k_greater_than_n(self):
        """Test that k > n raises ValueError"""
        with pytest.raises(ValueError):
            compute_pass_at_k(n=5, c=3, k=10)

    def test_invalid_c_greater_than_n(self):
        """Test that c > n raises ValueError"""
        with pytest.raises(ValueError):
            compute_pass_at_k(n=5, c=10, k=1)

    def test_invalid_negative_n(self):
        """Test that negative n raises ValueError"""
        with pytest.raises(ValueError):
            compute_pass_at_k(n=-1, c=0, k=1)

    def test_multiple_k_values_consistency(self):
        """Test that pass@k increases with k"""
        n, c = 10, 4
        pass_at_1 = compute_pass_at_k(n, c, k=1)
        pass_at_4 = compute_pass_at_k(n, c, k=4)
        pass_at_8 = compute_pass_at_k(n, c, k=8)

        assert pass_at_1 <= pass_at_4
        assert pass_at_4 <= pass_at_8


@pytest.mark.unit
class TestMetricsAggregation:
    """Test metrics aggregation across tasks"""

    def test_macro_average(self):
        """Test macro-averaged pass@k"""
        pass_k_task1 = compute_pass_at_k(n=8, c=5, k=1)
        pass_k_task2 = compute_pass_at_k(n=8, c=7, k=1)

        macro_avg = (pass_k_task1 + pass_k_task2) / 2

        assert pass_k_task1 == pytest.approx(0.625)
        assert pass_k_task2 == pytest.approx(0.875)
        assert macro_avg == pytest.approx(0.75)

    def test_micro_average(self):
        """Test micro-averaged pass@k"""
        micro_pass_k = compute_pass_at_k(n=16, c=12, k=1)
        assert micro_pass_k == pytest.approx(0.75)


@pytest.mark.unit
class TestExtendedMetrics:
    def _make_trajectory(self, trial_idx: int, passed: bool) -> Trajectory:
        return Trajectory(
            task_id="task_metrics",
            trial_index=trial_idx,
            start_ts=datetime.now(tz=timezone.utc),
            end_ts=datetime.now(tz=timezone.utc),
            messages=[],
            metrics=Metrics(latency_total_s=1.0 + trial_idx),
            grade=Grade(
                binary_pass=passed, score=1.0 if passed else 0.0, components=GradeComponents()
            ),
        )

    def test_calculate_pass_k_includes_pass_hat_alias(self):
        trajectories = [
            self._make_trajectory(0, True),
            self._make_trajectory(1, False),
            self._make_trajectory(2, True),
            self._make_trajectory(3, False),
        ]
        metrics = calculate_pass_k(trajectories, k_values=[1, 2])
        assert metrics["pass@1"] == metrics["pass_hat@1"]
        assert metrics["pass@2"] == metrics["pass_hat@2"]

    def test_latency_percentiles(self):
        percentiles = calculate_latency_percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
        assert percentiles["latency_p50_s"] == pytest.approx(3.0)
        assert percentiles["latency_p90_s"] > percentiles["latency_p50_s"]
        assert percentiles["latency_p99_s"] >= percentiles["latency_p90_s"]

    def test_api_call_latency_percentiles_aggregated_across_trials(self):
        """``calculate_task_metrics`` rolls per-call latencies across trials
        into ``api_call_latency_p{50,90,99}_s``. Latencies live on
        ``usage.calls[*].latency_s``; without this aggregation, the field
        is dead data on every trajectory."""

        def _trial(trial_idx: int, api_latencies: list[float]) -> Trajectory:
            calls = tuple(ProviderRawCall(latency_s=lat) for lat in api_latencies)
            return Trajectory(
                task_id="task_api_lat",
                trial_index=trial_idx,
                start_ts=datetime.now(tz=timezone.utc),
                end_ts=datetime.now(tz=timezone.utc),
                messages=[],
                metrics=Metrics(latency_total_s=1.0, usage=Usage(calls=calls)),
                grade=Grade(binary_pass=True, score=1.0, components=GradeComponents()),
            )

        trajectories = [
            _trial(0, [1.0, 2.0, 3.0]),
            _trial(1, [4.0, 5.0]),
        ]
        metrics = calculate_task_metrics(trajectories)
        assert metrics["api_call_latency_p50_s"] == pytest.approx(3.0)
        assert metrics["api_call_latency_p90_s"] > metrics["api_call_latency_p50_s"]
        assert metrics["api_call_latency_p99_s"] >= metrics["api_call_latency_p90_s"]


@pytest.mark.unit
class TestInfrastructureAbortsLeaveTheDenominator:
    """Rates describe the trials that measured the agent.

    The numbers below are the measured ones from the same four trials before
    and after: with two of four killed by rate limits, the task's true 1-of-2
    performance used to be reported as 1-of-4.
    """

    def test_two_of_four_rate_limited(self) -> None:
        metrics = calculate_task_metrics(
            [
                _trial(0, score=1.0),
                _trial(1, score=0.3),
                _rate_limited(2),
                _rate_limited(3),
            ]
        )

        assert metrics["total_trials"] == 4
        assert metrics["measured_trials"] == 2
        assert metrics["infrastructure_aborts"] == {
            "api_timeout": 0,
            "provision_error": 0,
            "rate_limit": 2,
        }
        assert metrics["harness_errors"] == 0
        assert metrics["successful_trials"] == 1
        assert metrics["success_rate"] == pytest.approx(0.5)
        assert metrics["avg_score"] == pytest.approx(0.65)
        assert metrics["pass@1"] == pytest.approx(0.5)

    def test_the_partition_accounts_for_every_trial(self) -> None:
        """``measured + aborted == attempted``, with harness errors *inside*
        the measured half — our own defects are counted, not excluded."""
        metrics = calculate_task_metrics(
            [
                _trial(0, score=1.0),
                _trial(
                    1,
                    score=0.0,
                    status=TrialStatus.ERROR,
                    termination_reason=TerminationReason.ERROR,
                ),
                _rate_limited(2),
            ]
        )

        assert (
            metrics["measured_trials"] + sum(metrics["infrastructure_aborts"].values())
            == metrics["total_trials"]
        )
        assert metrics["harness_errors"] == 1
        assert 0 <= metrics["harness_errors"] <= metrics["measured_trials"]

    def test_outcomes_by_reason_covers_every_observed_reason(self) -> None:
        """Every reason is counted with the class it was counted as, so a
        classification call can be recomputed from the aggregate alone."""
        metrics = calculate_task_metrics(
            [
                _trial(0, score=1.0),
                _trial(
                    1,
                    score=0.0,
                    status=TrialStatus.TIMEOUT,
                    termination_reason=TerminationReason.TIMEOUT,
                ),
                _rate_limited(2),
            ]
        )

        assert metrics["outcomes_by_reason"] == {
            "agent_done": {"class": "measured", "count": 1},
            "timeout": {"class": "measured", "count": 1},
            "rate_limit": {"class": "infrastructure_abort", "count": 1},
        }
        counted = sum(row["count"] for row in metrics["outcomes_by_reason"].values())
        assert counted == metrics["total_trials"]

    def test_a_stuck_trial_still_fails_and_still_counts(self) -> None:
        """The one auto-fail verdict produced host-side stays a fail: an agent
        that repeated itself was measured doing so."""
        metrics = calculate_task_metrics(
            [
                _trial(
                    0,
                    score=0.0,
                    termination_reason=TerminationReason.STUCK_DETECTED,
                ),
                _trial(1, score=1.0),
            ]
        )

        assert metrics["measured_trials"] == 2
        assert metrics["success_rate"] == pytest.approx(0.5)
        assert metrics["outcomes_by_reason"]["stuck_detected"]["class"] == "measured"

    def test_every_trial_aborted_reports_no_performance(self) -> None:
        """No rate is ``0.0`` when nothing was measured — a zero would read as
        a task the agent failed at."""
        metrics = calculate_task_metrics([_rate_limited(0), _rate_limited(1)])

        assert metrics["total_trials"] == 2
        assert metrics["measured_trials"] == 0
        assert metrics["infrastructure_aborts"]["rate_limit"] == 2
        for key in (
            "success_rate",
            "avg_score",
            "avg_latency_s",
            "avg_turns",
            "avg_tool_calls",
            "stuck_rate",
            "pass@1",
            "pass@5",
            "pass_hat@1",
        ):
            assert metrics[key] is None, f"{key} fabricated a number from zero measured trials"

    def test_an_all_aborted_task_is_excluded_from_the_macro_averages(self) -> None:
        measured_task = calculate_task_metrics([_trial(0, score=1.0), _trial(1, score=1.0)])
        aborted_task = calculate_task_metrics([_rate_limited(0)])

        agg = calculate_aggregate_metrics([measured_task, aborted_task], weighted=False)

        assert agg["success_rate_macro"] == pytest.approx(1.0)
        assert agg["avg_score_macro"] == pytest.approx(1.0)
        assert agg["total_trials"] == 3
        assert agg["measured_trials"] == 2
        assert agg["infrastructure_aborts"]["rate_limit"] == 1

    def test_the_micro_average_weighs_by_measured_trials(self) -> None:
        task_a = calculate_task_metrics([_trial(0, score=1.0), _rate_limited(1)])
        task_b = calculate_task_metrics([_trial(0, score=0.0)])

        agg = calculate_aggregate_metrics([task_a, task_b], weighted=True)

        # Two measured trials across both tasks, one of them successful.
        assert agg["measured_trials"] == 2
        assert agg["scored_trials"] == 2
        assert agg["success_rate_micro"] == pytest.approx(0.5)
        assert agg["avg_score_micro"] == pytest.approx(0.5)

    def test_the_score_micro_weighs_by_scored_trials_not_measured_ones(self) -> None:
        """A harness error is measured and never graded, so weighing the score
        micro by ``measured_trials`` would rebuild a numerator no trial produced.

        Here task A scores 1.0 over its one graded trial and task B scores 0.0
        over its one. Three trials are measured, two are scored, and the only
        honest run-level score is 0.5.
        """
        task_a = calculate_task_metrics([_trial(0, score=1.0), _harness_error(1)])
        task_b = calculate_task_metrics([_trial(0, score=0.0)])

        agg = calculate_aggregate_metrics([task_a, task_b], weighted=True)

        assert task_a["measured_trials"] == 2
        assert task_a["scored_trials"] == 1
        assert agg["measured_trials"] == 3
        assert agg["scored_trials"] == 2
        assert agg["avg_score_micro"] == pytest.approx(0.5)
        # The rate over the measured denominator keeps that denominator: an
        # ungraded trial is not a success, and dropping it would hide the defect.
        assert agg["success_rate_micro"] == pytest.approx(1 / 3)

    def test_an_all_ungraded_task_contributes_nothing_to_the_score_micro(self) -> None:
        """The reviewer's first measurement: one all-ungraded task beside one
        scoring 1.0 reported 0.5, when the only score in the run was 1.0."""
        ungraded = calculate_task_metrics([_harness_error(0)])
        scored = calculate_task_metrics([_trial(0, score=1.0)])

        agg = calculate_aggregate_metrics([ungraded, scored], weighted=True)

        assert ungraded["scored_trials"] == 0
        assert ungraded["avg_score"] is None
        assert agg["measured_trials"] == 2
        assert agg["scored_trials"] == 1
        assert agg["avg_score_micro"] == pytest.approx(1.0)

    def test_a_run_that_measured_nothing_reports_no_rates(self) -> None:
        agg = calculate_aggregate_metrics([calculate_task_metrics([_rate_limited(0)])])

        assert agg["success_rate_micro"] is None
        assert agg["avg_score_micro"] is None
        assert agg["avg_turns"] is None

    def test_pass_at_k_loses_coverage_rather_than_estimating_from_fewer(self) -> None:
        """Five trials with one aborted cannot estimate pass@5 — and the row
        carries the counts that say why."""
        trajectories = [_trial(i, score=1.0) for i in range(4)] + [_rate_limited(4)]

        metrics = calculate_task_metrics(trajectories)

        assert metrics["measured_trials"] == 4
        assert metrics["infrastructure_aborts"]["rate_limit"] == 1
        assert metrics["pass@1"] == pytest.approx(1.0)
        assert metrics["pass@5"] is None

    def test_token_and_cost_spend_still_covers_every_attempt(self) -> None:
        """An aborted trial bought its tokens before it died, so spend counts
        them. Only performance rates move to the measured denominator."""
        spender = _trial(0, score=1.0)
        spender.metrics.cost_usd = 0.02
        aborted = _rate_limited(1)
        aborted.metrics.cost_usd = 0.01

        metrics = calculate_task_metrics([spender, aborted])

        assert metrics["total_cost_usd"] == pytest.approx(0.03)
        assert metrics["avg_cost_usd"] == pytest.approx(0.015)


@pytest.mark.unit
class TestJudgeCost:
    """Judge spend is accounted separately from agent cost and rolled up."""

    def _trial(self, idx: int, agent_cost: float, judge_cost: float | None) -> Trajectory:
        judge_usage = None if judge_cost is None else JudgeUsage(calls=1, cost_usd=judge_cost)
        return Trajectory(
            task_id="task_judge_cost",
            trial_index=idx,
            start_ts=datetime.now(tz=timezone.utc),
            end_ts=datetime.now(tz=timezone.utc),
            messages=[],
            metrics=Metrics(latency_total_s=1.0, usage=Usage(), cost_usd=agent_cost),
            grade=Grade(
                binary_pass=True,
                score=1.0,
                components=GradeComponents(),
                judge_usage=judge_usage,
            ),
        )

    def test_task_metrics_separate_agent_and_judge_cost(self):
        trajectories = [self._trial(0, 0.01, 0.005), self._trial(1, 0.02, 0.005)]
        m = calculate_task_metrics(trajectories)
        # total_cost_usd stays agent-only.
        assert m["total_cost_usd"] == pytest.approx(0.03)
        assert m["judge_cost_usd"] == pytest.approx(0.01)
        assert m["total_cost_incl_judge_usd"] == pytest.approx(0.04)

    def test_judge_cost_none_when_no_judge_ran(self):
        trajectories = [self._trial(0, 0.01, None), self._trial(1, 0.02, None)]
        m = calculate_task_metrics(trajectories)
        assert m["judge_cost_usd"] is None
        # With no judge cost, the combined total equals the agent total.
        assert m["total_cost_incl_judge_usd"] == pytest.approx(0.03)

    def test_aggregate_rolls_up_judge_cost_across_tasks(self):
        # A multi-trial task in one group so the rollup can't pass by treating
        # per-task totals as per-trial (sum-of-task-totals, not an average).
        task_a = calculate_task_metrics([self._trial(0, 0.01, 0.005), self._trial(1, 0.01, 0.005)])
        task_b = calculate_task_metrics([self._trial(0, 0.02, 0.005)])
        agg = calculate_aggregate_metrics([task_a, task_b])
        assert agg["total_cost_usd"] == pytest.approx(0.04)  # 0.01+0.01+0.02
        assert agg["judge_cost_usd"] == pytest.approx(0.015)  # 3 × 0.005
        assert agg["total_cost_incl_judge_usd"] == pytest.approx(0.055)

    def test_mixed_judge_and_no_judge_trials_in_one_task(self):
        # Only trials that actually ran a judge contribute to judge_cost_usd.
        trajectories = [self._trial(0, 0.01, 0.005), self._trial(1, 0.02, None)]
        m = calculate_task_metrics(trajectories)
        assert m["total_cost_usd"] == pytest.approx(0.03)
        assert m["judge_cost_usd"] == pytest.approx(0.005)
        assert m["total_cost_incl_judge_usd"] == pytest.approx(0.035)

    def test_zero_judge_cost_counted_not_treated_as_absent(self):
        # A real 0.0 judge cost must not be conflated with "no judge ran" (None).
        m = calculate_task_metrics([self._trial(0, 0.01, 0.0)])
        assert m["judge_cost_usd"] == 0.0
        assert m["judge_cost_usd"] is not None
        assert m["total_cost_incl_judge_usd"] == pytest.approx(0.01)

    def test_errored_judge_cost_is_still_counted(self):
        # An ERRORED judge still spent tokens; its cost must roll up (metrics are
        # judge-status-agnostic — they sum whatever judge_usage.cost_usd exists).
        traj = self._trial(0, 0.01, 0.003)
        traj.grade.judge_status = JudgeStatus.ERRORED
        m = calculate_task_metrics([traj])
        assert m["judge_cost_usd"] == pytest.approx(0.003)
        assert m["total_cost_incl_judge_usd"] == pytest.approx(0.013)
