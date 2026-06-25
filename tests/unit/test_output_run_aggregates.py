"""``tolokaforge.core.output.aggregates`` unit tests.

Covers:

* :class:`FileAggregateWriter` — per-piece methods write the right JSON
  to the right filename with the orchestrator's pre-PR serializer
  conventions (``indent=2``, ``default=str``).
* :class:`InMemoryAggregateWriter` — per-piece methods populate the
  matching slot on the run's :class:`RunAggregateBundle`.
* :func:`write_run_aggregates` — bundle method on both writers writes
  all four artifacts in one call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.output.aggregates import (
    FileAggregateWriter,
    InMemoryAggregateWriter,
    RunAggregateBundle,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Synthetic payloads
# ---------------------------------------------------------------------------


def _per_task_metrics() -> list[dict[str, Any]]:
    return [
        {"task_id": "t1", "success_rate": 1.0, "tags": ["x"]},
        {"task_id": "t2", "success_rate": 0.0, "tags": []},
    ]


def _aggregate() -> dict[str, Any]:
    return {"schema_version": 1, "total_tasks": 2, "success_rate_micro": 0.5}


def _slices() -> dict[str, dict[str, Any]]:
    return {
        "by_benchmark_type": {"airline": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_complexity": {},
        "by_tag": {"x": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_expected_failure_mode": {},
    }


def _attribution() -> dict[str, Any]:
    return {
        "summary": {"total_failed_attempts": 1, "deterministic_attribution_coverage": 1.0},
        "failures": [{"task_id": "t2", "failure_class": "tool_error"}],
    }


# ---------------------------------------------------------------------------
# FileAggregateWriter
# ---------------------------------------------------------------------------


class TestFileAggregateWriterPerPiece:
    def test_write_per_task_metrics(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_per_task_metrics(tmp_path, _per_task_metrics())
        target = tmp_path / "per_task_metrics.json"
        assert target.is_file()
        assert json.loads(target.read_text()) == _per_task_metrics()

    def test_write_aggregate(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_aggregate(tmp_path, _aggregate())
        target = tmp_path / "aggregate.json"
        assert target.is_file()
        assert json.loads(target.read_text()) == _aggregate()

    def test_write_metadata_slices(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_metadata_slices(tmp_path, _slices())
        target = tmp_path / "metadata_slices.json"
        assert target.is_file()
        assert json.loads(target.read_text()) == _slices()

    def test_write_failure_attribution(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_failure_attribution(tmp_path, _attribution())
        target = tmp_path / "failure_attribution.json"
        assert target.is_file()
        assert json.loads(target.read_text()) == _attribution()

    def test_overwrite_unconditional(self, tmp_path: Path) -> None:
        """The run-output root is owned by the run; subsequent writes
        replace prior contents."""
        writer = FileAggregateWriter()
        writer.write_aggregate(tmp_path, {"schema_version": 1})
        writer.write_aggregate(tmp_path, {"schema_version": 2})
        assert json.loads((tmp_path / "aggregate.json").read_text())["schema_version"] == 2


class TestFileAggregateWriterBundle:
    def test_write_run_aggregates_emits_four_files(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_run_aggregates(
            tmp_path,
            _per_task_metrics(),
            _aggregate(),
            _slices(),
            _attribution(),
        )
        for name in (
            "per_task_metrics.json",
            "aggregate.json",
            "metadata_slices.json",
            "failure_attribution.json",
        ):
            assert (tmp_path / name).is_file(), f"missing {name}"

    def test_bundle_payloads_round_trip(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        per_task, aggregate, slices, attribution = (
            _per_task_metrics(),
            _aggregate(),
            _slices(),
            _attribution(),
        )
        writer.write_run_aggregates(tmp_path, per_task, aggregate, slices, attribution)
        assert json.loads((tmp_path / "per_task_metrics.json").read_text()) == per_task
        assert json.loads((tmp_path / "aggregate.json").read_text()) == aggregate
        assert json.loads((tmp_path / "metadata_slices.json").read_text()) == slices
        assert json.loads((tmp_path / "failure_attribution.json").read_text()) == attribution


# ---------------------------------------------------------------------------
# InMemoryAggregateWriter
# ---------------------------------------------------------------------------


class TestInMemoryAggregateWriterPerPiece:
    def test_write_per_task_metrics(self) -> None:
        writer = InMemoryAggregateWriter()
        payload = _per_task_metrics()
        writer.write_per_task_metrics(Path("r0"), payload)
        assert writer.runs[Path("r0")].per_task_metrics is payload

    def test_write_aggregate(self) -> None:
        writer = InMemoryAggregateWriter()
        payload = _aggregate()
        writer.write_aggregate(Path("r0"), payload)
        assert writer.runs[Path("r0")].aggregate is payload

    def test_write_metadata_slices(self) -> None:
        writer = InMemoryAggregateWriter()
        payload = _slices()
        writer.write_metadata_slices(Path("r0"), payload)
        assert writer.runs[Path("r0")].metadata_slices is payload

    def test_write_failure_attribution(self) -> None:
        writer = InMemoryAggregateWriter()
        payload = _attribution()
        writer.write_failure_attribution(Path("r0"), payload)
        assert writer.runs[Path("r0")].failure_attribution is payload

    def test_empty_writer_has_no_runs(self) -> None:
        writer = InMemoryAggregateWriter()
        assert writer.runs == {}

    def test_bundle_default_all_none(self) -> None:
        bundle = RunAggregateBundle()
        assert bundle.per_task_metrics is None
        assert bundle.aggregate is None
        assert bundle.metadata_slices is None
        assert bundle.failure_attribution is None


class TestInMemoryAggregateWriterBundle:
    def test_write_run_aggregates_populates_all_four_slots(self) -> None:
        writer = InMemoryAggregateWriter()
        per_task, aggregate, slices, attribution = (
            _per_task_metrics(),
            _aggregate(),
            _slices(),
            _attribution(),
        )
        writer.write_run_aggregates(Path("r0"), per_task, aggregate, slices, attribution)
        bundle = writer.runs[Path("r0")]
        assert bundle.per_task_metrics is per_task
        assert bundle.aggregate is aggregate
        assert bundle.metadata_slices is slices
        assert bundle.failure_attribution is attribution
