"""``compute.state_read_timeout_s`` reaches every runner container the engine starts.

The runner reads the bound from ``TOLOKAFORGE_STATE_READ_TIMEOUT_S``: the built-in
stack sets it on its runner, and a runner materialised from a task-declared compose
file gets it injected unless the compose file declares the variable itself. A drop
anywhere on the path silently reverts the runner to the default bound, which only
shows up when a slow state holder fails a trial, so each hop is pinned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._docker_compose_stubs import InertDockerCompose
from tolokaforge.core.compose_materialisation import inject_runner_limits_env_into_runner
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
from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.shared_stack_runtime import (
    SharedStackRuntimeBackend,
    shared_runtime_backend_factory,
)
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.stacks.full import full_stack
from tolokaforge.runner.grpc_limits import DEFAULT_MAX_MESSAGE_MB, MAX_MESSAGE_ENV_VAR
from tolokaforge.runner.state_read_limits import (
    DEFAULT_STATE_READ_TIMEOUT_S,
    STATE_READ_TIMEOUT_ENV_VAR,
)

pytestmark = pytest.mark.unit

_CONFIGURED_S = 180.0
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
def test_built_in_runner_reads_with_the_default_bound(factory) -> None:
    env = factory().services["runner"].environment

    assert float(env[STATE_READ_TIMEOUT_ENV_VAR]) == DEFAULT_STATE_READ_TIMEOUT_S


@pytest.mark.parametrize("factory", [core_stack, full_stack], ids=["core_stack", "full_stack"])
def test_built_in_runner_reads_with_the_configured_bound(factory) -> None:
    stack = factory(state_read_timeout_s=_CONFIGURED_S)

    carriers = {
        name: float(service.environment[STATE_READ_TIMEOUT_ENV_VAR])
        for name, service in stack.services.items()
        if STATE_READ_TIMEOUT_ENV_VAR in service.environment
    }
    assert carriers == {"runner": _CONFIGURED_S}


def test_a_fractional_bound_reaches_the_runner_exactly() -> None:
    env = core_stack(state_read_timeout_s=2.5).services["runner"].environment

    assert float(env[STATE_READ_TIMEOUT_ENV_VAR]) == 2.5


def test_built_in_stack_refuses_a_non_positive_bound() -> None:
    with pytest.raises(ValueError):
        core_stack(state_read_timeout_s=0)


# ---------------------------------------------------------------------------
# Task-declared compose: the injection primitive
# ---------------------------------------------------------------------------


def _inject(compose_file: Path, runner_service: str, state_read_timeout_s: float) -> None:
    inject_runner_limits_env_into_runner(
        compose_file,
        runner_service,
        grpc_max_message_mb=DEFAULT_MAX_MESSAGE_MB,
        state_read_timeout_s=state_read_timeout_s,
    )


class TestInjectRunnerLimitsEnvIntoRunner:
    def test_mapping_environment_gains_the_variable(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path, {"DB_SERVICE_URL": "http://db:8000"})

        _inject(compose_file, _RUNNER_SERVICE, _CONFIGURED_S)

        env = _service_env(compose_file, _RUNNER_SERVICE)
        assert env == {
            "DB_SERVICE_URL": "http://db:8000",
            MAX_MESSAGE_ENV_VAR: str(DEFAULT_MAX_MESSAGE_MB),
            STATE_READ_TIMEOUT_ENV_VAR: "180.0",
        }
        assert _service_env(compose_file, "db") is None

    def test_list_environment_gains_the_variable(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path, ["DB_SERVICE_URL=http://db:8000"])

        _inject(compose_file, _RUNNER_SERVICE, _CONFIGURED_S)

        env = _service_env(compose_file, _RUNNER_SERVICE)
        assert "DB_SERVICE_URL=http://db:8000" in env
        assert f"{STATE_READ_TIMEOUT_ENV_VAR}=180.0" in env

    @pytest.mark.parametrize(
        "declared",
        [
            {STATE_READ_TIMEOUT_ENV_VAR: "600"},
            [f"{STATE_READ_TIMEOUT_ENV_VAR}=600"],
            [STATE_READ_TIMEOUT_ENV_VAR],
        ],
        ids=["mapping", "list-entry", "pass-through"],
    )
    def test_a_compose_declaring_its_own_value_keeps_it(self, tmp_path: Path, declared) -> None:
        compose_file = _write_compose(tmp_path, declared)

        _inject(compose_file, _RUNNER_SERVICE, _CONFIGURED_S)

        env = _service_env(compose_file, _RUNNER_SERVICE)
        if isinstance(declared, dict):
            assert env[STATE_READ_TIMEOUT_ENV_VAR] == "600"
        else:
            assert [e for e in env if e.split("=", 1)[0] == STATE_READ_TIMEOUT_ENV_VAR] == declared

    def test_a_compose_declaring_both_variables_is_left_untouched(self, tmp_path: Path) -> None:
        compose_file = _write_compose(
            tmp_path, {STATE_READ_TIMEOUT_ENV_VAR: "600", MAX_MESSAGE_ENV_VAR: "300"}
        )
        before = compose_file.read_text()

        _inject(compose_file, _RUNNER_SERVICE, _CONFIGURED_S)

        assert compose_file.read_text() == before

    def test_an_undeclared_runner_service_is_refused(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)

        with pytest.raises(ValueError, match="'worker'"):
            _inject(compose_file, "worker", _CONFIGURED_S)

    def test_a_non_positive_bound_is_refused(self, tmp_path: Path) -> None:
        compose_file = _write_compose(tmp_path)

        with pytest.raises(ValueError):
            _inject(compose_file, _RUNNER_SERVICE, -1.0)


# ---------------------------------------------------------------------------
# Task-declared compose: the configured value through backend → composer →
# materialiser → runner env
# ---------------------------------------------------------------------------


class _InertRunnerClient:
    def __init__(self, runner_address: str) -> None:
        self.runner_address = runner_address

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
        runner_client_factory=lambda address, _events, _max_mb: _InertRunnerClient(address),
    )
    return SharedStackRuntimeBackend(
        env_manifest=manifest, composer=composer, state_read_timeout_s=_CONFIGURED_S
    )


def _materialised_runner_env(backend: SharedStackRuntimeBackend) -> Any:
    assert backend._run_substrate is not None
    handle = backend._run_substrate.run_stack_handles[0]
    assert isinstance(handle, _DockerComposeStackHandle)
    return _service_env(handle.temp_dir / "compose.yaml", _RUNNER_SERVICE)


class TestTaskDeclaredRunner:
    def test_runner_container_reads_with_the_configured_bound(self, tmp_path: Path) -> None:
        backend = _env_manifest_backend(_write_compose(tmp_path))
        backend.connect()
        try:
            env = _materialised_runner_env(backend)
            run_sub = backend._run_substrate
        finally:
            backend.close()

        assert float(env[STATE_READ_TIMEOUT_ENV_VAR]) == _CONFIGURED_S
        assert run_sub is not None and run_sub.state_read_timeout_s == _CONFIGURED_S

    def test_runner_declaring_its_own_value_keeps_it(self, tmp_path: Path) -> None:
        backend = _env_manifest_backend(
            _write_compose(tmp_path, {STATE_READ_TIMEOUT_ENV_VAR: "600"})
        )
        backend.connect()
        try:
            env = _materialised_runner_env(backend)
        finally:
            backend.close()

        assert env[STATE_READ_TIMEOUT_ENV_VAR] == "600"


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
        assert _build_ctx().state_read_timeout_s == DEFAULT_STATE_READ_TIMEOUT_S

    def test_shared_factory_threads_the_bound_to_its_runners(self) -> None:
        backend = shared_runtime_backend_factory(_build_ctx(state_read_timeout_s=_CONFIGURED_S))

        assert isinstance(backend, SharedStackRuntimeBackend)
        assert backend._run_substrate_or_empty().state_read_timeout_s == _CONFIGURED_S

    def test_per_trial_factory_threads_the_bound_to_trial_runners(self) -> None:
        backend = per_trial_runtime_backend_factory(_build_ctx(state_read_timeout_s=_CONFIGURED_S))

        assert isinstance(backend, PerTrialRuntimeBackend)
        run_sub = backend._delegate._run_substrate_or_empty()
        assert run_sub.state_read_timeout_s == _CONFIGURED_S


def _orchestrator(compute: ComputeConfig | None) -> Orchestrator:
    orch = Orchestrator(
        RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/state-read-timeout-plumbing"),
            compute=compute,
        )
    )
    orch.tasks = []
    orch.adapter = MagicMock()
    return orch


class TestOrchestrator:
    def test_configured_bound_reaches_the_runtime_backend(self) -> None:
        orch = _orchestrator(ComputeConfig(state_read_timeout_s=_CONFIGURED_S))

        backend = orch._construct_runtime_backend(runner_address="sentinel:50051")

        assert isinstance(backend, SharedStackRuntimeBackend)
        assert backend._run_substrate_or_empty().state_read_timeout_s == _CONFIGURED_S

    def test_absent_compute_block_uses_the_default(self) -> None:
        orch = _orchestrator(None)

        assert orch._state_read_timeout_s() == DEFAULT_STATE_READ_TIMEOUT_S

