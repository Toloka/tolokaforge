"""Unit tests for :meth:`Orchestrator._verify_isolation_compatibility`.

Per-service dispatcher admission (ADR-0044 §5, INV-2): every service's
``isolation`` label must have a registered dispatcher on the backend's
composer.

* Every label in the closed ``ServiceIsolation`` vocab (``shared`` /
  ``reset`` / ``ephemeral``) is admitted by the built-in dispatcher
  registry — the refusal fires only when a composer is constructed
  with a partial registry (a test double, or a downstream backend that
  drops a label deliberately).
* ``stack_scope`` is not part of the admission criterion: the composer's
  ``ephemeral`` dispatcher cycles the service (``docker compose rm`` +
  ``up`` on the named service), leaving the stack alive, so
  ``ephemeral`` is honourable at ``run`` / ``task`` / ``trial`` scope
  alike when the dispatcher is registered.

The ``PER_TRIAL_STACK`` short-circuit at the top of the method covers
backends injected via ``Orchestrator(runtime_backend=...)`` that
materialise every stack per trial — every dispatcher label is
honourable there regardless of registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    ResetSpec,
    RunConfig,
    ServiceSpec,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.service_lifecycle_dispatchers import (
    ResetDispatcher,
    SharedDispatcher,
)
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import StackDecl, StackScope, TaskDescription

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _task(task_id: str) -> Any:
    task = MagicMock()
    task.task_id = task_id
    return task


def _orch(tasks: list[Any], descriptions: dict[str, TaskDescription]) -> Orchestrator:
    orch = Orchestrator(_run_config())
    orch.tasks = tasks
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: descriptions[tid]
    return orch


def _manifest(
    services: dict[str, ServiceSpec],
    *,
    stack_scope: StackScope,
    fixture: str = "safe_two_service.yaml",
) -> EnvironmentManifest:
    compose_file = _FIXTURES / fixture
    manifest = EnvironmentManifest(compose_file=compose_file, services=services)
    manifest.stacks.append(
        StackDecl(
            stack_id="default",
            compose_file=compose_file,
            stack_scope=stack_scope,
            runner_service=manifest.runner_service,
        )
    )
    return manifest


def _shared_backend() -> SharedStackRuntimeBackend:
    return SharedStackRuntimeBackend(runner_address="sentinel:50051")


def _per_trial_backend() -> PerTrialRuntimeBackend:
    return PerTrialRuntimeBackend()


class TestTrialScopeAllowsEveryLabel:
    """``stack_scope="trial"`` — composer materialises fresh per trial,
    so ``shared`` / ``reset`` / ``ephemeral`` are all honourable."""

    def test_ephemeral_on_trial_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="trial",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())

    def test_all_shared_on_trial_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="shared"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="trial",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())

    def test_reset_on_trial_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="trial",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())


class TestNonTrialScopeAdmitsEphemeralUnderBuiltInDispatchers:
    """Under the built-in dispatcher registry (which carries ``shared``,
    ``reset``, ``ephemeral``), every label is admitted at every stack
    scope. The composer's ``EphemeralDispatcher`` cycles the service
    without composing the stack down, so ``ephemeral`` is honourable at
    ``run`` / ``task`` scope alike."""

    def test_ephemeral_on_run_scope_admitted(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {
            "wants-ephemeral": make_task_description(
                task_id="wants-ephemeral", environment_manifest=manifest
            )
        }
        orch = _orch([_task("wants-ephemeral")], descs)
        orch._verify_isolation_compatibility(_shared_backend())

    def test_ephemeral_on_task_scope_admitted(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="task",
        )
        descs = {
            "task-scope-ephemeral": make_task_description(
                task_id="task-scope-ephemeral", environment_manifest=manifest
            )
        }
        orch = _orch([_task("task-scope-ephemeral")], descs)
        orch._verify_isolation_compatibility(_shared_backend())

    def test_all_shared_on_run_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="shared"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())

    def test_all_shared_on_task_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="shared"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="task",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())

    def test_reset_on_run_scope_legal(self) -> None:
        """``reset`` is honourable at every scope — every built-in recipe
        (``sql_dump``, ``filesystem_dir``, ``redis_dump``, ``bare``)
        operates on a live service."""
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())

    def test_reset_on_task_scope_legal(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="task",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_shared_backend())


class TestDispatcherAdmissionRefusal:
    """When the backend's composer is constructed with a partial
    ``dispatcher_registry`` — dropping one label from the closed
    ``ServiceIsolation`` vocab — a task that requests the missing label
    is refused up front with the offending 5-tuple named."""

    def _partial_registry_backend(self) -> SharedStackRuntimeBackend:
        composer = DefaultSubstrateComposer(
            dispatcher_registry={
                "shared": SharedDispatcher(),
                "reset": ResetDispatcher(),
            }
        )
        return SharedStackRuntimeBackend(runner_address="sentinel:50051", composer=composer)

    def test_manifest_with_unregistered_isolation_label_raises(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {
            "wants-ephemeral": make_task_description(
                task_id="wants-ephemeral", environment_manifest=manifest
            )
        }
        orch = _orch([_task("wants-ephemeral")], descs)
        with pytest.raises(RuntimeError) as exc:
            orch._verify_isolation_compatibility(self._partial_registry_backend())
        msg = str(exc.value)
        assert "wants-ephemeral" in msg
        assert "default" in msg  # the stack_id
        assert "db" in msg  # the service name
        assert "ephemeral" in msg

    def test_refusal_message_names_stack_id_service_and_isolation(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {
            "wants-ephemeral": make_task_description(
                task_id="wants-ephemeral", environment_manifest=manifest
            )
        }
        orch = _orch([_task("wants-ephemeral")], descs)
        with pytest.raises(RuntimeError) as exc:
            orch._verify_isolation_compatibility(self._partial_registry_backend())
        msg = str(exc.value)
        assert "('wants-ephemeral', 'default', 'db', 'ephemeral', 'run')" in msg

    def test_refusal_message_carries_no_ticket_number(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {
            "wants-ephemeral": make_task_description(
                task_id="wants-ephemeral", environment_manifest=manifest
            )
        }
        orch = _orch([_task("wants-ephemeral")], descs)
        with pytest.raises(RuntimeError) as exc:
            orch._verify_isolation_compatibility(self._partial_registry_backend())
        assert "#1383" not in str(exc.value)


class TestPerTrialBackendShortCircuit:
    """An injected ``PerTrialRuntimeBackend`` (``isolation_mode ==
    PER_TRIAL_STACK``) materialises the whole plan per trial, so every
    label is honourable regardless of scope. The guard short-circuits
    at the top."""

    def test_ephemeral_on_run_scope_passes_under_per_trial_backend(self) -> None:
        manifest = _manifest(
            {
                "db": ServiceSpec(isolation="ephemeral"),
                "default": ServiceSpec(isolation="shared"),
            },
            stack_scope="run",
        )
        descs = {"t": make_task_description(task_id="t", environment_manifest=manifest)}
        _orch([_task("t")], descs)._verify_isolation_compatibility(_per_trial_backend())

    def test_no_manifest_task_passes(self) -> None:
        descs = {"legacy": make_task_description(task_id="legacy", environment_manifest=None)}
        _orch([_task("legacy")], descs)._verify_isolation_compatibility(_per_trial_backend())


class TestAdapterGuard:
    def test_missing_adapter_raises_clear_error(self) -> None:
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t")]
        orch.adapter = None
        with pytest.raises(RuntimeError, match="adapter"):
            orch._verify_isolation_compatibility(_shared_backend())


class TestNoTasksWithManifest:
    def test_all_tasks_without_manifest_passes(self) -> None:
        descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=None),
            "t2": make_task_description(task_id="t2", environment_manifest=None),
        }
        _orch([_task("t1"), _task("t2")], descs)._verify_isolation_compatibility(_shared_backend())
