"""Unit tests for the task-driven runtime-backend selector.

``Orchestrator._select_backend_from_tasks`` derives the backend choice
from the resolved task set: any task whose manifest requires per-trial
materialisation forces ``PerTrialRuntimeBackend``; otherwise
``SharedStackRuntimeBackend``. ``Orchestrator._resolve_effective_runtime_choice``
layers the deprecated ``orchestrator.runtime`` override on top so
operators retain an escape hatch — setting it emits a
``DeprecationWarning`` once at ``OrchestratorConfig`` construction time.
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
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _make_run_config(runtime_override: str | None = None) -> RunConfig:
    kwargs: dict[str, Any] = {"workers": 1, "repeats": 1, "auto_start_services": False}
    if runtime_override is not None:
        kwargs["runtime"] = runtime_override
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            orch = OrchestratorConfig(**kwargs)
    else:
        orch = OrchestratorConfig(**kwargs)
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=orch,
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _make_task_config(task_id: str) -> Any:
    task = MagicMock()
    task.task_id = task_id
    return task


def _make_orchestrator(
    tasks: list[Any],
    task_descriptions: dict[str, TaskDescription],
    runtime_override: str | None = None,
) -> Orchestrator:
    orch = Orchestrator(_make_run_config(runtime_override))
    orch.tasks = tasks
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: task_descriptions[tid]
    return orch


def _manifest_with_services(services: dict[str, ServiceSpec]) -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services=services,
    )


class TestTaskDrivenSelection:
    """Task-driven signal: read every task's manifest, pick per-trial
    iff any task requires it."""

    def test_no_tasks_with_manifest_selects_shared(self) -> None:
        tasks = [_make_task_config("t1"), _make_task_config("t2")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=None),
            "t2": make_task_description(task_id="t2", environment_manifest=None),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "shared"

    def test_all_shared_manifests_select_shared(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="shared"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "shared"

    def test_any_reset_service_selects_per_trial(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
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
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_any_ephemeral_service_selects_per_trial(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="ephemeral"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_empty_services_manifest_selects_per_trial(self) -> None:
        """A manifest with no explicit services is per-trial-requiring
        by the ADR-0009 safety default."""
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=EnvironmentManifest(
                    compose_file=_FIXTURES / "safe_two_service.yaml",
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_mixed_task_set_any_per_trial_wins(self) -> None:
        """A run with a mix of per-trial and shared-labelled tasks
        routes onto per-trial — one per-trial task is enough."""
        tasks = [_make_task_config("shared"), _make_task_config("stateful")]
        task_descs = {
            "shared": make_task_description(
                task_id="shared",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="shared"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
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
        assert orch._select_backend_from_tasks() == "per_trial"


class TestPinnedSharedServiceInMixedShape:
    """A per-trial-eligible run pins one service
    ``services.<name>.isolation=shared`` in an otherwise ``reset``
    task set. The selector still picks per-trial; the pinned service's
    ``shared`` label carries forward into the manifest for the reset
    dispatcher to honour between trials.
    """

    def test_pinned_shared_service_selects_per_trial(self) -> None:
        tasks = [_make_task_config("triage")]
        task_descs = {
            "triage": make_task_description(
                task_id="triage",
                environment_manifest=_manifest_with_services(
                    {
                        # postgres pinned shared: reset dispatcher will
                        # keep it alive between trials.
                        "db": ServiceSpec(isolation="shared"),
                        # worker service requires reset between trials.
                        "default": ServiceSpec(
                            isolation="reset",
                            reset=ResetSpec(seed="baseline"),
                        ),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._select_backend_from_tasks() == "per_trial"

    def test_pinned_shared_service_survives_on_manifest(self) -> None:
        manifest = _manifest_with_services(
            {
                "db": ServiceSpec(isolation="shared"),
                "default": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            }
        )
        assert manifest.services["db"].isolation == "shared"
        assert manifest.services["default"].isolation == "reset"
        assert manifest.requires_per_trial is True


class TestOperatorOverride:
    """Setting ``orchestrator.runtime`` bypasses the task-driven signal
    and forces the named backend. Setting it emits a
    ``DeprecationWarning`` at ``OrchestratorConfig`` construction time."""

    def test_setting_runtime_emits_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            OrchestratorConfig(workers=1, repeats=1, auto_start_services=False, runtime="per_trial")
        deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation, "expected a DeprecationWarning when runtime is set"
        assert any(
            "OrchestratorConfig.runtime is deprecated" in str(w.message) for w in deprecation
        )

    def test_omitting_runtime_emits_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            OrchestratorConfig(workers=1, repeats=1, auto_start_services=False)
        deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation == []

    def test_docker_alias_emits_two_deprecation_warnings(self) -> None:
        """``docker`` is a legacy alias for ``shared``; it triggers both
        the alias warning and the outer deprecation warning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            OrchestratorConfig(workers=1, repeats=1, auto_start_services=False, runtime="docker")
        messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any("docker" in m for m in messages)
        assert any("OrchestratorConfig.runtime is deprecated" in m for m in messages)

    def test_override_forces_per_trial_against_shared_task_set(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=_manifest_with_services(
                    {
                        "db": ServiceSpec(isolation="shared"),
                        "default": ServiceSpec(isolation="shared"),
                    }
                ),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs, runtime_override="per_trial")
        assert orch._resolve_effective_runtime_choice() == "per_trial"

    def test_override_forces_shared_against_per_trial_task_set(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
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
        orch = _make_orchestrator(tasks, task_descs, runtime_override="shared")
        assert orch._resolve_effective_runtime_choice() == "shared"

    def test_no_override_falls_back_to_task_driven(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
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
        orch = _make_orchestrator(tasks, task_descs, runtime_override=None)
        assert orch._resolve_effective_runtime_choice() == "per_trial"


class TestSelectorAdapterGuard:
    def test_missing_adapter_raises_clear_error(self) -> None:
        orch = Orchestrator(_make_run_config())
        orch.tasks = [_make_task_config("t")]
        orch.adapter = None
        with pytest.raises(RuntimeError, match="adapter"):
            orch._select_backend_from_tasks()
