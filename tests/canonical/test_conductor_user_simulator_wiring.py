"""Lock the ``actors.user.simulator`` selector end to end.

Two properties matter for the seam to be real:

- **An unknown simulator name is refused before any trial.** The orchestrator
  resolves every task's ``actors.user.simulator`` in ``load_tasks``, so a typo or
  a missing plugin is one refusal naming the task and the known registrations,
  not one scored failure per trial after each container is already up.
- **A third-party simulator is selectable through the same registry the conductor
  uses.** A simulator registered under ``tolokaforge.user_simulators`` resolves
  through ``load_user_simulator`` — the exact call the conductor makes with the
  task's declared name — and the declared name plus its opaque ``simulator_config``
  reach the resolved config the conductor reads.

The context the conductor builds also carries the task's directory, so a
simulator resolves the paths its ``simulator_config`` names against the task.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core import conductor as conductor_module
from tolokaforge.core.actors.user_simulator import UserSimulatorContext
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    ActorSpec,
    EvaluationConfig,
    InitialStateConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.plugin_registry import (
    USER_SIMULATORS_GROUP,
    _clear_discovery_cache,
    available_user_simulators,
    load_user_simulator,
)
from tolokaforge.observability.observer import TrialIdentity
from tolokaforge.testing.user_simulators import (
    InMemoryUserSimulator,
    in_memory_user_simulator_factory,
)

pytestmark = pytest.mark.canonical

#: The trial identity ``Conductor.run`` derives and hands to ``_run_agent_loop``.
_IDENTITY = TrialIdentity(run_id="run", task_id="t1", trial_index=0, attempt_id=0)


class _StubAdapter(BaseAdapter):
    """Adapter that serves a fixed set of tasks; only the load path is exercised."""

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


def _make_task(task_id: str, *, simulator: str | None = None) -> TaskConfig:
    user = ActorSpec(mode="llm", simulator=simulator)
    return TaskConfig(
        task_id=task_id,
        name=f"Task {task_id}",
        category="tool_use",
        description="stub",
        interaction_mode="conversational",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        actors={"user": user},
        grading="grading.yaml",
    )


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/user_simulator_wiring"),
    )


def _orchestrator_with(tasks: dict[str, TaskConfig]) -> Orchestrator:
    orch = Orchestrator(_make_run_config())
    orch.adapter = _StubAdapter({}, tasks=tasks)
    return orch


class TestUnregisteredSimulatorIsRefused:
    def test_unknown_name_refused_by_load_tasks(self) -> None:
        orch = _orchestrator_with({"TASK-A": _make_task("TASK-A", simulator="ghost-sim")})

        with pytest.raises(RuntimeError) as excinfo:
            orch.load_tasks()

        message = str(excinfo.value)
        assert "TASK-A" in message
        assert "ghost-sim" in message
        assert "builtin" in message, "the refusal must name the known registrations"

    def test_builtin_default_loads_without_complaint(self) -> None:
        orch = _orchestrator_with({"TASK-A": _make_task("TASK-A")})

        orch.load_tasks()

        assert {task.task_id for task in orch.tasks} == {"TASK-A"}
        assert orch.tasks[0].resolve_user_simulator().simulator == "builtin"


@pytest.fixture
def custom_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``custom-sim`` alongside the shipped ``builtin``."""

    class _EntryPointStub:
        def __init__(self, name: str, value: Any) -> None:
            self.name = name
            self.value = value

            class _Dist:
                name = "tests-fixture"

            self.dist = _Dist()

        def load(self) -> Any:
            return in_memory_user_simulator_factory

    _clear_discovery_cache()
    real_entry_points = importlib.metadata.entry_points
    shipped = list(real_entry_points(group=USER_SIMULATORS_GROUP))
    injected = _EntryPointStub("custom-sim", in_memory_user_simulator_factory)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == USER_SIMULATORS_GROUP:
            return [*shipped, injected]
        return list(real_entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()
    yield
    _clear_discovery_cache()


class TestRegisteredSimulatorIsSelectable:
    def test_a_registered_simulator_resolves_through_the_registry(
        self, custom_simulator: None
    ) -> None:
        assert "custom-sim" in available_user_simulators()

        built = load_user_simulator("custom-sim")(
            UserSimulatorContext(
                mode="scripted",
                persona="cooperative",
                backstory=None,
                scripted_flow=None,
                tool_schemas=None,
            )
        )
        assert isinstance(built, InMemoryUserSimulator)

    def test_a_task_selecting_the_registered_simulator_passes_load_tasks(
        self, custom_simulator: None
    ) -> None:
        orch = _orchestrator_with({"TASK-A": _make_task("TASK-A", simulator="custom-sim")})

        orch.load_tasks()

        assert orch.tasks[0].resolve_user_simulator().simulator == "custom-sim"


def test_declared_name_and_config_reach_the_resolved_config() -> None:
    """The value the conductor passes to ``load_user_simulator`` comes from ``actors.user``."""
    task = TaskConfig(
        task_id="TASK-CFG",
        name="Task CFG",
        category="tool_use",
        description="stub",
        interaction_mode="conversational",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        actors={
            "user": ActorSpec(
                mode="llm",
                simulator="custom-sim",
                simulator_config={"guidelines_file": "sim/user.md", "voice": True},
            )
        },
        grading="grading.yaml",
    )

    resolved = task.resolve_user_simulator()
    assert resolved.simulator == "custom-sim"
    assert resolved.simulator_config == {"guidelines_file": "sim/user.md", "voice": True}


def test_the_conductor_hands_the_simulator_its_task_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path ``simulator_config`` names resolves against the task (#1666).

    The context carries the trial setup's ``task_dir`` — the directory the
    adapter's ``get_task_dir`` returned — beside the config, which stays as the
    task declared it.
    """
    built: list[UserSimulatorContext] = []

    def recording_factory(context: UserSimulatorContext) -> InMemoryUserSimulator:
        built.append(context)
        return in_memory_user_simulator_factory(context)

    monkeypatch.setattr(conductor_module, "load_user_simulator", lambda name: recording_factory)
    agent_client = MagicMock()
    agent_client.capabilities.default_max_turns = None
    conductor = InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=_make_run_config(),
        logger=get_logger("user-simulator-wiring", strict=False),
        agent_client=agent_client,
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=tmp_path,
    )
    task = TaskConfig(
        task_id="TASK-DIR",
        description="stub",
        interaction_mode="conversational",
        actors={
            "user": ActorSpec(
                mode="llm",
                simulator="custom-sim",
                simulator_config={"prompt_template": "user_prompt.md"},
            )
        },
    )
    setup = MagicMock()
    setup.trial_idx = 0
    setup.task_dir = tmp_path / "TASK-DIR"
    setup.tool_schemas = []
    setup.user_tool_schemas = []
    spec = MagicMock()
    spec.task.metadata = {}

    with (
        patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
        patch("tolokaforge.core.conductor.TrialRunner"),
    ):
        conductor._run_agent_loop(spec, task, setup, _IDENTITY)

    assert len(built) == 1
    assert built[0].task_dir == tmp_path / "TASK-DIR"
    assert built[0].simulator_config == {"prompt_template": "user_prompt.md"}
