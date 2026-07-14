"""Unit tests for ``Orchestrator._verify_isolation_compatibility`` under
task-driven backend selection.

Backend selection is now task-driven: any task whose manifest carries a
non-``shared`` service label routes the run onto
:class:`PerTrialRuntimeBackend`. The isolation-compatibility guard is
therefore only reachable under the deprecated ``orchestrator.runtime``
override — when the operator forces a shared backend against a
per-trial-requiring task set (or an ``ephemeral``-labelled service in a
shared context). These tests pin those branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    ServiceSpec,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _make_task_config(task_id: str) -> Any:
    task = MagicMock()
    task.task_id = task_id
    return task


def _make_orchestrator(
    tasks: list[Any], task_descriptions: dict[str, TaskDescription]
) -> Orchestrator:
    orch = Orchestrator(_make_run_config())
    orch.tasks = tasks
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: task_descriptions[tid]
    return orch


def _manifest_with_services(services: dict[str, ServiceSpec]) -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services=services,
    )


def _shared_stack_backend() -> SharedStackRuntimeBackend:
    return SharedStackRuntimeBackend(runner_address="sentinel:50051")


def _per_trial_backend() -> PerTrialRuntimeBackend:
    return PerTrialRuntimeBackend()


class TestSharedStackRuntimePath:
    """The load-bearing case: SharedStackRuntimeBackend refuses tasks
    whose manifest requires per-trial materialisation."""

    def test_no_tasks_with_manifest_passes(self) -> None:
        tasks = [_make_task_config("t1"), _make_task_config("t2")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=None),
            "t2": make_task_description(task_id="t2", environment_manifest=None),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_shared_stack_backend())

    def test_all_shared_services_passes(self) -> None:
        tasks = [_make_task_config("stateless")]
        task_descs = {
            "stateless": make_task_description(
                task_id="stateless",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="shared"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_shared_stack_backend())

    def test_reset_service_raises(self) -> None:
        from tolokaforge.core.models import ResetSpec

        tasks = [_make_task_config("stateful")]
        task_descs = {
            "stateful": make_task_description(
                task_id="stateful",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(
                            isolation="reset",
                            reset=ResetSpec(seed="baseline"),
                        ),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError, match="per-trial") as exc:
            orch._verify_isolation_compatibility(_shared_stack_backend())
        assert "stateful" in str(exc.value)

    def test_empty_services_defaults_to_per_trial(self) -> None:
        """A manifest with no explicit services is treated as
        per-trial-requiring — that keeps the ADR-0009 safety default."""
        manifest = EnvironmentManifest(compose_file=_FIXTURES / "safe_two_service.yaml")
        assert manifest.requires_per_trial is True
        tasks = [_make_task_config("stateful-implicit")]
        task_descs = {
            "stateful-implicit": make_task_description(
                task_id="stateful-implicit",
                environment_manifest=manifest,
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError, match="stateful-implicit"):
            orch._verify_isolation_compatibility(_shared_stack_backend())

    def test_ephemeral_service_on_shared_raises_dedicated_error(self) -> None:
        tasks = [_make_task_config("wants-ephemeral")]
        task_descs = {
            "wants-ephemeral": make_task_description(
                task_id="wants-ephemeral",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="ephemeral"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError, match="per-trial") as exc:
            orch._verify_isolation_compatibility(_shared_stack_backend())
        # ephemeral triggers the requires_per_trial branch first.
        assert "wants-ephemeral" in str(exc.value)


class TestPerTrialRuntimePath:
    """PerTrialRuntimeBackend satisfies every isolation requirement."""

    def test_reset_service_passes(self) -> None:
        from tolokaforge.core.models import ResetSpec

        tasks = [_make_task_config("stateful")]
        task_descs = {
            "stateful": make_task_description(
                task_id="stateful",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(
                            isolation="reset",
                            reset=ResetSpec(seed="baseline"),
                        ),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_per_trial_backend())

    def test_all_shared_services_passes(self) -> None:
        tasks = [_make_task_config("stateless")]
        task_descs = {
            "stateless": make_task_description(
                task_id="stateless",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="shared"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_per_trial_backend())

    def test_no_manifest_task_passes(self) -> None:
        tasks = [_make_task_config("legacy")]
        task_descs = {
            "legacy": make_task_description(task_id="legacy", environment_manifest=None),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_per_trial_backend())


class TestAdapterGuard:
    def test_missing_adapter_raises_clear_error(self) -> None:
        orch = Orchestrator(_make_run_config())
        orch.tasks = [_make_task_config("t")]
        orch.adapter = None
        with pytest.raises(RuntimeError, match="adapter"):
            orch._verify_isolation_compatibility(_shared_stack_backend())
