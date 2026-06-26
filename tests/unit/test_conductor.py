"""Unit tests for ``tolokaforge.core.conductor``.

Covers the in-memory conductor's own shape, the call log dataclass,
and the default trajectory factory. Cross-implementation parity lives
in ``tests/canonical/test_conductor_contract.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import (
    ConductorCallLog,
    InMemoryConductor,
    _default_success_trajectory,
)

pytestmark = pytest.mark.unit


class TestConductorCallLog:
    def test_default_fields_are_empty(self) -> None:
        log = ConductorCallLog()
        assert log.runs == []

    def test_equality_holds_for_identical_state(self) -> None:
        assert ConductorCallLog() == ConductorCallLog()

    def test_inequality_when_runs_diverge(self) -> None:
        a = ConductorCallLog()
        b = ConductorCallLog()
        a.runs.append({"trial_id": "x:0"})
        assert a != b


class TestDefaultSuccessTrajectoryFactory:
    """The default factory builds a minimal completed trajectory with a
    passing grade. Sufficient for tests that don't care about content;
    callers that need failure scenarios pass a custom factory.
    """

    def test_default_trajectory_is_completed(self) -> None:
        from tolokaforge.core.models import TrialStatus

        traj = _default_success_trajectory("airline_001", 0)
        assert traj.status == TrialStatus.COMPLETED

    def test_default_trajectory_grade_is_passing(self) -> None:
        traj = _default_success_trajectory("airline_001", 0)
        assert traj.grade is not None
        assert traj.grade.binary_pass is True
        assert traj.grade.score == 1.0

    def test_default_trajectory_carries_task_and_index(self) -> None:
        traj = _default_success_trajectory("airline_001", 3)
        assert traj.task_id == "airline_001"
        assert traj.trial_index == 3

    def test_default_trajectory_has_empty_message_history(self) -> None:
        traj = _default_success_trajectory("airline_001", 0)
        assert traj.messages == []


class TestInMemoryConductorConstruction:
    def test_fresh_backend_has_a_call_log(self) -> None:
        backend = InMemoryConductor()
        assert isinstance(backend.call_log, ConductorCallLog)

    def test_fresh_backend_call_log_is_empty(self) -> None:
        backend = InMemoryConductor()
        assert backend.call_log.runs == []

    def test_each_backend_has_independent_call_log(self, tmp_path: Path) -> None:
        a = InMemoryConductor()
        b = InMemoryConductor()
        a.run(
            task=MagicMock(task_id="t1"),
            trial_idx=0,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            env_endpoints=MagicMock(),
        )
        assert len(a.call_log.runs) == 1
        assert b.call_log.runs == []

    def test_custom_factory_replaces_default(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from tolokaforge.core.models import Metrics, Trajectory, TrialStatus

        seen_args: list[tuple[str, int]] = []

        def factory(task_id: str, trial_idx: int):
            seen_args.append((task_id, trial_idx))
            now = datetime.now(UTC)
            return Trajectory(
                task_id=task_id,
                trial_index=trial_idx,
                start_ts=now,
                end_ts=now,
                status=TrialStatus.FAILED,
                messages=[],
                metrics=Metrics(),
                grade=None,
            )

        backend = InMemoryConductor(trajectory_factory=factory)
        backend.run(
            task=MagicMock(task_id="t1"),
            trial_idx=5,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            env_endpoints=MagicMock(),
        )
        assert seen_args == [("t1", 5)]


class TestInMemoryConductorRun:
    def test_trial_id_format_is_canonical(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        result = backend.run(
            task=MagicMock(task_id="airline_001"),
            trial_idx=3,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            env_endpoints=MagicMock(),
        )
        assert result.trial_id == "airline_001:3"

    def test_worker_id_threads_through(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        result = backend.run(
            task=MagicMock(task_id="t1"),
            trial_idx=0,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            worker_id="worker-42",
            env_endpoints=MagicMock(),
        )
        assert result.worker_id == "worker-42"

    def test_worker_id_default_is_none(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        result = backend.run(
            task=MagicMock(task_id="t1"),
            trial_idx=0,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            env_endpoints=MagicMock(),
        )
        assert result.worker_id is None
