"""The OTLP observer synthesises deterministic spans and never blocks (ADR-0047)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,  # noqa: E402
)
from tolokaforge_langfuse.otel import HARNESS_TAG, OTelTrialObserver, SpanQueue  # noqa: E402
from tolokaforge_langfuse.safety import SECRET_NAME, SafetyGate  # noqa: E402

from tolokaforge.core.llm.client import GenerationResult  # noqa: E402
from tolokaforge.core.llm.usage import ProviderRawCall, Usage  # noqa: E402
from tolokaforge.core.models import Message, MessageRole, ToolCall  # noqa: E402
from tolokaforge.observability.observer import ModelRef, TrialIdentity  # noqa: E402
from tolokaforge.tools.registry import ToolResult  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
IDENTITY = TrialIdentity(run_id="acme/pilot/v1/123/1", task_id="T-1", trial_index=0, attempt_id=0)


def _observer(exporter: InMemorySpanExporter, **kwargs) -> tuple[OTelTrialObserver, SpanQueue]:
    queue = SpanQueue(exporter, max_size=kwargs.pop("max_size", 100), batch_size=4, interval_s=0.05)
    # hermetic: the developer's environment holds no credential this observer knows
    kwargs.setdefault("gate", SafetyGate())
    observer = OTelTrialObserver(
        queue=queue,
        label="pilot_agent",
        session_id="acme/pilot/v1/pilot_agent/pilot_agent/123",
        tags=("config:pilot_agent", "domain:pilot-domain"),
        metadata={"model_stem": "pilot_agent"},
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

    finished = exporter.get_finished_spans()
    assert [s.name for s in finished][:1] == ["trial"]  # the provisional root goes first
    spans = {s.name: s for s in finished}  # the final root replaces the provisional one by name
    assert set(spans) == {"agent", "tool: shell", "trial"}
    assert receipt.spans_exported == 4 and receipt.spans_dropped == 0 and receipt.flushed
    provisional = finished[0]
    assert format(provisional.context.span_id, "016x") == IDENTITY.root_id
    assert _attrs(provisional)["langfuse.trace.metadata.status"] == "running"
    assert provisional.start_time == provisional.end_time == int(T0.timestamp() * 1e9)

    trace_int = int(IDENTITY.trace_id, 16)
    root = spans["trial"]
    assert root.context.trace_id == trace_int and root.parent is None
    assert format(root.context.span_id, "016x") == IDENTITY.root_id
    gen = spans["agent"]
    assert format(gen.context.span_id, "016x") == IDENTITY.observation_id("gen", 1)
    assert format(gen.parent.span_id, "016x") == IDENTITY.root_id
    tool = spans["tool: shell"]
    # contract v2: the tool span is keyed by the loop's call id, not by the message position
    assert format(tool.context.span_id, "016x") == IDENTITY.observation_id("tool", "c1")

    gen_attrs = _attrs(gen)
    assert gen_attrs["langfuse.observation.type"] == "generation"
    assert gen_attrs["langfuse.observation.model.name"] == "openai/gpt-6-astra"
    assert json.loads(gen_attrs["langfuse.observation.usage_details"]) == {
        "input": 100,
        "output": 20,
        "total": 120,
    }
    assert json.loads(gen_attrs["langfuse.observation.cost_details"]) == {"total": 0.01}
    # without a profile's [trace] the run and the task name the trace, and it has no user
    assert gen_attrs["langfuse.trace.name"] == "pilot_agent/T-1"
    assert "langfuse.user.id" not in gen_attrs
    assert gen_attrs["langfuse.session.id"] == "acme/pilot/v1/pilot_agent/pilot_agent/123"
    assert list(gen_attrs["langfuse.trace.tags"]) == [
        HARNESS_TAG,
        "source:trial",  # the producer's fact: a trial observer traces trials
        "model:openai/gpt-6-astra",
        "task:T-1",
        "config:pilot_agent",
        "domain:pilot-domain",
    ]
    assert "sk-secret" not in gen_attrs["langfuse.observation.output"]  # redacted tool arguments

    tool_attrs = _attrs(tool)
    assert tool_attrs["langfuse.observation.type"] == "tool"
    assert "sk-secret" not in tool_attrs["langfuse.observation.input"]
    assert tool_attrs["langfuse.observation.output"] == "file.txt"

    root_attrs = _attrs(root)
    assert root_attrs["langfuse.observation.type"] == "agent"
    assert root_attrs["langfuse.trace.metadata.pass"] is True
    assert root_attrs["langfuse.trace.metadata.score"] == 1.0
    assert root_attrs["langfuse.trace.metadata.trace_time_source"] == "live"
    assert root_attrs["langfuse.trace.metadata.model_name"] == "openai/gpt-6-astra"
    assert root_attrs["langfuse.trace.metadata.model_stem"] == "pilot_agent"
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


def test_shutdown_gives_up_after_the_flush_budget() -> None:
    """A receiver that answers slowly cannot hold the run open: the budget passes, the rest is
    counted as dropped and the receipt says so."""
    import time

    class _Slow(InMemorySpanExporter):
        def export(self, spans):
            time.sleep(0.25)
            return super().export(spans)

    queue = SpanQueue(_Slow(), max_size=100, batch_size=1, interval_s=60)
    observer = OTelTrialObserver(queue=queue, label="l", session_id="s", flush_timeout_s=0.6)
    for index in range(10):
        observer.tool_call(
            IDENTITY,
            role="agent",
            index=index,
            call=ToolCall(id=str(index), name="t", arguments={}),
            result=ToolResult(success=True, output="x"),
            started_at=T0,
            ended_at=T0,
        )
    started = time.monotonic()
    receipt = observer.run_finished()
    assert time.monotonic() - started < 3.0
    assert receipt.flushed is False
    assert receipt.spans_exported + receipt.spans_dropped == 10 and receipt.spans_dropped > 0


def test_trial_that_dies_before_a_trajectory_still_closes_its_trace() -> None:
    exporter = InMemorySpanExporter()
    observer, _ = _observer(exporter)
    observer.trial_started(
        IDENTITY, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
    )
    observer.trial_finished(IDENTITY, trajectory=None, error="RuntimeError: boom")
    observer.run_finished()
    provisional, root = exporter.get_finished_spans()
    assert _attrs(provisional)["langfuse.trace.metadata.status"] == "running"
    attrs = _attrs(root)
    assert root.name == "trial" and root.status.status_code.name == "ERROR"
    assert attrs["langfuse.trace.metadata.error"] == "RuntimeError: boom"
    assert attrs["langfuse.trace.metadata.status"] == "error"
    assert attrs["langfuse.trace.metadata.pass"] == "none"


def test_exporter_keys_win_over_caller_metadata() -> None:
    exporter = InMemorySpanExporter()
    queue = SpanQueue(exporter, max_size=10, batch_size=1, interval_s=60)
    observer = OTelTrialObserver(
        queue=queue, label="l", session_id="s", metadata={"status": "prod", "team": "pilot"}
    )
    observer.trial_finished(IDENTITY, trajectory=_Trajectory([], grade=_Grade()))
    observer.run_finished()
    (root,) = exporter.get_finished_spans()
    attrs = _attrs(root)
    assert attrs["langfuse.trace.metadata.status"] == "completed"  # the trial's, not the caller's
    assert attrs["langfuse.trace.metadata.team"] == "pilot"


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


class _FakeAttachments:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def attach(self, trace_id, trial_dir, *, trace_timestamp=None, metadata=None):
        from tolokaforge_langfuse.media import AttachCounts

        self.calls.append((trace_id, trial_dir, trace_timestamp, metadata))
        return AttachCounts(registered=8, uploaded=3, deduplicated=5, skipped=1, manifests_sent=1)


def test_trial_persisted_attaches_the_bundle_with_the_trial_start_and_counts_in_the_receipt(
    tmp_path,
) -> None:
    exporter = InMemorySpanExporter()
    step = _FakeAttachments()
    observer, _ = _observer(exporter, attachments=step)
    observer.trial_started(
        IDENTITY, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
    )
    observer.trial_finished(IDENTITY, trajectory=_Trajectory([], grade=_Grade()))
    observer.trial_persisted(IDENTITY, trial_dir=tmp_path)
    receipt = observer.run_finished()
    # the manifest update carries the trial start and the final status: the receiver merges
    # trace metadata in arrival order and the provisional root's "running" may land last
    assert step.calls == [(IDENTITY.trace_id, tmp_path, T0, {"status": "completed"})]
    assert (
        receipt.extra["langfuse.attachments_registered"],
        receipt.extra["langfuse.attachments_uploaded"],
        receipt.extra["langfuse.attachments_deduplicated"],
        receipt.extra["langfuse.attachments_skipped"],
        receipt.extra["langfuse.manifests_sent"],
    ) == (8, 3, 5, 1, 1)
    assert receipt.model_dump(mode="json")["extra"]["langfuse.attachments_registered"] == 8
    # the root span was not re-emitted: the trace's end time stays the trial end
    assert [s.name for s in exporter.get_finished_spans()].count("trial") == 2


def test_without_an_attachment_step_trial_persisted_is_a_no_op(tmp_path) -> None:
    exporter = InMemorySpanExporter()
    observer, _ = _observer(exporter)
    observer.trial_persisted(IDENTITY, trial_dir=tmp_path)
    receipt = observer.run_finished()
    assert (
        receipt.extra["langfuse.attachments_registered"] == 0
        and receipt.extra["langfuse.manifests_sent"] == 0
    )


# -- the write-once shape of a v4 receiver (ADR-0048) -------------------------------------------


class _V4Attachments:
    """The trial-end step as a v4 receiver offers it: files through the media API, the manifest
    handed back for the root observation (never sent), scores through the ingestion route."""

    mode = "all"
    tripped = False

    def __init__(self, manifest: dict | None = None, fail_scores: bool = False) -> None:
        self.ingested: list[dict] = []
        self.manifest = manifest if manifest is not None else {"attachments": {}}
        self.fail_scores = fail_scores

    def ingest(self, events, *, batch_size: int = 40) -> None:
        if self.fail_scores:
            raise RuntimeError("receiver down")
        self.ingested.extend(events)

    def attach_with_manifest(self, trace_id, trial_dir, *, trace_timestamp=None, metadata=None):
        from tolokaforge_langfuse.media import AttachCounts

        return AttachCounts(registered=1, uploaded=1), dict(self.manifest)

    def scan_events(self, events):
        return []

    def register_media(self, *args, **kwargs):
        return None


def _v4_observer(exporter, **kwargs):
    kwargs.setdefault("server_api", "v4")
    return _observer(exporter, **kwargs)


def _v4_trial(observer, identity=IDENTITY):
    observer.trial_started(
        identity, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
    )
    request = [Message(role=MessageRole.USER, content="hello", ts=T0)]
    result = GenerationResult(
        text="",
        tool_calls=[ToolCall(id="c1", name="shell", arguments={"cmd": "ls"})],
        usage=Usage(prompt_tokens=10, completion_tokens=2),
    )
    observer.generation(
        identity,
        role="agent",
        index=1,
        turn=0,
        request=request,
        result=result,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=1),
    )
    observer.tool_call(
        identity,
        role="agent",
        index=2,
        call=result.tool_calls[0],
        result=ToolResult(success=True, output="file.txt"),
        started_at=T0 + timedelta(seconds=2),
        ended_at=T0 + timedelta(seconds=2.2),
    )


class TestPreviewRows:
    def test_the_live_rows_are_previews_under_a_preview_root(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=_Trajectory([], grade=_Grade()))
        receipt = observer.run_finished()

        spans = exporter.get_finished_spans()
        preview_root = IDENTITY.observation_id("proot", "-")
        by_name = {s.name: s for s in spans}
        assert set(by_name) >= {
            "preview: trial",
            "preview: agent",
            "preview: tool: shell",
        }
        assert format(by_name["preview: trial"].context.span_id, "016x") == preview_root
        # shape R: the preview root names the final root as its parent, so the trace has one root
        assert format(by_name["preview: trial"].parent.span_id, "016x") == IDENTITY.root_id
        for name, kind, key in (
            ("preview: agent", "pgen", (1,)),
            ("preview: tool: shell", "ptool", ("c1",)),
        ):
            span = by_name[name]
            assert format(span.context.span_id, "016x") == IDENTITY.observation_id(kind, *key)
            assert format(span.parent.span_id, "016x") == preview_root
        previews = [s for s in spans if s.name.startswith("preview: ")]
        assert len(previews) == 3
        for span in previews:
            attributes = _attrs(span)
            assert attributes["langfuse.observation.metadata.preview"] is True
            assert attributes["langfuse.trace.metadata.task_id"] == "T-1"
            assert attributes["langfuse.trace.metadata.run_id"] == IDENTITY.run_id
        assert receipt.extra["langfuse.previews_sent"] == 3

    def test_no_preview_id_can_be_a_final_id(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        observer.run_finished()
        previews = {
            format(s.context.span_id, "016x")
            for s in exporter.get_finished_spans()
            if s.name.startswith("preview: ")
        }
        final = {
            IDENTITY.root_id,
            IDENTITY.observation_id("gen", 1),
            IDENTITY.observation_id("tool", "c1"),
        }
        assert previews and not previews & final

    def test_trial_finished_writes_no_root(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=_Trajectory([], grade=_Grade()))
        # the root is one of the bundle's observations; nothing may take its id before it
        assert IDENTITY.root_id not in {
            format(s.context.span_id, "016x") for s in exporter.get_finished_spans()
        }


class TestTheFinalLayout:
    def _run(self, tmp_path, **kwargs):
        import parity_bundle as pb

        from tolokaforge.observability.observer import TrialIdentity

        identity = TrialIdentity(
            run_id=pb.RUN_ID,
            task_id=pb.TASK_ID,
            trial_index=pb.TRIAL_INDEX,
            attempt_id=pb.ATTEMPT_ID,
            run_tag=pb.RUN_TAG,
        )
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        exporter = InMemorySpanExporter()
        step = _V4Attachments(**kwargs)
        observer, _ = _v4_observer(exporter, attachments=step)
        _v4_trial(observer, identity)
        observer.trial_finished(identity, trajectory=_Trajectory([], grade=_Grade()))
        observer.trial_persisted(identity, trial_dir=trial_dir)
        receipt = observer.run_finished()
        return identity, exporter.get_finished_spans(), step, receipt

    def test_the_bundle_is_written_once_with_the_root_last(self, tmp_path) -> None:
        identity, spans, step, receipt = self._run(tmp_path)
        final = [s for s in spans if not _attrs(s).get("langfuse.observation.metadata.preview")]
        ids = [format(s.context.span_id, "016x") for s in final]
        assert len(ids) == len(set(ids))  # every id exactly once
        roots = [s for s in final if s.parent is None]
        assert len(roots) == 1 and format(roots[0].context.span_id, "016x") == identity.root_id
        assert final[-1] is roots[0]  # the root completes the trace, so it goes last
        assert receipt.extra["langfuse.final_observations_sent"] == len(final)
        assert receipt.extra["langfuse.error_roots_sent"] == 0

    def test_the_root_carries_the_manifest_and_the_trace_metadata(self, tmp_path) -> None:
        manifest = {
            "attachments": {"grade.yaml": {"media_id": "m1", "sha256": "ab", "bytes": 3}},
            "attachments_complete": True,
        }
        identity, spans, step, receipt = self._run(tmp_path, manifest=manifest)
        root = next(s for s in spans if s.parent is None)
        attributes = _attrs(root)
        assert (
            json.loads(attributes["langfuse.trace.metadata.attachments"]) == manifest["attachments"]
        )
        assert attributes["langfuse.trace.metadata.attachments_complete"] is True
        assert attributes["langfuse.trace.metadata.status"] == "completed"
        assert receipt.extra["langfuse.manifests_sent"] == 1

    def test_the_scores_take_the_ingestion_route_with_the_gradings_own_time(self, tmp_path) -> None:
        import parity_bundle as pb

        identity, spans, step, receipt = self._run(tmp_path)
        assert step.ingested and {e["type"] for e in step.ingested} == {"score-create"}
        at = pb.trajectory()["end_ts"]
        for event in step.ingested:
            # a receiver keeps the first write's timestamp for ever: it must be the grading's
            assert event["body"]["timestamp"].startswith(str(at)[:19])
        assert receipt.extra["langfuse.scores_sent"] == len(step.ingested)

    def test_a_failed_score_batch_leaves_the_trace_complete_and_is_counted(self, tmp_path) -> None:
        identity, spans, step, receipt = self._run(tmp_path, fail_scores=True)
        assert any(s.parent is None for s in spans)  # the observations still went out
        assert receipt.extra["langfuse.scores_sent"] == 0
        assert receipt.extra["langfuse.gradings_failed"] == 1


class TestTheProfilesTrace:
    """A deployment's ``[trace]`` names every live row's trace and gives it its user, preview,
    final and error root alike, as the projection does."""

    SETTINGS = {"trace_name": "{domain}/{config}", "trace_user": "model"}

    def test_every_live_row_carries_the_profiles_name_and_user(self) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter, projection=ProjectionSettings(**self.SETTINGS))
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=None, error="RuntimeError: the worker died")
        observer.run_finished()
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} >= {"preview: trial", "preview: agent", "trial"}
        assert {_attrs(s)["langfuse.trace.name"] for s in spans} == {"pilot-domain/pilot_agent"}
        assert {_attrs(s)["langfuse.user.id"] for s in spans} == {"openai/gpt-6-astra"}

    def test_an_agent_the_rules_cannot_read_names_no_user(self) -> None:
        """The bundle pass has no identity for such a model, so no live row may claim one: the
        raw name stands in for the tags only."""
        from tolokaforge_langfuse.model_names import ModelNameResolverError
        from tolokaforge_langfuse.otel import ProjectionSettings

        class Refusing:
            description = "refusing"
            rules_version = "r-1"

            def resolve(self, provider, name):
                raise ModelNameResolverError(f"{name}: unresolved tokens")

        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(
            exporter, resolver=Refusing(), projection=ProjectionSettings(**self.SETTINGS)
        )
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=None, error="RuntimeError: the worker died")
        observer.run_finished()
        spans = exporter.get_finished_spans()
        assert spans and not any("langfuse.user.id" in _attrs(s) for s in spans)
        assert {_attrs(s)["langfuse.trace.name"] for s in spans} == {"pilot-domain/pilot_agent"}

    def test_a_template_the_trace_cannot_fill_falls_back_to_the_run_and_the_task(self) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        exporter = InMemorySpanExporter()
        observer, _ = _observer(
            exporter, projection=ProjectionSettings(trace_name="{dataset}/{domain}")
        )
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=None)
        observer.run_finished()
        assert {_attrs(s)["langfuse.trace.name"] for s in exporter.get_finished_spans()} == {
            "pilot_agent/T-1"
        }


class TestErrorRoots:
    def test_a_trial_that_never_persists_gets_one_minimal_root(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        observer.trial_finished(IDENTITY, trajectory=None, error="RuntimeError: the worker died")
        receipt = observer.run_finished()

        roots = [s for s in exporter.get_finished_spans() if s.parent is None]
        assert len(roots) == 1
        root = roots[0]
        attributes = _attrs(root)
        assert format(root.context.span_id, "016x") == IDENTITY.root_id
        assert attributes["langfuse.trace.metadata.status"] == "error"
        assert "the worker died" in attributes["langfuse.trace.metadata.error"]
        assert attributes["langfuse.observation.metadata.error_root"] is True
        assert "langfuse.trace.metadata.attachments" not in attributes  # no manifest
        assert "langfuse.trace.metadata.pass" not in attributes  # no verdict
        assert attributes["langfuse.trace.metadata.task_id"] == "T-1"
        assert root.start_time == int(T0.timestamp() * 1e9)
        assert receipt.extra["langfuse.error_roots_sent"] == 1

    def test_a_trial_that_never_finishes_gets_one_too(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        receipt = observer.run_finished()
        assert receipt.extra["langfuse.error_roots_sent"] == 1

    def test_a_completed_trial_gets_none(self, tmp_path) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter, attachments=_V4Attachments())
        import parity_bundle as pb

        from tolokaforge.observability.observer import TrialIdentity

        identity = TrialIdentity(
            run_id=pb.RUN_ID,
            task_id=pb.TASK_ID,
            trial_index=pb.TRIAL_INDEX,
            attempt_id=pb.ATTEMPT_ID,
            run_tag=pb.RUN_TAG,
        )
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        _v4_trial(observer, identity)
        observer.trial_finished(identity, trajectory=_Trajectory([], grade=_Grade()))
        observer.trial_persisted(identity, trial_dir=trial_dir)
        receipt = observer.run_finished()
        assert receipt.extra["langfuse.error_roots_sent"] == 0

    def test_a_root_the_queue_never_took_is_answered_with_one(self, tmp_path) -> None:
        """A span the queue refused never reached the receiver, so the trace has no root at all
        and the error root writes under its id."""
        import parity_bundle as pb

        from tolokaforge.observability.observer import TrialIdentity

        identity = TrialIdentity(
            run_id=pb.RUN_ID,
            task_id=pb.TASK_ID,
            trial_index=pb.TRIAL_INDEX,
            attempt_id=pb.ATTEMPT_ID,
            run_tag=pb.RUN_TAG,
        )

        class _FullWhenTheRootArrives(SpanQueue):
            """The real "queue full" branch, taken for the final root span alone."""

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.refused = 0

            def put(self, span, *, track=None):
                if format(span.context.span_id, "016x") == identity.root_id and not self.refused:
                    self.refused += 1
                    room, self._max_size = self._max_size, 0
                    try:
                        return super().put(span, track=track)
                    finally:
                        self._max_size = room
                return super().put(span, track=track)

        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        exporter = InMemorySpanExporter()
        queue = _FullWhenTheRootArrives(exporter, max_size=100, batch_size=4, interval_s=0.05)
        observer = OTelTrialObserver(
            queue=queue,
            label="pilot_agent",
            session_id="acme/pilot/v1/pilot_agent/pilot_agent/123",
            tags=("config:pilot_agent", "domain:pilot-domain"),
            metadata={"model_stem": "pilot_agent"},
            server_api="v4",
            attachments=_V4Attachments(),
        )
        _v4_trial(observer, identity)
        observer.trial_finished(identity, trajectory=_Trajectory([], grade=_Grade()))
        observer.trial_persisted(identity, trial_dir=trial_dir)
        receipt = observer.run_finished()

        assert queue.refused == 1
        assert receipt.extra["langfuse.error_roots_sent"] == 1
        assert receipt.extra["langfuse.roots_unconfirmed"] == 0
        roots = [s for s in exporter.get_finished_spans() if s.parent is None]
        assert len(roots) == 1 and format(roots[0].context.span_id, "016x") == identity.root_id
        assert "never left the queue" in _attrs(roots[0])["langfuse.trace.metadata.error"]

    def test_a_root_the_exporter_could_not_confirm_is_not_written_again(self, tmp_path) -> None:
        """A batch the exporter reported as failed may have been written anyway (the receiver
        wrote it and the answer was lost). A second root under the same id could never be
        removed there, so the run reports it instead (ADR-0048)."""
        import parity_bundle as pb

        from tolokaforge.observability.observer import TrialIdentity

        identity = TrialIdentity(
            run_id=pb.RUN_ID,
            task_id=pb.TASK_ID,
            trial_index=pb.TRIAL_INDEX,
            attempt_id=pb.ATTEMPT_ID,
            run_tag=pb.RUN_TAG,
        )

        class _RefusesTheRootOnce(InMemorySpanExporter):
            """The batch that carries the final root is refused; the ones after it are not."""

            def __init__(self) -> None:
                super().__init__()
                self.refused = 0

            def export(self, spans):
                carries_root = any(
                    format(s.context.span_id, "016x") == identity.root_id for s in spans
                )
                if carries_root and not self.refused:
                    self.refused += 1
                    return SpanExportResult.FAILURE
                return super().export(spans)

        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        exporter = _RefusesTheRootOnce()
        observer, _ = _v4_observer(exporter, attachments=_V4Attachments())
        _v4_trial(observer, identity)
        observer.trial_finished(identity, trajectory=_Trajectory([], grade=_Grade()))
        observer.trial_persisted(identity, trial_dir=trial_dir)
        receipt = observer.run_finished()

        assert exporter.refused == 1
        assert receipt.extra["langfuse.error_roots_sent"] == 0
        assert receipt.extra["langfuse.roots_unconfirmed"] == 1
        assert [s for s in exporter.get_finished_spans() if s.parent is None] == []


class TestHowManyTimesABatchIsPosted:
    """The v4 producer policy makes one POST attempt per batch (ADR-0048).

    ``test_otlp_transport.py`` owns the counts: it sends real batches to a local receiver and
    counts what reaches the wire. What stays here is what only an in-process test reaches: the
    endpoint's adapter, and the refusal when the single-attempt exporter cannot be built.
    """

    def _write_once(self):
        from tolokaforge_langfuse.otlp_transport import make_otlp_exporter

        return make_otlp_exporter(
            "http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"}, retry=False
        )

    def test_the_endpoints_adapter_makes_no_attempt_of_its_own(self) -> None:
        """requests' own adapters make no retries; the exporter mounts one with none for its
        endpoint anyway, so the guarantee does not rest on a library default."""
        exporter = self._write_once()
        adapter = exporter._session.get_adapter("http://127.0.0.1:9/v1/traces")
        assert adapter.max_retries.total == 0

    def test_an_install_that_cannot_post_once_refuses_the_run(self, monkeypatch) -> None:
        """An install that cannot build the single-attempt request must refuse the run instead
        of silently enabling retries."""
        from tolokaforge_langfuse import otlp_transport

        monkeypatch.setattr(otlp_transport, "_single_attempt_exporter_class", lambda: None)
        with pytest.raises(otlp_transport.SingleAttemptUnavailable, match="single-attempt"):
            otlp_transport.make_otlp_exporter(
                "http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"}, retry=False
            )

    def test_the_retrying_exporter_does_not_need_the_single_attempt_one(self, monkeypatch) -> None:
        """v3 keeps the stock exporter whether or not the single-attempt one can be built."""
        from tolokaforge_langfuse import otlp_transport

        monkeypatch.setattr(otlp_transport, "_single_attempt_exporter_class", lambda: None)
        exporter = otlp_transport.make_otlp_exporter(
            "http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"}
        )
        assert type(exporter).__name__ == "OTLPSpanExporter"


class TestLiveCost:
    """A live generation shows the charge its call record states, else the eval's figure, and
    names which."""

    BILLED = ProviderRawCall(
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=0.0266895,
        cost_source="litellm",
        billed_cost_usd=0.027022,
    )

    def _generation_attrs(self, result, *, role: str = "agent", server_api: str = "v3") -> dict:
        exporter = InMemorySpanExporter()
        observer, _ = _observer(exporter, server_api=server_api)
        observer.trial_started(
            IDENTITY, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
        )
        observer.generation(
            IDENTITY,
            role=role,
            index=1,
            turn=0,
            request=[Message(role=MessageRole.USER, content="hello", ts=T0)],
            result=result,
            started_at=T0,
            ended_at=T0 + timedelta(seconds=1),
        )
        observer.run_finished()
        name = "agent" if role == "agent" else "judge"
        if server_api == "v4":
            name = f"preview: {name}"
        (span,) = [s for s in exporter.get_finished_spans() if s.name == name]
        return _attrs(span)

    def test_the_stated_charge_is_the_generations_cost(self) -> None:
        result = GenerationResult(
            text="ok",
            usage=Usage(prompt_tokens=100, completion_tokens=20, calls=(self.BILLED,)),
            cost_usd=0.0266895,
        )
        attrs = self._generation_attrs(result)
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.027022}
        assert attrs["langfuse.observation.metadata.cost_basis"] == "billed"

    def test_a_live_judge_turn_is_priced_by_the_same_rule(self) -> None:
        result = GenerationResult(
            text="ok",
            usage=Usage(prompt_tokens=100, completion_tokens=20, calls=(self.BILLED,)),
            cost_usd=0.0266895,
        )
        attrs = self._generation_attrs(result, role="judge")
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.027022}
        assert attrs["langfuse.observation.metadata.cost_basis"] == "billed"

    def test_a_call_that_stated_no_charge_shows_the_eval_figure(self) -> None:
        call = ProviderRawCall(prompt_tokens=100, cost_usd=0.01, cost_source="local")
        result = GenerationResult(
            text="ok", usage=Usage(prompt_tokens=100, calls=(call,)), cost_usd=0.01
        )
        attrs = self._generation_attrs(result)
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.01}
        assert attrs["langfuse.observation.metadata.cost_basis"] == "list"

    def test_a_result_without_a_call_record_shows_the_eval_figure(self) -> None:
        result = GenerationResult(
            text="ok", usage=Usage(prompt_tokens=100, completion_tokens=20), cost_usd=0.01
        )
        attrs = self._generation_attrs(result)
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.01}
        assert attrs["langfuse.observation.metadata.cost_basis"] == "eval"

    def test_a_call_without_any_figure_states_a_zero_cost(self) -> None:
        """No stated charge and no eval figure: an explicit zero, so the receiver prices
        nothing from its own model table."""
        call = ProviderRawCall(prompt_tokens=100, completion_tokens=20, cost_source="unknown")
        result = GenerationResult(
            text="ok", usage=Usage(prompt_tokens=100, completion_tokens=20, calls=(call,))
        )
        attrs = self._generation_attrs(result)
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0}
        assert attrs["langfuse.observation.metadata.cost_basis"] == "none"
        assert json.loads(attrs["langfuse.observation.usage_details"])["total"] == 120

    @pytest.mark.parametrize("role", ["agent", "judge"])
    def test_a_preview_counts_nothing_toward_the_trace(self, role: str) -> None:
        """A preview stays beside the final row the bundle writes, and the receiver adds up the
        usage and cost of every row: the preview states zero usage and cost, explicitly so the
        receiver infers none from its model, and its figures as metadata, so each call counts
        once."""
        result = GenerationResult(
            text="ok",
            usage=Usage(prompt_tokens=100, completion_tokens=20, calls=(self.BILLED,)),
            cost_usd=0.0266895,
        )
        attrs = self._generation_attrs(result, role=role, server_api="v4")
        assert attrs["langfuse.observation.metadata.preview"] is True
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {
            "input": 0,
            "output": 0,
            "total": 0,
        }
        assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0}
        assert not [key for key in attrs if key.startswith("gen_ai.usage")]
        assert attrs["langfuse.observation.metadata.prompt_tokens"] == 100
        assert attrs["langfuse.observation.metadata.completion_tokens"] == 20
        assert attrs["langfuse.observation.metadata.cost"] == 0.027022
        assert attrs["langfuse.observation.metadata.cost_basis"] == "billed"

    def _live_usage(self, prompt: int) -> dict:
        result = GenerationResult(
            text="ok",
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=20,
                cache_read_input_tokens=100,
                cache_creation_input_tokens=200,
            ),
        )
        return json.loads(self._generation_attrs(result)["langfuse.observation.usage_details"])

    def test_a_live_generation_counts_cache_writes_once(self) -> None:
        """The engine's prompt total holds the cache reads and the cache writes, so each leaves
        ``input`` once and the components add up to ``total``."""
        details = self._live_usage(prompt=1000)
        assert details == {
            "input": 700,
            "output": 20,
            "total": 1020,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 200,
        }
        assert sum(value for key, value in details.items() if key != "total") == details["total"]

    def test_counters_larger_than_the_prompt_floor_a_live_input_at_zero(self) -> None:
        assert self._live_usage(prompt=250)["input"] == 0

    def test_a_live_row_on_a_v3_receiver_is_the_final_row(self) -> None:
        result = GenerationResult(
            text="ok",
            usage=Usage(prompt_tokens=100, completion_tokens=20, calls=(self.BILLED,)),
            cost_usd=0.0266895,
        )
        attrs = self._generation_attrs(result)
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {
            "input": 100,
            "output": 20,
            "total": 120,
        }
        assert attrs["gen_ai.usage.input_tokens"] == 100
        assert attrs["gen_ai.usage.output_tokens"] == 20
        assert "langfuse.observation.metadata.cost" not in attrs


class TestEverySpanIsScannedBeforeItLeaves:
    """A live span leaves before the bundle's own scan runs, so each one is scanned itself, free
    text (model input and output, tool output and errors) included. A span that would carry a
    secret is withheld and counted (docs/OBSERVABILITY.md, "Delivery")."""

    # the shape of an OpenRouter key: 64 hex characters after the prefix
    PROVIDER_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
    # no shape, only the environment knows it; JSON escapes its quote and its backslash
    AWKWARD_SECRET = 'tok"en\\8f3a91c2b7d04e56'
    FAMILIES = pytest.mark.parametrize("server_api", ["v3", "v4"])

    @pytest.fixture(autouse=True)
    def only_the_tests_own_credentials(self, monkeypatch) -> None:
        """The gate's known values come from the environment: only the test's own are in it."""
        for name in [name for name in os.environ if SECRET_NAME.search(name)]:
            monkeypatch.delenv(name)

    @staticmethod
    def _trial(
        observer,
        *,
        assistant_text="done",
        request_text="hello",
        tool_name="shell",
        tool_arguments=None,
        tool_output="file.txt",
    ):
        observer.trial_started(
            IDENTITY, models={"agent": ModelRef("openrouter", "openai/gpt-6-astra")}, started_at=T0
        )
        observer.generation(
            IDENTITY,
            role="agent",
            index=1,
            turn=0,
            request=[Message(role=MessageRole.USER, content=request_text, ts=T0)],
            result=GenerationResult(
                text=assistant_text, usage=Usage(prompt_tokens=10, completion_tokens=2)
            ),
            started_at=T0,
            ended_at=T0 + timedelta(seconds=1),
        )
        observer.tool_call(
            IDENTITY,
            role="agent",
            index=2,
            call=ToolCall(id="c1", name=tool_name, arguments=tool_arguments or {"cmd": "ls"}),
            result=ToolResult(success=True, output=tool_output),
            started_at=T0 + timedelta(seconds=2),
            ended_at=T0 + timedelta(seconds=2.2),
        )

    def _run(
        self,
        server_api,
        *,
        tool_error=None,
        final_text="done",
        error=None,
        gate=None,
        projection=None,
        **trial,
    ):
        exporter = InMemorySpanExporter()
        gate = gate if gate is not None else SafetyGate.from_environment()
        extra = {"projection": projection} if projection is not None else {}
        observer, _ = _observer(exporter, server_api=server_api, gate=gate, **extra)
        self._trial(observer, **trial)
        if tool_error is not None:
            observer.tool_call(
                IDENTITY,
                role="agent",
                index=3,
                call=ToolCall(id="c2", name="shell", arguments={}),
                result=ToolResult(success=False, output="", error=tool_error),
                started_at=T0 + timedelta(seconds=3),
                ended_at=T0 + timedelta(seconds=3.2),
            )
        messages = [
            Message(role=MessageRole.USER, content="hello", ts=T0),
            Message(role=MessageRole.ASSISTANT, content=final_text, ts=T0),
        ]
        if error is None:
            observer.trial_finished(IDENTITY, trajectory=_Trajectory(messages, grade=_Grade()))
        else:
            observer.trial_finished(IDENTITY, trajectory=None, error=error)
        receipt = observer.run_finished()
        return exporter.get_finished_spans(), receipt

    @staticmethod
    def _kinds(spans) -> list[str]:
        """What went out, a preview's prefix dropped: both families write the same rows (on v4
        the last ``trial`` is the error root, written when the run ends)."""
        return sorted(span.name.removeprefix("preview: ") for span in spans)

    @staticmethod
    def _carried(spans, value: str) -> bool:
        return any(value in json.dumps(dict(span.attributes), default=str) for span in spans)

    @FAMILIES
    def test_clean_spans_are_exported_and_nothing_is_counted(self, server_api, monkeypatch) -> None:
        monkeypatch.setenv("FOO_TOKEN", "tok_8f3a91c2b7d04e56")  # held, and in no span
        spans, receipt = self._run(server_api)
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 0
        assert (receipt.spans_queued, receipt.spans_exported) == (4, 4)

    @FAMILIES
    def test_a_provider_key_in_a_tool_result_withholds_that_span(self, server_api, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run(server_api, tool_output=f"found {self.PROVIDER_KEY} in it")
        assert self._kinds(spans) == ["agent", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        # neither queued nor dropped: it never got that far
        assert (receipt.spans_queued, receipt.spans_dropped) == (3, 0)
        assert not self._carried(spans, self.PROVIDER_KEY)
        # the warning names the span and the rule, never the value or a part of it
        assert "tool: shell" in caplog.text and "openrouter-key" in caplog.text
        assert "sk-or" not in caplog.text and "0123456789" not in caplog.text

    @FAMILIES
    def test_a_credential_the_process_holds_in_the_assistants_text_withholds_that_span(
        self, server_api, monkeypatch, caplog
    ) -> None:
        value = "tok_8f3a91c2b7d04e56"
        monkeypatch.setenv("FOO_TOKEN", value)
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run(server_api, assistant_text=f"the token is {value}")
        assert self._kinds(spans) == ["tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert not self._carried(spans, value)
        assert "known-secret-value" in caplog.text and value not in caplog.text

    @FAMILIES
    def test_a_credential_with_a_quote_and_a_backslash_is_found_in_the_raw_strings(
        self, server_api, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("FOO_TOKEN", self.AWKWARD_SECRET)
        # the serialised attributes hold it escaped, so only the raw strings give it away
        assert self.AWKWARD_SECRET not in json.dumps(self.AWKWARD_SECRET)
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run(server_api, tool_error=f"401 for {self.AWKWARD_SECRET}")
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert "known-secret-value" in caplog.text and "8f3a91c2" not in caplog.text

    @FAMILIES
    @pytest.mark.parametrize(
        ("field", "kinds"),
        [
            ("assistant_text", ["tool: shell", "trial", "trial"]),
            ("request_text", ["tool: shell", "trial", "trial"]),
            ("tool_arguments", ["agent", "trial", "trial"]),
        ],
    )
    def test_a_credential_json_escapes_is_found_in_an_attribute_that_is_json_text(
        self, server_api, field, kinds, monkeypatch
    ) -> None:
        """A generation's input and output and a tool's input are JSON text: the credential is
        escaped once more there, and withheld all the same."""
        monkeypatch.setenv("FOO_TOKEN", self.AWKWARD_SECRET)
        value = {"cmd": f"echo {self.AWKWARD_SECRET}"}
        if field != "tool_arguments":
            value = f"the token is {self.AWKWARD_SECRET}"
        spans, receipt = self._run(server_api, **{field: value})
        assert self._kinds(spans) == kinds
        assert receipt.extra["langfuse.spans_refused_secret"] == 1

    @FAMILIES
    def test_code_that_names_a_key_or_a_token_count_is_not_withheld(self, server_api) -> None:
        """Only the serialised span meets the shapes: a line-anchored one would stop the ordinary
        code a coding benchmark's tools and models print."""
        code = (
            "api_key = os.environ.get('X')\n"
            "total_tokens = response.usage.total_tokens\n"
            "GPG_KEY=0123456789ABCDEF0123456789ABCDEF01234567\n"
        )
        spans, receipt = self._run(
            server_api, assistant_text=code, request_text=code, tool_output=code
        )
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 0

    def test_the_observer_and_its_gate_print_no_known_value(self, monkeypatch) -> None:
        monkeypatch.setenv("FOO_TOKEN", self.AWKWARD_SECRET)
        observer, _ = _observer(InMemorySpanExporter(), gate=SafetyGate.from_environment())
        self._trial(observer, assistant_text=self.AWKWARD_SECRET)
        observer.run_finished()
        text = repr(observer) + repr(observer._gate)
        assert "8f3a91c2" not in text

    @FAMILIES
    def test_a_trials_error_that_would_carry_a_secret_withholds_the_root(self, server_api) -> None:
        """The root (on v4 the error root) is scanned like every other span; the trial's own
        rows went out before and stay."""
        spans, receipt = self._run(server_api, error=f"RuntimeError: key {self.PROVIDER_KEY}")
        assert self._kinds(spans) == ["agent", "tool: shell", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert receipt.extra["langfuse.error_roots_sent"] == 0
        assert not self._carried(spans, self.PROVIDER_KEY)

    def test_a_final_root_that_would_carry_a_secret_is_withheld(self) -> None:
        spans, receipt = self._run("v3", final_text=f"done, the key was {self.PROVIDER_KEY}")
        # the provisional root left at the trial's start, the final one is withheld
        assert self._kinds(spans) == ["agent", "tool: shell", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1

    @FAMILIES
    def test_a_secret_in_the_traces_own_identity_withholds_every_row_roots_included(
        self, server_api
    ) -> None:
        exporter = InMemorySpanExporter()
        queue = SpanQueue(exporter, max_size=100, batch_size=4, interval_s=0.05)
        observer = OTelTrialObserver(
            queue=queue,
            label="l",
            session_id="s",
            tags=(f"note:{self.PROVIDER_KEY}",),
            server_api=server_api,
        )
        self._trial(observer)
        observer.trial_finished(IDENTITY, trajectory=_Trajectory([], grade=_Grade()))
        receipt = observer.run_finished()
        assert exporter.get_finished_spans() == ()
        # every row of the trial: the (preview) root, the generation, the tool, the final or
        # error root
        assert receipt.extra["langfuse.spans_refused_secret"] == 4
        assert receipt.spans_queued == 0

    def test_a_tool_the_model_named_after_a_secret_is_not_named_in_the_warning(
        self, caplog
    ) -> None:
        observer, _ = _observer(InMemorySpanExporter())
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            self._trial(observer, tool_name=self.PROVIDER_KEY)
        observer.run_finished()
        assert "not exported" in caplog.text
        assert self.PROVIDER_KEY not in caplog.text and "sk-or" not in caplog.text

    def test_the_launchers_session_id_is_not_a_secret(self, monkeypatch) -> None:
        """Its name says SESSION, its value rides on every span by design: it must not stop them."""
        monkeypatch.setenv(
            "TOLOKAFORGE_TRACING_SESSION_ID", "acme/pilot/v1/pilot_agent/pilot_agent/123"
        )
        spans, receipt = self._run("v3")
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 0

    def test_an_injected_gate_replaces_the_environments(self, monkeypatch) -> None:
        monkeypatch.setenv("FOO_TOKEN", "tok_8f3a91c2b7d04e56")
        gate = SafetyGate(known_values=(b"injected-credential",))
        spans, receipt = self._run(
            "v3",
            gate=gate,
            assistant_text="tok_8f3a91c2b7d04e56",
            tool_output="injected-credential",
        )
        assert self._kinds(spans) == ["agent", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1

    # -- the gate of a run: what it leaves out, what it names, what it refreshes -------------------

    @staticmethod
    def _warnings(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    @FAMILIES
    def test_a_value_the_runs_own_tags_carry_is_left_out_and_named(
        self, server_api, caplog
    ) -> None:
        """``ACME_TOKEN=tolokaforge`` is also the harness tag's value, written on every span by
        design: a gate that knew it would withhold the whole run, silently."""
        from tolokaforge_langfuse.otel import ProjectionSettings

        gate = SafetyGate.from_environment({"ACME_TOKEN": "tolokaforge"})
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            # the harness tag alone carries the value: the producer is another name
            spans, receipt = self._run(
                server_api, gate=gate, projection=ProjectionSettings(producer="pilot-producer")
            )
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 0
        warnings = self._warnings(caplog)
        assert [w for w in warnings if "ACME_TOKEN" in w] and not [
            w for w in warnings if "tolokaforge" in w
        ]
        assert gate.known_values == ()

    @FAMILIES
    def test_a_real_secret_is_still_withheld_and_its_warning_names_its_variable(
        self, server_api, caplog
    ) -> None:
        value = "a-db-password-no-shape-matches"
        gate = SafetyGate.from_environment({"ACME_TOKEN": "tolokaforge", "DB_PASSWORD": value})
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run(server_api, gate=gate, tool_output=f"it said {value}")
        assert self._kinds(spans) == ["agent", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        span_warning = next(w for w in self._warnings(caplog) if "would carry a secret" in w)
        assert "known-secret-value from DB_PASSWORD" in span_warning
        assert value not in span_warning and "ACME_TOKEN" not in span_warning

    def test_the_runs_id_and_tag_are_ambient_too(self, caplog) -> None:
        gate = SafetyGate.from_environment({"RUN_SECRET": "acme/pilot/v1/123"})
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            queue = SpanQueue(InMemorySpanExporter(), max_size=10, batch_size=4, interval_s=0.05)
            OTelTrialObserver(
                queue=queue,
                label="l",
                session_id="s",
                gate=gate,
                ambient=("acme/pilot/v1/123/1", "v1"),
            ).run_finished()
        assert any("RUN_SECRET" in w for w in self._warnings(caplog))
        assert gate.known_values == ()

    def test_one_warning_at_run_end_says_what_was_withheld_and_why(self, caplog) -> None:
        value = "a-db-password-no-shape-matches"
        gate = SafetyGate.from_environment({"DB_PASSWORD": value})
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            self._run("v3", gate=gate, tool_output=f"it said {value}")
        summary = [w for w in self._warnings(caplog) if w.startswith("live tracing withheld")]
        assert len(summary) == 1
        assert "1 span(s)" in summary[0] and "known-secret-value from DB_PASSWORD x1" in summary[0]
        assert value not in summary[0]

    def test_a_run_that_withheld_nothing_says_nothing_at_run_end(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            self._run("v3", gate=SafetyGate())
        assert not [w for w in self._warnings(caplog) if w.startswith("live tracing withheld")]

    def test_a_secret_registered_after_the_gate_was_built_is_known_at_the_next_trial(
        self, caplog
    ) -> None:
        """The engine registers a generated key (``register_runtime_secret``) after the observer
        is built; the gate is told to re-read at a trial's start."""
        late = "late-registered-key-value"
        gate = SafetyGate()
        offers = [SafetyGate.from_environment({"TYPESENSE_API_KEY": late})]
        gate.reload = lambda: offers.pop() if offers else None
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run("v3", gate=gate, tool_output=f"the key is {late}")
        assert self._kinds(spans) == ["agent", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert any("known-secret-value from TYPESENSE_API_KEY" in w for w in self._warnings(caplog))

    def test_a_refreshed_value_the_run_carries_is_left_out_again(self, caplog) -> None:
        gate = SafetyGate()
        offers = [SafetyGate.from_environment({"ACME_TOKEN": "tolokaforge"})]
        gate.reload = lambda: offers.pop() if offers else None
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run("v3", gate=gate)
        assert self._kinds(spans) == ["agent", "tool: shell", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 0
        assert [w for w in self._warnings(caplog) if "ACME_TOKEN" in w]

    def test_a_gate_that_cannot_refresh_keeps_the_values_it_has(self, caplog) -> None:
        value = "a-db-password-no-shape-matches"
        gate = SafetyGate.from_environment({"DB_PASSWORD": value})

        def broken():
            raise ValueError("the manager is gone")

        gate.reload = broken
        with caplog.at_level(logging.WARNING, logger="tolokaforge_langfuse.otel"):
            spans, receipt = self._run("v3", gate=gate, tool_output=f"it said {value}")
        assert self._kinds(spans) == ["agent", "trial", "trial"]
        assert receipt.extra["langfuse.spans_refused_secret"] == 1
        assert any("were not re-read: ValueError" in w for w in self._warnings(caplog))
