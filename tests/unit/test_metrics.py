"""Tests for metrics calculation, especially pass@k"""

from datetime import datetime, timezone

import pytest

from tolokaforge.core.metrics import (
    calculate_latency_percentiles,
    calculate_pass_k,
    compute_pass_at_k,
)
from tolokaforge.core.models import Grade, GradeComponents, Metrics, Trajectory

pytestmark = pytest.mark.unit


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
