"""Unit tests for ``OrchestratorConfig.strict_task_load``.

Locks the ``load_tasks`` behaviour: default ``False`` matches the historical
log-and-skip on adapter exceptions; opt-in ``True`` re-raises with the task
id so a run refuses to start with a silently shorter task list.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


class _RaisingStubAdapter(BaseAdapter):
    """Adapter that raises from ``get_task()`` for a nominated set of task ids.

    Only implements the two methods :meth:`Orchestrator.load_tasks` reaches.
    The remaining abstracts stay defined so the class is instantiable, but
    the orchestrator's load path never touches them under test.
    """

    def __init__(self, params: dict[str, Any], *, tasks: dict[str, TaskConfig], raises: set[str]):
        super().__init__(params)
        self._tasks = tasks
        self._raises = raises

    def get_task_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def get_task(self, task_id: str) -> TaskConfig:
        if task_id in self._raises:
            raise RuntimeError(f"synthetic failure for {task_id}")
        return self._tasks[task_id]

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def create_environment(
        self, task_id: str
    ) -> AdapterEnvironment:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> Any:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def reset_environment(
        self, env: AdapterEnvironment
    ) -> None:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError


def _make_task(task_id: str) -> TaskConfig:
    from tolokaforge.core.models import ActorSpec, InitialStateConfig, ToolsConfig

    return TaskConfig(
        task_id=task_id,
        name=f"Task {task_id}",
        category="tool_use",
        description="stub",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        actors={"user": ActorSpec(mode="scripted")},
        grading="grading.yaml",
    )


def _make_run_config(*, strict: bool | None = None) -> RunConfig:
    kwargs: dict[str, Any] = {"workers": 1, "repeats": 1, "auto_start_services": False}
    if strict is not None:
        kwargs["strict_task_load"] = strict
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(**kwargs),
        evaluation=EvaluationConfig(output_dir="/tmp/strict_task_load"),
    )


class TestStrictTaskLoadDefault:
    """The new field must not change any existing run's meaning."""

    def test_default_is_false(self) -> None:
        """Backwards-compat lock: default remains log-and-skip."""
        assert OrchestratorConfig().strict_task_load is False


class TestLenientPreservesLogAndSkip:
    """Regression lock: default ``False`` matches the historical behaviour."""

    def test_broken_task_is_skipped_and_run_proceeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tasks = {tid: _make_task(tid) for tid in ("TASK-A", "TASK-B", "TASK-C")}
        adapter = _RaisingStubAdapter({}, tasks=tasks, raises={"TASK-B"})

        orch = Orchestrator(_make_run_config(strict=False))
        orch.adapter = adapter

        with caplog.at_level(logging.ERROR):
            orch.load_tasks()

        loaded_ids = {task.task_id for task in orch.tasks}
        assert loaded_ids == {"TASK-A", "TASK-C"}
        assert any(
            "Failed to load task" in record.getMessage()
            and getattr(record, "task_id", None) == "TASK-B"
            for record in caplog.records
        )


class TestStrictRefusesToStart:
    """Opt-in ``True`` propagates the adapter exception with the task id."""

    def test_adapter_raise_propagates_with_task_id(self) -> None:
        tasks = {tid: _make_task(tid) for tid in ("TASK-A", "TASK-B", "TASK-C")}
        adapter = _RaisingStubAdapter({}, tasks=tasks, raises={"TASK-B"})

        orch = Orchestrator(_make_run_config(strict=True))
        orch.adapter = adapter

        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        assert "TASK-B" in message
        assert "strict_task_load" in message
        # A partially loaded task list must not leak into a run under strict mode.
        assert orch.tasks == []

    def test_strict_mode_all_tasks_succeed_loads_normally(self) -> None:
        """Strict mode is a refusal on failure, not a stricter contract on success."""
        tasks = {tid: _make_task(tid) for tid in ("TASK-A", "TASK-B")}
        adapter = _RaisingStubAdapter({}, tasks=tasks, raises=set())

        orch = Orchestrator(_make_run_config(strict=True))
        orch.adapter = adapter

        orch.load_tasks()

        assert {task.task_id for task in orch.tasks} == {"TASK-A", "TASK-B"}
