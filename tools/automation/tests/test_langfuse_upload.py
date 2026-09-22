"""Unit tests for ``automation.langfuse_upload``: how a transcript file becomes a trace id, how
the receiver is read from the step's environment (and never printed), and what the command does
with a file the reader refuses, one the sentinel blocks and one the receiver will not take.

No network: the export is a fake exporter, and the dry run proves the whole path up to the send.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import automation.langfuse_upload as lu
import pytest

pytestmark = pytest.mark.unit

CALLER = {"team": "acme", "run_kind": "test", "ci_run": "12345"}

CLEAN_EVENTS: list[dict[str, Any]] = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "s1",
        "timestamp": "2026-09-20T10:00:00Z",
        "claude_code_version": "2.1.220",
        "cwd": "/home/runner/work/acme/acme",
    },
    {
        "type": "assistant",
        "session_id": "s1",
        "timestamp": "2026-09-20T10:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [
                {"type": "text", "text": "looking at the preset"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "p.yaml"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    },
    {
        "type": "user",
        "session_id": "s1",
        "timestamp": "2026-09-20T10:00:02Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "timestamp": "2026-09-20T10:00:03Z",
        "is_error": False,
        "num_turns": 2,
        "total_cost_usd": 0.01,
        "result": "the preset is fine",
    },
]


def write(directory: Path, name: str, events: list[dict[str, Any]]) -> Path:
    path = directory / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def upload(directory: Path, **overrides: Any) -> lu.UploadReport:
    kwargs: dict[str, Any] = {
        "run_id": "automation/integrate-model/1/1",
        "label": "pilot",
        "environment": "production-automation",
        "caller_tags": CALLER,
        "dry_run": True,
    }
    kwargs.update(overrides)
    return lu.upload(directory, **kwargs)


class FakeExporter:
    """Stands in for the OTLP exporter: records the batches, answers with what it was told to."""

    def __init__(self, outcome: str = "SUCCESS") -> None:
        self.batches: list[Any] = []
        self._outcome = outcome

    def export(self, spans: Any) -> Any:
        self.batches.append(list(spans))
        if self._outcome == "raise":
            raise ConnectionError("receiver unreachable")
        return type("Result", (), {"name": self._outcome})()


class TestTheTranscriptId:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("agent_iter_1.jsonl", "resolve/1"),
            ("agent_iter_12.json", "resolve/12"),
            ("agent_finalize.jsonl", "finalize"),
            ("analysis_harness.json", "analysis_harness"),
            ("agent_iter_x.jsonl", "agent_iter_x"),
        ],
    )
    def test_the_file_name_names_the_step(self, name: str, expected: str) -> None:
        assert lu.transcript_id_for(Path(name)) == expected


class TestTheReceiver:
    def test_it_builds_the_endpoint_and_the_basic_header(self) -> None:
        receiver = lu.Receiver.from_environment(
            {
                "LANGFUSE_BASE_URL": "https://receiver.example.com/",
                "LANGFUSE_PUBLIC_KEY": "public-value",
                "LANGFUSE_SECRET_KEY": "secret-value",
            }
        )
        assert receiver.endpoint == "https://receiver.example.com/api/public/otel/v1/traces"
        assert receiver.base_url == "https://receiver.example.com"
        assert receiver.headers["Authorization"].startswith("Basic ")

    def test_the_credentials_are_not_in_the_repr(self) -> None:
        """A dataclass repr lands in a rich traceback, which lands in a public job log."""
        receiver = lu.Receiver.from_environment(
            {
                "LANGFUSE_BASE_URL": "https://receiver.example.com",
                "LANGFUSE_PUBLIC_KEY": "public-value",
                "LANGFUSE_SECRET_KEY": "secret-value",
            }
        )
        assert "secret-value" not in repr(receiver)
        assert "Basic" not in repr(receiver)

    def test_an_explicit_endpoint_wins_over_the_base_url(self) -> None:
        receiver = lu.Receiver.from_environment(
            {
                "LANGFUSE_BASE_URL": "https://receiver.example.com",
                "LANGFUSE_OTLP_ENDPOINT": "https://alias.example.com/v1/traces",
                "LANGFUSE_PUBLIC_KEY": "p",
                "LANGFUSE_SECRET_KEY": "s",
            }
        )
        assert receiver.endpoint == "https://alias.example.com/v1/traces"

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({}, "no receiver"),
            ({"LANGFUSE_BASE_URL": "https://h"}, "no credentials"),
            ({"LANGFUSE_BASE_URL": "https://h", "LANGFUSE_PUBLIC_KEY": "p"}, "no credentials"),
        ],
    )
    def test_a_missing_piece_is_an_error_not_a_silent_skip(
        self, env: dict[str, str], expected: str
    ) -> None:
        with pytest.raises(lu.UploadError, match=expected):
            lu.Receiver.from_environment(env)

    def test_the_admission_header_rides_along(self) -> None:
        """The gateway in front of the receiver refuses a request without it, so it has to
        survive all the way to the exporter."""
        receiver = self._with_headers("X-GitHub-Runner-Key=admission-value")
        assert receiver.headers["X-GitHub-Runner-Key"] == "admission-value"
        assert receiver.headers["Authorization"].startswith("Basic ")

    def test_the_format_is_the_one_the_live_observer_already_reads(self) -> None:
        """Both read LANGFUSE_EXTRA_HEADERS, and a CI job sets it once for the whole runner, so
        a second spelling of the same variable would be a silent 403 waiting to happen."""
        from tolokaforge_langfuse.plugin import _parse_headers

        for raw in (
            "X-GitHub-Runner-Key=abc",
            "X-A=1,X-B=2",
            " X-A = 1 , X-B = 2 ",
            "X-A=",
            "X-A=1,skipped-without-an-equals",
        ):
            assert lu._extra_headers(raw) == _parse_headers(raw), raw

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_an_unset_variable_means_no_extra_header(self, raw: str | None) -> None:
        assert lu._extra_headers(raw) == {}

    @pytest.mark.parametrize("raw", ["not-a-pair", ",,,", "=novalue"])
    def test_a_value_that_yields_no_header_at_all_is_an_error(self, raw: str) -> None:
        """The lenient parser would send nothing and the gateway would answer 403 with no hint."""
        with pytest.raises(lu.UploadError, match="yields no header"):
            lu._extra_headers(raw)

    @staticmethod
    def _with_headers(raw: str) -> lu.Receiver:
        return lu.Receiver.from_environment(
            {
                "LANGFUSE_BASE_URL": "https://h",
                "LANGFUSE_PUBLIC_KEY": "p",
                "LANGFUSE_SECRET_KEY": "s",
                "LANGFUSE_EXTRA_HEADERS": raw,
            }
        )


class TestTheUpload:
    def test_a_dry_run_projects_everything_and_needs_no_key(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_finalize.jsonl", CLEAN_EVENTS)
        report = upload(tmp_path)
        assert report.ok and report.dry_run
        assert [e["transcript_id"] for e in report.sent] == ["finalize", "resolve/1"]
        # one root, one generation, one tool span per transcript
        assert {e["spans"] for e in report.sent} == {3}
        assert len({e["trace_id"] for e in report.sent}) == 2

    def test_an_empty_directory_is_not_an_error(self, tmp_path: Path) -> None:
        report = upload(tmp_path)
        assert report.ok and report.sent == []

    def test_a_refused_file_does_not_stop_the_others(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_iter_2.jsonl", [{"type": "tool_progress", "message": {}}])
        report = upload(tmp_path)
        assert [e["transcript_id"] for e in report.sent] == ["resolve/1"]
        assert [e["file"] for e in report.refused] == ["agent_iter_2.jsonl"]
        assert "unknown type 'tool_progress'" in report.refused[0]["reason"]
        assert not report.ok

    def test_a_missing_caller_tag_refuses_the_file_rather_than_sending_it_untagged(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        report = upload(tmp_path, caller_tags={"team": "acme"})
        assert report.sent == []
        assert "needs run_kind" in report.refused[0]["reason"]

    def test_the_sentinel_blocks_a_transcript_that_carries_a_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under ``scrub`` the shapes are gone but a value only this process knows is not."""
        value = "a-password-that-matches-no-shape"
        monkeypatch.setenv("ACME_UPLOAD_TOKEN", value)
        events = [dict(e) for e in CLEAN_EVENTS]
        events[2] = {
            "type": "user",
            "timestamp": "2026-09-20T10:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": f"got {value}"}
                ],
            },
        }
        write(tmp_path, "agent_iter_1.jsonl", events)
        report = upload(tmp_path, tool_io="scrub")
        assert report.sent == []
        assert report.blocked[0]["file"] == "agent_iter_1.jsonl"
        assert "known-secret-value" in report.blocked[0]["reason"]
        # the value itself is never in the report
        assert value not in json.dumps(report.as_dict())

    def test_the_default_drop_policy_means_the_same_transcript_goes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing a tool returned is sent at all, so the credential cannot be in the payload."""
        value = "a-password-that-matches-no-shape"
        monkeypatch.setenv("ACME_UPLOAD_TOKEN", value)
        events = [dict(e) for e in CLEAN_EVENTS]
        events[2] = {
            "type": "user",
            "timestamp": "2026-09-20T10:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": f"got {value}"}
                ],
            },
        }
        write(tmp_path, "agent_iter_1.jsonl", events)
        report = upload(tmp_path)
        assert report.ok and len(report.sent) == 1

    def test_it_exports_one_batch_per_transcript(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_finalize.jsonl", CLEAN_EVENTS)
        exporter = FakeExporter()
        report = self._send(tmp_path, exporter)
        assert report.ok
        assert len(exporter.batches) == 2
        assert all(len(batch) == 3 for batch in exporter.batches)

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [("FAILURE", "export returned"), ("raise", "export raised")],
    )
    def test_a_receiver_that_will_not_take_it_is_reported_not_raised(
        self, tmp_path: Path, outcome: str, expected: str
    ) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        report = self._send(tmp_path, FakeExporter(outcome))
        assert report.sent == []
        assert expected in report.failed[0]["reason"]
        assert not report.ok

    @staticmethod
    def _send(directory: Path, exporter: FakeExporter) -> lu.UploadReport:
        import tolokaforge_langfuse.otlp_transport as transport

        receiver = lu.Receiver(endpoint="https://h/v1/traces", headers={"Authorization": "Basic x"})
        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            return upload(directory, dry_run=False, receiver=receiver)
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]


class TestTheReport:
    def test_the_summary_names_every_file_it_could_not_send(self, tmp_path: Path) -> None:
        report = lu.UploadReport(
            sent=[{"file": "a.jsonl", "transcript_id": "resolve/1", "trace_id": "t", "spans": 3}],
            refused=[{"file": "b.jsonl", "reason": "unknown type 'x'"}],
        )
        markdown = report.as_markdown()
        assert "1** transcript(s), 3 span(s)" in markdown
        assert "b.jsonl" in markdown and "unknown type 'x'" in markdown
        assert not report.ok

    def test_it_writes_the_job_summary_when_the_runner_offers_one(self, tmp_path: Path) -> None:
        target = tmp_path / "summary.md"
        target.write_text("earlier content\n", encoding="utf-8")
        lu.write_summary(lu.UploadReport(), str(target))
        text = target.read_text(encoding="utf-8")
        assert text.startswith("earlier content")
        assert "Agent transcripts" in text

    def test_no_summary_target_is_not_an_error(self) -> None:
        lu.write_summary(lu.UploadReport(), None)


class TestThePairParsing:
    def test_it_reads_the_repeated_options(self) -> None:
        assert lu.parse_pairs(["team:acme", "run_kind:test"], ":", "--tag") == {
            "team": "acme",
            "run_kind": "test",
        }
        assert lu.parse_pairs(["stage=resolve"], "=", "--metadata") == {"stage": "resolve"}

    def test_a_value_may_hold_the_separator(self) -> None:
        assert lu.parse_pairs(["ci_chain:a:b"], ":", "--tag") == {"ci_chain": "a:b"}

    @pytest.mark.parametrize("value", ["noseparator", ":novalue"])
    def test_a_malformed_pair_is_an_error(self, value: str) -> None:
        with pytest.raises(lu.UploadError, match="must be name"):
            lu.parse_pairs([value], ":", "--tag")


class TestTheEnvironmentGuard:
    """A v4 receiver merges observations by id alone, so the same trace re-sent under a second
    environment is a silent no-op reported as success. One read before the write catches it."""

    @staticmethod
    def receiver_answering(pages: list[Any]) -> lu.Receiver:
        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        answers = iter(pages)
        object.__setattr__(receiver, "_get", lambda path: next(answers, None))
        return receiver

    def test_it_reads_the_environment_of_every_row(self) -> None:
        receiver = self.receiver_answering(
            [{"data": [{"id": "a", "environment": "test"}, {"id": "b", "environment": "test"}]}]
        )
        assert receiver.environments_of("trace") == {"test"}

    def test_a_row_without_an_environment_sits_in_the_default(self) -> None:
        receiver = self.receiver_answering([{"data": [{"id": "a"}]}])
        assert receiver.environments_of("trace") == {"default"}

    def test_a_trace_nobody_wrote_answers_with_nothing(self) -> None:
        receiver = self.receiver_answering([{"data": []}])
        assert receiver.environments_of("trace") == set()

    def test_it_walks_the_pages_and_stops_on_a_short_one(self) -> None:
        full = {
            "data": [{"id": str(i), "environment": "test"} for i in range(lu.READ_PAGE)],
            "meta": {"cursor": "next"},
        }
        receiver = self.receiver_answering(
            [full, {"data": [{"id": "last", "environment": "production-automation"}]}]
        )
        assert receiver.environments_of("trace") == {"test", "production-automation"}

    def test_a_receiver_whose_rest_api_is_unreachable_says_so(self) -> None:
        """``None`` is "could not ask", which is not the same as "nowhere"."""
        assert self.receiver_answering([None]).environments_of("trace") is None

    @pytest.mark.parametrize(
        ("found", "environment", "expected"),
        [
            ({"test"}, "production-automation", {"test"}),
            ({"production-automation"}, "production-automation", set()),
            ({"default"}, None, set()),
            (set(), "production-automation", set()),
            (None, "production-automation", set()),
        ],
    )
    def test_only_a_real_difference_counts(
        self, found: set[str] | None, environment: str | None, expected: set[str]
    ) -> None:
        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        object.__setattr__(receiver, "environments_of", lambda trace_id: found)
        assert lu._held_elsewhere(receiver, "trace", environment) == expected

    def test_no_receiver_means_no_question_to_ask(self) -> None:
        assert lu._held_elsewhere(None, "trace", "production-automation") == set()

    def test_a_trace_already_elsewhere_is_not_sent(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        receiver = self.receiver_answering(
            [{"data": [{"id": "a", "environment": "test-automation"}]}]
        )
        exporter = FakeExporter()
        import tolokaforge_langfuse.otlp_transport as transport

        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            report = upload(tmp_path, dry_run=False, receiver=receiver)
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]
        assert report.sent == [] and exporter.batches == []
        assert "test-automation" in report.mismatched[0]["reason"]
        assert not report.ok
        assert "already in another environment" in report.as_markdown()
