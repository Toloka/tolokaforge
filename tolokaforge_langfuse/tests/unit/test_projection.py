"""The default projection of a persisted trial (ADR-0047, parity amendment): the golden parity
test shared with the offline connector, and the trial-end pass of the OTLP observer."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import parity_bundle as pb
import pytest
import yaml
from tolokaforge_langfuse.gradings import message_window
from tolokaforge_langfuse.media import LangfuseApiError, iter_batches
from tolokaforge_langfuse.model_names import (
    ModelNameResolverError,
    RawModelNameResolver,
    build_model_name_resolver,
)
from tolokaforge_langfuse.projection import (
    LIVE_ONLY_KEYS,
    PRODUCER_KEYS,
    USAGE_MATCH_GENERATION_ID,
    USAGE_MATCH_POSITIONAL,
    ProjectionContext,
    build_projection,
    schema_keys,
    usage_fields,
)
from tolokaforge_langfuse.safety import SafetyGate

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
        "trace_name": pb.TRACE_NAME,
        "trace_user": pb.TRACE_USER,
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
                "model_generation:1",  # the facets are the normalizer's too
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

        def positions(name: str) -> list[int]:
            return sorted(
                e["body"]["metadata"]["message_index"]
                for e in projection.events
                if e["type"] == "generation-create" and e["body"]["name"] == name
            )

        # root, 3 agent turns, 2 tool spans from the log, the simulator's own tool, 2 user turns,
        # 1 INFO + 1 WARNING log event, the guard event, the limit hit, the grading + its judge;
        # a name says what an observation is, its position is metadata
        assert names.count("trial") == 1
        assert positions("agent") == [1, 3, 5]
        assert [n for n in names if n.startswith("tool:")] == [
            "tool: assign_seat",
            "tool: search_booking",
            "tool: sim_lookup_ticket",
        ]
        assert positions("user simulator") == [0, 6]
        assert [n for n in names if n.startswith("log:")] == [
            "log: Starting trial execution",
            "log: Tool execution failed",
        ]
        assert "user reply guard: accepted" in names and "run budget hit: cost" in names
        assert "grading" in names and "judge" in names
        # the root is the agent, a tool execution a tool, the grading an evaluator
        kinds = {e["type"] for e in projection.events}
        assert {"agent-create", "tool-create", "evaluator-create"} <= kinds
        assert "span-create" not in kinds
        (root,) = [e["body"] for e in projection.events if e["type"] == "agent-create"]
        assert root["name"] == "trial" and "parentObservationId" not in root
        assert all(
            e["body"]["name"].startswith(("tool:", "judge tool:"))
            for e in projection.events
            if e["type"] == "tool-create"
        )
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
        # the grading's scores, the trace-level mirror of the same, plus the mirror's
        # primary_grading pointer
        assert projection.stats.scores == 2 * 8 + 1
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
        assert not [e for e in projection.events if e["type"] == "evaluator-create"]
        metadata = projection.trace_body["metadata"]
        assert metadata["primary_grading"] == "none" and metadata["grading_count"] == 0
        assert metadata["pass"] == "none"

    def test_schema_keys_cover_the_projection_and_the_live_keys(self) -> None:
        keys = schema_keys()
        assert {"task_id", "attachments", "pass", "judge_status", "user_model", "error"} <= keys
        assert "model_stem" not in keys and "team" not in keys


class TestTheClocks:
    """A message's clock is when it was recorded, so what produced it (the model call of a
    generation, a tool's execution) ran from the message before it to its own clock."""

    @staticmethod
    def _windows(projection, kind: str, name: str | None = None) -> dict[int, tuple[str, str]]:
        return {
            e["body"]["metadata"]["message_index"]: (e["body"]["startTime"], e["body"]["endTime"])
            for e in projection.events
            if e["type"] == kind and name in (None, e["body"]["name"])
        }

    def test_a_generation_spans_its_call(self, tmp_path: Path) -> None:
        projection = _project(tmp_path, RawModelNameResolver())
        assert self._windows(projection, "generation-create", "agent") == {
            1: ("2026-09-17T09:00:01.000000Z", "2026-09-17T09:00:05.000000Z"),
            3: ("2026-09-17T09:00:06.000000Z", "2026-09-17T09:00:10.000000Z"),
            5: ("2026-09-17T09:00:12.000000Z", "2026-09-17T09:00:20.000000Z"),
        }
        # the simulator's turns alike; its opening turn's call started with the trial
        assert self._windows(projection, "generation-create", "user simulator") == {
            0: ("2026-09-17T09:00:00.000000+00:00", "2026-09-17T09:00:01.000000Z"),
            6: ("2026-09-17T09:00:20.000000Z", "2026-09-17T09:00:30.000000Z"),
        }

    def test_a_tool_without_a_log_spans_its_execution(self, tmp_path: Path) -> None:
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        (trial_dir / "tool_log.yaml").unlink()
        resolver = RawModelNameResolver()
        projection = build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        )
        assert self._windows(projection, "tool-create") == {
            2: ("2026-09-17T09:00:05.000000Z", "2026-09-17T09:00:06.000000Z"),
            4: ("2026-09-17T09:00:10.000000Z", "2026-09-17T09:00:12.000000Z"),
        }

    def test_a_missing_clock_collapses_the_window_never_stretches_it(self) -> None:
        messages = [
            {"role": "user", "ts": "2026-09-17T09:00:01Z"},
            {"role": "assistant"},
            {"role": "user", "ts": "2026-09-17T09:00:09Z"},
        ]
        start = "2026-09-17T09:00:00Z"
        assert message_window(messages, 0, start=start) == (start, "2026-09-17T09:00:01Z")
        assert message_window(messages, 1, start=start) == (
            "2026-09-17T09:00:01Z",
            "2026-09-17T09:00:01Z",
        )
        assert message_window(messages, 2, start=start) == (
            "2026-09-17T09:00:09Z",
            "2026-09-17T09:00:09Z",
        )


class TestTheNameAndTheUser:
    """The deployment's profile names a trace and says who its user is (``[trace]``)."""

    def _trace(self, tmp_path: Path, **overrides) -> dict:
        resolver = RawModelNameResolver()
        return build_projection(
            IDENTITY,
            pb.write_parity_bundle(tmp_path / "run"),
            _context(tags=overrides.pop("tags", _tags(resolver)), **overrides),
            resolver=resolver,
        ).trace_body

    def test_the_profiles_template_names_the_trace_and_the_model_is_its_user(
        self, tmp_path: Path
    ) -> None:
        trace = self._trace(tmp_path)
        assert trace["name"] == "pilot/pilot-domain"
        assert trace["userId"] == pb.AGENT_MODEL[1] == trace["metadata"]["model_name"]

    def test_without_a_profile_the_run_and_the_task_name_it_and_it_has_no_user(
        self, tmp_path: Path
    ) -> None:
        trace = self._trace(tmp_path, trace_name=None, trace_user="none")
        assert trace["name"] == f"{pb.LABEL}/{pb.TASK_ID}"
        assert trace["userId"] is None

    def test_an_agent_the_rules_cannot_read_names_no_user(self, tmp_path: Path) -> None:
        """No identity, no user: the live rows follow the same rule."""

        class Refusing:
            description = "refusing"
            rules_version = "r-1"

            def resolve(self, provider, name):
                raise ModelNameResolverError(f"{name}: unresolved tokens")

        trace = build_projection(
            IDENTITY,
            pb.write_parity_bundle(tmp_path / "run"),
            _context(tags=_tags(RawModelNameResolver())),
            resolver=Refusing(),
        ).trace_body
        assert trace["userId"] is None and trace["metadata"]["model_name"] == "none"

    def test_a_template_naming_a_value_the_trace_lacks_falls_back_to_the_default(
        self, tmp_path: Path
    ) -> None:
        resolver = RawModelNameResolver()
        tags = tuple(t for t in _tags(resolver) if not t.startswith("domain:"))
        assert self._trace(tmp_path, tags=tags)["name"] == f"{pb.LABEL}/{pb.TASK_ID}"


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

    def ingest(self, events, *, batch_size=40):
        if self.fail_ingest:
            raise LangfuseApiError("POST /api/public/ingestion: HTTP 500", status=500)
        self.batches.append(events)


class _CountingGate(SafetyGate):
    """A gate that counts the structured scans it was asked for."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.scans = 0

    def scan_structured(self, value, *, what="payload"):
        self.scans += 1
        return super().scan_structured(value, what=what)


class TestObserverProjection:
    def _observer(self, step, **kwargs):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from tolokaforge_langfuse.otel import OTelTrialObserver, ProjectionSettings, SpanQueue

        queue = SpanQueue(InMemorySpanExporter(), max_size=100, batch_size=4, interval_s=0.05)
        # hermetic: the developer's environment holds no credential this observer knows
        kwargs.setdefault("gate", SafetyGate())
        settings = kwargs.pop(
            "projection",
            ProjectionSettings(
                mode="full",
                environment=pb.ENVIRONMENT,
                release="tolokaforge-0.0.0",
                version="tolokaforge-0.0.0+parity",
                producer="tolokaforge-0.0.0",
                trace_name=pb.TRACE_NAME,
                trace_user=pb.TRACE_USER,
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
        gate = _CountingGate()
        observer = self._observer(step, gate=gate)
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        observer.trial_started(
            IDENTITY,
            models={"agent": ModelRef(*pb.AGENT_MODEL), "judge": ModelRef(*pb.JUDGE_MODEL)},
            started_at=datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
        )
        observer.trial_finished(IDENTITY, trajectory=None)
        observer.trial_persisted(IDENTITY, trial_dir=trial_dir)
        receipt = observer.run_finished()
        assert step.attached == [IDENTITY.trace_id] and step.budgets == 1
        # the pass was scanned once, with the observer's gate (the step has no scanner)
        assert gate.scans >= 1 and receipt.extra["langfuse.projections_refused_secret"] == 0
        (batch,) = step.batches
        trace = next(e["body"] for e in batch if e["type"] == "trace-create")
        assert trace["environment"] == pb.ENVIRONMENT and trace["release"] == "tolokaforge-0.0.0"
        assert trace["version"] == "tolokaforge-0.0.0+parity"
        # named after the trial's dataset and domain; the agent's model is the trace's user
        assert trace["sessionId"] == pb.SESSION_ID and trace["name"] == "pilot/pilot-domain"
        assert trace["userId"] == pb.AGENT_MODEL[1]
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
            "agent-create",
            "tool-create",
            "evaluator-create",
            "generation-create",
            "event-create",
            "score-create",
        }
        assert (
            receipt.extra["langfuse.projections_sent"],
            receipt.extra["langfuse.projections_failed"],
        ) == (1, 0)
        assert (
            receipt.extra["langfuse.observations_sent"] == 11
            and receipt.extra["langfuse.events_sent"] == 4
        )
        assert (
            receipt.extra["langfuse.scores_sent"] == 17
            and receipt.extra["langfuse.gradings_sent"] == 1
        )
        assert (
            receipt.extra["langfuse.user_generations_sent"] == 2
            and receipt.extra["langfuse.media_uploaded"] == 1
        )
        assert receipt.model_dump(mode="json")["extra"]["langfuse.projections_sent"] == 1

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
        assert (
            receipt.extra["langfuse.projections_sent"],
            receipt.extra["langfuse.projections_failed"],
        ) == (0, 1)
        assert (
            receipt.extra["langfuse.gradings_sent"] == 0
            and receipt.extra["langfuse.scores_sent"] == 0
        )

    def test_a_tripped_breaker_skips_the_pass(self, tmp_path: Path) -> None:
        step = _Step(tripped=True)
        observer = self._observer(step)
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.batches == [] and receipt.extra["langfuse.projections_failed"] == 1

    @staticmethod
    def _bundle_carrying(tmp_path: Path, secret: str) -> Path:
        """The parity bundle with ``secret`` in the agent's words and in the grader's reasons."""
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        trajectory = pb.trajectory()
        assistant = next(m for m in trajectory["messages"] if m["role"] == "assistant")
        assistant["content"] = f"the token is {secret}"
        (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory), encoding="utf-8")
        grade = pb.grade()
        grade["reasons"] = f"the agent leaked {secret}"
        (trial_dir / "grade.yaml").write_text(yaml.safe_dump(grade), encoding="utf-8")
        return trial_dir

    def test_a_secret_only_the_observers_gate_knows_withholds_the_pass(
        self, tmp_path: Path
    ) -> None:
        """``DB_PASSWORD`` is no name the SecretManager's list holds, so the attachment step's own
        scan never knew it and the pass was sent. The step here has no scanner at all: the pass
        scans with the observer's gate, and a missing scanner never means send."""
        value = "a-db-password-no-shape-matches"
        step = _Step()
        observer = self._observer(step, gate=SafetyGate.from_environment({"DB_PASSWORD": value}))
        observer.trial_persisted(IDENTITY, trial_dir=self._bundle_carrying(tmp_path, value))
        receipt = observer.run_finished()
        assert step.batches == []
        assert (
            receipt.extra["langfuse.projections_refused_secret"],
            receipt.extra["langfuse.projections_failed"],
            receipt.extra["langfuse.projections_sent"],
        ) == (1, 0, 0)

    def test_a_secret_json_would_escape_blocks_the_pass_too(self, tmp_path: Path) -> None:
        """A known value with a quote and a backslash in a projected event's output hides from
        the serialised events; the pass is scanned like a live span, in the raw strings too."""
        awkward = 'tok"en\\8f3a91c2b7d04e56'
        gate = SafetyGate.from_environment({"DB_PASSWORD": awkward})
        trial_dir = self._bundle_carrying(tmp_path, awkward)
        resolver = RawModelNameResolver()
        events = build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        ).events
        assert gate.scan(json.dumps(events, ensure_ascii=False).encode()) == []  # the old gate
        step = _Step()
        observer = self._observer(step, gate=gate)
        observer.trial_persisted(IDENTITY, trial_dir=trial_dir)
        receipt = observer.run_finished()
        assert step.batches == [] and receipt.extra["langfuse.projections_refused_secret"] == 1

    def test_a_pass_the_gate_cannot_scan_is_a_failure_not_a_refusal(self, tmp_path: Path) -> None:
        class Broken(SafetyGate):
            def scan_structured(self, value, *, what="payload"):
                raise ValueError("boom")

        step = _Step()
        observer = self._observer(step, gate=Broken())
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.batches == []
        assert (
            receipt.extra["langfuse.projections_failed"],
            receipt.extra["langfuse.projections_refused_secret"],
        ) == (1, 0)

    def test_the_gradings_pass_is_scanned_with_the_observers_gate_before_it_is_sent(
        self, tmp_path: Path
    ) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        step = _Step()
        gate = _CountingGate()
        observer = self._observer(step, gate=gate, projection=ProjectionSettings(mode="gradings"))
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert gate.scans == 1 and len(step.batches) == 1
        assert receipt.extra["langfuse.gradings_sent"] == 1

    def test_a_secret_withholds_the_gradings_pass_and_is_no_failure(self, tmp_path: Path) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        value = "a-db-password-no-shape-matches"
        step = _Step()
        observer = self._observer(
            step,
            gate=SafetyGate.from_environment({"DB_PASSWORD": value}),
            projection=ProjectionSettings(mode="gradings"),
        )
        observer.trial_persisted(IDENTITY, trial_dir=self._bundle_carrying(tmp_path, value))
        receipt = observer.run_finished()
        assert step.batches == []
        assert (
            receipt.extra["langfuse.gradings_refused_secret"],
            receipt.extra["langfuse.gradings_failed"],
            receipt.extra["langfuse.gradings_sent"],
        ) == (1, 0, 0)

    def test_projection_none_sends_only_the_attachments(self, tmp_path: Path) -> None:
        from tolokaforge_langfuse.otel import ProjectionSettings

        step = _Step()
        observer = self._observer(step, projection=ProjectionSettings(mode="none"))
        observer.trial_persisted(IDENTITY, trial_dir=pb.write_parity_bundle(tmp_path / "run"))
        receipt = observer.run_finished()
        assert step.attached == [IDENTITY.trace_id] and step.batches == []
        assert (
            receipt.extra["langfuse.projections_sent"] == 0
            and receipt.extra["langfuse.attachments_registered"] == 1
        )

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
        assert len(Bare.batches) == 1 and receipt.extra["langfuse.projections_sent"] == 1
        trace = next(e["body"] for e in Bare.batches[0] if e["type"] == "trace-create")
        assert trace["metadata"]["attachments"] == {} and trace["metadata"]["attach_mode"] == "none"


def test_the_png_of_the_bundle_is_a_real_image() -> None:
    assert base64.b64encode(pb.PNG).decode().startswith("iVBORw0KGgo")


class TestAgentOpeningLine:
    """A trial run with ``actors.user.first_agent_message`` opens its transcript with
    the agent's line: an agent message that no model generated."""

    LINE = "Hi! How can I help you today?"

    def _projection(
        self,
        tmp_path: Path,
        *,
        pinned: bool = False,
        generation_ids: bool = True,
        declared: bool = True,
    ):
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        trajectory = pb.trajectory()
        line = {
            **trajectory["messages"][5],
            "content": self.LINE,
            "openrouter_generation_id": None,
            "ts": "2026-09-17T09:00:00.100000Z",
        }
        trajectory["messages"] = [line, *trajectory["messages"]]
        if pinned:
            trajectory["first_user_message_source"] = "pinned"
            trajectory["messages"][1]["openrouter_generation_id"] = None
        if not generation_ids:
            for message in trajectory["messages"]:
                message["openrouter_generation_id"] = None
        (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory), encoding="utf-8")
        if declared:
            task = pb.task()
            task["user_actor"] = {**task["user_actor"], "first_agent_message": self.LINE}
            (trial_dir / "task.yaml").write_text(yaml.safe_dump(task), encoding="utf-8")
        resolver = RawModelNameResolver()
        return build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        )

    @staticmethod
    def _generations(projection) -> list[tuple[str, int]]:
        return [
            (e["body"]["name"], e["body"]["metadata"].get("message_index"))
            for e in projection.events
            if e["type"] == "generation-create"
        ]

    def test_the_line_is_an_event_and_no_generation(self, tmp_path: Path) -> None:
        projection = self._projection(tmp_path)
        line = [
            e["body"]
            for e in projection.events
            if e["type"] == "event-create" and e["body"]["name"] == "agent opening line"
        ]
        assert [event["output"] for event in line] == [self.LINE]
        assert ("agent", 0) not in self._generations(projection)

    @pytest.mark.parametrize(
        ("generation_ids", "match"),
        [(True, USAGE_MATCH_GENERATION_ID), (False, USAGE_MATCH_POSITIONAL)],
    )
    def test_usage_still_pairs_with_every_generation(
        self, tmp_path: Path, generation_ids: bool, match: str
    ) -> None:
        projection = self._projection(tmp_path, generation_ids=generation_ids)
        assert projection.stats.usage_match == match
        costs = [
            e["body"].get("costDetails")
            for e in projection.events
            if e["type"] == "generation-create" and e["body"]["name"] == "agent"
        ]
        # each generation is priced off its own call: the second call's stated charge (0.0025)
        # rather than its eval figure (0.002) shows the pairing reached the right record
        assert costs == [{"total": 0.001}, {"total": 0.0025}, {"total": 0.003}]

    def test_an_undeclared_leading_agent_turn_stays_a_generation(self, tmp_path: Path) -> None:
        """The line is read from ``task.yaml``, not guessed from the transcript's shape."""
        projection = self._projection(tmp_path, declared=False)
        assert ("agent", 0) in self._generations(projection)
        assert not any(e["body"]["name"] == "agent opening line" for e in projection.events)

    def test_a_pinned_opener_after_the_line_is_no_user_generation(self, tmp_path: Path) -> None:
        projection = self._projection(tmp_path, pinned=True)
        assert ("user simulator", 1) not in self._generations(projection)
        assert ("user simulator", 7) in self._generations(projection)


class TestUserToolSteps:
    """A trial run with ``actors.user.tool_turns: isolated`` records the simulator's
    tool steps as a user message carrying calls, then a tool message per result."""

    @staticmethod
    def _isolated_bundle(tmp_path: Path, *, with_tool_log: bool) -> Path:
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        trajectory = pb.trajectory()
        step_call = {"id": "u1", "name": "check_booking_app", "arguments": {"pnr": "PLT001"}}
        step = {**trajectory["messages"][0], "content": "", "tool_calls": [step_call]}
        result = {
            **trajectory["messages"][2],
            "content": '{"pnr": "PLT001"}',
            "tool_call_id": "u1",
            "ts": "2026-09-17T09:00:00.500000Z",
        }
        step["ts"] = "2026-09-17T09:00:00.200000Z"
        trajectory["messages"] = [step, result, *trajectory["messages"]]
        (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory), encoding="utf-8")
        if not with_tool_log:
            (trial_dir / "tool_log.yaml").unlink()
        return trial_dir

    def _projection(self, tmp_path: Path, *, with_tool_log: bool):
        trial_dir = self._isolated_bundle(tmp_path, with_tool_log=with_tool_log)
        resolver = RawModelNameResolver()
        return build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        )

    def test_the_trace_input_is_the_opening_not_the_step_before_it(self, tmp_path: Path) -> None:
        projection = self._projection(tmp_path, with_tool_log=True)
        assert projection.trace_body["input"] == pb.trajectory()["messages"][0]["content"]

    def test_the_step_generation_shows_its_calls(self, tmp_path: Path) -> None:
        projection = self._projection(tmp_path, with_tool_log=True)
        step = next(
            e["body"]
            for e in projection.events
            if e["type"] == "generation-create"
            and e["body"]["name"] == "user simulator"
            and e["body"]["metadata"]["message_index"] == 0
        )
        assert step["output"]["tool_calls"][0]["name"] == "check_booking_app"

    def test_without_a_tool_log_the_step_result_is_the_users(self, tmp_path: Path) -> None:
        projection = self._projection(tmp_path, with_tool_log=False)
        roles = {
            e["body"]["metadata"]["call_id"]: e["body"]["metadata"]["role"]
            for e in projection.events
            if (e["body"].get("metadata") or {}).get("kind") == "tool"
        }
        assert roles["u1"] == "user_tool"
        assert roles["call_1"] == "agent_tool"


class TestTheUsageBreakdown:
    """A generation's usage is a breakdown that adds up: the engine's prompt total holds the cache
    reads and the cache writes (``pricing.estimate_cost``), so each leaves ``input`` once."""

    CALL = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 200,
    }

    @staticmethod
    def _components(details: dict) -> int:
        return sum(value for key, value in details.items() if key != "total")

    def test_cache_reads_and_writes_leave_the_input_once(self) -> None:
        details, metadata = usage_fields(self.CALL)
        assert details == {
            "input": 700,
            "output": 20,
            "total": 1020,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 200,
        }
        assert self._components(details) == details["total"]
        assert metadata["usage_clamped"] is False

    @pytest.mark.parametrize(
        ("counters", "input_tokens"),
        [
            ({}, 1000),
            ({"cache_read_input_tokens": 100}, 900),
            ({"cache_creation_input_tokens": 200}, 800),
            ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, 1000),
        ],
    )
    def test_a_call_with_fewer_counters_takes_out_only_what_it_states(
        self, counters: dict, input_tokens: int
    ) -> None:
        details, metadata = usage_fields(
            {"prompt_tokens": 1000, "completion_tokens": 20, **counters}
        )
        assert details["input"] == input_tokens and details["total"] == 1020
        assert self._components(details) == details["total"]
        assert metadata["usage_clamped"] is False

    def test_counters_larger_than_the_prompt_floor_the_input_and_say_so(self) -> None:
        details, metadata = usage_fields({**self.CALL, "prompt_tokens": 250})
        assert details["input"] == 0 and details["total"] == 270
        assert metadata["usage_clamped"] is True


class TestCostOnTheTrace:
    """A generation's cost is the charge the provider stated, else the eval's own figure, and
    ``cost_basis`` says which; every LLM call of the bundle is counted on exactly one
    generation, so the trace's cost is the cost of every call the bundle records. The golden
    pins the billed path with the agent's and the user simulator's calls paired by generation
    id; these pin the rest."""

    def _projection(
        self,
        tmp_path: Path,
        *,
        calls: list[dict],
        judge_usage: dict | None = None,
        generation_ids: bool = True,
        ending: str | None = None,
    ):
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        metrics = pb.metrics()
        metrics["usage"]["calls"] = calls
        (trial_dir / "metrics.yaml").write_text(yaml.safe_dump(metrics), encoding="utf-8")
        if judge_usage is not None:
            grade = pb.grade()
            grade["judge_usage"] = judge_usage
            (trial_dir / "grade.yaml").write_text(yaml.safe_dump(grade), encoding="utf-8")
        trajectory = pb.trajectory()
        if not generation_ids:
            # a route other than OpenRouter: no generation id on any message (the engine stamps
            # none on a user message on any route)
            for message in trajectory["messages"]:
                message["openrouter_generation_id"] = None
        if ending is not None:
            # the simulator's second reply was the stop token alone (``stop_with_text:
            # deliver``): no user message, the loop's own system line in its place
            trajectory["messages"] = [
                *trajectory["messages"][:6],
                {
                    "role": "system",
                    "content": "User signaled stop (###STOP###). Dialogue ended.",
                    "ts": trajectory["messages"][6]["ts"],
                },
            ]
            trajectory["termination_reason"] = ending
        (trial_dir / "trajectory.yaml").write_text(yaml.safe_dump(trajectory), encoding="utf-8")
        resolver = RawModelNameResolver()
        return build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        )

    @staticmethod
    def _without_the_charge(call: dict, source: str = "litellm") -> dict:
        legacy = {k: v for k, v in call.items() if k != "billed_cost_usd"}
        return {**legacy, "cost_source": source}

    @staticmethod
    def _generations(projection, name: str | None = None) -> list[dict]:
        return [
            e["body"]
            for e in projection.events
            if e["type"] == "generation-create" and name in (None, e["body"]["name"])
        ]

    @staticmethod
    def _total(projection) -> float:
        """What the receiver adds up for the trace: every generation's cost."""
        return sum(
            (b.get("costDetails") or {}).get("total", 0)
            for b in TestCostOnTheTrace._generations(projection)
        )

    @staticmethod
    def _spent(calls: list[dict], judge_billed: float) -> float:
        return sum(c["billed_cost_usd"] for c in calls) + judge_billed

    def test_the_trace_cost_is_every_call_the_trial_made(self, tmp_path: Path) -> None:
        projection = _project(tmp_path, RawModelNameResolver())
        calls = pb.metrics()["usage"]["calls"]
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )
        users = self._generations(projection, "user simulator")
        assert [(b["costDetails"], b["metadata"]["cost_basis"]) for b in users] == [
            ({"total": 0.0002}, "billed"),
            ({"total": 0.0003}, "billed"),
        ]
        assert [b["usageDetails"]["total"] for b in users] == [325, 432]

    def test_a_calls_cache_reads_and_writes_leave_its_input_once(self, tmp_path: Path) -> None:
        """The engine's prompt total holds both, so the generation's ``input`` is what is left
        and its components add up to its ``total``."""
        calls = pb.metrics()["usage"]["calls"]
        cached = next(c for c in calls if c["openrouter_generation_id"] == "gen-agent-2")
        cached.update(
            prompt_tokens=1000,
            completion_tokens=20,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=200,
        )
        projection = self._projection(tmp_path, calls=calls)
        (body,) = [
            b
            for b in self._generations(projection, "agent")
            if b["metadata"]["openrouter_generation_id"] == "gen-agent-2"
        ]
        assert body["usageDetails"] == {
            "input": 700,
            "output": 20,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 200,
            "total": 1020,
        }
        assert body["metadata"]["usage_clamped"] is False

    def test_a_recorded_trial_pairs_the_user_turns_positionally(self, tmp_path: Path) -> None:
        """The engine stamps no generation id on a message: the agent's calls pair with the
        assistant turns and the simulator's calls with the user turns, each in order."""
        calls = pb.metrics()["usage"]["calls"]
        projection = self._projection(tmp_path, calls=calls, generation_ids=False)

        assert projection.stats.usage_match == "positional"
        users = self._generations(projection, "user simulator")
        assert [b["metadata"]["usage_match"] for b in users] == ["positional"] * 2
        assert [b["costDetails"] for b in users] == [{"total": 0.0002}, {"total": 0.0003}]
        assert [b["costDetails"] for b in self._generations(projection, "agent")] == [
            {"total": 0.001},
            {"total": 0.0025},
            {"total": 0.003},
        ]
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )

    def test_a_call_no_message_pairs_with_gets_a_generation_of_its_own(
        self, tmp_path: Path
    ) -> None:
        """A resample the loop discarded, a summarizer call: billed, and on no message."""
        calls = pb.metrics()["usage"]["calls"]
        discarded = {
            **calls[1],
            "openrouter_generation_id": "gen-agent-discarded",
            "billed_cost_usd": 0.0007,
        }
        calls.insert(2, discarded)
        projection = self._projection(tmp_path, calls=calls)

        (extra,) = self._generations(projection, "agent call (no message)")
        assert extra["metadata"]["call_index"] == 2
        assert extra["id"] == ids.observation_id(IDENTITY.trace_id, "gen", "call:2")
        assert extra["costDetails"] == {"total": 0.0007}
        assert extra["metadata"]["usage_match"] == "unpaired"
        assert extra["metadata"]["cost_basis"] == "billed"
        assert extra["model"] == "acme/pilot-1"
        # the assistant turns still pair by generation id; nothing is counted twice
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )

    def test_unpairable_user_calls_stay_on_the_trace(self, tmp_path: Path) -> None:
        """Without generation ids and with one simulator call more than turns, no user turn can
        be told its call: each turn states it has none, and every simulator call gets a
        generation of its own."""
        calls = pb.metrics()["usage"]["calls"]
        calls.append({**calls[-1], "openrouter_generation_id": "gen-user-x"})
        projection = self._projection(tmp_path, calls=calls, generation_ids=False)

        users = self._generations(projection, "user simulator")
        assert [(b["costDetails"], b["metadata"]["cost_basis"]) for b in users] == [
            ({"total": 0}, "none"),
            ({"total": 0}, "none"),
        ]
        assert [b["usageDetails"] for b in users] == [{"input": 0, "output": 0, "total": 0}] * 2
        extra = self._generations(projection, "user simulator call (no message)")
        assert [b["metadata"]["call_index"] for b in extra] == [0, 4, 5]
        assert all(b["model"] == "acme/sim-2" for b in extra)
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )

    def test_a_dialogue_ended_on_a_bare_stop_pairs_the_leading_calls(self, tmp_path: Path) -> None:
        """The stop token alone is recorded as a call and writes no message: the simulator has
        one call more than turns, its last, which gets a generation of its own."""
        calls = pb.metrics()["usage"]["calls"]
        projection = self._projection(
            tmp_path, calls=calls, generation_ids=False, ending="user_stop"
        )

        (user,) = self._generations(projection, "user simulator")
        assert (user["costDetails"], user["metadata"]["usage_match"]) == (
            {"total": 0.0002},
            "positional",
        )
        (stop,) = self._generations(projection, "user simulator call (no message)")
        assert stop["metadata"]["call_index"] == 4
        assert stop["costDetails"] == {"total": 0.0003}
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )

    @pytest.mark.parametrize("ending", ["max_turns", None])
    def test_one_call_too_many_without_a_bare_stop_is_not_guessed(
        self, tmp_path: Path, ending: str | None
    ) -> None:
        """Without a stop that wrote no message, nothing says which call is the extra one."""
        calls = pb.metrics()["usage"]["calls"]
        if ending is None:
            # one call too many, and the dialogue ends on a simulated turn: no bare stop
            calls.append({**calls[-1], "openrouter_generation_id": "gen-user-x"})
            projection = self._projection(tmp_path, calls=calls, generation_ids=False)
        else:
            projection = self._projection(
                tmp_path, calls=calls, generation_ids=False, ending=ending
            )

        users = self._generations(projection, "user simulator")
        assert {b["metadata"]["usage_match"] for b in users} == {"unmatched"}
        assert all(b["costDetails"] == {"total": 0} for b in users)
        assert self._total(projection) == pytest.approx(
            self._spent(calls, pb.grade()["judge_usage"]["billed_cost_usd"])
        )

    def test_a_bundle_from_before_the_billed_field_keeps_the_eval_cost(
        self, tmp_path: Path
    ) -> None:
        calls = [self._without_the_charge(c) for c in pb.metrics()["usage"]["calls"]]
        legacy_judge = {
            k: v for k, v in pb.grade()["judge_usage"].items() if k != "billed_cost_usd"
        }
        projection = self._projection(tmp_path, calls=calls, judge_usage=legacy_judge)

        bodies = self._generations(projection, "agent")
        assert [b["costDetails"] for b in bodies] == [
            {"total": 0.001},
            {"total": 0.002},
            {"total": 0.003},
        ]
        assert [b["metadata"]["cost_basis"] for b in bodies] == ["litellm"] * 3
        judge = next(
            b
            for b in self._generations(projection, "judge")
            if b.get("usageDetails", {}).get("total")
        )
        assert judge["costDetails"] == {"total": 0.0015}
        assert judge["metadata"]["cost_basis"] == "eval"
        # the trace's own metadata is untouched: cost_usd stays the eval's figure
        assert projection.trace_body["metadata"]["cost_usd"] == 0.00645
        assert "cost_basis" not in projection.trace_body["metadata"]

    def test_a_call_whose_route_stated_no_charge_keeps_its_eval_cost(self, tmp_path: Path) -> None:
        calls = pb.metrics()["usage"]["calls"]
        calls[2] = self._without_the_charge(calls[2], source="local")
        bodies = self._generations(self._projection(tmp_path, calls=calls), "agent")

        assert [(b["costDetails"], b["metadata"]["cost_basis"]) for b in bodies] == [
            ({"total": 0.001}, "billed"),
            ({"total": 0.002}, "list"),
            ({"total": 0.003}, "billed"),
        ]

    def test_a_call_without_any_figure_states_a_zero_cost(self, tmp_path: Path) -> None:
        """No stated charge and no eval figure (a route litellm cannot price, say): the usage is
        the call's, the cost an explicit zero, so the receiver prices nothing from its table."""
        calls = pb.metrics()["usage"]["calls"]
        unpriced = {**self._without_the_charge(calls[2], source="unknown"), "cost_usd": None}
        calls[2] = unpriced
        bodies = self._generations(self._projection(tmp_path, calls=calls), "agent")

        assert (bodies[1]["costDetails"], bodies[1]["metadata"]["cost_basis"]) == (
            {"total": 0},
            "none",
        )
        assert bodies[1]["usageDetails"]["total"] == (
            unpriced["prompt_tokens"] + unpriced["completion_tokens"]
        )

    def test_an_assistant_turn_without_a_call_states_it_has_none(self, tmp_path: Path) -> None:
        """No agent call is recorded (a mock run): the turns carry explicit zeros, so a
        receiver that merges an update cannot keep a live row's figures."""
        calls = [c for c in pb.metrics()["usage"]["calls"] if c["role"] == "user"]
        bodies = self._generations(self._projection(tmp_path, calls=calls), "agent")

        assert {b["metadata"]["usage_match"] for b in bodies} == {"unmatched"}
        assert all(b["costDetails"] == {"total": 0} for b in bodies)
        assert all(b["metadata"]["cost_basis"] == "none" for b in bodies)

    @pytest.mark.parametrize(
        ("judge_usage", "cost", "basis"),
        [
            ({"calls": 3, "cost_usd": 0.0142, "billed_cost_usd": 0.0145}, 0.0145, "billed"),
            ({"calls": 3, "cost_usd": 0.0142}, 0.0142, "eval"),  # a grade.yaml from before
            ({"calls": 3, "cost_usd": 0.0142, "billed_cost_usd": 0.0}, 0, "billed"),
            ({"calls": 3}, 0, "none"),
        ],
    )
    def test_a_judge_without_a_transcript_is_priced_by_the_same_rule(
        self, tmp_path: Path, judge_usage: dict, cost: float, basis: str
    ) -> None:
        """A grade with judge usage but no judge messages (an errored judge, a detached
        regrade) gets one synthetic generation carrying the aggregate."""
        trial_dir = pb.write_parity_bundle(tmp_path / "run")
        (trial_dir / "judge_trajectory.yaml").unlink()
        grade = pb.grade()
        grade["judge_usage"] = judge_usage
        (trial_dir / "grade.yaml").write_text(yaml.safe_dump(grade), encoding="utf-8")
        resolver = RawModelNameResolver()
        projection = build_projection(
            IDENTITY, trial_dir, _context(tags=_tags(resolver)), resolver=resolver
        )

        (judge,) = [
            e["body"]
            for e in projection.events
            if e["type"] == "generation-create" and e["body"]["name"].startswith("judge")
        ]
        assert judge["name"] == "judge (aggregate usage, no transcript)"
        assert judge["costDetails"] == {"total": cost}
        assert judge["metadata"]["cost_basis"] == basis
