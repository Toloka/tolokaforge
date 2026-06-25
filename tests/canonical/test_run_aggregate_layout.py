"""Canonical on-disk layout for the four run-level aggregate JSONs.

Locks the filenames, top-level shape, and envelope keys
(``schema_version``, ``summary`` / ``failures``) that downstream readers
depend on. Drift in any of these fails CI loudly.

Exercised through :class:`FileAggregateWriter` directly — the same
writer the orchestrator calls. Avoids spinning up a real run (Docker,
adapters, LLM) while still asserting on the bytes the orchestrator
would emit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.output.aggregates import FileAggregateWriter

pytestmark = pytest.mark.canonical


def _per_task_metrics() -> list[dict[str, Any]]:
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


def _aggregate() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "total_tasks": 1,
        "total_trials": 1,
        "success_rate_micro": 1.0,
        "avg_score_micro": 1.0,
        "avg_latency_s": 12.3,
    }


def _metadata_slices() -> dict[str, dict[str, Any]]:
    return {
        "by_benchmark_type": {"airline": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_complexity": {"simple": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_tag": {"domain:airline": {"total_tasks": 1, "success_rate_micro": 1.0}},
        "by_expected_failure_mode": {},
    }


def _failure_attribution() -> dict[str, Any]:
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
def populated_run_dir(tmp_path: Path) -> Path:
    """A run-output directory after :class:`FileAggregateWriter` has
    emitted all four aggregates via the bundle method."""
    writer = FileAggregateWriter()
    writer.write_run_aggregates(
        tmp_path,
        _per_task_metrics(),
        _aggregate(),
        _metadata_slices(),
        _failure_attribution(),
    )
    return tmp_path


class TestFilenamesAndDirectoryLayout:
    """The four aggregates land at the run-output root with the canonical
    names downstream readers expect."""

    def test_all_four_files_exist(self, populated_run_dir: Path) -> None:
        for name in (
            "per_task_metrics.json",
            "aggregate.json",
            "metadata_slices.json",
            "failure_attribution.json",
        ):
            assert (populated_run_dir / name).is_file(), f"missing {name}"

    def test_no_unexpected_files_at_run_root(self, populated_run_dir: Path) -> None:
        """The writer must not emit anything beyond the four declared files."""
        actual = {p.name for p in populated_run_dir.iterdir() if p.is_file()}
        assert actual == {
            "per_task_metrics.json",
            "aggregate.json",
            "metadata_slices.json",
            "failure_attribution.json",
        }


class TestTopLevelShapes:
    """Each artifact's JSON top-level type is what downstream consumers
    parse against. Drift here breaks every dashboard / analysis tool."""

    def test_per_task_metrics_is_list(self, populated_run_dir: Path) -> None:
        payload = json.loads((populated_run_dir / "per_task_metrics.json").read_text())
        assert isinstance(payload, list)
        assert len(payload) >= 1
        assert isinstance(payload[0], dict)

    def test_aggregate_is_dict_with_schema_version(self, populated_run_dir: Path) -> None:
        payload = json.loads((populated_run_dir / "aggregate.json").read_text())
        assert isinstance(payload, dict)
        assert payload["schema_version"] == 1

    def test_metadata_slices_is_dict_with_four_dimensions(self, populated_run_dir: Path) -> None:
        payload = json.loads((populated_run_dir / "metadata_slices.json").read_text())
        assert isinstance(payload, dict)
        assert set(payload.keys()) == {
            "by_benchmark_type",
            "by_complexity",
            "by_tag",
            "by_expected_failure_mode",
        }

    def test_failure_attribution_has_summary_and_failures(self, populated_run_dir: Path) -> None:
        payload = json.loads((populated_run_dir / "failure_attribution.json").read_text())
        assert isinstance(payload, dict)
        assert set(payload.keys()) == {"summary", "failures"}
        assert isinstance(payload["summary"], dict)
        assert isinstance(payload["failures"], list)


class TestSerializerConventions:
    """The writer must preserve the orchestrator's pre-PR serializer
    choices: ``indent=2`` for diff-able output, ``default=str`` so
    Enums / datetimes round-trip to strings without raising."""

    def test_indent_two_produces_pretty_output(self, populated_run_dir: Path) -> None:
        text = (populated_run_dir / "aggregate.json").read_text()
        assert "\n  " in text, "indent=2 should produce two-space indented JSON"

    def test_default_str_handles_enum_values(self, tmp_path: Path) -> None:
        """A non-JSON-native value (Enum) must not raise — it must stringify."""

        class _Status(Enum):
            FAILED = "failed"

        writer = FileAggregateWriter()
        writer.write_aggregate(
            tmp_path,
            {"schema_version": 1, "worst_status": _Status.FAILED},
        )
        payload = json.loads((tmp_path / "aggregate.json").read_text())
        assert isinstance(payload["worst_status"], str)
        assert "FAILED" in payload["worst_status"]

    def test_default_str_handles_datetime(self, tmp_path: Path) -> None:
        writer = FileAggregateWriter()
        writer.write_aggregate(
            tmp_path,
            {"schema_version": 1, "started_at": datetime(2026, 1, 1, tzinfo=UTC)},
        )
        payload = json.loads((tmp_path / "aggregate.json").read_text())
        assert payload["started_at"].startswith("2026-01-01")


class TestParentDirectoryCreation:
    """The writer creates ``output_dir`` if it doesn't already exist."""

    def test_creates_missing_parent(self, tmp_path: Path) -> None:
        nested = tmp_path / "does" / "not" / "exist"
        assert not nested.exists()
        FileAggregateWriter().write_aggregate(nested, _aggregate())
        assert (nested / "aggregate.json").is_file()
