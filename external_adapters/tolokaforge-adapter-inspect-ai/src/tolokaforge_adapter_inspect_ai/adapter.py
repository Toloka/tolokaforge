"""Inspect AI adapter for tolokaforge.

A *delegating* adapter: it does not reimplement Inspect. It discovers Inspect
``@task`` functions in a task pack and translates each into a tolokaforge
``TaskConfig``. Execution is delegated to Inspect itself (which runs its own
solvers, scorers and sandbox) via the local bridge
(:mod:`tolokaforge_adapter_inspect_ai.bridge`), and its results are projected back
onto tolokaforge's ``Grade`` / ``Trajectory`` by
:mod:`tolokaforge_adapter_inspect_ai.normalize`.

Execution through the tolokaforge runner (materialising Inspect's sandbox inside a
trial container and grading from the eval log) is not wired yet:
:meth:`InspectAiAdapter.to_task_description` and :meth:`InspectAiAdapter.grade`
raise ``NotImplementedError`` rather than present a runner path that would fail
later. Model routing goes through an OpenAI-compatible / LiteLLM-proxy endpoint;
secrets are resolved via :mod:`tolokaforge.secrets` by the caller wiring the eval
environment, never read from ``os.environ`` here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.models import (
    Grade,
    GradingCombineConfig,
    GradingConfig,
    InitialStateConfig,
    TaskConfig,
    ToolsConfig,
    Trajectory,
)
from tolokaforge.runner.models import TaskDescription
from tolokaforge_adapter_inspect_ai.inspect_loader import (
    InspectTaskInfo,
    discover_inspect_tasks,
)

_ADAPTER_TYPE = "inspect_ai"

_SYSTEM_PROMPT = (
    "This task is executed by the Inspect AI runtime. The task's own solver drives "
    "the interaction; this prompt is a fallback used only when none is supplied."
)

_NO_RUNNER_EXECUTION = (
    "InspectAiAdapter does not execute through the tolokaforge runner. Run Inspect "
    "tasks with tolokaforge_adapter_inspect_ai.bridge.run_inspect_eval and project the "
    "result with tolokaforge_adapter_inspect_ai.normalize."
)


class InspectAiAdapter(BaseAdapter):
    """Runs Inspect AI tasks by delegating execution to ``inspect_ai``."""

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        first_pack = str(self.task_packs[0]) if self.task_packs else None
        self.pack_dir = Path(params.get("inspect_task_dir") or first_pack or self.base_dir)
        self.tasks_glob: list[str] | None = _as_list(params.get("tasks_glob"))
        self.task_id_filter: list[str] | None = _as_list(params.get("task_ids"))
        self.agent_model: str = params.get("agent_model") or ""
        self._tasks: dict[str, InspectTaskInfo] = {}

    # -- discovery -----------------------------------------------------------

    def _ensure_discovered(self) -> None:
        if not self._tasks:
            self._tasks = discover_inspect_tasks(self.pack_dir, self.tasks_glob)

    def get_task_ids(self) -> list[str]:
        self._ensure_discovered()
        ids = list(self._tasks)
        if self.task_id_filter:
            ids = [tid for tid in ids if tid in self.task_id_filter]
        return ids

    def get_task_dir(self, task_id: str) -> Path:
        self._ensure_discovered()
        return self._tasks[task_id].task_dir

    def inspect_task(self, task_id: str) -> InspectTaskInfo:
        """The discovered Inspect task (file + name) for ``task_id``."""
        self._ensure_discovered()
        return self._tasks[task_id]

    # -- translation ---------------------------------------------------------

    def get_task(self, task_id: str) -> TaskConfig:
        self._ensure_discovered()
        info = self._tasks[task_id]
        return TaskConfig(
            task_id=task_id,
            name=info.name,
            category="inspect",
            description=f"Inspect AI task {info.name}",
            adapter_type=_ADAPTER_TYPE,
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(agent={"enabled": []}, user={"enabled": []}),
            grading="__adapter__",
            policies={"agent_system_prompt": self.get_system_prompt(task_id)},
            adapter_settings={
                "inspect_file": str(info.file),
                "inspect_task": info.name,
                "attribs": info.attribs,
            },
        )

    def get_system_prompt(self, task_id: str) -> str:
        return _SYSTEM_PROMPT

    def get_grading_config(self, task_id: str) -> GradingConfig:
        return GradingConfig(
            combine=GradingCombineConfig(
                method="weighted",
                weights={"custom_checks": 1.0},
                pass_threshold=0.5,
            ),
        )

    def to_task_description(self, task_id: str) -> TaskDescription:
        raise NotImplementedError(_NO_RUNNER_EXECUTION)

    # -- environment (Inspect owns the real environment) ---------------------

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        self._ensure_discovered()
        return AdapterEnvironment(
            data={}, tools=[], wiki="", rules=[], task_dir=self._tasks[task_id].task_dir
        )

    def get_tools(self, task_id: str) -> list[Any]:
        return []

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Any]:
        return []

    def reset_environment(self, env: AdapterEnvironment) -> None:
        pass

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        return None

    def grade(
        self,
        task_id: str,
        trajectory: Trajectory,
        final_state: dict[str, Any],
        env: AdapterEnvironment,
    ) -> Grade:
        raise NotImplementedError(_NO_RUNNER_EXECUTION)


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)
