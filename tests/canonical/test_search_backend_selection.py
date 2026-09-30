"""Lock ``initial_state.rag.backend`` resolution before any trial (ADR-0052).

``Orchestrator.load_tasks`` resolves every searching task's backend once, after the
tasks load and before any stack starts, so an unregistered name is one refusal naming
the task and the registered backends — not one refused ``RegisterTrial`` per trial.
The same resolution covers the default: a task that enables ``search_kb`` and writes
no ``rag`` block is served by ``rag_service``, and an install whose metadata predates
the ``tolokaforge.search_backends`` group is refused for it rather than failing later.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import pytest

from tests.utils.search_backends import register_search_backends
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core import plugin_registry
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.plugin_registry import SEARCH_BACKENDS_GROUP
from tolokaforge.testing.search_backends import in_memory_search_backend_factory

pytestmark = pytest.mark.canonical


class _StubAdapter(BaseAdapter):
    """Serves a fixed set of tasks; only the load path is exercised."""

    def __init__(self, params: dict[str, Any], *, tasks: dict[str, TaskConfig]):
        super().__init__(params)
        self._tasks = tasks

    def get_task_ids(self) -> list[str]:
        return list(self._tasks)

    def get_task(self, task_id: str) -> TaskConfig:
        return self._tasks[task_id]

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover - unused in load_tasks
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _task(task_id: str, *, tools: list[str], rag: dict[str, Any] | None = None) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=task_id,
        category="kb",
        description="stub",
        initial_user_message="look it up",
        initial_state={} if rag is None else {"rag": rag},
        tools={"agent": {"enabled": tools}, "user": {"enabled": []}},
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


def _run_config(*, strict_task_load: bool = False) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1, repeats=1, auto_start_services=False, strict_task_load=strict_task_load
        ),
        evaluation=EvaluationConfig(output_dir="/tmp/search_backend_selection"),
    )


def _orchestrator_with(*tasks: TaskConfig) -> Orchestrator:
    orch = Orchestrator(_run_config())
    orch.adapter = _StubAdapter({}, tasks={task.task_id: task for task in tasks})
    return orch


class _RawTaskAdapter(_StubAdapter):
    """Validates each task at ``get_task``, as a file-backed adapter does."""

    def __init__(self, raw: dict[str, dict[str, Any]]):
        super().__init__({}, tasks={})
        self._raw = raw

    def get_task_ids(self) -> list[str]:
        return list(self._raw)

    def get_task(self, task_id: str) -> TaskConfig:
        return TaskConfig(**self._raw[task_id])


def _raw_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "name": task_id,
        "category": "kb",
        "description": "stub",
        "initial_user_message": "look it up",
        "initial_state": {},
        "tools": {"agent": {"enabled": ["calculator"]}, "user": {"enabled": []}},
        "actors": {"user": {"mode": "llm"}},
        "grading": "grading.yaml",
        **overrides,
    }


class TestAMalformedRagBlockUnderANonStrictLoad:
    """A block that loaded while untyped must not now drop its task in silence."""

    @pytest.mark.parametrize(
        "rag",
        [
            {"corpus_dir": 5},
            {"corpus_dir": "kb", "tool": None},
            {"corpus_dir": "kb", "backnd": "x"},
        ],
        ids=["corpus-dir-not-a-path", "null-tool", "misspelt-key"],
    )
    def test_refuses_the_run_naming_the_task(self, rag: dict[str, Any]) -> None:
        orch = Orchestrator(_run_config(strict_task_load=False))
        orch.adapter = _RawTaskAdapter(
            {
                "TASK-OK": _raw_task("TASK-OK"),
                "TASK-RAG": _raw_task("TASK-RAG", initial_state={"rag": rag}),
            }
        )

        message = _refusal(orch)

        assert "TASK-RAG" in message
        assert "initial_state.rag" in message
        assert "strict_task_load" in message

    def test_another_malformed_task_is_still_dropped_as_before(self) -> None:
        orch = Orchestrator(_run_config(strict_task_load=False))
        orch.adapter = _RawTaskAdapter(
            {
                "TASK-OK": _raw_task("TASK-OK"),
                "TASK-BAD": _raw_task("TASK-BAD", interaction_mode="sideways"),
            }
        )

        orch.load_tasks()

        assert [task.task_id for task in orch.tasks] == ["TASK-OK"]


def _refusal(orch: Orchestrator) -> str:
    with pytest.raises(RuntimeError) as excinfo:
        orch.load_tasks()
    return str(excinfo.value)


def test_an_unregistered_backend_is_refused_naming_the_task_and_the_registered_ones() -> None:
    orch = _orchestrator_with(
        _task("TASK-OK", tools=["search_kb"]),
        _task("TASK-GHOST", tools=["search_kb"], rag={"corpus_dir": "kb", "backend": "ghost"}),
    )

    message = _refusal(orch)

    assert "TASK-GHOST" in message
    assert "initial_state.rag.backend" in message
    assert "'ghost'" in message
    assert "rag_service" in message, "the refusal names the registered backends"


def test_typesense_is_refused_as_a_reserved_name() -> None:
    orch = _orchestrator_with(
        _task("TASK-TS", tools=["search_kb"], rag={"corpus_dir": "kb", "backend": "typesense"})
    )

    message = _refusal(orch)

    assert "TASK-TS" in message
    assert "reserved" in message
    assert "search.plane: typesense" in message


def test_the_default_and_a_registered_backend_load(monkeypatch: pytest.MonkeyPatch) -> None:
    register_search_backends(monkeypatch, in_memory=in_memory_search_backend_factory)
    orch = _orchestrator_with(
        _task("TASK-DEFAULT", tools=["search_kb"], rag={"corpus_dir": "kb"}),
        _task("TASK-BARE", tools=["search_kb"]),
        _task(
            "TASK-MEM", tools=["lookup"], rag={"backend": "in_memory", "tool": {"name": "lookup"}}
        ),
    )

    orch.load_tasks()

    assert [task.task_id for task in orch.tasks] == ["TASK-DEFAULT", "TASK-BARE", "TASK-MEM"]


@pytest.fixture
def no_search_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """An install whose metadata predates the group: nothing is registered under it."""
    real = importlib.metadata.entry_points

    def entry_points(**params: Any) -> Any:
        if params == {"group": SEARCH_BACKENDS_GROUP}:
            return []
        return real(**params)

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    monkeypatch.setattr(plugin_registry, "_discovery_cache", {})


def test_a_searching_task_is_refused_when_nothing_is_registered(no_search_backends: None) -> None:
    orch = _orchestrator_with(_task("TASK-KB", tools=["search_kb"]))

    message = _refusal(orch)

    assert "TASK-KB" in message
    assert "'rag_service'" in message
    assert "(none registered)" in message


def test_a_task_that_does_not_search_resolves_no_backend(no_search_backends: None) -> None:
    orch = _orchestrator_with(_task("TASK-DB", tools=["calculator"]))

    orch.load_tasks()

    assert [task.task_id for task in orch.tasks] == ["TASK-DB"]
