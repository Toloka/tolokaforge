"""The agent's completion tool ends its own episode — and only where enabled.

Termination in the engine loop is otherwise a property of how a model writes:
the loop routes to the turn policy only on a turn carrying no tool call, so a
model that answers every turn with a bare tool call has no way to say it has
finished. ``submit`` makes the terminal act an action instead of a sentence.

Two things are locked here. That the signal works — a call ends the trial, with
a reason of its own that no other path produces. And that it is inert
everywhere it was not asked for: the same call, in a run that did not enable
the tool, changes nothing at all. The second matters more than the first.
Nearly every task pack in the field terminates through its user simulator, and
a completion tool that leaked into those runs would end trials early and score
the truncation.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult, UserSimulator
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    Message,
    TerminationReason,
    ToolCall,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.models.task_config import InteractionMode
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialRunner, _enabled_completion_tools
from tolokaforge.tools.builtin import registry as builtin_registry
from tolokaforge.tools.builtin.calculator import CalculatorTool
from tolokaforge.tools.builtin.submit import SUBMIT_TOOL_NAME, SubmitTool
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.unit


def completion_schema() -> dict[str, Any]:
    """The completion tool as a run offers it, from the registered class."""
    return builtin_registry.get_class(SUBMIT_TOOL_NAME)().get_schema()


class _ScriptedAgent:
    """Generate seam yielding one queued result per turn, repeating the last."""

    def __init__(self, *items: GenerationResult) -> None:
        self._items = list(items)
        self.capabilities = ModelCapabilities()

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        return self._items.pop(0) if len(self._items) > 1 else self._items[0]

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _usage() -> Usage:
    return Usage(prompt_tokens=10, completion_tokens=5)


def _text(body: str) -> GenerationResult:
    return GenerationResult(text=body, tool_calls=[], usage=_usage())


def _calls(*calls: ToolCall) -> GenerationResult:
    return GenerationResult(text="", tool_calls=list(calls), usage=_usage())


def _submit(**arguments: Any) -> ToolCall:
    return ToolCall(id="submit-1", name=SUBMIT_TOOL_NAME, arguments=dict(arguments))


def _run(
    *agent_items: GenerationResult,
    tool_schemas: list[dict[str, Any]] | None = None,
    registry: ToolRegistry | None = None,
    max_turns: int = 6,
    user_reply: str = "Please carry on.",
    interaction_mode: InteractionMode = "conversational",
) -> tuple[Trajectory, TrialRunner]:
    """Drive one whole trial and return its trajectory beside its runner."""
    runner = TrialRunner(
        task_id="completion",
        trial_index=0,
        agent_client=_ScriptedAgent(*agent_items),
        user_simulator=UserSimulator(mode="scripted", scripted_flow=[{"default": user_reply}]),
        tool_executor=ToolExecutor(registry or ToolRegistry()),
        tool_schemas=tool_schemas or [],
        max_turns=max_turns,
        interaction_mode=interaction_mode,
    )
    return runner.run("You are an agent.", "Do the task."), runner


# ---------------------------------------------------------------------------
# The signal works
# ---------------------------------------------------------------------------


def test_calling_the_completion_tool_ends_the_trial():
    trajectory, _ = _run(_submit_turn := _calls(_submit()), tool_schemas=[completion_schema()])

    assert trajectory.termination_reason is TerminationReason.AGENT_SUBMITTED
    assert trajectory.status is TrialStatus.COMPLETED
    assert _submit_turn.tool_calls[0].name == SUBMIT_TOOL_NAME


def test_it_ends_the_trial_on_the_turn_it_is_called_not_at_the_turn_budget():
    """The point of the tool: a model that would otherwise burn every turn."""
    trajectory, _ = _run(
        _calls(ToolCall(id="c1", name="calculator", arguments={"expression": "1+1"})),
        _calls(_submit(summary="done")),
        tool_schemas=[completion_schema()],
        registry=_registry_with(CalculatorTool()),
        max_turns=50,
    )

    assert trajectory.termination_reason is TerminationReason.AGENT_SUBMITTED
    assert sum(1 for m in trajectory.messages if m.role.value == "assistant") == 2


def test_the_reason_names_the_agent_and_not_the_user():
    """``user_stop`` describes the simulator closing the dialogue and
    ``agent_done`` the agent falling silent with nobody left to prompt it.
    Neither describes an agent that said so itself, so the signal carries a
    reason of its own — otherwise no trajectory could be asked how it ended."""
    submitted, _ = _run(_calls(_submit()), tool_schemas=[completion_schema()])
    stopped, _ = _run(_text("Anything else?"), user_reply="###STOP###")
    fell_silent, _ = _run(_text("All set."), interaction_mode="agent_only")

    reasons = {
        submitted.termination_reason,
        stopped.termination_reason,
        fell_silent.termination_reason,
    }
    assert reasons == {
        TerminationReason.AGENT_SUBMITTED,
        TerminationReason.USER_STOP,
        TerminationReason.AGENT_DONE,
    }


def test_the_trajectory_records_which_tool_ended_it():
    trajectory, _ = _run(_calls(_submit()), tool_schemas=[completion_schema()])

    closing = trajectory.messages[-1]
    assert closing.role.value == "system"
    assert SUBMIT_TOOL_NAME in closing.content


def test_a_sibling_call_on_the_completing_turn_does_not_execute():
    """Termination is decided before any tool on the turn runs, so a call made
    alongside the signal genuinely never happened. Recording it would credit
    the agent with work the substrate never did."""
    registry = _registry_with(CalculatorTool())
    trajectory, runner = _run(
        _calls(
            ToolCall(id="c1", name="calculator", arguments={"expression": "1+1"}),
            _submit(),
        ),
        tool_schemas=[completion_schema()],
        registry=registry,
    )

    assert trajectory.termination_reason is TerminationReason.AGENT_SUBMITTED
    assert runner.tool_call_recorder.recorded_for(ToolExecutorIdentity.AGENT) == ()


# ---------------------------------------------------------------------------
# Inert where it was not enabled — the safety lock
# ---------------------------------------------------------------------------


def test_a_run_that_did_not_enable_it_is_untouched_by_the_same_call():
    """Same scripted agent, same script, no completion tool on the surface.

    The name is not special: with the tool absent from the offered surface it
    is an unknown tool like any other, the loop answers the call with a failure
    and carries on to the turn budget."""
    enabled, _ = _run(_calls(_submit()), tool_schemas=[completion_schema()], max_turns=4)
    disabled, _ = _run(_calls(_submit()), tool_schemas=[], max_turns=4)

    assert enabled.termination_reason is TerminationReason.AGENT_SUBMITTED
    assert disabled.termination_reason is TerminationReason.MAX_TURNS


def test_an_unenabled_completion_call_is_indistinguishable_from_any_unknown_tool():
    """The strong form of the lock: with the tool not enabled, the transcript a
    ``submit`` call produces differs from an arbitrary unknown tool's only in
    the name that was called."""
    submit_run, _ = _run(_calls(_submit()), tool_schemas=[], max_turns=4)
    unknown_run, _ = _run(
        _calls(ToolCall(id="submit-1", name="no_such_tool", arguments={})),
        tool_schemas=[],
        max_turns=4,
    )

    assert submit_run.termination_reason == unknown_run.termination_reason
    assert submit_run.status == unknown_run.status
    assert [m.role for m in submit_run.messages] == [m.role for m in unknown_run.messages]


def test_a_conversational_trial_still_ends_at_its_user_simulator():
    """The shape ~every shipped pack runs: no completion tool anywhere near it."""
    trajectory, runner = _run(_text("Anything else?"), user_reply="###STOP###")

    assert runner._completion_tools == frozenset()
    assert trajectory.termination_reason is TerminationReason.USER_STOP


def test_the_enabled_set_is_read_off_the_offered_tool_surface():
    """Not off the run config: the surface the model is shown and the set that
    can end its episode are the same list, so they cannot disagree."""
    assert _enabled_completion_tools([]) == frozenset()
    assert _enabled_completion_tools([CalculatorTool().get_schema()]) == frozenset()
    assert _enabled_completion_tools([completion_schema()]) == frozenset({SUBMIT_TOOL_NAME})


# ---------------------------------------------------------------------------
# The tool itself
# ---------------------------------------------------------------------------


def test_the_schema_requires_no_argument():
    """A signal that costs a sentence to send is a signal some models will not
    send, and what this tool measures is whether a model reaches for it."""
    parameters = SubmitTool().get_schema()["function"]["parameters"]

    assert parameters["required"] == []
    assert set(parameters["properties"]) == {"summary"}


def test_the_description_tells_the_model_what_calling_it_costs():
    """The schema is the only channel the agent learns about this tool through
    — no system prompt mentions it — so the finality has to be stated here."""
    description = SubmitTool().get_schema()["function"]["description"]

    assert "ends the episode" in description


def test_executing_it_claims_nothing_about_the_work():
    """Reached only outside a loop the completion policy governs. It must not
    read as a verdict: nothing here has inspected the task."""
    result = SubmitTool().execute(summary="I rewrote the parser.")

    assert result.success
    assert result.metadata == {"summary": "I rewrote the parser."}


async def test_the_runner_can_reconstruct_it_like_any_other_builtin():
    """A registered tool the runner could not build would fail the trial at
    ``RegisterTrial``, before the agent ever saw the surface — and no
    completion policy governs that path, so the tool has to stand on its own."""
    from tolokaforge.runner.models import ToolSchema
    from tolokaforge.runner.tool_factory import ToolFactory

    function = SubmitTool().get_schema()["function"]
    schema = ToolSchema(
        name=SUBMIT_TOOL_NAME,
        description=function["description"],
        parameters=function["parameters"],
        category="compute",
        timeout_s=5.0,
    )

    tools = ToolFactory(db_client=None, trial_id="completion:0").reconstruct_tools(
        [schema.model_dump()]
    )

    assert sorted(tools.agent_tools) == [SUBMIT_TOOL_NAME]
    assert await tools.agent_tools[SUBMIT_TOOL_NAME].execute({}) == "Completion signal recorded."


def _registry_with(*tools) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry
