"""The default projection of a persisted trial (ADR-0047, parity amendment): the golden parity
test shared with the offline connector, and the trial-end pass of the OTLP observer."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import parity_bundle as pb
import pytest
from tolokaforge_langfuse.media import LangfuseApiError, iter_batches
from tolokaforge_langfuse.model_names import RawModelNameResolver, build_model_name_resolver
from tolokaforge_langfuse.projection import (
    LIVE_ONLY_KEYS,
    PRODUCER_KEYS,
    ProjectionContext,
    build_projection,
    schema_keys,
)

from tolokaforge.observability import ids
from tolokaforge.observability.observer import ModelRef, TrialIdentity

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).with_name("parity_golden.json")
IDENTITY = TrialIdentity(
    run_id=pb.RUN_ID,
    task_id=pb.TASK_ID,
    trial_index=pb.TRIAL_INDEX,
    attempt_id=pb.ATTEMPT_ID,
    run_tag=pb.RUN_TAG,
)


def _context(**overrides) -> ProjectionContext:
    params: dict = {
        "label": pb.LABEL,
        "session_id": pb.SESSION_ID,
        "tags": (),
        "metadata": dict(pb.CALLER_METADATA),
        "environment": pb.ENVIRONMENT,
        "release": "tolokaforge-0.0.0",
        "version": "tolokaforge-0.0.0+parity",
        "producer": "tolokaforge-0.0.0",
        "attach_mode": "all",
    }
    params.update(overrides)
    return ProjectionContext(**params)


def _tags(resolver) -> tuple[str, ...]:
    agent = resolver.resolve(*pb.AGENT_MODEL)
    return (
        "harness:tolokaforge",
        *agent.tags,
        f"task:{pb.TASK_ID}",
        *pb.CALLER_TAGS,
        f"project:{pb.PROJECT}",
        "source:trial",
    )


def _project(tmp_path: Path, resolver, **overrides):
    trial_dir = pb.write_parity_bundle(tmp_path / "run")
    return build_projection(
        IDENTITY, trial_dir, _context(tags=_tags(resolver), **overrides), resolver=resolver
    )


class TestGoldenParity:
    """The engine's projection of the synthetic bundle equals the connector's (the golden was
    produced by the connector's ``mapping.py`` over the same bundle) modulo envelope ids,
    timestamps, tag order and the documented producer keys."""

    def test_the_projection_equals_the_connectors_golden(self, tmp_path: Path) -> None:
        pytest.importorskip("toloka_model_name_normalizer")
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        projection = _project(tmp_path, build_model_name_resolver("toloka", None))
        assert pb.normalise_events(projection.events) == golden

    def test_without_the_normalizer_only_the_model_tags_differ(self, tmp_path: Path) -> None:
        # the slim schema carries no rule-derived facet: the trace differs from the golden only
        # in the vendor and family tags the normalizer adds
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        mine = pb.normalise_events(_project(tmp_path, RawModelNameResolver()).events)
        by_key = {(e["type"], e["body"]["id"]): e["body"] for e in golden}
        for event in mine:
            expected = by_key[(event["type"], event["body"]["id"])]
            if event["type"] != "trace-create":
                assert event["body"] == expected
                continue
            assert event["body"]["metadata"] == expected["metadata"]
            assert set(event["body"]["tags"]) ^ set(expected["tags"]) <= {
                "model_family:pilot",
                "model_vendor:acme",
            }

    def test_every_metadata_key_is_explicit_and_the_producer_keys_are_the_documented_ones(
        self, tmp_path: Path
    ) -> None:
        projection = _project(tmp_path, RawModelNameResolver())
        metadata = projection.trace_body["metadata"]
        assert None not in metadata.values()
        assert set(metadata) >= PRODUCER_KEYS
        assert metadata["upload_mode"] == "live" and metadata["trace_time_source"] == "live"
        assert metadata["uploader_version"] == "tolokaforge-0.0.0"
        assert (metadata["model_stem"], metadata["campaign"]) == ("pilot_agent", "parity")
        # the slim schema: the tags are not mirrored, the facets and the task facts stay in the
        # tags and the attached files, and the whole trace stays under the receiver's 100-key
        # table limit
        assert not {"project", "team", "ci_run", "model_vendor", "category", "api_calls"} & set(
            metadata
        )
        assert len(metadata) < 100
        assert metadata["user_model"] == "acme/sim-2" and metadata["judge_model"] == "acme/judge-3"
        assert metadata["judge_status"] == "ok" and metadata["pass"] is True
        assert projection.trace_body["environment"] == pb.ENVIRONMENT
        assert projection.trace_body["release"] == "tolokaforge-0.0.0"
        assert projection.trace_body["version"] == "tolokaforge-0.0.0+parity"
        # the live root span's own keys are not part of the bundle projection
        assert not LIVE_ONLY_KEYS & set(metadata)

    def test_observations_and_scores_carry_the_environment(self, tmp_path: Path) -> None:
        projection = _project(tmp_path, RawModelNameResolver())
        bodies = [e["body"] for e in projection.events if e["type"] != "trace-create"]
        assert bodies and all(b["environment"] == pb.ENVIRONMENT for b in bodies)
        without = _project(tmp_path / "b", RawModelNameResolver(), environment=None)
        assert without.trace_body["environment"] is None
        assert all(
            "environment" not in e["body"] for e in without.events if e["type"] != "trace-create"
        )

    def test_the_shape_of_the_event_list(self, tmp_path: Path) -> None:
        projection = _project(tmp_path, RawModelNameResolver())
        names = sorted(e["body"]["name"] for e in projection.events if e["type"] != "score-create")
        trace = IDENTITY.trace_id
        # root, 3 agent turns, 2 tool spans from the log, the simulator's own tool, 2 user turns,
        # 1 INFO + 1 WARNING log event, the guard event, the limit hit, the grading + its judge
        assert names.count("trial T-001/0") == 1
        assert [n for n in names if n.startswith("assistant turn")] == [
            "assistant turn 1",
            "assistant turn 3",
            "assistant turn 5",
        ]
        assert [n for n in names if n.startswith("tool:")] == [
            "tool: assign_seat",
            "tool: search_booking",
            "tool: sim_lookup_ticket",
        ]
        assert [n for n in names if n.startswith("user turn")] == ["user turn 0", "user turn 6"]
        assert [n for n in names if n.startswith("log:")] == [
            "log: Starting trial execution",
            "log: Tool execution failed",
        ]
        assert "user reply guard: accepted" in names and "run budget hit: cost" in names
        assert f"grading:live:{pb.RUN_ID}" in names and "judge turn 2" in names
        tool = next(
            e["body"]
            for e in projection.events
            if e["body"].get("id") == ids.observation_id(trace, "tool", "call_2")
        )
        # the grader's record is the authority; the transcript's text rides beside it
        assert tool["level"] == "ERROR" and tool["metadata"]["status"] == "error"
        assert tool["metadata"]["transcript_output_differs"] is True
        assert "Error: seat map unavailable" in tool["metadata"]["transcript_output"]
        # the image block of the tool message became a placeholder, never raw base64
        assert "iVBOR" not in json.dumps(tool["output"])
        assert projection.stats.scores == 2 * 8  # grading scores + the trace-level mirror
        assert projection.stats.user_generations == 2 and projection.stats.grading_id
        assert projection.stats.usage_match == "generation_id"

    def test_a_media_handler_replaces_the_image_block_with_its_token(self, tmp_path: Path) -> None:
        calls: list[tuple[str, str, str, str, int]] = []

        def media(trace_id, observation_id, field, content_type, raw):
            calls.append((trace_id, observation_id, field, content_type, len(raw)))
            return "@@@langfuseMedia:type=image/png|id=m1|source=bytes@@@"

        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        resolver = RawModelNameResolver()
        projection = build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver, media=media
        )
        tool_id = ids.observation_id(IDENTITY.trace_id, "tool", "call_2")
        assert calls == [(IDENTITY.trace_id, tool_id, "output", "image/png", len(pb.PNG))]
        tool = next(e["body"] for e in projection.events if e["body"].get("id") == tool_id)
        assert "@@@langfuseMedia:type=image/png|id=m1|source=bytes@@@" in json.dumps(tool["output"])
        assert projection.stats.media_uploaded == 1

    def test_grades_off_leaves_the_grading_and_scores_out(self, tmp_path: Path) -> None:
        projection = _project(tmp_path, RawModelNameResolver(), grades=False)
        assert not [e for e in projection.events if e["type"] == "score-create"]
        assert not [
            e for e in projection.events if e["body"].get("name", "").startswith("grading:")
        ]
        metadata = projection.trace_body["metadata"]
        assert metadata["primary_grading"] == "none" and metadata["grading_count"] == 0
        assert metadata["pass"] == "none"

    def test_schema_keys_cover_the_projection_and_the_live_keys(self) -> None:
        keys = schema_keys()
        assert {"task_id", "attachments", "pass", "judge_status", "user_model", "error"} <= keys
        assert "model_stem" not in keys and "team" not in keys


class TestBatches:
    def test_batches_respect_count_and_bytes(self) -> None:
        events = [{"id": str(i), "body": {"x": "y" * 100}} for i in range(5)]
        assert [len(b) for b in iter_batches(events, batch_size=2)] == [2, 2, 1]
        assert [len(b) for b in iter_batches(events, batch_size=10, max_bytes=250)] == [
            1,
            1,
            1,
            1,
            1,
        ]


class _Step:
    """A receiver that answers the trial-end pass like ``LangfuseAttachments`` does."""

    mode = "all"

    def __init__(self, *, fail_ingest: bool = False, tripped: bool = False) -> None:
        self.batches: list[list[dict]] = []
        self.attached: list[str] = []
        self.media: list[str] = []
        self.budgets = 0
        self.fail_ingest = fail_ingest
        self.tripped = tripped
        self.scanned = 0

    def budget(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            self.budgets += 1
            yield

        return scope()

    def attach_with_manifest(self, trace_id, trial_dir, *, trace_timestamp=None, metadata=None):
        from tolokaforge_langfuse.attachments import AttachCounts

        self.attached.append(trace_id)
        manifest = {
            "attachments_schema": 2,
            "attachments": {"task.yaml": {"media_id": "m9"}},
            "attachments_complete": True,
            "attachments_skipped": [],
        }
        return AttachCounts(registered=1, manifests_sent=1), manifest

    def register_media(self, trace_id, observation_id, field, content_type, raw):
        self.media.append(observation_id)
        return f"@@@langfuseMedia:type={content_type}|id=m-inline|source=bytes@@@"

    def scan_events(self, events):
        self.scanned += 1
        return []

    def ingest(self, events, *, batch_size=40):
        if self.fail_ingest:
            raise LangfuseApiError("POST /api/public/ingestion: HTTP 500", status=500)
        self.batches.append(events)


class TestObserverProjection:
    def _observer(self, step, **kwargs):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from tolokaforge_langfuse.otel import OTelTrialObserver, ProjectionSettings, SpanQueue

        queue = SpanQueue(InMemorySpanExporter(), max_size=100, batch_size=4, interval_s=0.05)
        settings = kwargs.pop(
            "projection",
            ProjectionSettings(
                mode="full",
                environment=pb.ENVIRONMENT,
                release="tolokaforge-0.0.0",
                version="tolokaforge-0.0.0+parity",
                producer="tolokaforge-0.0.0",
            ),
        )
        return OTelTrialObserver(
            queue=queue,
            label=pb.LABEL,
            session_id=pb.SESSION_ID,
            tags=(*pb.CALLER_TAGS, f"project:{pb.PROJECT}", "source:trial"),
            metadata=dict(pb.CALLER_METADATA),
            attachments=step,
            projection=settings,
            **kwargs,
        )

    def test_the_trial_end_pass_completes_the_trace_from_the_bundle(self, tmp_path: Path) -> None:
        step = _Step()
        observer = self._observer(step)
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        observer.trial_started(
            IDENTITY,
            models={"agent": ModelRef(*pb.AGENT_MODEL), "judge": ModelRef(*pb.JUDGE_MODEL)},
            started_at=datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
        )
        observer.trial_finished(IDENTITY, trajectory=None)
        observer.trial_persisted(IDENTITY, trial_dir=trial_dir)
        receipt = observer.run_finished()
        assert step.attached == [IDENTITY.trace_id] and step.budgets == 1 and step.scanned == 1
        (batch,) = step.batches
        trace = next(e["body"] for e in batch if e["type"] == "trace-create")
        assert trace["environment"] == pb.ENVIRONMENT and trace["release"] == "tolokaforge-0.0.0"
        assert trace["version"] == "tolokaforge-0.0.0+parity"
        assert trace["sessionId"] == pb.SESSION_ID and trace["name"] == f"{pb.LABEL}/{pb.TASK_ID}"
        # the tags of the trial's start (harness, model, task, launcher) travel with the pass
        assert set(trace["tags"]) >= {"harness:tolokaforge", f"task:{pb.TASK_ID}", *pb.CALLER_TAGS}
        assert f"model:{pb.AGENT_MODEL[1]}" in trace["tags"]
        metadata = trace["metadata"]
        # the manifest of the attachment step is the one the projection carries, complete
        assert metadata["attachments"] == {"task.yaml": {"media_id": "m9"}}
        assert metadata["attachments_complete"] is True
        assert metadata["model_name"] == pb.AGENT_MODEL[1]
        # inline media went through the receiver's media route
        assert step.media == [ids.observation_id(IDENTITY.trace_id, "tool", "call_2")]
        kinds = {e["type"] for e in batch}
        assert kinds == {
            "trace-create",
            "span-create",
            "generation-create",
            "event-create",
            "score-create",
        }
        assert (receipt.projections_sent, receipt.projections_failed) == (1, 0)
        assert receipt.observations_sent == 11 and receipt.events_sent == 4
        assert receipt.scores_sent == 16 and receipt.gradings_sent == 1
        assert receipt.user_generations_sent == 2 and receipt.media_uploaded == 1
        assert receipt.to_dict()["projections_sent"] == 1

    def test_the_provisional_root_span_carries_the_native_fields(self, tmp_path: Path) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from tolokaforge_langfuse.otel import OTelTrialObserver, ProjectionSettings, SpanQueue

        exporter = InMemorySpanExporter()
        queue = SpanQueue(exporter, max_size=10, batch_size=1, interval_s=0.05)
        observer = OTelTrialObserver(
            queue=queue,
            label="l",
            session_id="s",
            projection=ProjectionSettings(
                environment="development", release="tolokaforge-0.0.0", version="v"
            ),
        )
        observer.trial_started(
            IDENTITY, models={}, started_at=datetime(2026, 9, 17, tzinfo=timezone.utc)
        )
        observer.run_finished()
        (span,) = exporter.get_finished_spans()
        attributes = dict(span.attributes)
        # Langfuse fixes a trace's environment at the first write it sees: the first span is
        # the provisional root, so it must already say where the trace belongs
        assert attributes["langfuse.environment"] == "development"
        assert attributes["langfuse.release"] == "tolokaforge-0.0.0"
        assert attributes["langfuse.version"] == "v"
        assert attributes["langfuse.trace.metadata.status"] == "running"

    def test_a_refused_pass_is_counted_and_never_raises(self, tmp_path: Path) -> None:
        step = _Step(fail_ingest=True)
        observer = self._observer(step)
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert (receipt.projections_sent, receipt.projections_failed) == (0, 1)
        assert receipt.gradings_sent == 0 and receipt.scores_sent == 0

    def test_a_tripped_breaker_skips_the_pass(self, tmp_path: Path) -> None:
        step = _Step(tripped=True)
        observer = self._observer(step)
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.batches == [] and receipt.projections_failed == 1

    def test_a_data_safety_hit_blocks_the_pass(self, tmp_path: Path) -> None:
        step = _Step()
        step.scan_events = lambda events: ["openrouter-key (sk-o**** (40 chars))"]  # type: ignore[method-assign]
        observer = self._observer(step)
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.batches == [] and receipt.projections_failed == 1

    def test_projection_none_sends_only_the_attachments(self, tmp_path: Path) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        step = _Step()
        observer = self._observer(step, projection=ProjectionSettings(mode="none"))
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.attached == [IDENTITY.trace_id] and step.batches == []
        assert receipt.projections_sent == 0 and receipt.attachments_registered == 1

    def test_a_bare_receiver_with_only_attach_and_ingest_still_works(self, tmp_path: Path) -> None:
        class Bare:
            mode = "none"
            batches: list = []

            def attach(self, *a, **k):
                raise AssertionError("mode none never attaches")

            def ingest(self, events, *, batch_size=40):
                Bare.batches.append(events)

        observer = self._observer(Bare())
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert len(Bare.batches) == 1 and receipt.projections_sent == 1
        trace = next(e["body"] for e in Bare.batches[0] if e["type"] == "trace-create")
        assert trace["metadata"]["attachments"] == {} and trace["metadata"]["attach_mode"] == "none"


def test_the_png_of_the_bundle_is_a_real_image() -> None:
    assert base64.b64encode(pb.PNG).decode().startswith("iVBORw0KGgo")
