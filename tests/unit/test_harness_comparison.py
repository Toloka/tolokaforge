"""Unit tests for the per-harness comparison aggregation and labelled table."""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.output.harness_comparison import (
    HARNESS_COMPARISON_FOOTNOTE,
    NATIVE_BUCKET,
    build_harness_comparison_slices,
    format_harness_comparison_table,
    harness_bucket,
    has_comparable_harnesses,
)

pytestmark = pytest.mark.unit


def _row(
    *,
    task_id: str,
    harness_entry: str | None,
    execution_mode: str | None,
    benchmark_type: str | None,
    successful_trials: int,
    measured_trials: int = 2,
    avg_score: float = 1.0,
    total_cost_usd: float | None = 0.10,
    avg_turns: float | None = 4.0,
) -> dict[str, Any]:
    """A per-task metrics row shaped like the ones ``_generate_reports`` builds.

    Carries every field ``calculate_aggregate_metrics`` consumes, plus the
    ``harness_entry`` / ``execution_mode`` / ``benchmark_type`` the comparison
    groups on.
    """
    return {
        "task_id": task_id,
        "harness_entry": harness_entry,
        "execution_mode": execution_mode,
        "benchmark_type": benchmark_type,
        "total_trials": measured_trials,
        "measured_trials": measured_trials,
        "scored_trials": measured_trials,
        "successful_trials": successful_trials,
        "harness_errors": 0,
        "ungradeable": 0,
        "infrastructure_aborts": {},
        "outcomes_by_reason": {},
        "success_rate": successful_trials / measured_trials if measured_trials else None,
        "avg_score": avg_score,
        "pass@1": successful_trials / measured_trials if measured_trials else None,
        "pass@5": successful_trials / measured_trials if measured_trials else None,
        "pass@10": successful_trials / measured_trials if measured_trials else None,
        "avg_latency_s": 12.0,
        "avg_turns": avg_turns,
        "avg_tool_calls": 3.0,
        "stuck_rate": 0.0,
        "avg_cost_usd": total_cost_usd,
        "total_cost_usd": total_cost_usd,
        "costed_trials": measured_trials if total_cost_usd is not None else 0,
        "unpriced_trials": 0 if total_cost_usd is not None else measured_trials,
        "judge_cost_usd": None,
        "total_cost_incl_judge_usd": total_cost_usd,
        "total_cost_by_role": [],
    }


def _two_harness_run() -> list[dict[str, Any]]:
    """Two harness entries over overlapping families, one delegated + one engine loop."""
    return [
        _row(
            task_id="a",
            harness_entry="claude_code",
            execution_mode="delegated",
            benchmark_type="db",
            successful_trials=2,
        ),
        _row(
            task_id="b",
            harness_entry="claude_code",
            execution_mode="delegated",
            benchmark_type="browser",
            successful_trials=1,
        ),
        _row(
            task_id="a",
            harness_entry="native_loop",
            execution_mode="engine_loop",
            benchmark_type="db",
            successful_trials=1,
        ),
        _row(
            task_id="b",
            harness_entry="native_loop",
            execution_mode="engine_loop",
            benchmark_type="browser",
            successful_trials=0,
        ),
    ]


class TestHarnessBucketNormalisation:
    def test_none_and_empty_bucket_to_native(self) -> None:
        assert harness_bucket(None) == NATIVE_BUCKET
        assert harness_bucket("") == NATIVE_BUCKET

    def test_named_entry_keeps_its_name(self) -> None:
        assert harness_bucket("claude_code") == "claude_code"

    def test_mixed_old_none_and_new_named_rows_do_not_raise(self) -> None:
        rows = [
            _row(
                task_id="a",
                harness_entry=None,
                execution_mode=None,
                benchmark_type="db",
                successful_trials=1,
            ),
            _row(
                task_id="a",
                harness_entry="claude_code",
                execution_mode="delegated",
                benchmark_type="db",
                successful_trials=2,
            ),
        ]
        slices = build_harness_comparison_slices(rows)

        assert set(slices["by_harness_entry"]) == {NATIVE_BUCKET, "claude_code"}
        # None execution mode lands in the shared "unknown" bucket.
        assert set(slices["by_execution_mode"]) == {"unknown", "delegated"}


class TestBuildSlices:
    def test_grouped_by_harness_entry_with_right_numbers(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        by_entry = slices["by_harness_entry"]

        assert set(by_entry) == {"claude_code", "native_loop"}
        # claude_code: 2/2 + 1/2 = 3 of 4 measured.
        assert by_entry["claude_code"]["success_rate_micro"] == pytest.approx(0.75)
        # native_loop: 1/2 + 0/2 = 1 of 4 measured.
        assert by_entry["native_loop"]["success_rate_micro"] == pytest.approx(0.25)
        assert by_entry["claude_code"]["total_tasks"] == 2

    def test_grouped_by_execution_mode(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        by_mode = slices["by_execution_mode"]

        assert set(by_mode) == {"delegated", "engine_loop"}
        assert by_mode["delegated"]["success_rate_micro"] == pytest.approx(0.75)
        assert by_mode["engine_loop"]["success_rate_micro"] == pytest.approx(0.25)

    def test_grouped_by_harness_and_task_family(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        by_family = slices["by_harness_and_task_family"]

        assert set(by_family) == {
            "claude_code::db",
            "claude_code::browser",
            "native_loop::db",
            "native_loop::browser",
        }
        # Single-task groups: the micro rate is that task's rate.
        assert by_family["claude_code::db"]["success_rate_micro"] == pytest.approx(1.0)
        assert by_family["native_loop::browser"]["success_rate_micro"] == pytest.approx(0.0)

    def test_missing_benchmark_type_buckets_to_unknown(self) -> None:
        rows = [
            _row(
                task_id="a",
                harness_entry="x",
                execution_mode="delegated",
                benchmark_type=None,
                successful_trials=1,
            ),
            _row(
                task_id="b",
                harness_entry="y",
                execution_mode="delegated",
                benchmark_type=None,
                successful_trials=1,
            ),
        ]
        slices = build_harness_comparison_slices(rows)
        assert "x::unknown" in slices["by_harness_and_task_family"]
        assert "y::unknown" in slices["by_harness_and_task_family"]


class TestComparabilityHelper:
    def test_two_non_native_buckets_are_comparable(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        assert has_comparable_harnesses(slices["by_harness_entry"]) is True

    def test_single_bucket_is_not_comparable(self) -> None:
        assert has_comparable_harnesses({"claude_code": {}}) is False

    def test_only_native_is_not_comparable(self) -> None:
        assert has_comparable_harnesses({NATIVE_BUCKET: {}}) is False


class TestFormatter:
    def test_labelled_headers_and_footnote(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        table = format_harness_comparison_table(slices)

        assert table is not None
        # Cost and turns carry the explicit per-harness basis tag.
        assert "cost (per-harness basis)" in table
        assert "turns (per-harness basis)" in table
        # The cross-harness comparables do not carry a basis tag.
        for line in table.splitlines():
            if line.startswith("harness") and "per-harness basis" in line:
                # The only basis tags on a header line are cost/turns.
                assert line.count("per-harness basis") == 2
        assert "success rate" in table
        assert "pass@1" in table and "pass@5" in table and "pass@10" in table
        # The footnote is present verbatim.
        assert HARNESS_COMPARISON_FOOTNOTE in table

    def test_cross_harness_columns_have_no_basis_tag(self) -> None:
        slices = build_harness_comparison_slices(_two_harness_run())
        table = format_harness_comparison_table(slices)
        assert table is not None

        # Isolate the header line of the per-harness-entry table.
        header_line = next(
            line for line in table.splitlines() if line.lstrip().startswith("harness")
        )
        # Strip the two tagged columns; no other column may carry the tag.
        remainder = header_line.replace("cost (per-harness basis)", "").replace(
            "turns (per-harness basis)", ""
        )
        assert "per-harness basis" not in remainder

    def test_single_entry_input_degrades_to_none(self) -> None:
        rows = [
            _row(
                task_id="a",
                harness_entry="claude_code",
                execution_mode="delegated",
                benchmark_type="db",
                successful_trials=2,
            ),
            _row(
                task_id="b",
                harness_entry="claude_code",
                execution_mode="delegated",
                benchmark_type="browser",
                successful_trials=1,
            ),
        ]
        slices = build_harness_comparison_slices(rows)
        # The slices still compute...
        assert set(slices["by_harness_entry"]) == {"claude_code"}
        # ...but the formatter renders nothing to compare.
        assert format_harness_comparison_table(slices) is None

    def test_old_single_adapter_run_degrades_to_none(self) -> None:
        rows = [
            _row(
                task_id="a",
                harness_entry=None,
                execution_mode=None,
                benchmark_type="db",
                successful_trials=1,
            ),
        ]
        slices = build_harness_comparison_slices(rows)
        assert set(slices["by_harness_entry"]) == {NATIVE_BUCKET}
        assert format_harness_comparison_table(slices) is None
