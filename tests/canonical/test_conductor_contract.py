"""Pin the ``Conductor`` Protocol contract — runtime check + parity.

Two implementations are checked: :class:`InProcessConductor` (constructed
with the orchestrator's per-run dependencies but never actually invoked
on a real trial — would require the full Docker stack) and
:class:`InMemoryConductor` (records calls + returns a configurable
synthetic trial result).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import (
    Conductor,
    ConductorCallLog,
    InMemoryConductor,
    InProcessConductor,
)

pytestmark = pytest.mark.canonical


def _make_in_process_conductor() -> InProcessConductor:
    """Build an :class:`InProcessConductor` with stubbed dependencies. Never
    invoked end-to-end in the contract tests — its presence is what
    matters for the Protocol check."""
    return InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        verbose=False,
        strict=False,
    )


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy
    it via ``isinstance`` (not just by structural type-hint compatibility).
    """

    def test_in_process_conductor_passes_isinstance(self) -> None:
        assert isinstance(_make_in_process_conductor(), Conductor)

    def test_in_memory_conductor_passes_isinstance(self) -> None:
        assert isinstance(InMemoryConductor(), Conductor)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAConductor:
            pass

        assert not isinstance(_NotAConductor(), Conductor)


class TestRunMethodSignature:
    """Both implementations accept the same arguments on ``run()``. The
    in-memory conductor exercises the call end-to-end; the in-process
    conductor's signature is verified by ``inspect.signature`` because
    invoking ``run()`` for real would require a full Docker stack.
    """

    def test_in_memory_run_accepts_all_keyword_args(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        # All 12 parameters from the Protocol must be accepted.
        result = backend.run(
            task=MagicMock(task_id="t1"),
            trial_idx=0,
            agent_client=MagicMock(),
            user_config=MagicMock(),
            output_dir=tmp_path,
            docker_runtime=MagicMock(),
            request_limiter=MagicMock(),
            attempt_id=2,
            worker_id="worker-7",
            env_endpoints=MagicMock(),
            judge_config=MagicMock(),
        )
        assert result.trial_id == "t1:0"
        assert result.worker_id == "worker-7"

    def test_in_process_run_method_signature_matches_protocol(self) -> None:
        import inspect

        protocol_sig = inspect.signature(Conductor.run)
        impl_sig = inspect.signature(InProcessConductor.run)
        # Parameter names must match (allows both impls to be drop-in).
        assert list(protocol_sig.parameters.keys()) == list(impl_sig.parameters.keys())


class TestInMemoryConductorSemantics:
    """The in-memory conductor records every ``run()`` and returns a
    synthetic :class:`TrialResult`. The default factory returns a
    success trajectory; a custom factory drives any scenario.
    """

    def test_call_log_records_trial_metadata(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        backend.run(
            task=MagicMock(task_id="airline_001"),
            trial_idx=3,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            attempt_id=1,
            worker_id="w-1",
            env_endpoints=MagicMock(),
        )
        assert backend.call_log.runs == [
            {
                "trial_id": "airline_001:3",
                "task_id": "airline_001",
                "trial_idx": 3,
                "attempt_id": 1,
                "worker_id": "w-1",
            }
        ]

    def test_default_factory_returns_success_trajectory(self, tmp_path: Path) -> None:
        from tolokaforge.core.models import TrialStatus

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
        assert result.trajectory.status == TrialStatus.COMPLETED
        assert result.trajectory.grade is not None
        assert result.trajectory.grade.binary_pass is True

    def test_custom_factory_drives_failure_scenario(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from tolokaforge.core.models import (
            Metrics,
            TerminationReason,
            Trajectory,
            TrialStatus,
        )

        def error_factory(task_id: str, trial_idx: int) -> Trajectory:
            now = datetime.now(UTC)
            return Trajectory(
                task_id=task_id,
                trial_index=trial_idx,
                start_ts=now,
                end_ts=now,
                status=TrialStatus.ERROR,
                termination_reason=TerminationReason.ERROR,
                messages=[],
                metrics=Metrics(),
                grade=None,
            )

        backend = InMemoryConductor(trajectory_factory=error_factory)
        result = backend.run(
            task=MagicMock(task_id="t1"),
            trial_idx=0,
            agent_client=None,
            user_config=None,
            output_dir=tmp_path,
            docker_runtime=None,
            env_endpoints=MagicMock(),
        )
        assert result.trajectory.status == TrialStatus.ERROR
        assert result.trajectory.grade is None

    def test_each_run_appends_independently(self, tmp_path: Path) -> None:
        backend = InMemoryConductor()
        for i in range(3):
            backend.run(
                task=MagicMock(task_id="t1"),
                trial_idx=i,
                agent_client=None,
                user_config=None,
                output_dir=tmp_path,
                docker_runtime=None,
                env_endpoints=MagicMock(),
            )
        assert len(backend.call_log.runs) == 3
        assert [r["trial_idx"] for r in backend.call_log.runs] == [0, 1, 2]

    def test_fresh_backend_has_empty_call_log(self) -> None:
        backend = InMemoryConductor()
        assert backend.call_log == ConductorCallLog()
