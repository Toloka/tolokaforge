"""Integration tests for :class:`LocalRuntimeBackend`.

Real docker-daemon coverage: spins up a two-service compose stack
(``postgres:16`` + ``nginx:alpine`` — both public images), verifies
provision → endpoints → teardown, and asserts per-trial isolation
across concurrent instances.

The fixture stops short of the runner RPC surface: nginx does not
speak gRPC, so ``register_trial`` / ``execute_tool`` / etc. are not
exercised here. That coverage arrives in the validation-gate PR
against a real workload; see the LocalRuntimeBackend Jira sub-task.
Unit-level RPC coverage lives in
``tests/canonical/test_local_runtime_backend.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_task_description
from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.local_runtime import LocalRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec

pytestmark = [pytest.mark.integration, pytest.mark.docker]


_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "canonical"
    / "fixtures"
    / "environment_manifest"
    / "lifecycle_public.yaml"
)


def _make_trial_spec(trial_id: str) -> TrialSpec:
    manifest = EnvironmentManifest(compose_file=_FIXTURE)
    return TrialSpec(
        trial_id=trial_id,
        run_id="local-runtime-integration",
        task=make_task_description(
            task_id="task-1",
            name="probe",
            category="general",
            description="LocalRuntimeBackend integration test",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestLocalRuntimeBackendLifecycle:
    """End-to-end lifecycle against a real Docker daemon.

    Uses the public-images fixture. Each test provisions, resolves
    endpoints, and tears down; the compose stack lives for the duration
    of the test only.
    """

    def test_provision_endpoints_teardown_cycle(self) -> None:
        backend = LocalRuntimeBackend()
        spec = _make_trial_spec(trial_id="lifecycle:0")
        handle = backend.provision(spec)
        try:
            assert isinstance(handle, _LocalEnvHandle)
            assert handle.trial_id == "lifecycle:0"
            assert handle.compose is not None
            assert handle.temp_dir.exists()

            endpoints = backend.endpoints(handle)
            assert endpoints.runner_url.startswith("http://")
            assert endpoints.db_url.startswith("http://")
            # rag_url is None — fixture has no rag service.
            assert endpoints.rag_url is None
        finally:
            backend.teardown(handle)

        # Post-teardown: temp dir removed, cache empty.
        assert not handle.temp_dir.exists()
        assert handle.trial_id not in backend._clients

    def test_per_trial_isolation_across_concurrent_instances(self) -> None:
        """Two concurrent trials get different compose projects and
        resolve to different host-side ports for the same container
        port. Proves per-trial isolation."""
        backend = LocalRuntimeBackend()
        spec_a = _make_trial_spec(trial_id="lifecycle:a")
        spec_b = _make_trial_spec(trial_id="lifecycle:b")
        handle_a = backend.provision(spec_a)
        try:
            handle_b = backend.provision(spec_b)
            try:
                ep_a = backend.endpoints(handle_a)
                ep_b = backend.endpoints(handle_b)
                # Same container ports, different host-side host:port pairs.
                assert ep_a.runner_url != ep_b.runner_url
                assert ep_a.db_url != ep_b.db_url
                assert isinstance(handle_a, _LocalEnvHandle)
                assert isinstance(handle_b, _LocalEnvHandle)
                assert handle_a.temp_dir != handle_b.temp_dir
            finally:
                backend.teardown(handle_b)
        finally:
            backend.teardown(handle_a)

    def test_teardown_removes_containers_and_networks(self) -> None:
        """After teardown, the compose stack's containers and network
        are gone. Verified by observing that a subsequent provision
        with the same trial_id succeeds without collision."""
        backend = LocalRuntimeBackend()
        spec = _make_trial_spec(trial_id="lifecycle:cleanup")
        handle_first = backend.provision(spec)
        backend.teardown(handle_first)

        # Same trial_id, fresh temp dir, must succeed without any
        # docker-side leftover interfering.
        handle_second = backend.provision(spec)
        try:
            assert isinstance(handle_second, _LocalEnvHandle)
            assert handle_second.temp_dir != handle_first.temp_dir
        finally:
            backend.teardown(handle_second)
