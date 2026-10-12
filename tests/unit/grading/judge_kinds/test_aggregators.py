"""Unit tests for :mod:`tolokaforge.core.grading.judge_kinds.aggregators`.

Pure math over score vectors — no ``JudgeKind``/LLM stack involved. Covers
per-aggregator correctness, the validation helpers' fail-loud contracts, and
the Weiszfeld ``geometric_median`` implementation's convergence, its
robustness to a single outlier sample, its degenerate-iterate handling of
coincident points, and its fail-loud behaviour on forced non-convergence.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds import aggregators
from tolokaforge.core.grading.judge_kinds.aggregators import (
    aggregate_scores,
    require_binary_only,
    validate_aggregator_name,
    validate_sample_count,
)
from tolokaforge.runner.models import Criterion

pytestmark = pytest.mark.unit


class TestValidateAggregatorName:
    def test_accepts_every_known_aggregator(self) -> None:
        for name in ("majority", "median", "geometric_median"):
            validate_aggregator_name(name)  # must not raise

    def test_rejects_unknown_name(self) -> None:
        with pytest.raises(ValueError, match=r"unknown aggregator 'bogus'.*majority.*median"):
            validate_aggregator_name("bogus")


class TestValidateSampleCount:
    @pytest.mark.parametrize("n_samples", [0, 1, -1])
    def test_rejects_k_less_than_two(self, n_samples: int) -> None:
        with pytest.raises(ValueError, match="K<2 makes voting undefined"):
            validate_sample_count(n_samples, aggregator="median")

    def test_accepts_k_two_for_non_majority(self) -> None:
        validate_sample_count(2, aggregator="median")  # must not raise

    @pytest.mark.parametrize("n_samples", [2, 4, 6])
    def test_majority_rejects_even_k(self, n_samples: int) -> None:
        with pytest.raises(ValueError, match="odd n_samples"):
            validate_sample_count(n_samples, aggregator="majority")

    @pytest.mark.parametrize("n_samples", [3, 5, 7])
    def test_majority_accepts_odd_k(self, n_samples: int) -> None:
        validate_sample_count(n_samples, aggregator="majority")  # must not raise


class TestRequireBinaryOnly:
    def test_passes_all_binary_rubric(self) -> None:
        criteria = [Criterion(id="a", description="d", kind="binary")]
        require_binary_only(criteria)  # must not raise

    def test_raises_naming_graded_criterion_id(self) -> None:
        criteria = [
            Criterion(id="a", description="d", kind="binary"),
            Criterion(id="clarity", description="d", kind="graded"),
        ]
        with pytest.raises(ValueError, match=r"clarity"):
            require_binary_only(criteria)


class TestAggregateMajority:
    def test_per_criterion_vote_at_threshold(self) -> None:
        # 3 samples x 2 criteria: criterion 0 -> 2/3 met, criterion 1 -> 1/3 met
        rows = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
        result = aggregate_scores(aggregator="majority", per_sample_scores=rows)
        assert result == [1.0, 0.0]

    def test_unanimous_vote(self) -> None:
        rows = [[1.0], [1.0], [1.0]]
        assert aggregate_scores(aggregator="majority", per_sample_scores=rows) == [1.0]


class TestAggregateMedian:
    def test_per_criterion_median_over_column(self) -> None:
        rows = [[0.2, 0.9], [0.5, 0.1], [0.8, 0.5]]
        result = aggregate_scores(aggregator="median", per_sample_scores=rows)
        assert result == [0.5, 0.5]

    def test_even_row_count_averages_middle_two(self) -> None:
        rows = [[0.0], [1.0], [1.0], [1.0]]
        result = aggregate_scores(aggregator="median", per_sample_scores=rows)
        assert result == [1.0]


class TestAggregateGeometricMedian:
    def test_identical_rows_return_exact_vector(self) -> None:
        rows = [[0.3, 0.7, 1.0]] * 5
        result = aggregate_scores(aggregator="geometric_median", per_sample_scores=rows)
        assert result == pytest.approx([0.3, 0.7, 1.0], abs=1e-9)

    def test_robust_to_one_outlier_sample(self) -> None:
        rows = [[1.0, 1.0], [1.01, 1.01], [100.0, 100.0]]
        result = aggregate_scores(aggregator="geometric_median", per_sample_scores=rows)
        distance_to_cluster = ((result[0] - 1.0) ** 2 + (result[1] - 1.0) ** 2) ** 0.5
        distance_to_outlier = ((result[0] - 100.0) ** 2 + (result[1] - 100.0) ** 2) ** 0.5
        assert distance_to_cluster < 1.0
        assert distance_to_cluster < distance_to_outlier

    def test_k_minus_one_coincident_points_returns_coincident_vector(self) -> None:
        # RoPoLL's "all-but-one agree" shape: 4 samples agree, 1 diverges.
        # The coincident point is the exact geometric median (not merely
        # close to it) whenever K >= 3, per the Vardi-Zhang stationarity
        # test — must not raise despite the degenerate mid-iteration case.
        coincident = [0.9, 0.9, 0.1]
        rows = [coincident] * 4 + [[0.1, 0.1, 0.9]]
        result = aggregate_scores(aggregator="geometric_median", per_sample_scores=rows)
        assert result == pytest.approx(coincident, abs=1e-6)

    def test_single_criterion_matches_median(self) -> None:
        rows = [[0.2], [0.9], [0.5]]
        result = aggregate_scores(aggregator="geometric_median", per_sample_scores=rows)
        assert result == pytest.approx([0.5], abs=1e-6)

    def test_forced_non_convergence_raises_runtime_error(self, monkeypatch) -> None:
        monkeypatch.setattr(aggregators, "MAX_ITERATIONS", 1)
        rows = [[0.0, 0.0], [10.0, 0.0], [5.0, 10.0]]
        with pytest.raises(RuntimeError, match=r"MAX_ITERATIONS=1.*CONVERGENCE_TOLERANCE=1e-08"):
            aggregate_scores(aggregator="geometric_median", per_sample_scores=rows)


class TestAggregateScoresValidatesAggregator:
    def test_unknown_aggregator_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown aggregator"):
            aggregate_scores(aggregator="mean", per_sample_scores=[[1.0]])
