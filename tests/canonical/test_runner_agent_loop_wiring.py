"""Runner ↔ ``AgentLoop`` wiring lock — the ``tolokaforge.agent_loops`` seam.

Three invariants this file locks:

* A loop registered by a third party under the ``tolokaforge.agent_loops``
  entry-point group and named by ``TrialRunner(agent_loop=...)`` is the loop
  that drives the trial: it receives the runner's own ``messages`` list, its
  appends land in the produced :class:`~tolokaforge.core.models.Trajectory`,
  its :class:`~tolokaforge.core.loop.LoopOutcome` becomes the trial's verdict,
  and the built-in :class:`~tolokaforge.core.loop.ToolCallingLoop` never runs.
* The built-in loop is not special-cased: ``engine-loop`` resolves through the
  same registry to a :class:`~tolokaforge.core.loop.ToolCallingLoop`, and the
  runner's default drives it.
* An unregistered name is refused with :class:`UnknownImplementationError`
  listing the known names, like every other seam in the registry.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass, field
from typing import Any

import pytest

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
    Message,
    MessageRole,
    Metrics,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.core.plugin_registry import (
    AGENT_LOOPS_GROUP,
    UnknownImplementationError,
    _clear_discovery_cache,
    load_agent_loop,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.canonical

_STUB_REPLY = "the stub loop drove this trial"


class _NeverCalledAgent:
    """Agent generate seam that fails the test if the built-in loop runs."""

    def __init__(self) -> None:
        self.capabilities = ModelCapabilities()

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
