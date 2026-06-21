"""Pin the ``TrialArtifactWriter`` Protocol contract — runtime check + parity."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    Metrics,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    InMemoryArtifactWriter,
    TrialArtifactBundle,
    TrialArtifactWriter,
)

pytestmark = pytest.mark.canonical


def _make_trajectory() -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id="airline_001",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[Message(role="assistant", content="done")],
        metrics=Metrics(),
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(),
            reasons="ok",
        ),
    )


def _make_task_snapshot() -> dict[str, Any]:
    return {"task_id": "airline_001", "category": "airline", "description": "book"}


def _make_env_state() -> dict[str, Any]:
    return {"users": [{"id": "u1", "name": "Alice"}]}


class _FakeStructuredLogger:
    """Minimal stand-in for ``StructuredLogger`` for tests that need to invoke
    the writers without pulling in the real logging stack. The disk-backed
    ``OutputWriter.write_logs`` calls ``logger.save_to_file(path)``; the
    fake honours that contract by emitting a tiny YAML."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def save_to_file(self, path: Path) -> None:
        path.write_text("records: []\n", encoding="utf-8")


@pytest.fixture
def writers(tmp_path: Path) -> list[TrialArtifactWriter]:
    """Both production-shaped writers, ready to receive calls."""
    return [FileArtifactWriter(), InMemoryArtifactWriter()]


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy it
    via ``isinstance`` (not just by structural type-hint compatibility).
    """

    def test_file_artifact_writer_passes_isinstance(self) -> None:
        assert isinstance(FileArtifactWriter(), TrialArtifactWriter)

    def test_in_memory_artifact_writer_passes_isinstance(self) -> None:
        assert isinstance(InMemoryArtifactWriter(), TrialArtifactWriter)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAWriter:
            pass

        assert not isinstance(_NotAWriter(), TrialArtifactWriter)


class TestPerTrialMethodParity:
    """Every Protocol method accepts the same arguments on every implementation."""

    def test_write_trajectory(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        trajectory = _make_trajectory()
        for i, writer in enumerate(writers):
            writer.write_trajectory(tmp_path / f"trial_{i}", trajectory)

    def test_write_task(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        snapshot = _make_task_snapshot()
        for i, writer in enumerate(writers):
            writer.write_task(tmp_path / f"trial_{i}", snapshot)

    def test_write_env(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        env = _make_env_state()
        for i, writer in enumerate(writers):
            writer.write_env(tmp_path / f"trial_{i}", env)

    def test_write_metrics(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        trajectory = _make_trajectory()
        for i, writer in enumerate(writers):
            writer.write_metrics(tmp_path / f"trial_{i}", trajectory)

    def test_write_grade(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        grade = _make_trajectory().grade
        assert grade is not None
        for i, writer in enumerate(writers):
            writer.write_grade(tmp_path / f"trial_{i}", grade)

    def test_write_logs(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        logger = _FakeStructuredLogger()
        for i, writer in enumerate(writers):
            writer.write_logs(tmp_path / f"trial_{i}", logger)  # type: ignore[arg-type]

    def test_write_tools_schemas(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        schemas = [{"type": "function", "function": {"name": "echo"}}]
        for i, writer in enumerate(writers):
            writer.write_tools_schemas(tmp_path / f"trial_{i}", schemas)

    def test_write_prompts(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        for i, writer in enumerate(writers):
            writer.write_prompts(tmp_path / f"trial_{i}", "sys", "user-sys")

    def test_write_trial_bundle(self, writers: list[TrialArtifactWriter], tmp_path: Path) -> None:
        trajectory = _make_trajectory()
        snapshot = _make_task_snapshot()
        env = _make_env_state()
        logger = _FakeStructuredLogger()
        for i, writer in enumerate(writers):
            writer.write_trial_bundle(
                tmp_path / f"trial_{i}",
                trajectory,
                snapshot,
                env,
                logger,  # type: ignore[arg-type]
            )


class TestInMemoryWriterBundleSemantics:
    """The in-memory writer stores artifacts under ``self.trials[trial_dir]``
    as a :class:`TrialArtifactBundle`. Tests assert through that surface
    instead of YAML-parsing files.
    """

    def test_per_trial_isolation(self) -> None:
        writer = InMemoryArtifactWriter()
        writer.write_trajectory(Path("a:0"), _make_trajectory())
        writer.write_trajectory(Path("b:0"), _make_trajectory())
        assert set(writer.trials.keys()) == {Path("a:0"), Path("b:0")}
        assert isinstance(writer.trials[Path("a:0")], TrialArtifactBundle)

    def test_write_trial_bundle_populates_six_artifacts(self) -> None:
        writer = InMemoryArtifactWriter()
        trajectory = _make_trajectory()
        snapshot = _make_task_snapshot()
        env = _make_env_state()
        logger = _FakeStructuredLogger()
        writer.write_trial_bundle(
            Path("x:0"),
            trajectory,
            snapshot,
            env,
            logger,  # type: ignore[arg-type]
        )
        bundle = writer.trials[Path("x:0")]
        assert bundle.task is snapshot
        assert bundle.trajectory is trajectory
        assert bundle.env is env
        assert bundle.metrics is trajectory.metrics
        assert isinstance(bundle.metrics, Metrics)
        assert bundle.grade is trajectory.grade
        assert bundle.logs is logger

    def test_overwrites_on_repeat_write(self) -> None:
        writer = InMemoryArtifactWriter()
        first = _make_trajectory()
        second = _make_trajectory()
        writer.write_trajectory(Path("x:0"), first)
        writer.write_trajectory(Path("x:0"), second)
        assert writer.trials[Path("x:0")].trajectory is second

    def test_stores_by_reference_not_by_copy(self) -> None:
        """Mutating the source after the call surfaces through ``trials`` —
        matching the disk-backed writer's serialise-at-write-time semantics."""
        writer = InMemoryArtifactWriter()
        snapshot: dict[str, Any] = {"task_id": "x"}
        writer.write_task(Path("x:0"), snapshot)
        snapshot["task_id"] = "y"
        assert writer.trials[Path("x:0")].task["task_id"] == "y"

    def test_write_metrics_stores_metrics_extract_not_whole_trajectory(self) -> None:
        """``write_metrics`` mirrors the disk-backed writer's behaviour:
        ``metrics.yaml`` is serialised from ``trajectory.metrics``, so the
        in-memory bundle stores that same :class:`Metrics` extract, not the
        whole :class:`Trajectory`."""
        writer = InMemoryArtifactWriter()
        trajectory = _make_trajectory()
        writer.write_metrics(Path("x:0"), trajectory)
        stored = writer.trials[Path("x:0")].metrics
        assert isinstance(stored, Metrics)
        assert stored is trajectory.metrics

    def test_surface_path_forms_bucket_separately(self) -> None:
        """The in-memory writer keys on ``Path(trial_dir)`` as supplied — it
        does not ``.resolve()`` (it does not touch the filesystem). Two
        surface forms of the same logical path bucket into separate trials.
        Pinned so a future "normalise the key" change has to update this
        test deliberately."""
        writer = InMemoryArtifactWriter()
        writer.write_trajectory(Path("a/b"), _make_trajectory())
        writer.write_trajectory(Path("./a/b"), _make_trajectory())
        assert set(writer.trials.keys()) == {Path("a/b"), Path("./a/b")}
