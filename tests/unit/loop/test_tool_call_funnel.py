"""The obligations :class:`ToolCallFunnel` discharges, one test each.

The funnel is the single path from a parsed tool call to its executed, recorded
result. Two of the obligations it carries are invisible at write time and wrong
at grade time, so each is pinned against the real join rather than against the
funnel's own shape:

* the episode-unique id the assistant message, the executor and the record all
  carry — pinned by running two calls to the *same* tool through the built-in
  loop and grading the pair back out of ``build_trial_timeline``, which is the
  case that mis-joins silently rather than raising;
* the ``Error: `` prefix a failed call's ``role: tool`` content carries, without
  which the failure reads as a success to a ``result:`` trace check.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tolokaforge.core.grading.trace_timeline import TraceEventKind, build_trial_timeline
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import (
    LoopConfig,
    LoopOutcome,
    MetricsSink,
    ToolCallFunnel,
    ToolCallingLoop,
    UnassignedToolCallError,
    classify_loop_error,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ToolCall,
    ToolExecutionStatus,
)
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.core.tool_message_format import TOOL_ERROR_MESSAGE_PREFIX
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


class _EchoExecutor:
    """Returns the argument it was handed, so a mis-joined result is visible."""

    def __init__(self, failing_ids: frozenset[str] = frozenset()) -> None:
        self.failing_ids = failing_ids
        self.executed: list[tuple[str, str]] = []

    def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
        self.executed.append((tool_name, call_id))
        who = (arguments or {}).get("employee_id", "?")
        if call_id in self.failing_ids:
            return ToolResult(success=False, output="", error=f"no such employee {who}")
        return ToolResult(success=True, output=f"record for {who}")


class _Sink(MetricsSink):
    def __init__(self) -> None:
        self.tool_calls = 0

    def record_generation(self, result: GenerationResult) -> None:
        return None

    def record_tool_call(self) -> None:
        self.tool_calls += 1


class _ScriptedClient:
    def __init__(self, results: list[GenerationResult]) -> None:
        self._results = list(results)

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._results.pop(0)


def _call(call_id: str, employee_id: str, name: str = "get_employee") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"employee_id": employee_id})


def _turn(*calls: ToolCall) -> GenerationResult:
    return GenerationResult(text="", tool_calls=list(calls), usage=Usage(prompt_tokens=1))


def _funnel(executor: Any, *, recorder=None, call_ids=None) -> ToolCallFunnel:
    return ToolCallFunnel(
        tool_executor=executor,
        call_ids=call_ids or EpisodeUniqueCallIds(),
        metrics=_Sink(),
        logger=get_logger("funnel-test", strict=False),
        recorder=recorder,
    )


def _appender(messages: list[Message]):
    def append(message: Message) -> int:
        messages.append(message)
        return len(messages) - 1

    return append


def _loop(client: _ScriptedClient, executor: Any, recorder, max_turns: int) -> ToolCallingLoop:
    return ToolCallingLoop(
        llm_client=client,
        tool_executor=executor,
        tool_schemas=[],
        config=LoopConfig(max_turns=max_turns, episode_timeout_s=10_000),
        metrics=_Sink(),
        should_terminate=lambda result, turn, messages: None,
        logger=get_logger("loop-test", strict=False),
        classify_error=lambda exc: classify_loop_error(exc, ()),
        recorder=recorder,
        retry_sleep=lambda _s: None,
    )


def test_assign_ids_draws_every_key_from_the_episode_assigner():
    """A raw id the provider repeats within the episode comes back disambiguated."""
    assigner = EpisodeUniqueCallIds()
    funnel = _funnel(_EchoExecutor(), call_ids=assigner)

    first = funnel.assign_ids([_call("get_employee:1", "A")])
    second = funnel.assign_ids([_call("get_employee:1", "B")])

    assert [c.id for c in first] == ["get_employee:1"]
    assert [c.id for c in second] == ["get_employee:1#2"]
    # The same assigner would have produced exactly these keys on its own.
    assert EpisodeUniqueCallIds().assign("get_employee:1") == "get_employee:1"


def test_assigned_id_reaches_the_executor_the_recorder_and_the_tool_message():
    """One key in all four views: the call, the executor, the record, the message."""
    executor = _EchoExecutor()
    recorder = TrialToolCallRecorder()
    funnel = _funnel(executor, recorder=recorder)
    messages: list[Message] = []

    (call,) = funnel.assign_ids([_call("call_abc", "E7")])
    funnel.execute(call, _appender(messages))

    assert executor.executed == [("get_employee", "call_abc")]
    assert [r.call_id for r in recorder.recorded] == ["call_abc"]
    assert messages[0].tool_call_id == "call_abc"
    assert messages[0].role is MessageRole.TOOL


def test_execute_refuses_a_call_the_funnel_never_assigned():
    """An id minted outside the funnel cannot reach the executor."""
    executor = _EchoExecutor()
    funnel = _funnel(executor)
    messages: list[Message] = []

    with pytest.raises(UnassignedToolCallError):
        funnel.execute(_call("hand_rolled", "E7"), _appender(messages))

    assert executor.executed == []
    assert messages == []


def test_failed_call_tool_message_carries_the_error_prefix():
    """Without the prefix every FAILED call reads as SUCCESSFUL to a result: check."""
    executor = _EchoExecutor(failing_ids=frozenset({"call_bad"}))
    recorder = TrialToolCallRecorder()
    funnel = _funnel(executor, recorder=recorder)
    messages: list[Message] = []

    (call,) = funnel.assign_ids([_call("call_bad", "E9")])
    result = funnel.execute(call, _appender(messages))

    assert result.success is False
    assert messages[0].content.startswith(TOOL_ERROR_MESSAGE_PREFIX)
    assert messages[0].content == f"{TOOL_ERROR_MESSAGE_PREFIX}no such employee E9"
    # The record keeps the untruncated, unprefixed failure text.
    assert recorder.recorded[0].status is ToolExecutionStatus.ERROR
    assert recorder.recorded[0].output == "no such employee E9"


def test_completed_environment_error_keeps_its_original_text_and_status():
    class EnvironmentErrorExecutor:
        def execute(self, tool_name, arguments, *, call_id, validation_schema=None):
            return ToolResult(
                success=True,
                output="Error: case tool raised",
                status=ToolExecutionStatus.ENVIRONMENT_ERROR,
            )

    recorder = TrialToolCallRecorder()
    funnel = _funnel(EnvironmentErrorExecutor(), recorder=recorder)
    messages: list[Message] = []
    (call,) = funnel.assign_ids([_call("call_native_error", "E9")])

    funnel.execute(call, _appender(messages))

    assert messages[0].content == "Error: case tool raised"
    assert messages[0].tool_status is ToolExecutionStatus.ENVIRONMENT_ERROR
    assert recorder.recorded[0].status is ToolExecutionStatus.ENVIRONMENT_ERROR
    assert recorder.recorded[0].output == "Error: case tool raised"
    timeline = build_trial_timeline(
        [Message(role=MessageRole.ASSISTANT, tool_calls=[call]), *messages], [], None
    )
    result_events = [event for event in timeline.events if event.kind is TraceEventKind.TOOL_RESULT]
    assert len(result_events) == 1
    assert result_events[0].result == "Error: case tool raised"
    assert result_events[0].status is ToolExecutionStatus.ENVIRONMENT_ERROR


def test_same_tool_calls_executed_out_of_declaration_order_keep_their_own_results():
    """The silent-defect case: same tool, repeated raw id, parallel execution.

    A loop that executes a turn's calls out of declaration order — what a
    parallel executor does — makes the two views of the trial observe the calls
    in different orders. ``build_trial_timeline`` re-derives episode-unique keys
    per view from that view's own order, so on a provider that repeats a raw id
    the derivations disagree, and because both calls name one tool the mis-join
    is silent: ``_require_records_reconcile`` sees the tool it expected and the
    wrong output lands on the wrong call.

    The funnel's pre-assigned ids are already episode-unique, so both
    derivations are the identity and the order cannot matter.
    """
    executor = _EchoExecutor()
    recorder = TrialToolCallRecorder()
    funnel = _funnel(executor, recorder=recorder)

    calls = funnel.assign_ids([_call("get_employee:1", "A"), _call("get_employee:1", "B")])
    messages: list[Message] = [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=list(calls))
    ]
    # The second call completes first — declaration order and execution order part.
    for call in reversed(calls):
        funnel.execute(call, _appender(messages))

    timeline = build_trial_timeline(messages, recorder.recorded, None)
    attributed = {
        event.call_id: event.result
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_RESULT
    }

    assert [c.id for c in calls] == ["get_employee:1", "get_employee:1#2"]
    assert attributed == {
        "get_employee:1": "record for A",
        "get_employee:1#2": "record for B",
    }


def test_built_in_loop_round_trips_repeated_calls_to_one_tool():
    """Two parallel calls to ``get_employee``, then a third under a reused raw id.

    Drives the real engine loop end to end and grades the pair back out, so the
    assistant message, the executor, the record and the ``role: tool`` message
    are pinned against the join rather than against each other.
    """
    executor = _EchoExecutor()
    recorder = TrialToolCallRecorder()
    client = _ScriptedClient(
        [
            _turn(_call("get_employee:1", "A"), _call("get_employee:2", "B")),
            _turn(_call("get_employee:1", "C")),
            GenerationResult(text="done", usage=Usage(prompt_tokens=1)),
        ]
    )
    messages: list[Message] = []
    _loop(client, executor, recorder, max_turns=3).run("sys", messages, time.time())

    timeline = build_trial_timeline(messages, recorder.recorded, None)
    results = {
        event.call_id: event.result
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_RESULT
    }
    declared = {
        event.call_id: event.arguments["employee_id"]
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_CALL
    }

    assert declared == {
        "get_employee:1": "A",
        "get_employee:2": "B",
        "get_employee:1#2": "C",
    }
    assert results == {
        "get_employee:1": "record for A",
        "get_employee:2": "record for B",
        "get_employee:1#2": "record for C",
    }


def test_built_in_loop_executes_through_its_funnel():
    """The engine loop owns one funnel and makes every call through it.

    A funnel whose ``execute`` is never reached would leave the assigner
    untouched, so the loop's own record is the evidence the shared path is the
    production path.
    """
    executor = _EchoExecutor()
    recorder = TrialToolCallRecorder()
    client = _ScriptedClient(
        [
            _turn(_call("get_employee:1", "A")),
            GenerationResult(text="done", usage=Usage(prompt_tokens=1)),
        ]
    )
    loop = _loop(client, executor, recorder, max_turns=2)
    assert isinstance(loop.funnel, ToolCallFunnel)

    messages: list[Message] = []
    loop.run("sys", messages, time.time())

    assert loop.funnel.call_ids is loop.call_ids
    assert loop.funnel.recorder is recorder
    with pytest.raises(UnassignedToolCallError):
        loop.funnel.execute(_call("never_assigned", "Z"), _appender(messages))


def test_excluding_reason_evidence_defaults_to_absent():
    """The built-in loop makes no denominator-exclusion claim it cannot evidence.

    A loop that emits a reason in ``EXCLUDED_TYPED_REASONS`` with this unset has
    its outcome counted as ``ERROR`` rather than dropped from the measured
    denominator, so the default is the safe one.
    """
    executor = _EchoExecutor()
    client = _ScriptedClient([GenerationResult(text="done", usage=Usage(prompt_tokens=1))])
    outcome = _loop(client, executor, None, max_turns=1).run("sys", [], time.time())

    assert isinstance(outcome, LoopOutcome)
    assert outcome.excluding_reason_evidence is None
    assert (
        LoopOutcome(status=outcome.status, termination_reason=None).excluding_reason_evidence
        is None
    )
