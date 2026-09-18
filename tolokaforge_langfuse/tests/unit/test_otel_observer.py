"""The OTLP observer synthesises deterministic spans and never blocks (ADR-0047)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,  # noqa: E402
)
from tolokaforge_langfuse.otel import HARNESS_TAG, OTelTrialObserver, SpanQueue  # noqa: E402

from tolokaforge.core.llm.client import GenerationResult  # noqa: E402
from tolokaforge.core.llm.usage import Usage  # noqa: E402
from tolokaforge.core.models import Message, MessageRole, ToolCall  # noqa: E402
from tolokaforge.observability.observer import ModelRef, TrialIdentity  # noqa: E402
from tolokaforge.tools.registry import ToolResult  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
IDENTITY = TrialIdentity(run_id="acme/pilot/v1/123/1", task_id="T-1", trial_index=0, attempt_id=0)


def _observer(exporter: InMemorySpanExporter, **kwargs) -> tuple[OTelTrialObserver, SpanQueue]:
    queue = SpanQueue(exporter, max_size=kwargs.pop("max_size", 100), batch_size=4, interval_s=0.05)
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
    assert [s.name for s in finished][:1] == ["trial T-1/0"]  # the provisional root goes first
    spans = {s.name: s for s in finished}  # the final root replaces the provisional one by name
    assert set(spans) == {"assistant turn 1", "tool: shell", "trial T-1/0"}
    assert receipt.spans_exported == 4 and receipt.spans_dropped == 0 and receipt.flushed
    provisional = finished[0]
    assert format(provisional.context.span_id, "016x") == IDENTITY.root_id
    assert _attrs(provisional)["langfuse.trace.metadata.status"] == "running"
    assert provisional.start_time == provisional.end_time == int(T0.timestamp() * 1e9)

    trace_int = int(IDENTITY.trace_id, 16)
    root = spans["trial T-1/0"]
    assert root.context.trace_id == trace_int and root.parent is None
    assert format(root.context.span_id, "016x") == IDENTITY.root_id
    gen = spans["assistant turn 1"]
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
    assert gen_attrs["langfuse.trace.name"] == "pilot_agent/T-1"
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
    assert tool_attrs["langfuse.observation.type"] == "span"
    assert "sk-secret" not in tool_attrs["langfuse.observation.input"]
    assert tool_attrs["langfuse.observation.output"] == "file.txt"

    root_attrs = _attrs(root)
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
    assert root.name == "trial T-1/0" and root.status.status_code.name == "ERROR"
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
    assert [s.name for s in exporter.get_finished_spans()].count("trial T-1/0") == 2


def test_without_an_attachment_step_trial_persisted_is_a_no_op(tmp_path) -> None:
    exporter = InMemorySpanExporter()
    observer, _ = _observer(exporter)
    observer.trial_persisted(IDENTITY, trial_dir=tmp_path)
    receipt = observer.run_finished()
    assert (
        receipt.extra["langfuse.attachments_registered"] == 0
        and receipt.extra["langfuse.manifests_sent"] == 0
    )


# -- the write-once shape of a v4 receiver (ADR-0048, D-v4-2 shape C) ---------------------------


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
            "preview: trial T-1/0",
            "preview: assistant turn 1",
            "preview: tool: shell",
        }
        assert format(by_name["preview: trial T-1/0"].context.span_id, "016x") == preview_root
        # shape R: the preview root names the final root as its parent, so the trace has one root
        assert format(by_name["preview: trial T-1/0"].parent.span_id, "016x") == IDENTITY.root_id
        for name, kind, key in (
            ("preview: assistant turn 1", "pgen", (1,)),
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
        assert json.loads(attributes["langfuse.trace.metadata.attachments"]) == (
            manifest["attachments"]
        )
        assert attributes["langfuse.trace.metadata.attachments_complete"] is True
        assert attributes["langfuse.trace.metadata.status"] == "completed"
        assert receipt.extra["langfuse.manifests_sent"] == 1

    def test_the_scores_take_the_ingestion_route_with_the_gradings_own_time(
        self, tmp_path
    ) -> None:
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


class TestErrorRoots:
    def test_a_trial_that_never_persists_gets_one_minimal_root(self) -> None:
        exporter = InMemorySpanExporter()
        observer, _ = _v4_observer(exporter)
        _v4_trial(observer)
        observer.trial_finished(
            IDENTITY, trajectory=None, error="RuntimeError: the worker died"
        )
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

    def test_a_root_the_exporter_never_took_is_answered_with_one(self, tmp_path) -> None:
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
            """The batch that carries the final root is refused; the retry after it is not."""

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
        assert receipt.extra["langfuse.error_roots_sent"] == 1
        # the lost root's id was never taken, so the error root writes under it
        roots = [s for s in exporter.get_finished_spans() if s.parent is None]
        assert len(roots) == 1 and format(roots[0].context.span_id, "016x") == identity.root_id
        assert "did not reach the receiver" in _attrs(roots[0])["langfuse.trace.metadata.error"]


class TestTheIngestionHeader:
    def test_the_exporter_asks_for_the_direct_ingestion_path(self) -> None:
        from tolokaforge_langfuse.otel import INGESTION_VERSION_HEADER, make_otlp_exporter

        exporter = make_otlp_exporter("http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"})
        assert exporter._session.headers[INGESTION_VERSION_HEADER] == "4"

    def test_a_caller_may_turn_it_off_and_keeps_its_own_headers(self) -> None:
        from tolokaforge_langfuse.otel import INGESTION_VERSION_HEADER, make_otlp_exporter

        exporter = make_otlp_exporter(
            "http://127.0.0.1:9/v1/traces", {"Authorization": "Basic x"}, ingestion_version=None
        )
        assert INGESTION_VERSION_HEADER not in exporter._session.headers
        assert exporter._session.headers["Authorization"] == "Basic x"
