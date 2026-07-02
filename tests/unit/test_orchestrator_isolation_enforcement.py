"""Unit tests for ``Orchestrator._verify_isolation_compatibility``.

Refusing a shared-stack backend when any task in the run declares
``environment_manifest.isolation: per_trial`` is the load-bearing
invariant that prevents silent cross-trial state contamination.
The tests here pin every branch of the enforcement helper.
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
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest, TaskIsolation
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
    """The enforcement helper only reads ``.task_id`` from each entry —
    a bare object with that attribute is sufficient."""
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


def _manifest_with(isolation: TaskIsolation) -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        isolation=isolation,
    )


def _shared_stack_backend() -> SharedStackRuntimeBackend:
    """Constructed but never connected — enforcement only touches
    ``isinstance``, so no daemon required."""
    return SharedStackRuntimeBackend(runner_address="sentinel:50051")


def _per_trial_backend() -> PerTrialRuntimeBackend:
    return PerTrialRuntimeBackend()


class TestSharedStackRuntimePath:
    """The load-bearing case: SharedStackRuntimeBackend refuses per-trial tasks."""

    def test_no_tasks_with_manifest_passes(self) -> None:
        tasks = [_make_task_config("t1"), _make_task_config("t2")]
        task_descs = {
            "t1": make_task_description(task_id="t1", environment_manifest=None),
            "t2": make_task_description(task_id="t2", environment_manifest=None),
        }
        orch = _make_orchestrator(tasks, task_descs)
        # Must not raise.
        orch._verify_isolation_compatibility(_shared_stack_backend())

    def test_shared_ok_manifest_passes(self) -> None:
        tasks = [_make_task_config("stateless")]
        task_descs = {
            "stateless": make_task_description(
                task_id="stateless",
                environment_manifest=_manifest_with(TaskIsolation.SHARED_OK),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_shared_stack_backend())

    def test_per_trial_manifest_raises(self) -> None:
        tasks = [_make_task_config("stateful")]
        task_descs = {
            "stateful": make_task_description(
                task_id="stateful",
                environment_manifest=_manifest_with(TaskIsolation.PER_TRIAL),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError, match="per_trial") as exc:
            orch._verify_isolation_compatibility(_shared_stack_backend())
        assert "stateful" in str(exc.value)

    def test_per_trial_default_is_the_enforced_default(self) -> None:
        """A manifest that does not specify isolation defaults to
        per_trial — that default must trigger the shared-stack refusal
        just like an explicit per_trial declaration."""
        manifest = EnvironmentManifest(compose_file=_FIXTURES / "safe_two_service.yaml")
        assert manifest.isolation is TaskIsolation.PER_TRIAL  # documents the default
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

    def test_mixed_manifests_only_per_trial_names_reported(self) -> None:
        tasks = [
            _make_task_config("stateful-a"),
            _make_task_config("stateful-b"),
            _make_task_config("stateless"),
            _make_task_config("no-manifest"),
        ]
        task_descs = {
            "stateful-a": make_task_description(
                task_id="stateful-a",
                environment_manifest=_manifest_with(TaskIsolation.PER_TRIAL),
            ),
            "stateful-b": make_task_description(
                task_id="stateful-b",
                environment_manifest=_manifest_with(TaskIsolation.PER_TRIAL),
            ),
            "stateless": make_task_description(
                task_id="stateless",
                environment_manifest=_manifest_with(TaskIsolation.SHARED_OK),
            ),
            "no-manifest": make_task_description(task_id="no-manifest", environment_manifest=None),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError) as exc:
            orch._verify_isolation_compatibility(_shared_stack_backend())
        message = str(exc.value)
        assert "stateful-a" in message
        assert "stateful-b" in message
        assert "stateless" not in message
        assert "no-manifest" not in message
        assert "2 task(s)" in message

    def test_error_names_the_fix(self) -> None:
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=_manifest_with(TaskIsolation.PER_TRIAL),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        with pytest.raises(RuntimeError) as exc:
            orch._verify_isolation_compatibility(_shared_stack_backend())
        message = str(exc.value)
        assert "PerTrialRuntimeBackend" in message
        assert "shared_ok" in message


class TestPerTrialRuntimePath:
    """PerTrialRuntimeBackend satisfies every isolation requirement."""

    def test_per_trial_task_passes(self) -> None:
        tasks = [_make_task_config("stateful")]
        task_descs = {
            "stateful": make_task_description(
                task_id="stateful",
                environment_manifest=_manifest_with(TaskIsolation.PER_TRIAL),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        orch._verify_isolation_compatibility(_per_trial_backend())

    def test_shared_ok_task_passes(self) -> None:
        tasks = [_make_task_config("stateless")]
        task_descs = {
            "stateless": make_task_description(
                task_id="stateless",
                environment_manifest=_manifest_with(TaskIsolation.SHARED_OK),
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
        orch.adapter = None  # simulates called before load_tasks()
        with pytest.raises(RuntimeError, match="adapter"):
            orch._verify_isolation_compatibility(_shared_stack_backend())


class TestTaskDescriptionCaching:
    def test_cache_populated_from_call(self) -> None:
        """Repeated invocations don't re-query the adapter for
        already-resolved task descriptions."""
        tasks = [_make_task_config("t")]
        task_descs = {
            "t": make_task_description(
                task_id="t",
                environment_manifest=_manifest_with(TaskIsolation.SHARED_OK),
            ),
        }
        orch = _make_orchestrator(tasks, task_descs)
        assert orch._task_desc_cache == {}
        orch._verify_isolation_compatibility(_shared_stack_backend())
        assert "t" in orch._task_desc_cache
        # Second call hits the cache — adapter not called again.
        adapter_calls_before = orch.adapter.to_task_description.call_count
        orch._verify_isolation_compatibility(_shared_stack_backend())
        assert orch.adapter.to_task_description.call_count == adapter_calls_before
