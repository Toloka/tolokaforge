"""Task-driven backend selection.

The orchestrator picks :class:`PerTrialRuntimeBackend` when any task
manifest requires per-trial materialisation, otherwise
:class:`SharedStackRuntimeBackend`. The legacy ``orchestrator.runtime``
override still wins but emits ``DeprecationWarning``. These tests pin
every branch of :meth:`Orchestrator._select_backend_from_tasks` and
:meth:`Orchestrator._construct_runtime_backend`.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    ResetSpec,
    RunConfig,
    ServiceSpec,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import (
    PerTrialRuntimeBackend,
    per_trial_runtime_backend_factory,
)
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    UnknownImplementationError,
)
from tolokaforge.core.shared_stack_runtime import (
    SharedStackRuntimeBackend,
    shared_runtime_backend_factory,
)
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _run_config(**orchestrator_overrides: Any) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            **orchestrator_overrides,
        ),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _task_stub(task_id: str) -> Any:
    task = MagicMock()
    task.task_id = task_id
    return task


def _make_orchestrator(
    tasks: list[Any],
    task_descs: dict[str, TaskDescription],
    run_config_kwargs: dict[str, Any] | None = None,
) -> Orchestrator:
    orch = Orchestrator(_run_config(**(run_config_kwargs or {})))
    orch.tasks = tasks
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: task_descs[tid]
    return orch


def _manifest_all_shared() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services={
            "db": ServiceSpec(isolation="shared"),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _manifest_with_reset() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services={
            "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _manifest_with_ephemeral() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services={
            "db": ServiceSpec(isolation="ephemeral"),
            "default": ServiceSpec(isolation="shared"),
        },
    )


class TestSelectBackendFromTasks:
    def test_all_shared_returns_shared(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared())
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "shared"

    def test_any_reset_returns_per_trial(self) -> None:
        tasks = [_task_stub("t1"), _task_stub("t2")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared()),
            "t2": make_task_description(task_id="t2", environment_manifest=_manifest_with_reset()),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_any_ephemeral_returns_per_trial(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(
                task_id="t1", environment_manifest=_manifest_with_ephemeral()
            )
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_no_manifest_defaults_to_shared(self) -> None:
        tasks = [_task_stub("legacy")]
        task_descs = {"legacy": make_task_description(task_id="legacy", environment_manifest=None)}
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "shared"

    def test_missing_adapter_raises(self) -> None:
        orch = Orchestrator(_run_config())
        orch.tasks = [_task_stub("t")]
        orch.adapter = None
        with pytest.raises(RuntimeError, match="adapter"):
            orch._select_backend_from_tasks()


class TestConstructRuntimeBackend:
    @pytest.fixture(autouse=True)
    def _stub_runtime_loader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolve backend names to the real built-in factories directly,
        bypassing entry-point discovery.

        Patches the loader at its orchestrator-module binding so these stay
        unit tests of dispatch wiring — the name the orchestrator selects and
        the factory it invokes — decoupled from installed package metadata.
        """
        factories = {
            "shared": shared_runtime_backend_factory,
            "per_trial": per_trial_runtime_backend_factory,
        }
        monkeypatch.setattr(
            "tolokaforge.core.orchestrator.load_runtime_backend",
            lambda name: factories[name],
        )

    def test_task_driven_all_shared_picks_shared(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared())
        }
        orch = _make_orchestrator(tasks, task_descs)
        backend = orch._construct_runtime_backend(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
        )
        assert isinstance(backend, SharedStackRuntimeBackend)

    def test_task_driven_reset_picks_per_trial(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_with_reset())
        }
        orch = _make_orchestrator(tasks, task_descs)
        backend = orch._construct_runtime_backend(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
        )
        assert isinstance(backend, PerTrialRuntimeBackend)

    def test_explicit_override_wins_and_warns(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared())
        }
        # Explicit per_trial override on a shared-labelled task fires the
        # deprecation warning at RunConfig construction time and picks
        # per-trial anyway.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orch = _make_orchestrator(tasks, task_descs, run_config_kwargs={"runtime": "per_trial"})
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "OrchestratorConfig.runtime is deprecated" in str(w.message)
            for w in caught
        )
        backend = orch._construct_runtime_backend(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
        )
        assert isinstance(backend, PerTrialRuntimeBackend)

    def test_shared_override_on_per_trial_task_still_picks_shared(self) -> None:
        # The override wins at construction; the isolation-compat guard
        # elsewhere refuses this configuration at run time.
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_with_reset())
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            orch = _make_orchestrator(tasks, task_descs, run_config_kwargs={"runtime": "shared"})
        backend = orch._construct_runtime_backend(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
        )
        assert isinstance(backend, SharedStackRuntimeBackend)


class TestUnknownRuntimeName:
    def test_unknown_override_raises_listing_known_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown ``orchestrator.runtime`` override propagates the loader's
        actionable error (listing the known names) out of run start rather than
        being swallowed."""
        known = ["in_memory", "per_trial", "shared"]

        def _raise(name: str):
            raise UnknownImplementationError(name, "tolokaforge.runtime_backends", known)

        monkeypatch.setattr("tolokaforge.core.orchestrator.load_runtime_backend", _raise)

        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared())
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            orch = _make_orchestrator(tasks, task_descs, run_config_kwargs={"runtime": "bogus"})

        with pytest.raises(UnknownImplementationError) as excinfo:
            orch._construct_runtime_backend(
                runner_address="sentinel:50051",
                env_manifest=None,
                run_id="test-run",
            )
        message = str(excinfo.value)
        assert "bogus" in message
        for name in known:
            assert name in message


class TestRunDisplayEventsPropagation:
    """The orchestrator's display-events sink must reach the runner client.

    ``SharedStackRuntimeBackend`` forwards its ``events`` sink to
    :class:`GrpcRunnerClient`, which is what publishes the runner's
    ``component_registered`` / ``component_status_changed`` /
    ``component_log_appended`` rows (ADR-0021). Nothing else asserts that
    chain end to end, so a break degrades silently to a null sink: no
    exception, no failing test, just an Engine Components panel that never
    shows the runner. PR #557 broke exactly this by dropping the ``events``
    field from ``RuntimeBackendBuildContext`` while the orchestrator kept
    passing the kwarg.
    """

    def test_orchestrator_events_reach_the_runner_client(self) -> None:
        tasks = [_task_stub("t1")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=_manifest_all_shared())
        }
        orch = _make_orchestrator(tasks, task_descs)
        sentinel = MagicMock()
        orch._events = sentinel

        backend = orch._construct_runtime_backend(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
        )

        assert isinstance(backend, SharedStackRuntimeBackend)
        assert backend._events is sentinel
        assert backend.runner_client._events is sentinel

    def test_build_context_defaults_events_to_a_null_sink(self) -> None:
        """A factory that omits ``events`` still gets a usable no-op sink."""
        ctx = RuntimeBackendBuildContext(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
            seeds={},
            log_capture=None,
        )

        ctx.events.component_status_changed(component_id="probe", status="healthy")


class TestMountDockerSocketPropagation:
    """The compose-variant docker-socket trigger must reach the backend that
    materialises the runner. The flag lives on the build context and each
    factory threads it onto the backend; the materialiser then injects
    ``/var/run/docker.sock`` into the runner service. A drop here silently
    reverts to no socket, so every compose-variant ``docker exec`` fails
    with a no-daemon error surfaced as ``bash session failed to
    become ready``."""

    def _ctx(self, *, env_manifest: EnvironmentManifest | None, mount: bool):
        return RuntimeBackendBuildContext(
            runner_address="sentinel:50051",
            env_manifest=env_manifest,
            run_id="test-run",
            seeds={},
            log_capture=None,
            mount_docker_socket=mount,
        )

    def test_context_defaults_to_false(self) -> None:
        ctx = RuntimeBackendBuildContext(
            runner_address="sentinel:50051",
            env_manifest=None,
            run_id="test-run",
            seeds={},
            log_capture=None,
        )
        assert ctx.mount_docker_socket is False

    def test_per_trial_factory_threads_flag(self) -> None:
        backend = per_trial_runtime_backend_factory(self._ctx(env_manifest=None, mount=True))
        assert backend.mount_docker_socket is True

    def test_shared_factory_threads_flag(self) -> None:
        backend = shared_runtime_backend_factory(
            self._ctx(env_manifest=_manifest_all_shared(), mount=True)
        )
        assert backend._mount_docker_socket is True
