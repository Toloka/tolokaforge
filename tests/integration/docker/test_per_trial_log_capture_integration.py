"""Integration tests for per-service log capture on a real Docker stack.

Real docker-daemon coverage for :class:`PerTrialRuntimeBackend`'s two
capture paths, both driven over the public two-service fixture
(``postgres:16`` + ``nginx:alpine``):

* **Provision-stage capture** — a forced ``reset_recipe`` failure (a
  service labelled ``reset`` whose seed is absent from the backend
  registry). The stack is healthy at that point, so every declared service
  has real logs to capture *before* teardown; asserts the failure surfaces
  as :class:`ProvisionError` and that per-service ``.log`` files plus the
  ``_capture.yaml`` manifest land in the trial ``services/`` dir.
* **Executor trial-body graded-fail capture** — a successful provision
  followed by a completed-but-red trial (``status COMPLETED``,
  ``grade.binary_pass False``) driven through
  :meth:`ProvisioningTrialExecutor.execute` with an
  :class:`InMemoryConductor`. The executor computes the trial
  capture-worthy and the real backend writes per-service ``.log`` files
  before teardown; asserts the files land on disk and the amended
  ``metrics.yaml.captured_service_logs`` byte counts match them.

Only the trial outcome is set deterministically — the Docker stack, the
compose-logs subprocess, and the capture are all real. No LLM keys are
required (the in-memory conductor runs no agent loop).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_task_description
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor
from tolokaforge.runner.models import ResetSpec, ServiceSpec

pytestmark = [pytest.mark.integration, pytest.mark.docker]


_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "canonical"
    / "fixtures"
    / "environment_manifest"
    / "lifecycle_public.yaml"
)


def _make_trial_spec(trial_id: str, *, manifest: EnvironmentManifest | None = None) -> TrialSpec:
    # 'db' is labelled reset with a seed the backend registry will not carry,
    # so provision brings the healthy stack up and then fails in the
    # reset_recipe stage — the branch that captures logs before teardown.
    if manifest is None:
        manifest = EnvironmentManifest(
            compose_file=_FIXTURE,
            services={"db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="absent-seed"))},
        )
    return TrialSpec(
        trial_id=trial_id,
        run_id="log-capture-integration",
        task=make_task_description(
            task_id="task-1",
            name="probe",
            category="general",
            description="per-service log capture integration test",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestProvisionFailureLogCapture:
    def test_reset_recipe_failure_captures_service_logs_before_teardown(
        self, tmp_path: Path
    ) -> None:
        backend = PerTrialRuntimeBackend(
            log_capture=LogCaptureConfig(output_root=tmp_path, tail=200, on_success=False)
        )
        spec = _make_trial_spec(trial_id="task-1:0")

        with pytest.raises(ProvisionError) as exc:
            backend.provision(spec)
        assert exc.value.stage == "reset_recipe"

        services_dir = tmp_path / "trials" / "task-1" / "0" / "services"
        assert services_dir.is_dir()

        # Both declared services were healthy, so both produced logs.
        for service in ("db", "default"):
            log_file = services_dir / f"{service}.log"
            assert log_file.is_file(), f"missing {service}.log"
            assert log_file.stat().st_size > 0, f"empty {service}.log"

        manifest_path = services_dir / "_capture.yaml"
        assert manifest_path.is_file()
        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["tail"] == 200
        assert manifest["capture_reason"] == "provision_error"

        # Recorded byte counts match the files actually written.
        for service, entry in manifest["services"].items():
            assert entry["bytes"] == (services_dir / f"{service}.log").stat().st_size


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestSuccessPathCapture:
    """After a *successful* provision, ``capture_service_logs(handle,
    capture_worthy=False)`` writes per-service ``.log`` files only when the
    on-success debug policy is set — the default keeps the output dir bounded."""

    @staticmethod
    def _success_spec(trial_id: str) -> TrialSpec:
        # No reset service: the healthy stack comes up and provision succeeds.
        return _make_trial_spec(trial_id, manifest=EnvironmentManifest(compose_file=_FIXTURE))

    def test_on_success_false_captures_nothing(self, tmp_path: Path) -> None:
        backend = PerTrialRuntimeBackend(
            log_capture=LogCaptureConfig(output_root=tmp_path, tail=200, on_success=False)
        )
        handle = backend.provision(self._success_spec("task-1:0"))
        try:
            assert backend.capture_service_logs(handle, capture_worthy=False) == {}
        finally:
            backend.teardown(handle)

        assert not (tmp_path / "trials" / "task-1" / "0" / "services").exists()

    def test_on_success_true_captures_logs(self, tmp_path: Path) -> None:
        backend = PerTrialRuntimeBackend(
            log_capture=LogCaptureConfig(output_root=tmp_path, tail=200, on_success=True)
        )
        handle = backend.provision(self._success_spec("task-1:0"))
        try:
            captured = backend.capture_service_logs(handle, capture_worthy=False)
        finally:
            backend.teardown(handle)

        services_dir = tmp_path / "trials" / "task-1" / "0" / "services"
        assert services_dir.is_dir()
        for service in captured:
            log_file = services_dir / f"{service}.log"
            assert log_file.is_file()
            assert log_file.stat().st_size > 0


def _completed_red_factory(task_id: str, trial_idx: int) -> Trajectory:
    """Trajectory factory for a completed-but-red trial (the grade-fail trigger)."""
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_idx,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        metrics=Metrics(),
        grade=Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(),
            reasons="synthetic-red",
        ),
    )


def _write_metrics(output_root: Path, task_id: str, trial_idx: int) -> Path:
    metrics_path = output_root / "trials" / task_id / str(trial_idx) / "metrics.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(yaml.safe_dump({"cost_usd": 0.5}, sort_keys=False))
    return metrics_path


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestGradedFailLogCapture:
    """A completed-but-red trial on the per-trial backend captures each
    declared service's logs to ``services/<name>.log`` and amends the trial's
    ``metrics.yaml`` before teardown. The stack, the compose-logs subprocess,
    and the capture are all real — only the trial outcome is deterministic."""

    def test_completed_but_red_grade_captures_service_logs_before_teardown(
        self, tmp_path: Path
    ) -> None:
        log_capture = LogCaptureConfig(output_root=tmp_path, tail=200, on_success=False)
        backend = PerTrialRuntimeBackend(log_capture=log_capture)
        # No reset service: the healthy stack comes up and provision succeeds,
        # so the completed-but-red grade is the sole capture trigger.
        spec = _make_trial_spec(
            trial_id="task-1:0", manifest=EnvironmentManifest(compose_file=_FIXTURE)
        )
        executor = ProvisioningTrialExecutor(
            runtime_backend=backend,
            conductor=InMemoryConductor(trajectory_factory=_completed_red_factory),
            logger=StructuredLogger("test-graded-fail-log-capture"),
            log_capture=log_capture,
        )
        metrics_path = _write_metrics(tmp_path, "task-1", 0)

        # execute() owns provision + teardown (teardown in its own finally).
        result = executor.execute(spec, make_task_config(task_id="task-1"))
        assert result.trajectory.status == TrialStatus.COMPLETED
        assert result.trajectory.grade is not None
        assert result.trajectory.grade.binary_pass is False

        services_dir = tmp_path / "trials" / "task-1" / "0" / "services"
        assert services_dir.is_dir()

        metrics = yaml.safe_load(metrics_path.read_text())
        captured = metrics["captured_service_logs"]
        assert set(captured) == {"db", "default"}

        # Both declared services were healthy, so both produced non-empty logs,
        # and the amended byte counts match the files actually written.
        for service in ("db", "default"):
            log_file = services_dir / f"{service}.log"
            assert log_file.is_file(), f"missing {service}.log"
            size = log_file.stat().st_size
            assert size > 0, f"empty {service}.log"
            assert captured[service] == size

        # Pre-existing metrics keys survive the read-add-write amendment.
        assert metrics["cost_usd"] == 0.5
