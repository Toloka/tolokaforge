"""``compute.grpc_max_message_mb`` reaches both ends of every runner channel.

The engine's client channels take the value directly; every runner container
the engine starts — the built-in stack's, or one materialised from a
task-declared compose file — receives it as ``TOLOKAFORGE_GRPC_MAX_MESSAGE_MB``.
A task compose that declares the variable itself keeps its own value. A drop
anywhere on the path silently reverts one end to a different bound, which
only shows up when a large world is refused, so each hop is pinned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._docker_compose_stubs import InertDockerCompose
from tolokaforge.core.compose_materialisation import inject_grpc_limit_env_into_runner
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.docker_compose_materialiser import (
    DockerComposeMaterialiser,
    _DockerComposeStackHandle,
)
from tolokaforge.core.models import (
    ComputeConfig,
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    ServiceSpec,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import (
    PerTrialRuntimeBackend,
    per_trial_runtime_backend_factory,
)
from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext, TrialGraderContext
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.shared_stack_runtime import (
    SharedStackRuntimeBackend,
    shared_runtime_backend_factory,
)
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.core.trial_grader import (
    grader_rpc_trial_grader_factory,
    runner_rpc_trial_grader_factory,
)
from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.stacks.full import full_stack
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB, MAX_MESSAGE_ENV_VAR

pytestmark = pytest.mark.unit

_CONFIGURED_MB = 120
_RUNNER_SERVICE = "default"
_TWO_SERVICE_COMPOSE = {
    "services": {
        "db": {"image": "postgres:16"},
        _RUNNER_SERVICE: {"image": "tolokaforge/runner:0.5.0", "depends_on": ["db"]},
    }
}


def _write_compose(tmp_path: Path, runner_environment: Any = None) -> Path:
    doc = yaml.safe_load(yaml.safe_dump(_TWO_SERVICE_COMPOSE))
    if runner_environment is not None:
        doc["services"][_RUNNER_SERVICE]["environment"] = runner_environment
    tmp_path.mkdir(parents=True, exist_ok=True)
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(yaml.safe_dump(doc, sort_keys=False))
    return compose_file


def _service_env(compose_file: Path, service: str) -> Any:
    return yaml.safe_load(compose_file.read_text())["services"][service].get("environment")


# ---------------------------------------------------------------------------
# Built-in stack: the runner container's env
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_built_in_runner_serves_with_the_default_bound(factory) -> None:
    assert factory().services["runner"].environment[MAX_MESSAGE_ENV_VAR] == str(
        DEFAULT_MAX_MESSAGE_MB
    )


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_built_in_runner_serves_with_the_configured_bound(factory) -> None:
    stack = factory(grpc_max_message_mb=_CONFIGURED_MB)

    carriers = {
        name: service.environment[MAX_MESSAGE_ENV_VAR]
        for name, service in stack.services.items()
        if MAX_MESSAGE_ENV_VAR in service.environment
    }
    assert carriers == {"runner": str(_CONFIGURED_MB)}


def test_built_in_stack_refuses_an_out_of_range_bound() -> None:
    with pytest.raises(ValueError):
        core_stack(grpc_max_message_mb=2048)


# ---------------------------------------------------------------------------
# Task-declared compose: the injection primitive
# ---------------------------------------------------------------------------


class TestInjectGrpcLimitEnvIntoRunner:
    def test_mapping_environment_gains_the_variable(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path, {"DB_SERVICE_URL": "http://db:8000"})

        inject_grpc_limit_env_into_runner(compose_file, _RUNNER_SERVICE, _CONFIGURED_MB)

        env = _service_env(compose_file, _RUNNER_SERVICE)
        assert env == {"DB_SERVICE_URL": "http://db:8000", MAX_MESSAGE_ENV_VAR: "120"}
        assert _service_env(compose_file, "db") is None

    def test_list_environment_gains_the_variable(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path, ["DB_SERVICE_URL=http://db:8000"])

        inject_grpc_limit_env_into_runner(compose_file, _RUNNER_SERVICE, _CONFIGURED_MB)

        env = _service_env(compose_file, _RUNNER_SERVICE)
        assert "DB_SERVICE_URL=http://db:8000" in env
        assert f"{MAX_MESSAGE_ENV_VAR}=120" in env

    @pytest.mark.parametrize(
        "declared",
        [{MAX_MESSAGE_ENV_VAR: "300"}, [f"{MAX_MESSAGE_ENV_VAR}=300"], [MAX_MESSAGE_ENV_VAR]],
        ids=["mapping", "list-entry", "pass-through"],
    )
    def test_a_compose_declaring_its_own_value_keeps_it(self, tmp_path: Path, declared) -> None:
        compose_file = _write_compose(tmp_path, declared)
        before = compose_file.read_text()

        inject_grpc_limit_env_into_runner(compose_file, _RUNNER_SERVICE, _CONFIGURED_MB)

        assert compose_file.read_text() == before

    def test_an_undeclared_runner_service_is_refused(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)

        with pytest.raises(ValueError, match="'worker'"):
            inject_grpc_limit_env_into_runner(compose_file, "worker", _CONFIGURED_MB)


# ---------------------------------------------------------------------------
# Task-declared compose: the configured value through backend → composer →
# materialiser → runner env, and into the client the composer builds
# ---------------------------------------------------------------------------


class _RecordingRunnerClient:
    def __init__(self, runner_address: str, max_message_mb: int) -> None:
        self.runner_address = runner_address
        self.max_message_mb = max_message_mb

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval

    def close(self) -> None:
        return None


def _env_manifest_backend(compose_file: Path) -> SharedStackRuntimeBackend:
    manifest = EnvironmentManifest(
        compose_file=compose_file,
        runner_service=_RUNNER_SERVICE,
        services={
            "db": ServiceSpec(isolation="shared"),
            _RUNNER_SERVICE: ServiceSpec(isolation="shared"),
        },
    )
    _synthesise_composition_plan(manifest, {})
    composer = DefaultSubstrateComposer(
        materialiser=DockerComposeMaterialiser(
            docker_compose_factory=lambda **kwargs: InertDockerCompose(**kwargs)
        ),
        runner_client_factory=lambda address, _events, max_mb: _RecordingRunnerClient(
            address, max_mb
        ),
    )
    return SharedStackRuntimeBackend(
        env_manifest=manifest, composer=composer, grpc_max_message_mb=_CONFIGURED_MB
    )


def _materialised_runner_env(backend: SharedStackRuntimeBackend) -> Any:
    assert backend._run_substrate is not None
    handle = backend._run_substrate.run_stack_handles[0]
    assert isinstance(handle, _DockerComposeStackHandle)
    return _service_env(handle.temp_dir / "compose.yaml", _RUNNER_SERVICE)


class TestTaskDeclaredRunner:
    def test_runner_container_and_client_share_the_configured_bound(self, tmp_path: Path) -> None:
        backend = _env_manifest_backend(_write_compose(tmp_path))
        backend.connect()
        try:
            env = _materialised_runner_env(backend)
            client = backend._run_substrate.runner_client  # type: ignore[union-attr]
        finally:
            backend.close()

        assert env[MAX_MESSAGE_ENV_VAR] == str(_CONFIGURED_MB)
        assert isinstance(client, _RecordingRunnerClient)
        assert client.max_message_mb == _CONFIGURED_MB

    def test_runner_declaring_its_own_value_keeps_it(self, tmp_path: Path) -> None:
        backend = _env_manifest_backend(_write_compose(tmp_path, {MAX_MESSAGE_ENV_VAR: "300"}))
        backend.connect()
        try:
            env = _materialised_runner_env(backend)
        finally:
            backend.close()

        assert env[MAX_MESSAGE_ENV_VAR] == "300"


# ---------------------------------------------------------------------------
# Factories and the orchestrator
# ---------------------------------------------------------------------------


def _build_ctx(**overrides: Any) -> RuntimeBackendBuildContext:
    return RuntimeBackendBuildContext(
        runner_address="sentinel:50051",
        env_manifest=None,
        run_id="run",
        seeds={},
        log_capture=None,
        **overrides,
    )


class TestBackendFactories:
    def test_build_context_defaults_to_the_default_bound(self) -> None:
        assert _build_ctx().grpc_max_message_mb == DEFAULT_MAX_MESSAGE_MB

    def test_shared_factory_bounds_the_built_in_runner_client(self) -> None:
        backend = shared_runtime_backend_factory(_build_ctx(grpc_max_message_mb=_CONFIGURED_MB))

        assert backend.runner_client is not None
        assert backend.runner_client.max_message_mb == _CONFIGURED_MB

    def test_per_trial_factory_threads_the_bound_to_trial_runners(self) -> None:
        backend = per_trial_runtime_backend_factory(_build_ctx(grpc_max_message_mb=_CONFIGURED_MB))

        assert isinstance(backend, PerTrialRuntimeBackend)
        run_sub = backend._delegate._run_substrate_or_empty()
        assert run_sub.grpc_max_message_mb == _CONFIGURED_MB


class TestGraderFactories:
    def test_runner_rpc_grader_bounds_its_runner_client(self) -> None:
        grader = runner_rpc_trial_grader_factory(
            TrialGraderContext(
                runner_address="runner:1", logger=MagicMock(), grpc_max_message_mb=_CONFIGURED_MB
            )
        )

        assert grader.runner_client.max_message_mb == _CONFIGURED_MB

    def test_grader_rpc_grader_bounds_its_grader_client(self) -> None:
        grader = grader_rpc_trial_grader_factory(
            TrialGraderContext(
                runner_address="runner:1",
                grader_address="grader:2",
                logger=MagicMock(),
                grpc_max_message_mb=_CONFIGURED_MB,
            )
        )

        assert grader.grader_client.max_message_mb == _CONFIGURED_MB


def _orchestrator(compute: ComputeConfig | None) -> Orchestrator:
    orch = Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/grpc-limit-plumbing"),
            compute=compute,
        )
    )
    orch.tasks = []
    orch.adapter = MagicMock()
    return orch


class TestOrchestrator:
    def test_configured_bound_reaches_the_runner_client(self) -> None:
        orch = _orchestrator(ComputeConfig(grpc_max_message_mb=_CONFIGURED_MB))

        backend = orch._construct_runtime_backend(runner_address="sentinel:50051")

        assert isinstance(backend, SharedStackRuntimeBackend)
        assert backend.runner_client is not None
        assert backend.runner_client.max_message_mb == _CONFIGURED_MB

    def test_absent_compute_block_uses_the_default(self) -> None:
        orch = _orchestrator(None)

        assert orch._grpc_max_message_mb() == DEFAULT_MAX_MESSAGE_MB
