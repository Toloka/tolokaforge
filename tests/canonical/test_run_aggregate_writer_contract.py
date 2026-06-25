"""Pin the ``RunAggregateWriter`` Protocol contract — runtime check + parity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.output.aggregates import (
    FileAggregateWriter,
    InMemoryAggregateWriter,
    RunAggregateBundle,
    RunAggregateWriter,
)

pytestmark = pytest.mark.canonical


def _make_per_task_metrics() -> list[dict[str, Any]]:
    return [
        {
            "task_id": "airline_001",
            "total_trials": 1,
            "successful_trials": 1,
            "success_rate": 1.0,
            "avg_score": 1.0,
            "benchmark_type": "airline",
            "complexity": "simple",
            "tags": ["domain:airline"],
            "expected_failure_modes": [],
        }
    ]


def _make_aggregate() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "total_tasks": 1,
        "total_trials": 1,
        "success_rate_micro": 1.0,
        "avg_score_micro": 1.0,
        "avg_latency_s": 12.3,
    }


def _make_metadata_slices() -> dict[str, dict[str, Any]]:
    return {
        "by_benchmark_type": {"airline": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_complexity": {"simple": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_tag": {"domain:airline": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_expected_failure_mode": {},
    }


def _make_failure_attribution() -> dict[str, Any]:
    return {
        "summary": {
            "total_failed_attempts": 0,
            "deterministic_attribution_coverage": 1.0,
            "by_failure_class": {},
            "by_tool": {},
        },
        "failures": [],
    }


@pytest.fixture
def writers(tmp_path: Path) -> list[RunAggregateWriter]:
    """Both production-shaped writers, ready to receive calls."""
    return [FileAggregateWriter(), InMemoryAggregateWriter()]


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy it
    via ``isinstance`` (not just by structural type-hint compatibility).
    """

    def test_file_aggregate_writer_passes_isinstance(self) -> None:
        assert isinstance(FileAggregateWriter(), RunAggregateWriter)

    def test_in_memory_aggregate_writer_passes_isinstance(self) -> None:
        assert isinstance(InMemoryAggregateWriter(), RunAggregateWriter)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAWriter:
            pass

        assert not isinstance(_NotAWriter(), RunAggregateWriter)


class TestPerRunMethodParity:
    """Every Protocol method accepts the same arguments on every implementation."""

    def test_write_per_task_metrics(
        self, writers: list[RunAggregateWriter], tmp_path: Path
    ) -> None:
        metrics = _make_per_task_metrics()
        for i, writer in enumerate(writers):
            writer.write_per_task_metrics(tmp_path / f"run_{i}", metrics)

    def test_write_aggregate(self, writers: list[RunAggregateWriter], tmp_path: Path) -> None:
        aggregate = _make_aggregate()
        for i, writer in enumerate(writers):
            writer.write_aggregate(tmp_path / f"run_{i}", aggregate)

    def test_write_metadata_slices(self, writers: list[RunAggregateWriter], tmp_path: Path) -> None:
        slices = _make_metadata_slices()
        for i, writer in enumerate(writers):
            writer.write_metadata_slices(tmp_path / f"run_{i}", slices)

    def test_write_failure_attribution(
        self, writers: list[RunAggregateWriter], tmp_path: Path
    ) -> None:
        attribution = _make_failure_attribution()
        for i, writer in enumerate(writers):
            writer.write_failure_attribution(tmp_path / f"run_{i}", attribution)

    def test_write_run_aggregates(self, writers: list[RunAggregateWriter], tmp_path: Path) -> None:
        for i, writer in enumerate(writers):
            writer.write_run_aggregates(
                tmp_path / f"run_{i}",
                _make_per_task_metrics(),
                _make_aggregate(),
                _make_metadata_slices(),
                _make_failure_attribution(),
            )


class TestInMemoryWriterBundleSemantics:
    """The in-memory writer stores aggregates under ``self.runs[output_dir]``
    as a :class:`RunAggregateBundle`. Tests assert through that surface
    instead of JSON-parsing files.
    """

    def test_per_run_isolation(self) -> None:
        writer = InMemoryAggregateWriter()
        writer.write_aggregate(Path("run_a"), _make_aggregate())
        writer.write_aggregate(Path("run_b"), _make_aggregate())
        assert set(writer.runs.keys()) == {Path("run_a"), Path("run_b")}
        assert isinstance(writer.runs[Path("run_a")], RunAggregateBundle)

    def test_write_run_aggregates_populates_four_artifacts(self) -> None:
        writer = InMemoryAggregateWriter()
        per_task = _make_per_task_metrics()
        aggregate = _make_aggregate()
        slices = _make_metadata_slices()
        attribution = _make_failure_attribution()
        writer.write_run_aggregates(Path("run_x"), per_task, aggregate, slices, attribution)
        bundle = writer.runs[Path("run_x")]
        assert bundle.per_task_metrics is per_task
        assert bundle.aggregate is aggregate
        assert bundle.metadata_slices is slices
        assert bundle.failure_attribution is attribution

    def test_overwrites_on_repeat_write(self) -> None:
        writer = InMemoryAggregateWriter()
        first = _make_aggregate()
        second = _make_aggregate()
        second["schema_version"] = 2
        writer.write_aggregate(Path("run_x"), first)
        writer.write_aggregate(Path("run_x"), second)
        assert writer.runs[Path("run_x")].aggregate is second

    def test_stores_by_reference_not_by_copy(self) -> None:
        """Mutating the source after the call surfaces through ``runs`` —
        matching the disk-backed writer's serialise-at-write-time semantics."""
        writer = InMemoryAggregateWriter()
        aggregate: dict[str, Any] = {"schema_version": 1, "total_tasks": 1}
        writer.write_aggregate(Path("run_x"), aggregate)
        aggregate["total_tasks"] = 99
        assert writer.runs[Path("run_x")].aggregate["total_tasks"] == 99

    def test_per_piece_writes_only_populate_their_slot(self) -> None:
        """``write_aggregate`` should not touch the other three slots."""
        writer = InMemoryAggregateWriter()
        writer.write_aggregate(Path("run_x"), _make_aggregate())
        bundle = writer.runs[Path("run_x")]
        assert bundle.aggregate is not None
        assert bundle.per_task_metrics is None
        assert bundle.metadata_slices is None
        assert bundle.failure_attribution is None

    def test_surface_path_forms_bucket_separately(self) -> None:
        """The in-memory writer keys on ``Path(output_dir)`` as supplied — it
        does not ``.resolve()`` (it does not touch the filesystem). Two
        surface forms of the same logical path bucket into separate runs.
        Pinned so a future "normalise the key" change has to update this
        test deliberately."""
        writer = InMemoryAggregateWriter()
        writer.write_aggregate(Path("a/b"), _make_aggregate())
        writer.write_aggregate(Path("./a/b"), _make_aggregate())
        assert set(writer.runs.keys()) == {Path("a/b"), Path("./a/b")}
