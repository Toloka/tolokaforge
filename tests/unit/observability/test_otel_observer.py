"""The OTLP observer synthesises deterministic spans and never blocks (ADR-0046)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,  # noqa: E402
)

from tolokaforge.core.llm.client import GenerationResult  # noqa: E402
from tolokaforge.core.llm.usage import Usage  # noqa: E402
from tolokaforge.core.models import Message, MessageRole, ToolCall  # noqa: E402
from tolokaforge.observability.observer import ModelRef, TrialIdentity  # noqa: E402
from tolokaforge.observability.otel import HARNESS_TAG, OTelTrialObserver, SpanQueue  # noqa: E402
from tolokaforge.tools.registry import ToolResult  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
IDENTITY = TrialIdentity(run_id="toloka-arena/v1/123/1", task_id="T-1", trial_index=0, attempt_id=0)


def _observer(exporter: InMemorySpanExporter, **kwargs) -> tuple[OTelTrialObserver, SpanQueue]:
    queue = SpanQueue(exporter, max_size=kwargs.pop("max_size", 100), batch_size=4, interval_s=0.05)
    observer = OTelTrialObserver(
        queue=queue,
        label="gpt6_astra",
        session_id="toloka-arena/v1/gpt6_astra/gpt6_astra/123",
        tags=("config:gpt6_astra", "domain:ots_19_airlines"),
        metadata={"model_stem": "gpt6_astra"},
        **kwargs,
    )
    return observer, queue


def _attrs(span) -> dict:
    return dict(span.attributes)


class _Trajectory:
    """The slice of ``Trajectory`` the root span reads."""

    def __init__(self, messages, grade=None, status="completed", termination="user_stop"):
        self.messages = messages
        self.grade = grade
        self.status = status
        self.termination_reason = termination
        self.grading_error = None
        self.start_ts = T0
        self.end_ts = T0 + timedelta(seconds=30)
        self.metrics = None


class _Grade:
    binary_pass = True
    score = 1.0


def test_one_trial_produces_root_generation_and_tool_spans_with_contract_ids() -> None:
    exporter = InMemorySpanExporter()
    observer, queue = _observer(exporter)
    observer.trial_started(
        IDENTITY, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
    )
    request = [Message(role=MessageRole.USER, content="hello", ts=T0)]
    result = GenerationResult(
        text="",
        tool_calls=[
            ToolCall(id="c1", name="shell", arguments={"cmd": "ls", "api_key": "sk-secret"})
        ],
        usage=Usage(prompt_tokens=100, completion_tokens=20),
        latency_s=1.5,
        cost_usd=0.01,
    )
    observer.generation(
        IDENTITY,
        role="agent",
        index=1,
        turn=0,
        request=request,
        result=result,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=1.5),
    )
    tool_result = ToolResult(success=True, output="file.txt", duration_s=0.2)
    observer.tool_call(
        IDENTITY,
        role="agent",
        index=2,
        call=result.tool_calls[0],
        result=tool_result,
        started_at=T0 + timedelta(seconds=2),
        ended_at=T0 + timedelta(seconds=2.2),
    )
    messages = request + [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=result.tool_calls, ts=T0),
        Message(role=MessageRole.TOOL, content="file.txt", tool_call_id="c1", ts=T0),
        Message(role=MessageRole.ASSISTANT, content="done", ts=T0),
    ]
    observer.trial_finished(IDENTITY, trajectory=_Trajectory(messages, grade=_Grade()))
    receipt = observer.run_finished()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert set(spans) == {"assistant turn 1", "tool: shell", "trial T-1/0"}
    assert receipt.spans_exported == 3 and receipt.spans_dropped == 0 and receipt.flushed

    trace_int = int(IDENTITY.trace_id, 16)
    root = spans["trial T-1/0"]
    assert root.context.trace_id == trace_int and root.parent is None
    assert format(root.context.span_id, "016x") == IDENTITY.observation_id("root", 0)
    gen = spans["assistant turn 1"]
    assert format(gen.context.span_id, "016x") == IDENTITY.observation_id("gen", 1)
    assert format(gen.parent.span_id, "016x") == IDENTITY.observation_id("root", 0)
    tool = spans["tool: shell"]
    assert format(tool.context.span_id, "016x") == IDENTITY.observation_id("tool", 2)

    gen_attrs = _attrs(gen)
    assert gen_attrs["langfuse.observation.type"] == "generation"
    assert gen_attrs["langfuse.observation.model.name"] == "openai/gpt-6-astra"
    assert json.loads(gen_attrs["langfuse.observation.usage_details"]) == {
        "input": 100,
        "output": 20,
        "total": 120,
    }
    assert json.loads(gen_attrs["langfuse.observation.cost_details"]) == {"total": 0.01}
    assert gen_attrs["langfuse.trace.name"] == "gpt6_astra/T-1"
    assert gen_attrs["langfuse.session.id"] == "toloka-arena/v1/gpt6_astra/gpt6_astra/123"
    assert list(gen_attrs["langfuse.trace.tags"]) == [
        HARNESS_TAG,
        "model:openai/gpt-6-astra",
        "config:gpt6_astra",
        "domain:ots_19_airlines",
    ]
    assert "sk-secret" not in gen_attrs["langfuse.observation.output"]  # redacted tool arguments

    tool_attrs = _attrs(tool)
    assert tool_attrs["langfuse.observation.type"] == "span"
    assert "sk-secret" not in tool_attrs["langfuse.observation.input"]
    assert tool_attrs["langfuse.observation.output"] == "file.txt"

    root_attrs = _attrs(root)
    assert root_attrs["langfuse.trace.metadata.pass"] is True
    assert root_attrs["langfuse.trace.metadata.score"] == 1.0
    assert root_attrs["langfuse.trace.metadata.trace_time_source"] == "live"
    assert root_attrs["langfuse.trace.metadata.model_name"] == "openai/gpt-6-astra"
    assert root_attrs["langfuse.trace.metadata.model_stem"] == "gpt6_astra"
    assert root_attrs["langfuse.trace.metadata.attempt"] == 0
    assert (
        root_attrs["langfuse.trace.input"] == "hello"
        and root_attrs["langfuse.trace.output"] == "done"
    )
    assert root.start_time == int(T0.timestamp() * 1e9)


def test_same_identity_twice_yields_the_same_ids() -> None:
    a = TrialIdentity(run_id="r", task_id="T", trial_index=2, attempt_id=1)
    b = TrialIdentity(run_id="r", task_id="T", trial_index=2, attempt_id=1)
    assert a.trace_id == b.trace_id and a.observation_id("gen", 5) == b.observation_id("gen", 5)


def test_failed_tool_call_is_an_error_span() -> None:
    exporter = InMemorySpanExporter()
    observer, _ = _observer(exporter)
    call = ToolCall(id="c1", name="shell", arguments={})
    observer.tool_call(
        IDENTITY,
        role="agent",
        index=2,
        call=call,
        result=ToolResult(success=False, output="", error="boom"),
        started_at=T0,
        ended_at=T0,
    )
    observer.run_finished()
    (span,) = exporter.get_finished_spans()
    assert _attrs(span)["langfuse.observation.level"] == "ERROR"
    assert span.status.status_code.name == "ERROR"


def test_full_queue_drops_and_counts_instead_of_blocking() -> None:
    class _Stuck(InMemorySpanExporter):
        def export(
            self, spans
        ):  # never called before shutdown: the worker is starved by the tiny interval
            return super().export(spans)

    exporter = InMemorySpanExporter()
    queue = SpanQueue(exporter, max_size=2, batch_size=100, interval_s=60)
    observer = OTelTrialObserver(queue=queue, label="l", session_id="s")
    for index in range(5):
        observer.tool_call(
            IDENTITY,
            role="agent",
            index=index,
            call=ToolCall(id=str(index), name="t", arguments={}),
            result=ToolResult(success=True, output="x"),
            started_at=T0,
            ended_at=T0,
        )
    receipt = observer.run_finished()
    assert receipt.spans_queued == 2 and receipt.spans_dropped == 3 and receipt.spans_exported == 2


def test_export_failure_is_counted_not_raised() -> None:
    class _Failing(InMemorySpanExporter):
        def export(self, spans):
            return SpanExportResult.FAILURE

    observer, _ = _observer(_Failing())
    observer.tool_call(
        IDENTITY,
        role="agent",
        index=0,
        call=ToolCall(id="0", name="t", arguments={}),
        result=ToolResult(success=True, output="x"),
        started_at=T0,
        ended_at=T0,
    )
    receipt = observer.run_finished()
    assert (
        receipt.export_failures == 1 and receipt.spans_dropped == 1 and receipt.spans_exported == 0
    )


def test_long_attributes_are_capped() -> None:
    exporter = InMemorySpanExporter()
    observer, _ = _observer(exporter, attribute_max_chars=200)
    observer.tool_call(
        IDENTITY,
        role="agent",
        index=0,
        call=ToolCall(id="0", name="t", arguments={}),
        result=ToolResult(success=True, output="x" * 1000),
        started_at=T0,
        ended_at=T0,
    )
    observer.run_finished()
    (span,) = exporter.get_finished_spans()
    assert len(_attrs(span)["langfuse.observation.output"]) <= 200
