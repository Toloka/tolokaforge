"""Tests for demo_recorder.batch — job discovery and result aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest
from demo_recorder.batch import JobResult, collect_jobs

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# JobResult dataclass
# ---------------------------------------------------------------------------


class TestJobResult:
    def test_defaults(self) -> None:
        result = JobResult(task_id="task-1", ok=True, output=Path("out.mp4"))
        assert result.task_id == "task-1"
        assert result.ok is True
        assert result.output == Path("out.mp4")
        assert result.error == ""

    def test_with_error(self) -> None:
        result = JobResult(task_id="task-2", ok=False, output=Path("x.mp4"), error="boom")
        assert not result.ok
        assert result.error == "boom"


# ---------------------------------------------------------------------------
# collect_jobs
# ---------------------------------------------------------------------------


class TestCollectJobs:
    def test_discovers_trajectories(self, tmp_path: Path) -> None:
        """Finds trials/*/0/trajectory.yaml under the run directory."""
        trials = tmp_path / "trials"
        for task_id in ("task_a", "task_b", "task_c"):
            traj_dir = trials / task_id / "0"
            traj_dir.mkdir(parents=True)
            (traj_dir / "trajectory.yaml").write_text("messages: []")

        jobs = collect_jobs(tmp_path)
        assert len(jobs) == 3
        task_ids = [j[0] for j in jobs]
        assert sorted(task_ids) == ["task_a", "task_b", "task_c"]

    def test_returns_empty_for_no_trajectories(self, tmp_path: Path) -> None:
        """Returns empty list when no trajectory files are found."""
        (tmp_path / "trials").mkdir()
        assert collect_jobs(tmp_path) == []

    def test_ignores_non_matching_structure(self, tmp_path: Path) -> None:
        """Only matches trials/*/0/trajectory.yaml pattern."""
        trials = tmp_path / "trials"
        # Wrong nesting — trajectory at top trial level
        wrong = trials / "task_x"
        wrong.mkdir(parents=True)
        (wrong / "trajectory.yaml").write_text("messages: []")
        # Wrong filename
        correct_dir = trials / "task_y" / "0"
        correct_dir.mkdir(parents=True)
        (correct_dir / "other.yaml").write_text("messages: []")

        assert collect_jobs(tmp_path) == []

    def test_extracts_task_id_from_path(self, tmp_path: Path) -> None:
        """Task ID is extracted from the path component between trials/ and /0/."""
        traj_dir = tmp_path / "trials" / "my_special_task" / "0"
        traj_dir.mkdir(parents=True)
        (traj_dir / "trajectory.yaml").write_text("messages: []")

        jobs = collect_jobs(tmp_path)
        assert len(jobs) == 1
        task_id, traj_path = jobs[0]
        assert task_id == "my_special_task"
        assert traj_path.name == "trajectory.yaml"

    def test_results_sorted(self, tmp_path: Path) -> None:
        """Jobs are returned sorted by trajectory path."""
        trials = tmp_path / "trials"
        for task_id in ("z_task", "a_task", "m_task"):
            traj_dir = trials / task_id / "0"
            traj_dir.mkdir(parents=True)
            (traj_dir / "trajectory.yaml").write_text("messages: []")

        jobs = collect_jobs(tmp_path)
        task_ids = [j[0] for j in jobs]
        assert task_ids == ["a_task", "m_task", "z_task"]
