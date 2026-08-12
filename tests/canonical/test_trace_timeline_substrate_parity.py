"""One scripted tool-call sequence, recorded by each substrate, builds one timeline.

This is what makes "a single shared timeline builder" a fact rather than a claim.
The same three tool calls are driven through **each substrate's own real
recording path** — the core substrate's ``ToolCallingLoop`` + ``ToolExecutor`` +
``TrialToolCallRecorder`` over a registered tool, and the runner's real
``ExecuteTool`` handler recording into ``TrialContextRuntime`` — and the runner's
message view is the wire round-trip of the core trace, exactly as ``GradeTrial``
receives it. Both are then fed to :func:`build_trial_timeline` and the resulting
events compared field by field. No mocks, no fakes, no LLM.

``latency_seconds`` is excluded from the equality: two substrates executing the
same tool cannot measure the same wall time. What is asserted instead is that
every ``TOOL_RESULT`` on **both** substrates carries a positive float — the
regression being a substrate that records a constant ``0.0``, which no
cross-substrate equality could ever catch.

The tool succeeds on every call, because a *failed* call's recorded text is the
executing layer's own and the two layers word failure differently: the core
executor records the tool's ``ToolResult.error`` while the runner wraps the raised
exception as ``Tool error: <Type>: <msg>``. Statuses agree; the failure text does
not, so a ``result`` predicate on a failed call is not substrate-portable.
"""

from __future__ import annotations

import json
import time
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
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    classify_loop_error,
)
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
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.canonical

_AGENT_POLICY = "You are a refund agent."

# One assistant generation per tuple. The first declares two calls whose
# arguments are identical, including a ``token``-named one: they are
# distinguishable only by id, and their results differ, so a positional join
# would swap them.
_TURNS: tuple[tuple[tuple[str, dict[str, Any]], ...], ...] = (
    (
        ("call_A", {"order_id": "42", "token": "sk-live-secret"}),
        ("call_B", {"order_id": "42", "token": "sk-live-secret"}),
    ),
    (("call_C", {"order_id": "77", "token": "sk-live-secret"}),),
)

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


def _classify_no_patterns(exc: Exception) -> TerminationDecision:
    return classify_loop_error(exc, ())


def _core_substrate() -> tuple[list[Message], tuple[RecordedToolCall, ...]]:
    """Drive the scripted calls through the core substrate's real recording path."""
    registry = ToolRegistry()
    registry.register(_RefundTool(_Refunder()))
    recorder = TrialToolCallRecorder()
    messages = [Message(role=MessageRole.USER, content="refund order 42, twice, then 77")]
    ToolCallingLoop(
        llm_client=_ScriptedClient(
            [
                _generation(
                    [ToolCall(id=call_id, name="refund", arguments=args) for call_id, args in turn]
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
        classify_error=_classify_no_patterns,
        logger=get_logger("trace-timeline-parity", strict=False),
        recorder=recorder,
    ).run(_AGENT_POLICY, messages, time.time())
    return messages, recorder.recorded


def _runner_substrate(
    runner_service, mock_grpc_context, request_name: str
) -> tuple[RecordedToolCall, ...]:
    """Drive the same calls through the runner's real ``ExecuteTool`` handler."""
    trial_id = f"{request_name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    runner_service.trials[trial_id].agent_tools["refund"] = _Refunder()

    for turn in _TURNS:
        for call_id, arguments in turn:
            response = runner_service.ExecuteTool(
                execute_request(
                    trial_id,
                    "refund",
                    arguments_json=json.dumps(arguments),
                    call_id=call_id,
                ),
                mock_grpc_context,
            )
            assert response.error_message == "", response.error_message
    return runner_service.trials[trial_id].recorded


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


@pytest.fixture
def substrate_timelines(runner_service, mock_grpc_context, request) -> tuple[TrialTimeline, ...]:
    core_messages, core_records = _core_substrate()
    runner_records = _runner_substrate(runner_service, mock_grpc_context, request.node.name)
    return (
        build_trial_timeline(core_messages, core_records, TerminationReason.MAX_TURNS),
        build_trial_timeline(
            _wire_round_trip(core_messages), runner_records, TerminationReason.MAX_TURNS
        ),
    )


def test_the_two_substrates_build_the_same_events(substrate_timelines) -> None:
    core, runner = substrate_timelines

    assert _compared(core) == _compared(runner)


def test_the_compared_timeline_is_the_scripted_trial(substrate_timelines) -> None:
    """Without this the equality above would also hold for two empty timelines."""
    for timeline in substrate_timelines:
        assert timeline.message_view_present is True
        assert timeline.records_present is True
        assert [event.kind for event in timeline.events] == [
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
        assert {event.call_id: event.result for event in _results(timeline)} == {
            "call_A": '{"call_index": 1, "refunded": "42"}',
            "call_B": '{"call_index": 2, "refunded": "42"}',
            "call_C": '{"call_index": 3, "refunded": "77"}',
        }
        assert all(event.status is ToolExecutionStatus.SUCCESS for event in _results(timeline))
        assert all(event.executor is ToolExecutorIdentity.AGENT for event in _results(timeline))


def test_both_substrates_record_the_token_argument_raw(substrate_timelines) -> None:
    """A matcher on a ``token``-named argument must mean the same thing on both
    substrates, which holds only because neither redacts at record time."""
    for timeline in substrate_timelines:
        calls = [event for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL]
        assert [event.arguments["token"] for event in calls] == ["sk-live-secret"] * 3


def test_both_substrates_measure_a_real_latency(substrate_timelines) -> None:
    """Excluded from the equality above, so this is the only guard on the field:
    a substrate recording a constant ``0.0`` is caught here and nowhere else."""
    for timeline in substrate_timelines:
        latencies = [event.latency_seconds for event in _results(timeline)]
        assert latencies and all(isinstance(value, float) for value in latencies)
        assert all(value > 0.0 for value in latencies), latencies
