"""Trial lifecycle: the conductor opens and closes every trial, the orchestrator closes the run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.models import Trajectory
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.observability.factory import TRACING_RECEIPT_FILE, RunIdentity
from tolokaforge.observability.observer import (
    ExportReceipt,
    InMemoryTrialObserver,
    NullTrialObserver,
)

pytestmark = pytest.mark.unit


def _spec(attempt: int = 0) -> MagicMock:
    spec = MagicMock()
    spec.trial_id = "T-1:0"
    spec.run_id = "engine-run"
    spec.attempt_id = attempt
    spec.worker_id = "w1"
    spec.agent_model_config.provider = "openrouter"
    spec.agent_model_config.name = "acme/agent-1"
    spec.user_model_config = None
    spec.judge_model_config = None
    return spec


def _trajectory() -> Trajectory:
    now = datetime.now(tz=timezone.utc)
    return Trajectory(task_id="T-1", trial_index=0, start_ts=now, end_ts=now, messages=[])


def _conductor(observer, run_identity=None, trial_dir: Path | None = None) -> InProcessConductor:
    conductor = InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=Path("/tmp"),
        trial_observer=observer,
        run_identity=run_identity,
    )
    setup = MagicMock()
    setup.trial_id = "T-1:0"
    setup.trial_idx = 0
    # a real path: the error path asks whether a bundle exists under it
    setup.trial_dir = trial_dir or Path("/nonexistent/tolokaforge-trial")
    conductor._setup_trial = MagicMock(return_value=setup)
    conductor._capture_final_state = MagicMock()
    conductor._grade = MagicMock()
    conductor._produce_grade_bundle = MagicMock()
    conductor._write_artifacts = MagicMock()
    return conductor


class TestConductorLifecycle:
    def test_started_then_finished_with_the_attempt_recorded_before_the_bundle(self) -> None:
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        conductor = _conductor(observer, RunIdentity(run_id="acme/pilot/v1/1/1", run_tag="v2"))
        trajectory = _trajectory()
        conductor._run_agent_loop = MagicMock(return_value=(trajectory, MagicMock(), "sys"))
        conductor.run(_spec(attempt=1), MagicMock())

        names = [name for name, _ in observer.call_log.calls]
        # the bundle is announced by the trial executor after its own writes, not by run()
        assert names == ["trial_started", "trial_finished"]
        started, finished = observer.call_log.calls[0][1], observer.call_log.calls[1][1]
        identity = started["identity"]
        assert (
            identity.run_id,
            identity.run_tag,
            identity.task_id,
            identity.trial_index,
            identity.attempt_id,
        ) == (
            "acme/pilot/v1/1/1",
            "v2",
            "T-1",
            0,
            1,
        )
        assert started["models"]["agent"].name == "acme/agent-1"
        assert finished["trajectory"] is trajectory and finished["error"] is None
        assert trajectory.attempt_id == 1
        # the loop received a binding for the agent role
        assert conductor._run_agent_loop.call_args.args[3] is identity
        # the bundle is written after the trace closed
        conductor._write_artifacts.assert_called_once()

    def test_snapshot_and_primary_bundle_record_the_same_attempt(self, tmp_path: Path) -> None:
        conductor = _conductor(InMemoryTrialObserver())
        trajectory = _trajectory()
        conductor._run_agent_loop = MagicMock(return_value=(trajectory, MagicMock(), "sys"))

        def snapshot(spec, setup, value):
            (tmp_path / "snapshot.json").write_text(value.model_dump_json())

        def primary(spec, task_config, setup, value, runner):
            (tmp_path / "trajectory.yaml").write_text(yaml.safe_dump(value.model_dump(mode="json")))

        conductor._produce_grade_bundle = snapshot
        conductor._write_artifacts = primary
        conductor.run(_spec(attempt=3), MagicMock())
        snapshot_value = json.loads((tmp_path / "snapshot.json").read_text())
        primary_value = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        assert snapshot_value["attempt_id"] == primary_value["attempt_id"] == 3

    def test_trial_persisted_announces_an_existing_bundle_under_the_contract_identity(
        self, tmp_path: Path
    ) -> None:
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        conductor = _conductor(observer, RunIdentity(run_id="acme/pilot/v1/1/1", run_tag="v2"))
        conductor.output_dir = tmp_path
        conductor.trial_persisted(_spec(attempt=1))  # no bundle yet: nothing announced
        assert observer.call_log.calls == []
        trial_dir = tmp_path / "trials" / "T-1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "trajectory.yaml").write_text("task_id: T-1\n")
        conductor.trial_persisted(_spec(attempt=1))
        ((name, payload),) = observer.call_log.calls
        assert name == "trial_persisted" and payload["trial_dir"] == trial_dir
        identity = payload["identity"]
        assert (identity.run_id, identity.run_tag, identity.task_id, identity.trial_index) == (
            "acme/pilot/v1/1/1",
            "v2",
            "T-1",
            0,
        )
        assert identity.attempt_id == 1

    def test_without_tracing_the_engine_run_id_is_the_identity_and_no_binding_is_built(
        self,
    ) -> None:
        conductor = _conductor(NullTrialObserver())
        trajectory = _trajectory()
        conductor._run_agent_loop = MagicMock(return_value=(trajectory, MagicMock(), "sys"))
        conductor.run(_spec(), MagicMock())
        identity = conductor._run_agent_loop.call_args.args[3]
        assert identity.run_id == "engine-run" and identity.run_tag == "v1"
        assert trajectory.attempt_id == 0

    def test_a_trial_that_raises_still_closes_its_trace_with_the_error(self) -> None:
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        conductor = _conductor(observer)
        conductor._run_agent_loop = MagicMock(side_effect=RuntimeError("lost"))
        with pytest.raises(RuntimeError):
            conductor.run(_spec(), MagicMock())
        names = [name for name, _ in observer.call_log.calls]
        # no bundle exists on this path, so nothing is announced as persisted
        assert names == ["trial_started", "trial_finished"]
        finished = observer.call_log.calls[1][1]
        assert finished["trajectory"] is None and finished["error"] == "RuntimeError: lost"
        conductor._write_artifacts.assert_not_called()

    def test_a_failing_trial_announces_nothing_even_when_a_stale_bundle_exists(
        self, tmp_path: Path
    ) -> None:
        """trajectory.yaml is only written after the trial body succeeded, so a bundle found on
        the error path belongs to an earlier attempt (same directory, no cleanup between
        attempts): announcing it would attach attempt 0's files to attempt 1's trace."""
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        trial_dir = tmp_path / "trials" / "T-1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "trajectory.yaml").write_text("task_id: T-1\nattempt_id: 0\n")
        conductor = _conductor(observer, trial_dir=trial_dir)
        conductor.output_dir = tmp_path
        conductor._run_agent_loop = MagicMock(side_effect=RuntimeError("lost"))
        with pytest.raises(RuntimeError):
            conductor.run(_spec(attempt=1), MagicMock())
        names = [name for name, _ in observer.call_log.calls]
        assert names == ["trial_started", "trial_finished"]

    def test_an_observer_without_the_hook_or_a_raising_one_never_reaches_the_caller(
        self, tmp_path: Path
    ) -> None:
        class _Legacy:  # an observer written before the amendment: no trial_persisted
            def trial_started(self, identity, **kwargs):
                pass

            def trial_finished(self, identity, **kwargs):
                pass

            def run_finished(self):
                return ExportReceipt()

        conductor = _conductor(_Legacy())
        conductor.output_dir = tmp_path
        trial_dir = tmp_path / "trials" / "T-1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "trajectory.yaml").write_text("task_id: T-1\n")
        conductor.trial_persisted(_spec())  # no AttributeError
        raising = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        raising.trial_persisted = MagicMock(side_effect=RuntimeError("receiver down"))
        conductor = _conductor(raising)
        conductor.output_dir = tmp_path
        conductor.trial_persisted(_spec())  # swallowed by safely

    def test_a_trial_that_raises_after_the_loop_closes_with_its_trajectory(self) -> None:
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        conductor = _conductor(observer)
        trajectory = _trajectory()
        conductor._run_agent_loop = MagicMock(return_value=(trajectory, MagicMock(), "sys"))
        conductor._grade = MagicMock(side_effect=ValueError("grader down"))
        with pytest.raises(ValueError):
            conductor.run(_spec(attempt=2), MagicMock())
        finished = observer.call_log.calls[-1][1]
        assert finished["trajectory"] is trajectory and finished["error"].startswith("ValueError")
        assert trajectory.attempt_id == 2


class TestFinishTracing:
    def _orchestrator(self, observer) -> Orchestrator:
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.logger = MagicMock()
        orchestrator._trial_observer = observer
        return orchestrator

    def test_receipt_is_written_and_the_observer_reset(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(
            InMemoryTrialObserver(
                receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
            )
        )
        orchestrator._finish_tracing(tmp_path)
        receipt = json.loads((tmp_path / TRACING_RECEIPT_FILE).read_text())
        assert (receipt["spans_exported"], receipt["exporter"], receipt["flushed"]) == (
            3,
            "fake",
            True,
        )
        assert isinstance(orchestrator._trial_observer, NullTrialObserver)
        orchestrator.logger.info.assert_called_once()

    def test_null_observer_writes_nothing(self, tmp_path: Path) -> None:
        orchestrator = self._orchestrator(NullTrialObserver())
        orchestrator._finish_tracing(tmp_path)
        assert not (tmp_path / TRACING_RECEIPT_FILE).exists()

    def test_a_raising_observer_is_reported_not_propagated(self, tmp_path: Path) -> None:
        observer = InMemoryTrialObserver(
            receipt=ExportReceipt(spans_queued=3, spans_exported=3, exporter="fake")
        )
        observer.run_finished = MagicMock(side_effect=RuntimeError("exporter gone"))
        orchestrator = self._orchestrator(observer)
        orchestrator._finish_tracing(tmp_path)
        orchestrator.logger.warning.assert_called()
