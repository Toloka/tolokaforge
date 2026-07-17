"""Lock the trial-body log-capture contract on ``ProvisioningTrialExecutor``.

After ``conductor.run`` returns and before teardown, the executor calls
``runtime_backend.capture_service_logs(handle, capture_worthy=...)`` where
``capture_worthy`` is true for an execution failure (``ERROR`` / ``TIMEOUT``)
or a completed-but-red grade (``COMPLETED`` with ``binary_pass is False``). A
completed trial that passes is not capture-worthy. When the backend returns a
non-empty byte map the executor emits the ``trial.service_logs_captured``
summary line and amends the trial's ``metrics.yaml`` with a top-level
``captured_service_logs`` mapping.

No Docker: an :class:`InMemoryRuntimeBackend` subclass returns a stub byte map
(gated on the ``capture_worthy`` flag, writing no ``.log`` files) and an
:class:`InMemoryConductor` drives the trajectory status and grade. The real
per-service ``.log`` capture is locked by the Docker integration test.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_trial_spec
from tolokaforge.core import trial_executor
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, GradeComponents, Metrics, Trajectory, TrialStatus
from tolokaforge.core.runtime import EnvHandle, InMemoryRuntimeBackend
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

pytestmark = pytest.mark.canonical

_BYTE_MAP = {"db": 128, "runner": 64}


def _monotonic_sequence(ticks: list[float]) -> Callable[[], float]:
    it = iter(ticks)
    return lambda: next(it)


class _StubCaptureBackend(InMemoryRuntimeBackend):
    """In-memory backend whose ``capture_service_logs`` returns a stub byte
    map, gated exactly like the real backend (``capture_worthy`` or
    on-success), and writes no ``.log`` files. Still records the
    ``(trial_id, capture_worthy)`` call."""

    def __init__(self, *, on_success: bool = False) -> None:
        super().__init__()
        self._on_success = on_success

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        self.call_log.capture_service_logs_calls.append((handle.trial_id, capture_worthy))
        if capture_worthy or self._on_success:
            return dict(_BYTE_MAP)
        return {}


def _factory_for(status: TrialStatus, *, binary_pass: bool | None) -> object:
    def _factory(task_id: str, trial_idx: int) -> Trajectory:
        now = datetime.now(UTC)
        grade = (
            None
            if binary_pass is None
            else Grade(
                binary_pass=binary_pass,
                score=1.0 if binary_pass else 0.0,
                components=GradeComponents(),
                reasons="synthetic",
            )
        )
        return Trajectory(
            task_id=task_id,
            trial_index=trial_idx,
            start_ts=now,
            end_ts=now,
            status=status,
            messages=[],
            metrics=Metrics(),
            grade=grade,
        )

    return _factory


def _write_metrics(output_root: Path, task_id: str, trial_idx: int) -> Path:
    metrics_path = output_root / "trials" / task_id / str(trial_idx) / "metrics.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(yaml.safe_dump({"cost_usd": 0.5}, sort_keys=False))
    return metrics_path


def _make_executor(
    backend: InMemoryRuntimeBackend, factory, output_root: Path
) -> ProvisioningTrialExecutor:
    return ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=InMemoryConductor(trajectory_factory=factory),
        logger=StructuredLogger("test-executor-log-capture"),
        log_capture=LogCaptureConfig(output_root=output_root, tail=200, on_success=False),
    )


def _summary_lines(logger: StructuredLogger) -> list[dict]:
    return [e for e in logger.logs if e["message"] == "trial.service_logs_captured"]


class TestErrorTrialCapture:
    def test_error_trajectory_captures_and_amends_metrics(self, tmp_path: Path) -> None:
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.ERROR, binary_pass=False)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert backend.call_log.capture_service_logs_calls == [("task-1:0", True)]

        summary = _summary_lines(executor.logger)
        assert len(summary) == 1
        assert summary[0]["context"]["services"] == _BYTE_MAP

        metrics = yaml.safe_load(metrics_path.read_text())
        assert metrics["captured_service_logs"] == _BYTE_MAP
        # Pre-existing keys survive the read-add-write amendment.
        assert metrics["cost_usd"] == 0.5

    def test_timeout_trajectory_is_also_a_failure(self, tmp_path: Path) -> None:
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.TIMEOUT, binary_pass=False)
        executor = _make_executor(backend, factory, tmp_path)
        _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert backend.call_log.capture_service_logs_calls == [("task-1:0", True)]
        assert len(_summary_lines(executor.logger)) == 1


class TestGradedFailIsCapture:
    def test_completed_binary_fail_captures_and_amends_metrics(self, tmp_path: Path) -> None:
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.COMPLETED, binary_pass=False)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert backend.call_log.capture_service_logs_calls == [("task-1:0", True)]

        summary = _summary_lines(executor.logger)
        assert len(summary) == 1
        assert summary[0]["context"]["services"] == _BYTE_MAP

        metrics = yaml.safe_load(metrics_path.read_text())
        assert metrics["captured_service_logs"] == _BYTE_MAP
        # Pre-existing keys survive the read-add-write amendment.
        assert metrics["cost_usd"] == 0.5


class TestPassingTrialIsNotCapture:
    def test_completed_binary_pass_does_not_capture(self, tmp_path: Path) -> None:
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.COMPLETED, binary_pass=True)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        # Capture is still consulted, but with capture_worthy=False the map is empty.
        assert backend.call_log.capture_service_logs_calls == [("task-1:0", False)]
        assert _summary_lines(executor.logger) == []

        metrics = yaml.safe_load(metrics_path.read_text())
        assert "captured_service_logs" not in metrics


class TestUngradedCompletedIsNotCapture:
    def test_completed_without_grade_does_not_capture(self, tmp_path: Path) -> None:
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.COMPLETED, binary_pass=None)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        assert backend.call_log.capture_service_logs_calls == [("task-1:0", False)]
        assert _summary_lines(executor.logger) == []

        metrics = yaml.safe_load(metrics_path.read_text())
        assert "captured_service_logs" not in metrics


class TestProvisioningDuration:
    def test_records_on_passing_trial(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(trial_executor.time, "monotonic", _monotonic_sequence([10.0, 11.5]))
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.COMPLETED, binary_pass=True)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        metrics = yaml.safe_load(metrics_path.read_text())
        assert metrics["provisioning_duration_s"] == 1.5
        assert isinstance(metrics["provisioning_duration_s"], float)
        # Pre-existing keys survive; a passing trial is not capture-worthy.
        assert metrics["cost_usd"] == 0.5
        assert "captured_service_logs" not in metrics

    def test_coexists_with_captured_service_logs_on_red_trial(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(trial_executor.time, "monotonic", _monotonic_sequence([100.0, 105.5]))
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.ERROR, binary_pass=False)
        executor = _make_executor(backend, factory, tmp_path)
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        metrics = yaml.safe_load(metrics_path.read_text())
        assert metrics["provisioning_duration_s"] == 5.5
        assert metrics["captured_service_logs"] == _BYTE_MAP
        assert metrics["cost_usd"] == 0.5

    def test_no_output_root_writes_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(trial_executor.time, "monotonic", _monotonic_sequence([1.0, 2.0]))
        backend = _StubCaptureBackend()
        factory = _factory_for(TrialStatus.COMPLETED, binary_pass=True)
        executor = ProvisioningTrialExecutor(
            runtime_backend=backend,
            conductor=InMemoryConductor(trajectory_factory=factory),
            logger=StructuredLogger("test-executor-log-capture"),
            log_capture=None,
        )
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        executor.execute(make_trial_spec(trial_id="task-1:0"), make_task_config(task_id="task-1"))

        metrics = yaml.safe_load(metrics_path.read_text())
        assert "provisioning_duration_s" not in metrics
