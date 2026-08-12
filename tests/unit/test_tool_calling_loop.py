"""Characterization tests for the generic ToolCallingLoop engine.

These pin the engine seams the agent does NOT exercise but a future read-only
judge will rely on:

* running with NO user simulator (``user_turn=None``) — the loop must never
  reference user-simulator concepts, and a no-tool-call turn just re-prompts;
* terminating on a *specific tool call* via the termination callback (how the
  judge will stop when ``submit_report`` is called);
* metrics accumulation through an arbitrary ``MetricsSink``;
* error classification through the shared ``classify_loop_error``.

We use the project's real ``ToolExecutor``-shaped fakes minimally — only a
generate seam and a tool executor are faked, no over-mocking of the loop.
"""

import time

import pytest

from tolokaforge.core.llm.client import GenerationResult, LLMApiTimeoutError
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    classify_loop_error,
)
from tolokaforge.core.models import Message, MessageRole, TerminationReason, ToolCall, TrialStatus
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


class _ScriptedClient:
    """Yields a fixed sequence of GenerationResults, one per generate call."""

    def __init__(self, results: list[GenerationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.calls += 1
        return self._results.pop(0)


class _RecordingExecutor:
    """Minimal ToolExecutor-shaped fake."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict, str]] = []

    def execute(self, tool_name, arguments, *, call_id):
        self.executed.append((tool_name, arguments, call_id))
        return ToolResult(success=True, output=f"ran {tool_name}")


class _CountingSink(MetricsSink):
    def __init__(self) -> None:
        self.generations = 0
        self.tool_calls = 0
        self.prompt_tokens = 0

    def record_generation(self, result: GenerationResult) -> None:
        self.generations += 1
        self.prompt_tokens += result.usage.prompt_tokens

    def record_tool_call(self) -> None:
        self.tool_calls += 1


def _logger():
    return get_logger("loop-test", strict=False)


def _never_terminate(result, turn, messages):
    return None


def _classify_no_patterns(exc: Exception) -> TerminationDecision:
    return classify_loop_error(exc, ())


def _loop(client, *, should_terminate, user_turn=None, max_turns=5, executor=None, sink=None):
    return ToolCallingLoop(
        llm_client=client,
        tool_executor=executor or _RecordingExecutor(),
        tool_schemas=[],
        config=LoopConfig(max_turns=max_turns, episode_timeout_s=10_000),
        metrics=sink or _CountingSink(),
        should_terminate=should_terminate,
        user_turn=user_turn,
        classify_error=_classify_no_patterns,
        logger=_logger(),
    )


def test_no_user_simulator_no_tool_calls_re_prompts_until_max_turns():
    """Judge-shaped: with no user_turn, a no-tool-call turn advances to the next
    turn (re-prompt) rather than terminating, until max_turns is hit."""
    client = _ScriptedClient(
        [GenerationResult(text=f"thinking {i}", usage=Usage(prompt_tokens=1)) for i in range(3)]
    )
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=3).run(
        "sys", messages, time.time()
    )

    assert client.calls == 3
    assert outcome.termination_reason == TerminationReason.MAX_TURNS
    assert outcome.status == TrialStatus.COMPLETED
    # No USER-role message ever appears without a user simulator.
    assert all(m.role != MessageRole.USER for m in messages)


def test_terminates_on_specific_tool_call():
    """Judge-shaped: terminate when a named tool is called (submit_report)."""

    def stop_on_submit(result, turn, messages):
        if any(tc.name == "submit_report" for tc in result.tool_calls):
            return TerminationDecision(
                reason=TerminationReason.AGENT_DONE,
                system_message="report submitted",
            )
        return None

    client = _ScriptedClient(
        [
            GenerationResult(
                text="look first",
                tool_calls=[ToolCall(id="t1", name="get_state", arguments={})],
                usage=Usage(prompt_tokens=2),
            ),
            GenerationResult(
                text="now report",
                tool_calls=[ToolCall(id="t2", name="submit_report", arguments={"score": 1})],
                usage=Usage(prompt_tokens=2),
            ),
        ]
    )
    executor = _RecordingExecutor()
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=stop_on_submit, executor=executor, max_turns=10).run(
        "sys", messages, time.time()
    )

    assert outcome.termination_reason == TerminationReason.AGENT_DONE
    # submit_report terminates BEFORE its own tool execution; only get_state ran.
    assert executor.executed == [("get_state", {}, "t1")]
    assert messages[-1].role == MessageRole.SYSTEM
    assert messages[-1].content == "report submitted"


def test_tool_calls_executed_and_counted_then_loop_continues():
    executor = _RecordingExecutor()
    sink = _CountingSink()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="call two",
                tool_calls=[
                    ToolCall(id="a", name="query", arguments={"q": 1}),
                    ToolCall(id="b", name="query", arguments={"q": 2}),
                ],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    _loop(client, should_terminate=_never_terminate, executor=executor, sink=sink, max_turns=2).run(
        "sys", messages, time.time()
    )

    assert len(executor.executed) == 2
    assert sink.tool_calls == 2
    assert sink.generations == 2
    # Each tool call produced a TOOL message after the assistant message.
    tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) == 2


def test_each_executed_call_carries_its_own_provider_call_id():
    """The executor is handed ``ToolCall.id``, and the result message keys on the
    same id — so a call and its result join on the id, never on position. The two
    calls here differ only in that id and their arguments."""
    executor = _RecordingExecutor()
    client = _ScriptedClient(
        [
            GenerationResult(
                text="refund twice",
                tool_calls=[
                    ToolCall(id="toolu_A", name="refund", arguments={"payment_id": "PAY-1"}),
                    ToolCall(id="toolu_B", name="refund", arguments={"payment_id": "PAY-1"}),
                ],
                usage=Usage(prompt_tokens=5),
            ),
            GenerationResult(text="done", usage=Usage(prompt_tokens=5)),
        ]
    )
    messages: list[Message] = []
    _loop(client, should_terminate=_never_terminate, executor=executor, max_turns=2).run(
        "sys", messages, time.time()
    )

    assert [call_id for _, _, call_id in executor.executed] == ["toolu_A", "toolu_B"]
    assert [m.tool_call_id for m in messages if m.role == MessageRole.TOOL] == [
        "toolu_A",
        "toolu_B",
    ]


def test_episode_timeout_terminates_before_first_generation():
    client = _ScriptedClient([GenerationResult(text="never", usage=Usage())])
    loop = ToolCallingLoop(
        llm_client=client,
        tool_executor=_RecordingExecutor(),
        tool_schemas=[],
        config=LoopConfig(max_turns=5, episode_timeout_s=0),
        metrics=_CountingSink(),
        should_terminate=_never_terminate,
        classify_error=_classify_no_patterns,
        logger=_logger(),
    )
    messages: list[Message] = []
    # start_time in the past so elapsed > 0 immediately.
    outcome = loop.run("sys", messages, time.time() - 100)

    assert outcome.status == TrialStatus.TIMEOUT
    assert outcome.termination_reason == TerminationReason.TIMEOUT
    assert client.calls == 0


def test_generation_error_is_classified_via_shared_classifier():
    class _Boom:
        def generate(self, system, messages, tools, tool_choice="auto", observation=None):
            raise LLMApiTimeoutError("LLM API call timed out")

    messages: list[Message] = []
    outcome = _loop(_Boom(), should_terminate=_never_terminate).run("sys", messages, time.time())

    assert outcome.status == TrialStatus.ERROR
    assert outcome.termination_reason == TerminationReason.API_TIMEOUT
    assert messages[-1].role == MessageRole.SYSTEM


def test_effective_system_prompt_captured_from_first_generation_only():
    client = _ScriptedClient(
        [
            GenerationResult(text="a", usage=Usage(), effective_system_prompt="FIRST"),
            GenerationResult(text="b", usage=Usage(), effective_system_prompt="SECOND"),
        ]
    )
    messages: list[Message] = []
    outcome = _loop(client, should_terminate=_never_terminate, max_turns=2).run(
        "sys", messages, time.time()
    )
    assert outcome.captured_effective_system_prompt == "FIRST"
