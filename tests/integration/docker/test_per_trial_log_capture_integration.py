"""Integration test for per-service log capture on a provision failure.

Real docker-daemon coverage for :class:`PerTrialRuntimeBackend`'s
provision-stage capture path. Brings up the public two-service fixture
(``postgres:16`` + ``nginx:alpine``), then forces a ``reset_recipe``
failure (a service labelled ``reset`` whose seed is absent from the
backend registry). The stack is healthy at that point, so every declared
service has real logs to capture *before* teardown.

Asserts the provision failure surfaces as :class:`ProvisionError` and that
per-service ``.log`` files plus the ``_capture.yaml`` manifest land in the
trial ``services/`` dir with byte counts that match the files on disk.

No LLM keys required — the failure happens before the agent loop. Mirrors
the harness in ``test_per_trial_runtime_backend_integration.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.canonical._factories import make_task_description
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
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
    failed=False)`` writes per-service ``.log`` files only when the on-success
    debug policy is set — the default keeps the output dir bounded."""

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
            assert backend.capture_service_logs(handle, failed=False) == {}
        finally:
            backend.teardown(handle)

        assert not (tmp_path / "trials" / "task-1" / "0" / "services").exists()

    def test_on_success_true_captures_logs(self, tmp_path: Path) -> None:
        backend = PerTrialRuntimeBackend(
            log_capture=LogCaptureConfig(output_root=tmp_path, tail=200, on_success=True)
        )
        handle = backend.provision(self._success_spec("task-1:0"))
        try:
            captured = backend.capture_service_logs(handle, failed=False)
        finally:
            backend.teardown(handle)

        services_dir = tmp_path / "trials" / "task-1" / "0" / "services"
        assert services_dir.is_dir()
        for service in captured:
            log_file = services_dir / f"{service}.log"
            assert log_file.is_file()
            assert log_file.stat().st_size > 0
