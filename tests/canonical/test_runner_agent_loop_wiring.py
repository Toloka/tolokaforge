"""Runner ↔ ``AgentLoop`` wiring lock — the ``tolokaforge.agent_loops`` seam.

Five invariants this file locks:

* A loop registered by a third party under the ``tolokaforge.agent_loops``
  entry-point group and named by ``TrialRunner(agent_loop=...)`` is the loop
  that drives the trial: it receives the runner's own ``messages`` list, its
  appends land in the produced :class:`~tolokaforge.core.models.Trajectory`,
  its :class:`~tolokaforge.core.loop.LoopOutcome` becomes the trial's verdict,
  and the built-in :class:`~tolokaforge.core.loop.ToolCallingLoop` never runs.
* ``orchestrator.agent_loop`` is the only way a user reaches that kwarg, so the
  config → conductor → runner path is locked end-to-end: the conductor drives
  the loop the run config names.
* The built-in loop is not special-cased: ``engine-loop`` resolves through the
  same registry to a :class:`~tolokaforge.core.loop.ToolCallingLoop`, and the
  runner's default drives it.
* An unregistered name is refused with :class:`UnknownImplementationError`
  listing the known names, like every other seam in the registry.
* That refusal lands before any trial work — at run start, naming the config
  field — rather than as one scored trial failure per trial.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.loop import (
    AgentLoop,
    AgentLoopContext,
    LoopConfig,
    LoopOutcome,
    TerminationDecision,
    ToolCallingLoop,
    classify_loop_error,
)
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GradeComponents,
    InitialStateConfig,
    Message,
    MessageRole,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    ToolsConfig,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.plugin_registry import (
    AGENT_LOOPS_GROUP,
    UnknownImplementationError,
    _clear_discovery_cache,
    load_agent_loop,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical

_STUB_REPLY = "the stub loop drove this trial"


class _NeverCalledAgent:
    """Agent generate seam that fails the test if the built-in loop runs."""

    def __init__(self) -> None:
        self.capabilities = ModelCapabilities()
        # Read by the conductor's artifact-write phase, never by a generation.
        self.config = ModelConfig(provider="openai", name="gpt-4")

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        raise AssertionError(
            "the registered stub loop was not the loop that drove the trial — "
            "the built-in tool-calling loop generated instead"
        )

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


@dataclass
class _StubAgentLoop:
    """Third-party loop shape: appends one assistant turn and returns its verdict."""

    context: AgentLoopContext
    seen: list[tuple[str, int, float]] = field(default_factory=list)

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        self.seen.append((system_prompt, len(messages), start_time))
        messages.append(Message(role=MessageRole.ASSISTANT, content=_STUB_REPLY))
        return LoopOutcome(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.STUCK_DETECTED,
            captured_effective_system_prompt="stub effective prompt",
        )


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan."""

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


@pytest.fixture
def stub_loops(monkeypatch: pytest.MonkeyPatch) -> list[_StubAgentLoop]:
    """Register ``stub_loop`` alongside the shipped ``engine-loop``.

    Returns the list every factory call appends its built loop to, so a case
    can assert both that the factory ran and what the loop it built saw.
    """
    built: list[_StubAgentLoop] = []

    def factory(context: AgentLoopContext) -> _StubAgentLoop:
        loop = _StubAgentLoop(context=context)
        built.append(loop)
        return loop

    _clear_discovery_cache()
    # Bound before the patch: the runner resolves its turn policy through the
    # same scan, so the fallback must reach the real implementation.
    real_entry_points = importlib.metadata.entry_points
    shipped = list(real_entry_points(group=AGENT_LOOPS_GROUP))
    injected = _EntryPointStub("stub_loop", factory)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == AGENT_LOOPS_GROUP:
            return [*shipped, injected]
        return list(real_entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()
    yield built
    _clear_discovery_cache()


def _run_trial(agent_loop: str) -> Any:
    return TrialRunner(
        task_id="agent-loop-wiring",
        trial_index=0,
        agent_client=_NeverCalledAgent(),
        user_simulator=None,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
        agent_loop=agent_loop,
    ).run("You are an agent.", "Do the task.")


def test_registered_loop_drives_the_trial(stub_loops: list[_StubAgentLoop]) -> None:
    """The named loop runs, and its outcome is the trial's outcome."""
    trajectory = _run_trial("stub_loop")

    assert len(stub_loops) == 1, (
        "the runner did not build the loop named by ``agent_loop`` through the "
        f"``{AGENT_LOOPS_GROUP}`` registry"
    )
    loop = stub_loops[0]
    assert len(loop.seen) == 1, "the resolved loop's ``run`` was never called"
    system_prompt, _, start_time = loop.seen[0]
    assert system_prompt == "You are an agent."
    assert start_time > 0.0, "the loop must receive the episode's start epoch"

    assert trajectory.termination_reason is TerminationReason.STUCK_DETECTED, (
        "the trial's verdict must come from the resolved loop's ``LoopOutcome``; "
        f"got {trajectory.termination_reason!r}"
    )
    assert trajectory.status is TrialStatus.COMPLETED
    assert any(message.content == _STUB_REPLY for message in trajectory.messages), (
        "the loop must receive the runner's own ``messages`` list — its appends "
        "are missing from the trajectory"
    )


def test_registered_loop_receives_the_trial_recorder_and_id_assigner(
    stub_loops: list[_StubAgentLoop],
) -> None:
    """The context carries the two halves of the timeline join key.

    A loop that records a tool call under an id its assistant message did not
    declare makes the trial ungradeable, so both the trial's recorder and the
    episode-wide id assigner reach the loop through the context rather than
    being re-derived inside it.
    """
    runner = TrialRunner(
        task_id="agent-loop-context",
        trial_index=0,
        agent_client=_NeverCalledAgent(),
        user_simulator=None,
        tool_executor=ToolExecutor(ToolRegistry()),
        tool_schemas=[],
        max_turns=5,
        episode_timeout_s=1200,
        interaction_mode="agent_only",
        agent_loop="stub_loop",
    )
    runner.run("You are an agent.", "Do the task.")

    context = stub_loops[0].context
    assert context.recorder is runner.tool_call_recorder
    assert context.call_ids is runner._call_ids
    assert context.tool_executor is runner.tool_executor


def test_builtin_loop_resolves_through_the_registry() -> None:
    """``engine-loop`` is a registration, not a special case."""
    factory = load_agent_loop("engine-loop")
    loop = factory(
        AgentLoopContext(
            llm_client=_NeverCalledAgent(),
            tool_executor=ToolExecutor(ToolRegistry()),
            tool_schemas=[],
            config=LoopConfig(max_turns=1),
            metrics=Metrics(),
            should_terminate=lambda result, turn, messages: None,
            logger=StructuredLogger("test"),
            classify_error=lambda exc: classify_loop_error(exc, ()),
            call_ids=EpisodeUniqueCallIds(),
        )
    )
    assert isinstance(loop, ToolCallingLoop)
    assert isinstance(loop, AgentLoop)


def test_unknown_loop_name_is_refused() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_agent_loop("this_loop_is_not_registered")
    assert AGENT_LOOPS_GROUP in str(excinfo.value)
    assert "engine-loop" in str(excinfo.value)


class _StubAdapter:
    """Adapter surface ``Orchestrator.load_tasks`` reaches, and nothing else."""

    def get_task_ids(self) -> list[str]:
        return []

    def get_task(self, task_id: str) -> Any:  # pragma: no cover - no ids to load
        raise AssertionError("no task should be loaded past the agent-loop refusal")


def _run_config(agent_loop: str, output_dir: str) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            agent_loop=agent_loop,
        ),
        evaluation=EvaluationConfig(output_dir=output_dir),
    )


class TestAnUnregisteredNameFailsBeforeTheTrial:
    """A name no package registers is a config fault, not a trial outcome.

    Resolved per trial, an unregistered name costs one ``TrialStatus.ERROR``
    per trial — after that trial's container is provisioned, and priced as a
    scored agent failure. Both of these lock the resolution out of that path.
    """

    def test_the_run_refuses_before_any_task_is_loaded(self, tmp_path: Path) -> None:
        orchestrator = Orchestrator(_run_config("engine_loop", str(tmp_path)))
        orchestrator.adapter = _StubAdapter()

        with pytest.raises(RuntimeError) as excinfo:
            orchestrator.load_tasks()

        message = str(excinfo.value)
        assert "orchestrator.agent_loop" in message, (
            "the refusal must name the config field the operator typed the name in; "
            f"got {message!r}"
        )
        assert "engine_loop" in message
        assert "engine-loop" in message, "the refusal must list the registered names"

    def test_the_default_name_passes_the_gate(self, tmp_path: Path) -> None:
        orchestrator = Orchestrator(_run_config("engine-loop", str(tmp_path)))
        orchestrator.adapter = _StubAdapter()

        orchestrator.load_tasks()

        assert orchestrator.tasks == []

    def test_the_runner_raises_rather_than_scoring_a_trial_error(self) -> None:
        """Resolution sits outside the runner's trial-fault classifier."""
        with pytest.raises(UnknownImplementationError):
            _run_trial("this_loop_is_not_registered")


class _NullRuntime:
    """Runtime backend fake: registers a tool-less trial and reads empty state."""

    def register_trial(self, **kwargs: Any) -> dict[str, Any]:
        return {"success": True, "num_agent_tools": 0, "tool_schemas": []}

    def get_state(self, trial_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"success": True, "state_json": "{}"}


class _PassGrader:
    """Trial grader fake — the grading phase is not what this file locks."""

    def grade(self, spec: TrialSpec, trajectory: Any, system_prompt: str) -> Grade:
        return Grade(binary_pass=True, score=1.0, components=GradeComponents(), reasons="ok")


_CONDUCTOR_TASK_ID = "agent-loop-conductor"


def _conductor_task_config() -> TaskConfig:
    return TaskConfig(
        task_id=_CONDUCTOR_TASK_ID,
        name=_CONDUCTOR_TASK_ID,
        category="test",
        description="config → conductor → runner wiring",
        initial_user_message="Do the task.",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(agent={"enabled": []}, user={"enabled": []}),
        interaction_mode="agent_only",
    )


def _conductor_spec() -> TrialSpec:
    return TrialSpec(
        trial_id=f"{_CONDUCTOR_TASK_ID}:0",
        run_id="agent-loop-wiring",
        task_id=_CONDUCTOR_TASK_ID,
        trial_index=0,
        task=TaskDescription(
            task_id=_CONDUCTOR_TASK_ID,
            name=_CONDUCTOR_TASK_ID,
            category="test",
            description="config → conductor → runner wiring",
            adapter_type="native",
            system_prompt="",
            agent_tools=[],
        ),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


def test_the_run_config_name_reaches_the_runner_through_the_conductor(
    stub_loops: list[_StubAgentLoop], tmp_path: Path
) -> None:
    """``orchestrator.agent_loop`` is the only way a user reaches the kwarg.

    Every other case here names the loop on ``TrialRunner`` directly, so the
    conductor could stop passing ``agent_loop=`` and each of them would still
    pass while every real run silently fell back to ``engine-loop``. This drives
    :meth:`InProcessConductor.run` from a real :class:`RunConfig` instead.
    """
    adapter = MagicMock()
    adapter.get_task_dir.return_value = tmp_path
    adapter.create_environment.return_value = MagicMock(data={}, task_dir=tmp_path)
    adapter.get_grading_config.return_value = None

    conductor = InProcessConductor(
        adapter=adapter,
        artifact_writer=MagicMock(),
        config=_run_config("stub_loop", str(tmp_path)),
        logger=StructuredLogger("agent-loop-conductor-wiring"),
        agent_client=_NeverCalledAgent(),
        runtime_backend=_NullRuntime(),
        trial_grader=_PassGrader(),
        output_dir=tmp_path / "out",
    )

    result = conductor.run(_conductor_spec(), _conductor_task_config())

    assert len(stub_loops) == 1, (
        "the conductor did not thread ``orchestrator.agent_loop`` into the "
        "TrialRunner — the run silently fell back to the built-in loop"
    )
    assert any(message.content == _STUB_REPLY for message in result.trajectory.messages)


def test_the_seam_types_are_reachable_from_the_plugin_registry() -> None:
    """A loop author follows the turn-policy precedent and must not hit an ImportError.

    ``TurnPolicyContext`` and ``TurnPolicyFactory`` both resolve from
    ``tolokaforge.core.plugin_registry``, so the agent-loop seam exposes its
    Protocol, context and factory alias there too — the same objects
    ``tolokaforge.core.loop`` declares, not copies.
    """
    from tolokaforge.core import loop as loop_module
    from tolokaforge.core import plugin_registry

    for name in ("AgentLoop", "AgentLoopContext", "AgentLoopFactory"):
        assert name in plugin_registry.__all__, (
            f"{name} is part of the agent-loop seam and must be re-exported "
            "beside TurnPolicyContext / TurnPolicyFactory"
        )
        assert getattr(plugin_registry, name) is getattr(loop_module, name)
