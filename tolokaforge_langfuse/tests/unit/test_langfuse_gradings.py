"""The gradings amendment: the persisted bundle's grading, judge transcript, scores and user
turns leave as ingestion events under the connector's id contract; one switch turns tracing on."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from tolokaforge_langfuse.gradings import (
    build_grading_events,
    content_fingerprint,
    grade_summary,
)
from tolokaforge_langfuse.media import LangfuseApiError, LangfuseAttachments

from tolokaforge.observability import ids

pytestmark = pytest.mark.unit

TRACE = "b" * 32
RUN_ID = "pilot-dev/v3/live/gemini/20260916T195039Z"
GRADE = {
    "binary_pass": True,
    "score": 0.875,
    "components": {"state_checks": 1.0, "llm_judge": 0.75},
    "reasons": "Two of three criteria met.",
    "criterion_results": [
        {"id": "c1", "met": True, "score": 1.0, "justification": "ok"},
        {"id": "c2", "met": False, "score": 0.5, "justification": "partial"},
    ],
    "trace_check_results": [
        {"id": "no_refund", "passed": True, "severity": "high", "kind": "forbidden", "weight": 1},
        {"id": "gate", "passed": False, "withheld": True, "severity": "low", "kind": "gate"},
    ],
    "trace_checks_summary": {"gate_failed": False, "winning_path": "main", "failed_gate_ids": []},
    "judge_status": "ok",
    "judge_usage": {
        "calls": 2,
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "reasoning_tokens": 7,
        "cost_usd": 0.01,
        "tool_calls": 1,
        "consistency_rejections": 0,
    },
}
JUDGE = {
    "messages": [
        {"role": "system", "content": "You judge."},
        {"role": "user", "content": "Transcript..."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"q": 1}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "found"},
        {"role": "assistant", "content": "PASS", "tool_calls": None},
    ]
}
TRAJECTORY = {
    "task_id": "T-1",
    "trial_index": 0,
    "attempt_id": 0,
    "start_ts": "2026-09-16T19:50:00+00:00",
    "end_ts": "2026-09-16T19:54:49.095114+00:00",
    "first_user_message_source": "simulator",
    "messages": [
        {"role": "user", "content": "Hi, I need a refund", "ts": "2026-09-16T19:50:01+00:00"},
        {"role": "assistant", "content": "Sure", "ts": "2026-09-16T19:50:02+00:00"},
        {
            "role": "user",
            "content": "Booking 42",
            "openrouter_generation_id": "gen-9",
            "ts": "2026-09-16T19:50:03+00:00",
        },
        {"role": "assistant", "content": "Done", "ts": "2026-09-16T19:50:04+00:00"},
    ],
}
TASK = {
    "task_id": "T-1",
    "interaction_mode": "conversational",
    "grading_config": {
        "state_checks": {},
        "llm_judge": {},
        "combine": {"method": "weighted", "pass_threshold": 0.7, "weights": {"state_checks": 1}},
    },
    "model_config": {"agent": {}, "user": {"name": "sonnet"}, "judge": {"name": "gemini"}},
}


def write_bundle(trial_dir: Path, *, grade=GRADE, judge=JUDGE, trajectory=TRAJECTORY, task=TASK):
    trial_dir.mkdir(parents=True, exist_ok=True)
    for name, doc in (
        ("grade.yaml", grade),
        ("judge_trajectory.yaml", judge),
        ("trajectory.yaml", trajectory),
        ("task.yaml", task),
    ):
        if doc is not None:
            (trial_dir / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return trial_dir


def _by_type(events):
    out: dict[str, list] = {}
    for event in events:
        out.setdefault(event["type"], []).append(event["body"])
    return out


class TestBuildGradingEvents:
    def test_the_bundle_becomes_grading_judge_scores_mirror_users_and_trace_keys(
        self, tmp_path: Path
    ) -> None:
        built = build_grading_events(
            TRACE,
            write_bundle(tmp_path / "T-1" / "0"),
            run_id=RUN_ID,
            judge_model_name="google/gemini-3.6-flash",
            user_model_name="anthropic/claude-sonnet-4.6",
        )
        grading_id = f"live:{RUN_ID}"
        assert built.grading_id == grading_id
        by = _by_type(built.events)
        root = ids.observation_id(TRACE, "root", "-")
        # the grading observation under the root, keyed by the grading id
        (grading,) = [b for b in by["span-create"] if b["name"].startswith("grading:")]
        assert grading["id"] == ids.grading_observation_id(TRACE, grading_id)
        assert grading["parentObservationId"] == root
        assert grading["metadata"]["content_fingerprint"] == content_fingerprint(GRADE)
        assert (
            grading["metadata"]["content_fingerprint"]
            == hashlib.sha256(
                json.dumps(
                    GRADE, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
        )
        assert grading["metadata"]["provenance_grading_run_id"] == grading_id
        assert grading["metadata"]["provenance_engine_version"] == "unknown"
        assert grading["metadata"]["judge_model"] == "google/gemini-3.6-flash"
        assert grading["startTime"] == "2026-09-16T19:54:49.095114+00:00"
        # judge: two generations and one tool span, children of the grading; usage on the last
        judge_gens = [b for b in by["generation-create"] if b["metadata"]["role"] == "judge"]
        assert [b["id"] for b in judge_gens] == [
            ids.observation_id(TRACE, "jgen", grading_id, 2),
            ids.observation_id(TRACE, "jgen", grading_id, 4),
        ]
        assert all(b["parentObservationId"] == grading["id"] for b in judge_gens)
        assert judge_gens[0]["usageDetails"] == {"input": 0, "output": 0, "total": 0}
        assert judge_gens[1]["usageDetails"] == {"input": 100, "output": 40, "total": 140}
        assert judge_gens[1]["metadata"]["usage_source"] == "aggregate"
        (jtool,) = [b for b in by["span-create"] if b["name"].startswith("judge tool")]
        assert jtool["id"] == ids.observation_id(TRACE, "jtool", grading_id, "call_1")
        assert built.judge_observations == 3
        # scores: on the grading (scope grading) and mirrored on the trace (scope primary)
        scores = by["score-create"]
        on_grading = [s for s in scores if s.get("observationId") == grading["id"]]
        mirror = [s for s in scores if "observationId" not in s]
        names = sorted(s["name"] for s in on_grading)
        assert names == [
            "binary_pass",
            "component:llm_judge",
            "component:state_checks",
            "criterion:c1",
            "criterion:c2",
            "score",
            "trace_check:gate",
            "trace_check:no_refund",
        ]
        # the mirror is the same set plus the pointer naming the grading it mirrors (D-v4-3)
        assert sorted(s["name"] for s in mirror) == sorted([*names, "primary_grading"])
        pointer = next(s for s in mirror if s["name"] == "primary_grading")
        assert pointer["value"] == grading_id and pointer["dataType"] == "CATEGORICAL"
        assert pointer["metadata"]["scope"] == "primary"
        assert built.scores == len(scores) == 17
        pick = {s["name"]: s for s in on_grading}
        assert pick["score"]["id"] == ids.grading_score_id(TRACE, grading_id, "score")
        assert pick["score"]["metadata"] == {
            "scope": "grading",
            "grading_id": grading_id,
            "stale": False,
        }
        assert pick["score"]["comment"] == "Two of three criteria met."
        assert pick["binary_pass"] == {
            **pick["binary_pass"],
            "value": 1,
            "dataType": "BOOLEAN",
            "traceId": TRACE,
        }
        assert pick["trace_check:gate"]["value"] == "withheld"
        assert pick["criterion:c2"]["metadata"]["met"] is False
        mirror_score = next(s for s in mirror if s["name"] == "score")
        assert mirror_score["id"] == ids.primary_score_id(TRACE, "score")
        assert mirror_score["metadata"]["scope"] == "primary"
        # the trace-level grading keys and the grade summary
        (trace,) = by["trace-create"]
        assert trace["id"] == TRACE
        assert trace["metadata"]["primary_grading"] == grading_id
        assert trace["metadata"]["gradings"] == json.dumps([grading_id])
        assert trace["metadata"]["grading_count"] == 1
        assert trace["metadata"]["pass"] is True and trace["metadata"]["score"] == 0.875
        assert trace["metadata"]["trace_checks_withheld"] == 1
        assert trace["metadata"]["criteria_met"] == 1
        assert "timestamp" not in trace  # the manifest update owns the trace clock
        # the simulated user turns: the opener (simulator) and the generation-id turn
        users = [b for b in by["generation-create"] if b["metadata"]["role"] == "user"]
        assert [b["id"] for b in users] == [
            ids.observation_id(TRACE, "ugen", 0),
            ids.observation_id(TRACE, "ugen", 2),
        ]
        assert users[1]["model"] == "anthropic/claude-sonnet-4.6"
        assert users[1]["startTime"] == "2026-09-16T19:50:03+00:00"
        assert users[1]["input"] == [
            {"role": "user", "content": "Hi, I need a refund"},
            {"role": "assistant", "content": "Sure"},
        ]
        assert built.user_generations == 2
        # every event carries an envelope the ingestion API accepts
        assert all(set(e) == {"id", "type", "timestamp", "body"} for e in built.events)

    def test_the_pinned_opener_of_an_agent_only_task_is_not_a_user_generation(
        self, tmp_path: Path
    ) -> None:
        task = {**TASK, "interaction_mode": "agent_only"}
        trajectory = {
            **TRAJECTORY,
            "first_user_message_source": "task",
            "messages": [
                {"role": "user", "content": "Do X", "ts": "2026-09-16T19:50:01+00:00"},
                {"role": "assistant", "content": "Done", "ts": "2026-09-16T19:50:02+00:00"},
            ],
        }
        built = build_grading_events(
            TRACE,
            write_bundle(tmp_path / "T-2" / "0", task=task, trajectory=trajectory),
            run_id=RUN_ID,
        )
        assert built.user_generations == 0

    def test_model_names_fall_back_to_the_task_config(self, tmp_path: Path) -> None:
        task = {
            **TASK,
            "model_config": {
                "agent": {"provider": "openrouter", "name": "google/gemini-3.7-flash"},
                "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
                "judge": {"provider": "azure", "name": "gpt-6-astra"},
            },
        }
        built = build_grading_events(
            TRACE, write_bundle(tmp_path / "T-6" / "0", task=task), run_id=RUN_ID
        )
        by = _by_type(built.events)
        grading = next(b for b in by["span-create"] if b["name"].startswith("grading:"))
        assert grading["metadata"]["judge_model"] == "azure/gpt-6-astra"
        users = [b for b in by["generation-create"] if b["metadata"]["role"] == "user"]
        assert users[0]["model"] == "anthropic/claude-sonnet-4.6"

    def test_a_bundle_without_a_grade_yields_the_user_turns_only(self, tmp_path: Path) -> None:
        built = build_grading_events(
            TRACE, write_bundle(tmp_path / "T-3" / "0", grade=None, judge=None), run_id=RUN_ID
        )
        assert built.grading_id is None and built.scores == 0
        assert {e["type"] for e in built.events} == {"generation-create"}
        assert built.user_generations == 2

    def test_a_grade_without_a_judge_transcript_carries_the_aggregate_usage(
        self, tmp_path: Path
    ) -> None:
        built = build_grading_events(
            TRACE, write_bundle(tmp_path / "T-4" / "0", judge=None), run_id=RUN_ID
        )
        judge = [
            b
            for b in _by_type(built.events)["generation-create"]
            if b["metadata"]["role"] == "judge"
        ]
        assert len(judge) == 1 and judge[0]["name"].startswith("judge (aggregate")
        assert judge[0]["usageDetails"]["total"] == 140

    def test_a_malformed_grade_file_yields_no_grading_and_no_exception(
        self, tmp_path: Path
    ) -> None:
        trial = write_bundle(tmp_path / "T-5" / "0", grade=None)
        (trial / "grade.yaml").write_text("binary_pass: [unclosed", encoding="utf-8")
        built = build_grading_events(TRACE, trial, run_id=RUN_ID)
        assert built.grading_id is None and built.user_generations == 2

    def test_grade_summary_is_explicit_for_a_bare_grade(self) -> None:
        summary = grade_summary({"binary_pass": False})
        assert summary["pass"] is False and summary["score"] == "none"
        assert summary["judge_status"] == "unspecified" and summary["judge_calls"] == 0
        assert summary["trace_checks_gate_failed"] == "none"


class _Recorder:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.answers.pop(0) if self.answers else (207, b'{"errors": []}')


class TestIngest:
    def test_events_leave_in_batches_and_a_rejection_is_named(self) -> None:
        ok = (207, json.dumps({"successes": [], "errors": []}).encode())
        recorder = _Recorder([ok, ok])
        step = LangfuseAttachments(api_base="https://lf.example", opener=recorder)
        events = [{"id": str(i), "type": "score-create", "body": {}} for i in range(3)]
        step.ingest(events, batch_size=2)
        assert [len(c[2]["batch"]) for c in recorder.calls] == [2, 1]
        assert all(c[1] == "https://lf.example/api/public/ingestion" for c in recorder.calls)
        rejected = (
            207,
            json.dumps(
                {"errors": [{"id": "1", "status": 400, "message": "invalid_format"}]}
            ).encode(),
        )
        step = LangfuseAttachments(api_base="https://lf.example", opener=_Recorder([rejected]))
        with pytest.raises(LangfuseApiError, match="400 invalid_format"):
            step.ingest(events[:1])
        step = LangfuseAttachments(api_base="https://lf.example", opener=_Recorder([(503, b"")]))
        with pytest.raises(LangfuseApiError, match="HTTP 503"):
            step.ingest(events[:1])


class TestObserverGradings:
    """The ``gradings`` projection mode: the behaviour of the gradings amendment, kept for a
    deployment that wants the grading alone at trial end (the default is the full projection,
    tested in ``test_projection.py``)."""

    def _observer(self, attachments, **kwargs):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from tolokaforge_langfuse.otel import OTelTrialObserver, ProjectionSettings, SpanQueue

        queue = SpanQueue(InMemorySpanExporter(), max_size=100, batch_size=4, interval_s=0.05)
        return OTelTrialObserver(
            queue=queue,
            label="l",
            session_id="s",
            attachments=attachments,
            projection=ProjectionSettings(mode="gradings"),
            **kwargs,
        )

    def test_the_grading_leaves_after_the_attachments_and_is_counted(self, tmp_path: Path) -> None:
        from tolokaforge_langfuse.attachments import AttachCounts

        from tolokaforge.observability.observer import ModelRef, TrialIdentity

        class Step:
            mode = "all"

            def __init__(self):
                self.attached: list[str] = []
                self.batches: list[list[dict]] = []

            def attach(self, trace_id, trial_dir, *, trace_timestamp=None, metadata=None):
                self.attached.append(trace_id)
                return AttachCounts(registered=1, manifests_sent=1)

            def ingest(self, events, *, batch_size=40):
                self.batches.append(events)

        step = Step()
        observer = self._observer(step)
        identity = TrialIdentity(run_id=RUN_ID, task_id="T-1", trial_index=0, attempt_id=0)
        observer.trial_started(
            identity,
            models={
                "agent": ModelRef("openrouter", "google/gemini-3.7-flash"),
                "judge": ModelRef("openrouter", "google/gemini-3.6-flash"),
            },
            started_at=__import__("datetime").datetime(
                2026, 9, 16, tzinfo=__import__("datetime").timezone.utc
            ),
        )
        # the realistic order: the trial finishes (its state is dropped) before it is persisted
        observer.trial_finished(identity, trajectory=None)
        observer.trial_persisted(identity, trial_dir=write_bundle(tmp_path / "T-1" / "0"))
        receipt = observer.run_finished()
        assert step.attached == [identity.trace_id]
        (batch,) = step.batches
        types = {e["type"] for e in batch}
        assert types == {"span-create", "generation-create", "score-create", "trace-create"}
        grading = next(
            e
            for e in batch
            if e["type"] == "span-create" and e["body"]["name"].startswith("grading:")
        )
        # the judge model the observer resolved at trial start survives the state drop
        assert grading["body"]["metadata"]["judge_model"].endswith("gemini-3.6-flash")
        judge_gen = next(e for e in batch if e["body"].get("name", "").startswith("judge turn"))
        assert judge_gen["body"]["model"].endswith("gemini-3.6-flash")
        assert (
            receipt.extra["langfuse.gradings_sent"],
            receipt.extra["langfuse.gradings_failed"],
            receipt.extra["langfuse.scores_sent"],
        ) == (
            1,
            0,
            17,
        )  # 8 grading scores + 8 mirrored + the primary_grading pointer
        assert receipt.extra["langfuse.user_generations_sent"] == 2
        assert receipt.model_dump(mode="json")["extra"]["langfuse.gradings_sent"] == 1

    def test_attach_none_skips_the_files_but_the_grading_still_leaves(self, tmp_path: Path) -> None:
        from tolokaforge.observability.observer import TrialIdentity

        class Step:
            mode = "none"
            attached = 0
            batches: list = []

            def attach(self, *a, **k):
                Step.attached += 1
                raise AssertionError("attach must not run under mode none")

            def ingest(self, events, *, batch_size=40):
                Step.batches.append(events)

        observer = self._observer(Step())
        identity = TrialIdentity(run_id=RUN_ID, task_id="T-1", trial_index=0, attempt_id=0)
        observer.trial_persisted(identity, trial_dir=write_bundle(tmp_path / "T-1" / "0"))
        receipt = observer.run_finished()
        assert Step.attached == 0 and len(Step.batches) == 1
        assert (
            receipt.extra["langfuse.gradings_sent"] == 1
            and receipt.extra["langfuse.attachments_registered"] == 0
        )

    def test_a_refused_batch_is_a_failure_count_not_an_exception(self, tmp_path: Path) -> None:
        from tolokaforge.observability.observer import TrialIdentity

        class Step:
            mode = "none"

            def attach(self, *a, **k):
                raise AssertionError

            def ingest(self, events, *, batch_size=40):
                raise LangfuseApiError("POST /api/public/ingestion: HTTP 500", status=500)

        observer = self._observer(Step())
        identity = TrialIdentity(run_id=RUN_ID, task_id="T-1", trial_index=0, attempt_id=0)
        observer.trial_persisted(identity, trial_dir=write_bundle(tmp_path / "T-1" / "0"))
        receipt = observer.run_finished()
        assert (
            receipt.extra["langfuse.gradings_sent"],
            receipt.extra["langfuse.gradings_failed"],
        ) == (0, 1)

    def test_gradings_off_sends_nothing(self, tmp_path: Path) -> None:
        from tolokaforge.observability.observer import TrialIdentity

        class Step:
            mode = "none"

            def attach(self, *a, **k):
                raise AssertionError

            def ingest(self, events, *, batch_size=40):
                raise AssertionError("gradings are off")

        observer = self._observer(Step(), gradings=False)
        identity = TrialIdentity(run_id=RUN_ID, task_id="T-1", trial_index=0, attempt_id=0)
        observer.trial_persisted(identity, trial_dir=write_bundle(tmp_path / "T-1" / "0"))
        assert observer.run_finished().extra["langfuse.gradings_sent"] == 0


class TestLangfuseSwitch:
    """``LANGFUSE_TRACING_ENABLED`` and the plain Langfuse variables."""

    @pytest.fixture
    def clean_env(self, monkeypatch):
        for name in (
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "TOLOKAFORGE_TRACING_TAGS",
            "TOLOKAFORGE_TRACING_EXPECT_PROJECT",
            "TOLOKAFORGE_TRACING_RUN_ID",
            "TOLOKAFORGE_TRACING_RUN_TAG",
            "TOLOKAFORGE_TRACING_SESSION_ID",
            "TOLOKAFORGE_TRACING_LABEL",
            "LANGFUSE_TRACING_ENABLED",
            "LANGFUSE_BASE_URL",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "LANGFUSE_PROJECT",
            "LANGFUSE_EXTRA_HEADERS",
        ):
            monkeypatch.delenv(name, raising=False)
        return monkeypatch

    def _projects(self, monkeypatch, names):
        from tolokaforge_langfuse import media

        calls: list[tuple[str, str, dict]] = []

        def opener(method, url, headers, body, timeout):
            calls.append((method, url, dict(headers)))
            if media.V2_OBSERVATIONS_PATH in url:
                return (404, b"")  # the receiver-family probe: this fixture is a v3 receiver
            return (
                200,
                json.dumps(
                    {"data": [{"id": f"p{i}", "name": n} for i, n in enumerate(names)]}
                ).encode(),
            )

        monkeypatch.setattr(media, "urllib_opener", opener)
        return calls

    def test_off_by_default_and_a_false_value_is_off(self, clean_env) -> None:
        from tolokaforge.core.models import ObservabilityConfig
        from tolokaforge.observability.factory import build_trial_observer
        from tolokaforge.observability.observer import NullTrialObserver

        observer, identity = build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")
        assert isinstance(observer, NullTrialObserver) and identity.run_id == "run-1"
        clean_env.setenv("LANGFUSE_TRACING_ENABLED", "false")
        observer, _ = build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")
        assert isinstance(observer, NullTrialObserver)

    def test_the_switch_builds_the_receiver_from_the_plain_variables(
        self, clean_env, tmp_path: Path
    ) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge.core.models import ObservabilityConfig
        from tolokaforge.observability.factory import build_trial_observer

        calls = self._projects(clean_env, ["pilot-dev"])
        clean_env.setenv("LANGFUSE_TRACING_ENABLED", "true")
        clean_env.setenv("LANGFUSE_BASE_URL", "https://lf.example/")
        clean_env.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        clean_env.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        clean_env.setenv("LANGFUSE_PROJECT", "pilot-dev")
        clean_env.setenv("LANGFUSE_EXTRA_HEADERS", "X-GitHub-Runner-Key=abc")
        clean_env.setenv("TOLOKAFORGE_TRACING_RUN_ID", "acme/pilot/v1/123/1")
        clean_env.setenv("TOLOKAFORGE_TRACING_SESSION_ID", "acme/pilot/v1/m/cfg/123")
        clean_env.setenv("TOLOKAFORGE_TRACING_LABEL", "cfg")
        observer, identity = build_trial_observer(
            ObservabilityConfig(), engine_run_id="run-1", output_dir=tmp_path
        )
        try:
            assert identity.run_id == "acme/pilot/v1/123/1" and identity.run_tag == "v1"
            expected = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
            headers = {"Authorization": expected, "X-GitHub-Runner-Key": "abc"}
            assert calls == [
                ("GET", "https://lf.example/api/public/projects", headers),
                ("GET", "https://lf.example/api/public/v2/observations?limit=1", headers),
            ]
            assert observer._tags == ("project:pilot-dev",)
            assert observer._label == "cfg" and observer._session_id == "acme/pilot/v1/m/cfg/123"
            assert observer._attachments is not None
            assert observer._attachments._api_base == "https://lf.example"
            assert observer._queue._exporter._endpoint.endswith("/api/public/otel/v1/traces")
        finally:
            receipt = observer.run_finished()
        assert (
            receipt.details[0]["expect_project"] == "pilot-dev"
            and receipt.details[0]["project_verified"] == "verified"
        )
        assert (tmp_path / "run_identity.json").exists()

    def test_the_switch_without_credentials_or_with_half_a_pair_refuses(self, clean_env) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge.core.models import ObservabilityConfig
        from tolokaforge.observability.factory import TracingConfigError, build_trial_observer

        clean_env.setenv("LANGFUSE_TRACING_ENABLED", "1")
        clean_env.setenv("LANGFUSE_BASE_URL", "https://lf.example")
        with pytest.raises(TracingConfigError, match="no receiver credentials"):
            build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")
        clean_env.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        with pytest.raises(TracingConfigError, match="must be set together"):
            build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")

    def test_the_switch_without_a_base_url_names_every_source(self, clean_env) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge.core.models import ObservabilityConfig
        from tolokaforge.observability.factory import TracingConfigError, build_trial_observer

        clean_env.setenv("LANGFUSE_TRACING_ENABLED", "yes")
        with pytest.raises(TracingConfigError, match="LANGFUSE_BASE_URL"):
            build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")

    def test_a_launcher_header_wins_over_the_key_pair_and_the_project_mismatch_refuses(
        self, clean_env
    ) -> None:
        pytest.importorskip("opentelemetry.sdk")
        from tolokaforge.core.models import ObservabilityConfig
        from tolokaforge.observability.factory import TracingConfigError, build_trial_observer

        calls = self._projects(clean_env, ["pilot-dev"])
        clean_env.setenv("LANGFUSE_TRACING_ENABLED", "true")
        clean_env.setenv("LANGFUSE_BASE_URL", "https://lf.example")
        clean_env.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic bGF1bmNoZXI=")
        clean_env.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        clean_env.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
        clean_env.setenv("LANGFUSE_PROJECT", "pilot")
        with pytest.raises(TracingConfigError, match="expect_project='pilot'"):
            build_trial_observer(ObservabilityConfig(), engine_run_id="run-1")
        assert calls[0][2]["Authorization"] == "Basic bGF1bmNoZXI="
