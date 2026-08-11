"""One scripted tool-call sequence, recorded by each substrate, builds one timeline.

This is what makes "a single shared timeline builder" a fact rather than a claim.
The same tool calls are driven through **each substrate's own real recording
path** — the core substrate's ``ToolCallingLoop`` + ``ToolExecutor`` +
``TrialToolCallRecorder`` over a registered tool, and the runner's real
``ExecuteTool`` handler recording into ``TrialContextRuntime`` — and the runner's
message view is the wire round-trip of the core trace, exactly as ``GradeTrial``
receives it. Both are then fed to :func:`build_trial_timeline` and the resulting
events compared field by field. No mocks, no fakes, no LLM.

The runner half executes the ids, tool names and arguments **the loop produced**,
read off the core substrate's message view, because that is what the runner
receives in production. That coupling is what lets one assertion here fail on the
*runtime* rather than on the builder: the scripted trial reuses one tool-call id
across two turns, the loop assigns an episode-unique id at ingestion, and
``test_both_substrates_record_the_ids_the_loop_assigned`` reads the ids as
recorded — before any timeline exists. Every claim made on a built timeline is
blind to that, since the builder derives its own key per view and would re-key a
raw duplicate on both substrates alike.

``latency_seconds`` is excluded from the equality: two substrates executing the
same tool cannot measure the same wall time. What is asserted instead is that
every ``TOOL_RESULT`` on **both** substrates carries a positive float — the
regression being a substrate that records a constant ``0.0``, which no
cross-substrate equality could ever catch.

The scripted trial fails five ways as well as succeeding — a tool signalling its
own failure with and without a message, a tool raising with and without one, and
a call to a tool neither substrate has — because a failed call's ``result`` text
is what a matcher reads. Those five are compared as absolute texts, not merely
for cross-substrate equality: the two substrates share the helper and the
constant that word a failure, and a bug in something shared is symmetric, so
equality alone would hold while both sides were wrong.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.core.grading.trace_timeline import (
    TraceEventKind,
    TrialTimeline,
    build_trial_timeline,
)
from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import LoopConfig, MetricsSink, ToolCallingLoop
from tolokaforge.core.models import (
    Message,
    MessageRole,
    RecordedToolCall,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import BuiltinGenericToolWrapper
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.canonical

_AGENT_POLICY = "You are a refund agent."

# One assistant generation per tuple, each call ``(id, tool, arguments)``. This
# is the script the core loop's generations replay — it is not a source of wire
# ids for the runner half, which takes those from what the loop produced.
#
# The first turn declares two calls whose arguments are identical, including a
# ``token``-named one: they are distinguishable only by id, and their results
# differ, so a positional join would swap them. The third turn fails five ways.
# The last turn **reuses ``call_A``**, the shape a provider that numbers its
# calls within a turn produces — with a different ``order_id`` so a mis-pairing
# shows up in ``arguments`` and ``result``, not only in the id.
_TURNS: tuple[tuple[tuple[str, str, dict[str, Any]], ...], ...] = (
    (
        ("call_A", "refund", {"order_id": "42", "token": "sk-live-secret"}),
        ("call_B", "refund", {"order_id": "42", "token": "sk-live-secret"}),
    ),
    (("call_C", "refund", {"order_id": "77", "token": "sk-live-secret"}),),
    (
        ("call_D", "calculator", {"mode": "signals_failure"}),
        ("call_E", "calculator", {"mode": "signals_failure_silently"}),
        ("call_F", "calculator", {"mode": "raises"}),
        ("call_G", "calculator", {"mode": "raises_silently"}),
        ("call_H", "nope", {}),
    ),
    (("call_A", "refund", {"order_id": "99", "token": "sk-live-secret"}),),
)

# The ids the loop assigns, in execution order — ``call_A``'s second occurrence
# disambiguated. Pinned absolutely because it is the value on disk and on the
# wire, not merely something the two substrates happen to agree on.
_ASSIGNED_CALL_IDS = (
    "call_A",
    "call_B",
    "call_C",
    "call_D",
    "call_E",
    "call_F",
    "call_G",
    "call_H",
    "call_A#2",
)

# What makes a scripted call fail, rather than which id it carries: the ids now
# come from the loop, which rewrites one of them, so keying the expectation on
# an id would key it on the thing under test.
_FAILING_MODES = frozenset(
    {"signals_failure", "signals_failure_silently", "raises", "raises_silently"}
)
_UNREGISTERED_TOOL = "nope"


def _is_scripted_failure(call: ToolCall) -> bool:
    return call.name == _UNREGISTERED_TOOL or call.arguments.get("mode") in _FAILING_MODES


# Everything the two substrates must agree on. ``latency_seconds`` is checked
# separately, per this module's docstring.
_COMPARED_FIELDS = (
    "position",
    "turn_index",
    "kind",
    "text",
    "call_id",
    "tool_name",
    "executor",
    "arguments",
    "status",
    "result",
)


class _Refunder:
    """The tool implementation both substrates run.

    Its output depends on how many times it has been called, so two calls with
    identical arguments produce different results — which is what makes the id
    join load-bearing rather than decorative.
    """

    def __init__(self) -> None:
        self._calls = 0

    def __call__(self, arguments: dict[str, Any]) -> str:
        self._calls += 1
        return json.dumps(
            {"refunded": arguments["order_id"], "call_index": self._calls}, sort_keys=True
        )


class _RefundTool(Tool):
    """``_Refunder`` registered the way the core substrate registers tools."""

    def __init__(self, impl: _Refunder) -> None:
        super().__init__("refund", "Refund an order")
        self._impl = impl

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}, "token": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output=self._impl(kwargs))


class _ScriptedFailures(Tool):
    """The failing tool implementation both substrates run.

    Named for a builtin because the runner reaches it through the real
    ``BuiltinGenericToolWrapper``, which only constructs tools the builtin
    registry knows — and that wrapper, not the service's raw-callable branch,
    is how a builtin tool's signalled failure reaches the runner in production.
    """

    def __init__(self) -> None:
        super().__init__("calculator", "Fails the way its argument asks for")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "required": ["mode"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        mode = kwargs["mode"]
        if mode == "signals_failure":
            return ToolResult(success=False, output="", error="order 42 is already refunded")
        if mode == "signals_failure_silently":
            return ToolResult(success=False, output="", error="")
        if mode == "raises":
            raise RuntimeError("kaboom")
        if mode == "raises_silently":
            raise ValueError()
        raise AssertionError(f"unscripted failure mode {mode!r}")


class _NullSink(MetricsSink):
    def record_generation(self, result: GenerationResult) -> None:
        pass

    def record_tool_call(self) -> None:
        pass


class _ScriptedClient:
    def __init__(self, results: list[GenerationResult]) -> None:
        self._results = list(results)

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._results.pop(0)


def _generation(tool_calls: list[ToolCall], text: str = "") -> GenerationResult:
    return GenerationResult(text=text, tool_calls=tool_calls, usage=Usage(prompt_tokens=1))


def _core_substrate() -> tuple[list[Message], tuple[RecordedToolCall, ...]]:
    """Drive the scripted calls through the core substrate's real recording path."""
    registry = ToolRegistry()
    registry.register(_RefundTool(_Refunder()))
    registry.register(_ScriptedFailures())
    recorder = TrialToolCallRecorder()
    messages = [Message(role=MessageRole.USER, content="refund order 42, twice, then 77")]
    ToolCallingLoop(
        llm_client=_ScriptedClient(
            [
                _generation(
                    [
                        ToolCall(id=call_id, name=tool_name, arguments=args)
                        for call_id, tool_name, args in turn
                    ]
                )
                for turn in _TURNS
            ]
            + [_generation([], text="All three refunds are done.")]
        ),
        tool_executor=ToolExecutor(registry),
        tool_schemas=[],
        config=LoopConfig(max_turns=len(_TURNS) + 1, episode_timeout_s=10_000),
        metrics=_NullSink(),
        should_terminate=lambda result, turn, messages: None,
        logger=get_logger("trace-timeline-parity", strict=False),
        recorder=recorder,
    ).run(_AGENT_POLICY, messages, time.time())
    return messages, recorder.recorded


def _runner_substrate(
    runner_service, mock_grpc_context, request_name: str, core_messages: list[Message]
) -> tuple[RecordedToolCall, ...]:
    """Drive the same calls through the runner's real ``ExecuteTool`` handler.

    The calls come from the core substrate's own message view — the ids, tool
    names and arguments the loop produced — because that is exactly what the
    runner receives in production. Reading them from ``_TURNS`` again would make
    the runner a second, independent source of wire ids, and then no assertion
    anywhere could tell whether the loop assigned anything: the timeline derives
    its keys per view, so it would re-key a raw duplicate on both sides alike.
    """
    trial_id = f"{request_name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    agent_tools = runner_service.trials[trial_id].agent_tools
    agent_tools["refund"] = _Refunder()
    agent_tools["calculator"] = _wrapped_failing_tool()

    for call in (call for message in core_messages for call in (message.tool_calls or [])):
        response = runner_service.ExecuteTool(
            execute_request(
                trial_id,
                call.name,
                arguments_json=json.dumps(call.arguments),
                call_id=call.id,
            ),
            mock_grpc_context,
        )
        failed = response.status != pb2.EXECUTION_STATUS_SUCCESS
        assert failed is _is_scripted_failure(call), response.error_message
    return runner_service.trials[trial_id].recorded


def _wrapped_failing_tool() -> BuiltinGenericToolWrapper:
    """``_ScriptedFailures`` behind the wrapper the runner builds for a builtin."""
    wrapper = BuiltinGenericToolWrapper(
        ToolSchema(
            name="calculator",
            description="Fails the way its argument asks for",
            parameters={"type": "object"},
        )
    )
    wrapper._tool = _ScriptedFailures()
    return wrapper


def _wire_round_trip(messages: list[Message]) -> list[Message]:
    """The message view as ``GradeTrial`` receives it on the runner substrate."""
    now = datetime.now(tz=timezone.utc)
    payload = encode_transcript_wire(
        Trajectory(
            task_id="trace_timeline_parity",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            messages=messages,
        ),
        agent_system_prompt=_AGENT_POLICY,
    )
    policy, transcript = split_leading_system_message(json.loads(payload))
    assert policy == _AGENT_POLICY
    return decode_transcript_wire(transcript)


def _compared(timeline: TrialTimeline) -> list[dict[str, Any]]:
    return [
        {field: getattr(event, field) for field in _COMPARED_FIELDS} for event in timeline.events
    ]


def _results(timeline: TrialTimeline) -> list:
    return [event for event in timeline.events if event.kind is TraceEventKind.TOOL_RESULT]


@dataclass(frozen=True)
class _Recordings:
    """What each substrate recorded, before any timeline is built over it."""

    core_messages: list[Message]
    core_records: tuple[RecordedToolCall, ...]
    runner_records: tuple[RecordedToolCall, ...]


@pytest.fixture
def substrate_recordings(runner_service, mock_grpc_context, request) -> _Recordings:
    core_messages, core_records = _core_substrate()
    return _Recordings(
        core_messages=core_messages,
        core_records=core_records,
        runner_records=_runner_substrate(
            runner_service, mock_grpc_context, request.node.name, core_messages
        ),
    )


@pytest.fixture
def substrate_timelines(substrate_recordings: _Recordings) -> tuple[TrialTimeline, ...]:
    return (
        build_trial_timeline(
            substrate_recordings.core_messages,
            substrate_recordings.core_records,
            TerminationReason.MAX_TURNS,
        ),
        build_trial_timeline(
            _wire_round_trip(substrate_recordings.core_messages),
            substrate_recordings.runner_records,
            TerminationReason.MAX_TURNS,
        ),
    )


def test_both_substrates_record_the_ids_the_loop_assigned(
    substrate_recordings: _Recordings,
) -> None:
    """The only assertion in this module that can fail when the loop stops assigning.

    Every claim made on a *built* timeline is blind to it: the timeline derives
    its join key per view by occurrence, so a raw duplicate would be re-keyed on
    both substrates identically and the field-by-field comparison would still
    hold — as would an absolute ``#2`` pin read off the events. What only the ids
    *as recorded* can show is that the loop assigned before the id crossed gRPC,
    so both substrates recorded the same unambiguous value.
    """
    core_ids = [record.call_id for record in substrate_recordings.core_records]
    runner_ids = [record.call_id for record in substrate_recordings.runner_records]

    assert core_ids == runner_ids
    assert len(set(core_ids)) == len(core_ids), core_ids
    assert tuple(core_ids) == _ASSIGNED_CALL_IDS


def test_the_two_substrates_build_the_same_events(substrate_timelines) -> None:
    core, runner = substrate_timelines

    assert _compared(core) == _compared(runner)


def test_the_compared_timeline_is_the_scripted_trial(substrate_timelines) -> None:
    """Without this the equality above would also hold for two empty timelines."""
    for timeline in substrate_timelines:
        assert timeline.message_view_present is True
        assert timeline.records_present is True
        assert [event.kind for event in timeline.events] == (
            [
                TraceEventKind.USER_MESSAGE,
                TraceEventKind.ASSISTANT_MESSAGE,
                TraceEventKind.TOOL_CALL,
                TraceEventKind.TOOL_RESULT,
                TraceEventKind.TOOL_CALL,
                TraceEventKind.TOOL_RESULT,
                TraceEventKind.ASSISTANT_MESSAGE,
                TraceEventKind.TOOL_CALL,
                TraceEventKind.TOOL_RESULT,
                TraceEventKind.ASSISTANT_MESSAGE,
            ]
            + [TraceEventKind.TOOL_CALL, TraceEventKind.TOOL_RESULT] * 5
            + [
                TraceEventKind.ASSISTANT_MESSAGE,
                TraceEventKind.TOOL_CALL,
                TraceEventKind.TOOL_RESULT,
                TraceEventKind.ASSISTANT_MESSAGE,
            ]
        )
        assert {event.call_id: event.result for event in _results(timeline)} == {
            "call_A": '{"call_index": 1, "refunded": "42"}',
            "call_B": '{"call_index": 2, "refunded": "42"}',
            "call_C": '{"call_index": 3, "refunded": "77"}',
            "call_D": "order 42 is already refunded",
            "call_E": "Tool returned failure with no error message",
            "call_F": "kaboom",
            "call_G": "ValueError",
            "call_H": "Tool 'nope' not found",
            "call_A#2": '{"call_index": 4, "refunded": "99"}',
        }
        assert {event.call_id: event.status for event in _results(timeline)} == {
            "call_A": ToolExecutionStatus.SUCCESS,
            "call_B": ToolExecutionStatus.SUCCESS,
            "call_C": ToolExecutionStatus.SUCCESS,
            "call_D": ToolExecutionStatus.ERROR,
            "call_E": ToolExecutionStatus.ERROR,
            "call_F": ToolExecutionStatus.ERROR,
            "call_G": ToolExecutionStatus.ERROR,
            "call_H": ToolExecutionStatus.TOOL_NOT_FOUND,
            "call_A#2": ToolExecutionStatus.SUCCESS,
        }
        assert all(event.executor is ToolExecutorIdentity.AGENT for event in _results(timeline))


def test_both_substrates_record_the_token_argument_raw(substrate_timelines) -> None:
    """A matcher on a ``token``-named argument must mean the same thing on both
    substrates, which holds only because neither redacts at record time."""
    for timeline in substrate_timelines:
        calls = [
            event
            for event in timeline.events
            if event.kind is TraceEventKind.TOOL_CALL and event.tool_name == "refund"
        ]
        assert [event.arguments["token"] for event in calls] == ["sk-live-secret"] * 4


def test_both_substrates_measure_a_real_latency(substrate_timelines) -> None:
    """Excluded from the equality above, so this is the only guard on the field:
    a substrate recording a constant ``0.0`` is caught here and nowhere else.

    A call the runner refuses before execution ran for no time and is stamped
    ``0.0``, so the guard covers the calls that ran — named here, so narrowing
    it further would fail rather than pass vacuously."""
    for timeline in substrate_timelines:
        executed = [
            event
            for event in _results(timeline)
            if event.status is not ToolExecutionStatus.TOOL_NOT_FOUND
        ]
        assert [event.call_id for event in executed] == [
            "call_A",
            "call_B",
            "call_C",
            "call_D",
            "call_E",
            "call_F",
            "call_G",
            "call_A#2",
        ]
        latencies = [event.latency_seconds for event in executed]
        assert all(isinstance(value, float) for value in latencies)
        assert all(value > 0.0 for value in latencies), latencies
