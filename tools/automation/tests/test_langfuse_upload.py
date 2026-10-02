"""Unit tests for ``automation.langfuse_upload``: how a transcript file becomes a trace id, how
the receiver is read from the step's environment (and never printed), and what the command does
with a file the reader refuses, one the sentinel blocks and one the receiver will not take.

No network: the export is a fake exporter, and the dry run proves the whole path up to the send.
"""

from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path
from typing import Any

import automation.langfuse_upload as lu
import pytest

from tolokaforge.secrets import DictProvider, SecretManager

pytestmark = pytest.mark.unit

CALLER = {"team": "acme", "run_kind": "test", "ci_run": "12345"}

# the name the CLI reports in CLEAN_EVENTS, and the model a gateway routed that alias to
ALIAS = "claude-opus-4-8"
SERVED = "anthropic/claude-opus-5.5"

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


# what an agent's caller keeps beside its output as ``<stem>.prompt.txt``
PROMPT = "You are one of five independent analysis agents.\n\n== CONTEXT ==\nthe run's facts\n"


def write(directory: Path, name: str, events: list[dict[str, Any]]) -> Path:
    path = directory / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def root_of(batch: list[Any]) -> Any:
    """The root span of one exported transcript: the trace itself on a v4 receiver."""
    return next(span for span in batch if span.parent is None)


def receiver_from(env: dict[str, str]) -> lu.Receiver:
    """The receiver the command builds, its secrets from a fixed manager instead of the process."""
    return lu.Receiver.from_environment(env, secrets=SecretManager([DictProvider(env)]))


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


def model_facts(batch: list[Any]) -> dict[str, Any]:
    """What one exported transcript says about its model: the generations' native model, the
    root's model tags and the root's model metadata."""
    generations = [
        span for span in batch if span.attributes["langfuse.observation.type"] == "generation"
    ]
    root = next(span for span in batch if span.parent is None)
    metadata = "langfuse.trace.metadata."
    return {
        "generation_models": {
            span.attributes["langfuse.observation.model.name"] for span in generations
        },
        "model_tags": sorted(
            tag for tag in root.attributes["langfuse.trace.tags"] if tag.startswith("model")
        ),
        "model_name": root.attributes[f"{metadata}model_name"],
        "model_names": root.attributes[f"{metadata}model_names"],
        "cli_model": root.attributes.get(f"{metadata}cli_model"),
    }


class TestTheTranscriptId:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("agent_iter_1.jsonl", "resolve/1"),
            ("agent_iter_12.json", "resolve/12"),
            ("agent_finalize.jsonl", "finalize"),
            ("agent_iter_x.jsonl", "agent_iter_x"),
            ("capture.jsonl", "capture"),
        ],
    )
    def test_the_file_name_names_the_step(self, name: str, expected: str) -> None:
        assert lu.transcript_id_for(Path(name)) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # the dimension keeps its own underscores: they are not separators
            ("analysis_harness_infra.json", "analysis/harness_infra"),
            ("analysis_task_design_oracle.json", "analysis/task_design_oracle"),
            # the writer numbers a dimension's later runs from 2 in the same directory
            ("analysis_harness_infra.2.json", "analysis/harness_infra/2"),
            ("analysis_four_bucket.12.json", "analysis/four_bucket/12"),
            # a dot inside the dimension, and a trailing number the writer never puts there
            ("analysis_v1.5_x.json", "analysis/v1.5_x"),
            ("analysis_codec.1.json", "analysis/codec.1"),
            ("analysis_codec.02.json", "analysis/codec.02"),
            ("analysis_codec.0.json", "analysis/codec.0"),
            ("analysis_agent.jsonl", "analysis/agent"),
        ],
    )
    def test_an_analysis_file_names_its_dimension_and_its_run(
        self, name: str, expected: str
    ) -> None:
        assert lu.transcript_id_for(Path(name)) == expected

    def test_two_runs_of_one_dimension_are_two_transcripts(self) -> None:
        """A retried dimension keeps both files: the first attempt is the one worth reading when
        it died on a budget, so the two must not merge into one trace."""
        first = lu.transcript_id_for(Path("analysis_four_bucket.json"))
        retry = lu.transcript_id_for(Path("analysis_four_bucket.2.json"))
        assert first != retry
        assert retry.startswith(first + "/")

    @pytest.mark.parametrize(
        "name",
        [
            "agent_iter_1.jsonl",
            "agent_finalize.jsonl",
            "analysis_harness_infra.json",
            "analysis_consistency_passk.3.json",
            "analysis_v1.5_x.jsonl",
        ],
    )
    def test_an_agent_output_name_is_read_from_a_directory(self, name: str) -> None:
        assert lu.is_agent_output(Path(name))

    @pytest.mark.parametrize(
        "name",
        [
            "decision.json",
            "last_reprobe.json",
            "verdict.harness_infra.json",
            "analysis.json",
            "analysis_.json",
            "analysis-harness_infra.json",
            "cost_summary.json",
            "agent_iter_x.jsonl",
        ],
    )
    def test_any_other_name_is_not(self, name: str) -> None:
        assert not lu.is_agent_output(Path(name))


class TestTheReceiver:
    def test_it_builds_the_endpoint_and_the_basic_header(self) -> None:
        receiver = receiver_from(
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
        receiver = receiver_from(
            {
                "LANGFUSE_BASE_URL": "https://receiver.example.com",
                "LANGFUSE_PUBLIC_KEY": "public-value",
                "LANGFUSE_SECRET_KEY": "secret-value",
            }
        )
        assert "secret-value" not in repr(receiver)
        assert "Basic" not in repr(receiver)

    def test_an_explicit_endpoint_wins_over_the_base_url(self) -> None:
        receiver = receiver_from(
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
            receiver_from(env)

    def test_the_key_pair_comes_through_the_secret_manager_not_the_mapping(self) -> None:
        """The mapping carries the address only; a credential in it is not read."""
        env = {
            "LANGFUSE_BASE_URL": "https://h",
            "LANGFUSE_PUBLIC_KEY": "p",
            "LANGFUSE_SECRET_KEY": "s",
        }
        with pytest.raises(lu.UploadError, match="no credentials"):
            lu.Receiver.from_environment(env, secrets=SecretManager([DictProvider({})]))

    def test_by_default_the_secrets_are_the_steps_own_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public-value")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret-value")
        monkeypatch.setenv("LANGFUSE_EXTRA_HEADERS", "X-GitHub-Runner-Key=admission-value")
        receiver = lu.Receiver.from_environment({"LANGFUSE_BASE_URL": "https://h"})
        assert receiver.headers["Authorization"] == "Basic " + b64encode(
            b"public-value:secret-value"
        ).decode("ascii")
        assert receiver.headers["X-GitHub-Runner-Key"] == "admission-value"

    def test_a_dotenv_in_the_working_directory_does_not_answer_for_the_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A developer's .env must not stand in for the secrets the workflow maps in."""
        (tmp_path / ".env").write_text(
            "LANGFUSE_PUBLIC_KEY=from-dotenv\nLANGFUSE_SECRET_KEY=from-dotenv\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        with pytest.raises(lu.UploadError, match="no credentials"):
            lu.Receiver.from_environment({"LANGFUSE_BASE_URL": "https://h"})

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

    def test_the_error_never_echoes_the_value(self) -> None:
        """The likeliest typo is the bare admission key without its name, and that is a
        credential: the message gives its size only."""
        with pytest.raises(lu.UploadError) as caught:
            lu._extra_headers("admission-not-real-0123")
        assert "admission-not-real-0123" not in str(caught.value)

    @staticmethod
    def _with_headers(raw: str) -> lu.Receiver:
        return receiver_from(
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

    def test_a_stage_directory_sends_only_the_agents_output_files(self, tmp_path: Path) -> None:
        """The resolve directory also holds the compose step's decision.json and the reprobe
        findings: read as transcripts they were refused, and every real run exited 1."""
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_finalize.jsonl", CLEAN_EVENTS)
        (tmp_path / "decision.json").write_text('{"fix_targets": []}', encoding="utf-8")
        (tmp_path / "last_reprobe.json").write_text('{"findings": []}', encoding="utf-8")
        report = upload(tmp_path)
        assert report.ok and report.refused == []
        assert [e["transcript_id"] for e in report.sent] == ["finalize", "resolve/1"]
        assert report.ignored == ["decision.json", "last_reprobe.json"]
        assert report.as_dict()["ignored"] == ["decision.json", "last_reprobe.json"]
        assert "`decision.json`" in report.as_markdown()

    def test_an_analysis_directory_sends_each_dimension_agents_output(self, tmp_path: Path) -> None:
        """An evaluation's analysis keeps one file per dimension agent, the JSON array that
        ``claude -p --output-format json --verbose`` prints, and a retry beside the first attempt.
        The verdicts and the run's other records are not transcripts."""
        for name in (
            "analysis_harness_infra.json",
            "analysis_harness_infra.2.json",
            "analysis_four_bucket.json",
        ):
            (tmp_path / name).write_text(json.dumps(CLEAN_EVENTS), encoding="utf-8")
        (tmp_path / "decision.json").write_text('{"fix_targets": []}', encoding="utf-8")
        (tmp_path / "verdict.harness_infra.json").write_text(
            '{"verdict": "CLEAN"}', encoding="utf-8"
        )
        report = upload(tmp_path, run_id="automation/eval-orchestrate/1/1")
        assert report.ok and report.refused == []
        assert [(e["file"], e["transcript_id"]) for e in report.sent] == [
            ("analysis_four_bucket.json", "analysis/four_bucket"),
            ("analysis_harness_infra.2.json", "analysis/harness_infra/2"),
            ("analysis_harness_infra.json", "analysis/harness_infra"),
        ]
        assert len({e["trace_id"] for e in report.sent}) == 3
        assert {e["spans"] for e in report.sent} == {3}
        assert report.ignored == ["decision.json", "verdict.harness_infra.json"]

    def test_a_file_named_on_the_command_line_is_read_whatever_its_name(
        self, tmp_path: Path
    ) -> None:
        report = upload(write(tmp_path, "capture.jsonl", CLEAN_EVENTS))
        assert report.ok and report.ignored == []
        assert [e["transcript_id"] for e in report.sent] == ["capture"]

    def test_a_refused_file_does_not_stop_the_others(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_iter_2.jsonl", [{"type": "tool_progress", "message": {}}])
        report = upload(tmp_path)
        assert [e["transcript_id"] for e in report.sent] == ["resolve/1"]
        assert [e["file"] for e in report.refused] == ["agent_iter_2.jsonl"]
        assert "unknown type 'tool_progress'" in report.refused[0]["reason"]
        assert not report.ok

    def test_a_run_id_the_id_contract_refuses_refuses_each_file_without_a_crash(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_finalize.jsonl", CLEAN_EVENTS)
        report = upload(tmp_path, run_id="a|b")
        assert report.sent == []
        assert [e["file"] for e in report.refused] == ["agent_finalize.jsonl", "agent_iter_1.jsonl"]
        assert "refuses this trace id" in report.refused[0]["reason"]

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

    def test_a_dotenv_line_in_the_agents_own_words_is_blocked(self, tmp_path: Path) -> None:
        """No policy redacts what the agent says, so the sentinel is its only guard, and a
        dotenv line matches only with its line break intact."""
        line = "NPM_" + "TOKEN=" + "q" * 16
        events = json.loads(json.dumps(CLEAN_EVENTS))
        events[1]["message"]["content"][0]["text"] = f"found this in .env:\n{line}\n"
        write(tmp_path, "agent_iter_1.jsonl", events)
        report = upload(tmp_path)
        assert report.sent == []
        assert "dotenv-secret" in report.blocked[0]["reason"]

    def test_a_run_of_blank_lines_does_not_stall_the_scan(self) -> None:
        """An agent turn that degenerates into newlines is a known failure mode; a line-anchored
        shape made the raw scan quadratic in them (50k lines took tens of seconds)."""
        import time

        from tolokaforge_langfuse import safety

        events = [{"type": "trace-create", "body": {"output": "a" + "\n" * 50_000 + "b"}}]
        started = time.monotonic()
        assert lu._scan(safety.SafetyGate(), events, what="t") == []
        assert time.monotonic() - started < 5.0

    def test_a_dotenv_line_after_blank_lines_is_still_found(self) -> None:
        from tolokaforge_langfuse import safety

        line = "NPM_" + "TOKEN=" + "q" * 16
        events = [{"type": "trace-create", "body": {"output": "see:\n\n\n" + line}}]
        assert [f.rule for f in lu._scan(safety.SafetyGate(), events, what="t")] == [
            "dotenv-secret"
        ]

    @pytest.mark.parametrize(
        "value",
        ['Zq"8!mK-p2wX-9', "p\u00e4ssw\u00f6rd-12345", "back\\slash-12345"],
        ids=["quote", "non-ascii", "backslash"],
    )
    def test_a_known_value_json_would_escape_is_still_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("ACME_DB_PASSWORD", value)
        events = json.loads(json.dumps(CLEAN_EVENTS))
        events[-1]["result"] = f"the password is {value}"
        write(tmp_path, "agent_iter_1.jsonl", events)
        report = upload(tmp_path)
        assert report.sent == []
        assert "known-secret-value" in report.blocked[0]["reason"]
        assert value not in json.dumps(report.as_dict(), ensure_ascii=False)

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

    def test_the_admission_key_is_a_value_the_sentinel_knows(self, tmp_path: Path) -> None:
        """It reaches the process as LANGFUSE_EXTRA_HEADERS, which the gate does not read as a
        secret's name, and the agent's own words are never redacted."""
        admission = "admission-not-real-0123456789"
        events = json.loads(json.dumps(CLEAN_EVENTS))
        events[1]["message"]["content"][0]["text"] = f"the runner key is {admission}"
        write(tmp_path, "agent_iter_1.jsonl", events)
        receiver = lu.Receiver(
            endpoint="https://h/v1/traces",
            headers={"Authorization": "Basic x", "X-GitHub-Runner-Key": admission},
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
        assert "known-secret-value" in report.blocked[0]["reason"]
        assert admission not in json.dumps(report.as_dict())

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

    def test_the_served_model_names_the_trace_the_receiver_gets(self, tmp_path: Path) -> None:
        """An analysis agent runs ``claude -p`` against an alias a gateway routes to another
        model, and the CLI reports the alias: the model that served is the caller's to name."""
        (tmp_path / "analysis_four_bucket.json").write_text(
            json.dumps(CLEAN_EVENTS), encoding="utf-8"
        )
        exporter = FakeExporter()
        report = self._send(tmp_path, exporter, model=SERVED)
        assert report.ok and len(exporter.batches) == 1
        assert model_facts(exporter.batches[0]) == {
            "generation_models": {SERVED},
            "model_tags": [f"model:{SERVED}"],
            "model_name": SERVED,
            "model_names": f"{ALIAS} {SERVED}",
            "cli_model": ALIAS,
        }

    def test_without_a_served_model_the_cli_reported_name_stays(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        exporter = FakeExporter()
        assert self._send(tmp_path, exporter).ok
        assert model_facts(exporter.batches[0]) == {
            "generation_models": {ALIAS},
            "model_tags": [f"model:{ALIAS}"],
            "model_name": ALIAS,
            "model_names": ALIAS,
            "cli_model": None,
        }

    def test_surrounding_whitespace_never_reaches_the_model_tag(self, tmp_path: Path) -> None:
        """A value read from a file keeps its line break unless something takes it off."""
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        exporter = FakeExporter()
        assert self._send(tmp_path, exporter, model=f" {SERVED}\n").ok
        facts = model_facts(exporter.batches[0])
        assert facts["model_tags"] == [f"model:{SERVED}"]
        assert facts["generation_models"] == {SERVED}

    def test_a_served_model_the_vocabulary_refuses_sends_nothing(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        exporter = FakeExporter()
        report = self._send(tmp_path, exporter, model="two words")
        assert report.sent == [] and exporter.batches == []
        assert "the value must be" in report.refused[0]["reason"]
        assert not report.ok

    @staticmethod
    def _send(directory: Path, exporter: FakeExporter, **overrides: Any) -> lu.UploadReport:
        import tolokaforge_langfuse.otlp_transport as transport

        receiver = lu.Receiver(endpoint="https://h/v1/traces", headers={"Authorization": "Basic x"})
        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            return upload(directory, dry_run=False, receiver=receiver, **overrides)
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]


class TestThePrompt:
    """The agent's output does not repeat the prompt it was given: its caller keeps it beside the
    output as ``<stem>.prompt.txt``, and it becomes that transcript's trace input."""

    INPUT = "langfuse.observation.input"
    META = "langfuse.trace.metadata."

    def test_a_prompt_beside_a_transcript_is_its_traces_input(self, tmp_path: Path) -> None:
        for name in ("analysis_four_bucket.json", "analysis_harness_infra.json"):
            (tmp_path / name).write_text(json.dumps(CLEAN_EVENTS), encoding="utf-8")
        (tmp_path / "analysis_four_bucket.prompt.txt").write_text(PROMPT, encoding="utf-8")
        exporter = FakeExporter()
        report = TestTheUpload._send(tmp_path, exporter)
        assert report.ok and len(exporter.batches) == 2
        # name order: the dimension with the prompt first
        prompted, plain = (root_of(batch) for batch in exporter.batches)
        assert prompted.attributes[self.INPUT] == PROMPT
        assert prompted.attributes[f"{self.META}input_chars"] == len(PROMPT)
        assert prompted.attributes[f"{self.META}input_truncated"] is False
        assert prompted.attributes[f"{self.META}input_redacted_rules"] == "none"
        # without one nothing changes: no input, no input facts
        assert self.INPUT not in plain.attributes
        assert not [key for key in plain.attributes if key.startswith(f"{self.META}input_")]
        assert [entry.get("prompt") for entry in report.sent] == [
            "analysis_four_bucket.prompt.txt",
            None,
        ]
        assert "2** transcript(s), 6 span(s), 1 with the agent's prompt as input" in (
            report.as_markdown()
        )

    def test_a_retry_takes_its_own_prompt(self, tmp_path: Path) -> None:
        for stem, text in (
            ("analysis_four_bucket", "the first attempt's prompt"),
            ("analysis_four_bucket.2", "the retry's prompt, with its correction"),
        ):
            (tmp_path / f"{stem}.json").write_text(json.dumps(CLEAN_EVENTS), encoding="utf-8")
            (tmp_path / f"{stem}.prompt.txt").write_text(text, encoding="utf-8")
        exporter = FakeExporter()
        report = TestTheUpload._send(tmp_path, exporter)
        assert report.ok
        assert [(e["transcript_id"], e["prompt"]) for e in report.sent] == [
            ("analysis/four_bucket/2", "analysis_four_bucket.2.prompt.txt"),
            ("analysis/four_bucket", "analysis_four_bucket.prompt.txt"),
        ]
        assert [root_of(batch).attributes[self.INPUT] for batch in exporter.batches] == [
            "the retry's prompt, with its correction",
            "the first attempt's prompt",
        ]

    def test_a_prompt_file_is_never_a_transcript_nor_a_file_not_read(self, tmp_path: Path) -> None:
        """Not parsed, not refused, not listed as not read. One without its transcript, which no
        writer leaves, pairs with nothing and is not read either."""
        (tmp_path / "analysis_four_bucket.json").write_text(
            json.dumps(CLEAN_EVENTS), encoding="utf-8"
        )
        (tmp_path / "analysis_four_bucket.prompt.txt").write_text(PROMPT, encoding="utf-8")
        (tmp_path / "analysis_harness_infra.prompt.txt").write_text("orphan", encoding="utf-8")
        (tmp_path / "decision.json").write_text('{"fix_targets": []}', encoding="utf-8")
        report = upload(tmp_path)
        assert report.ok and report.refused == []
        assert [e["file"] for e in report.sent] == ["analysis_four_bucket.json"]
        assert report.ignored == ["decision.json"]
        assert "harness_infra" not in json.dumps(report.as_dict())
        assert "harness_infra" not in report.as_markdown()

    def test_a_file_named_on_the_command_line_takes_its_prompt_too(self, tmp_path: Path) -> None:
        path = write(tmp_path, "capture.jsonl", CLEAN_EVENTS)
        (tmp_path / "capture.prompt.txt").write_text(PROMPT, encoding="utf-8")
        report = upload(path)
        assert report.ok and report.sent[0]["prompt"] == "capture.prompt.txt"

    def test_a_prompt_that_cannot_be_read_refuses_its_transcript(self, tmp_path: Path) -> None:
        """Sent without it, the trace would read as an agent that was given no prompt."""
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        write(tmp_path, "agent_iter_2.jsonl", CLEAN_EVENTS)
        (tmp_path / "agent_iter_2.prompt.txt").write_bytes(b"\xff\xfe not utf-8")
        report = upload(tmp_path)
        assert [e["transcript_id"] for e in report.sent] == ["resolve/1"]
        assert [e["file"] for e in report.refused] == ["agent_iter_2.jsonl"]
        assert "agent_iter_2.prompt.txt cannot be read" in report.refused[0]["reason"]
        assert not report.ok

    def test_a_prompt_past_the_cap_is_cut_and_says_so(self, tmp_path: Path) -> None:
        from tolokaforge_langfuse import transcripts as tr

        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        prompt = "p" * tr.INPUT_MAX_CHARS + "q" * 1000
        (tmp_path / "agent_iter_1.prompt.txt").write_text(prompt, encoding="utf-8")
        exporter = FakeExporter()
        assert TestTheUpload._send(tmp_path, exporter).ok
        root = root_of(exporter.batches[0])
        assert root.attributes[self.INPUT] == "p" * tr.INPUT_MAX_CHARS + (
            "... [1000 more characters]"
        )
        assert root.attributes[f"{self.META}input_chars"] == len(prompt)
        assert root.attributes[f"{self.META}input_truncated"] is True

    def test_a_credential_shape_in_the_prompt_is_scrubbed_not_blocked(self, tmp_path: Path) -> None:
        line = "NPM_" + "TOKEN=" + "q" * 16
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        (tmp_path / "agent_iter_1.prompt.txt").write_text(f"{PROMPT}{line}\n", encoding="utf-8")
        exporter = FakeExporter()
        assert TestTheUpload._send(tmp_path, exporter).ok
        root = root_of(exporter.batches[0])
        assert line not in root.attributes[self.INPUT]
        assert "[redacted:dotenv-secret]" in root.attributes[self.INPUT]
        assert root.attributes[f"{self.META}input_redacted_rules"] == "dotenv-secret"

    def test_the_sentinel_guards_the_prompt_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scrub knows shapes; a value only this process holds is the sentinel's to stop."""
        value = "a-password-that-matches-no-shape"
        monkeypatch.setenv("ACME_UPLOAD_TOKEN", value)
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        (tmp_path / "agent_iter_1.prompt.txt").write_text(
            f"{PROMPT}log in with {value}\n", encoding="utf-8"
        )
        report = upload(tmp_path)
        assert report.sent == []
        assert "known-secret-value" in report.blocked[0]["reason"]
        assert value not in json.dumps(report.as_dict())

    def test_its_metadata_keys_are_not_the_callers(self, tmp_path: Path) -> None:
        """Reserved even on a transcript without a prompt, like ``cli_model``."""
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        report = upload(tmp_path, metadata={"input_chars": "12"})
        assert report.sent == []
        assert "may not override schema keys: input_chars" in report.refused[0]["reason"]


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


class TestACrash:
    """A traceback of this command lands in a public job log, and its locals hold the sentinel's
    known values and the agent's raw output. The command must crash without printing them."""

    CRASH = (
        "import automation.cli as cli\n"
        "import automation.langfuse_upload as lu\n"
        "def crash(*args, **kwargs):\n"
        "    held = 'fake-credential-value-0123456789'\n"
        "    raise RuntimeError('the upload crashed')\n"
        "lu.upload = crash\n"
        "cli.app(['langfuse-upload', 'unused', '--run-id', 'r', '--label', 'l'])\n"
    )

    def test_a_crash_prints_no_local_value(self) -> None:
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != "_TYPER_STANDARD_TRACEBACK"}
        result = subprocess.run(
            [sys.executable, "-c", self.CRASH],
            capture_output=True,
            text=True,
            env={**env, "COLUMNS": "200"},
            check=False,
            timeout=60,
        )
        assert result.returncode != 0
        assert "the upload crashed" in result.stderr
        assert "fake-credential-value" not in result.stderr + result.stdout


class TestTheModelOption:
    """``--model`` through the command itself, as far as the spans it hands the exporter."""

    @staticmethod
    def _run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str, prompt: str | None = None
    ) -> tuple[Any, FakeExporter]:
        import automation.cli as cli
        import tolokaforge_langfuse.otlp_transport as transport
        from typer.testing import CliRunner

        exporter = FakeExporter()
        monkeypatch.setattr(transport, "make_otlp_exporter", lambda *a, **k: exporter)
        for name in ("LANGFUSE_BASE_URL", "LANGFUSE_PROJECT", "LANGFUSE_ENVIRONMENT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("LANGFUSE_EXTRA_HEADERS", raising=False)
        monkeypatch.setenv("LANGFUSE_OTLP_ENDPOINT", "https://receiver.invalid/v1/traces")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public-not-real")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret-not-real")
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / "analysis_four_bucket.json").write_text(json.dumps(CLEAN_EVENTS), encoding="utf-8")
        if prompt is not None:
            (stage / "analysis_four_bucket.prompt.txt").write_text(prompt, encoding="utf-8")
        result = CliRunner().invoke(
            cli.app,
            [
                "langfuse-upload",
                str(stage),
                "--run-id",
                "automation/eval-orchestrate/1/1",
                "--label",
                "pilot",
                "--tag",
                "team:acme",
                "--tag",
                "run_kind:test",
                "--summary",
                str(tmp_path / "summary.md"),
                *extra,
            ],
        )
        return result, exporter

    def test_it_names_the_model_that_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, exporter = self._run(tmp_path, monkeypatch, "--model", SERVED)
        assert result.exit_code == 0, result.output
        facts = model_facts(exporter.batches[0])
        assert facts["generation_models"] == {SERVED}
        assert facts["model_tags"] == [f"model:{SERVED}"]
        assert facts["cli_model"] == ALIAS

    def test_without_it_the_command_sends_what_the_cli_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, exporter = self._run(tmp_path, monkeypatch)
        assert result.exit_code == 0, result.output
        facts = model_facts(exporter.batches[0])
        assert facts["generation_models"] == {ALIAS}
        assert facts["cli_model"] is None


class TestThePromptThroughTheCommand:
    """The command pairs a prompt with its transcript by itself: the step needs no option."""

    def test_a_prompt_beside_the_output_is_the_input_the_receiver_gets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, exporter = TestTheModelOption._run(
            tmp_path, monkeypatch, "--model", SERVED, prompt=PROMPT
        )
        assert result.exit_code == 0, result.output
        root = root_of(exporter.batches[0])
        assert root.attributes["langfuse.observation.input"] == PROMPT
        assert root.attributes["langfuse.trace.metadata.input_chars"] == len(PROMPT)
        assert "1 with the agent's prompt as input" in result.output

    def test_without_one_the_trace_has_no_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, exporter = TestTheModelOption._run(tmp_path, monkeypatch, "--model", SERVED)
        assert result.exit_code == 0, result.output
        assert "langfuse.observation.input" not in root_of(exporter.batches[0]).attributes
        assert "with the agent's prompt" not in result.output


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


class TestTheProjectCheck:
    """The keys must open the project the traces are tagged with, as on the live path."""

    @staticmethod
    def receiver_opening(name: str | None) -> lu.Receiver:
        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        object.__setattr__(receiver, "project_name", lambda: name)
        return receiver

    def test_keys_that_open_another_project_refuse_before_any_send(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        exporter = FakeExporter()
        import tolokaforge_langfuse.otlp_transport as transport

        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            with pytest.raises(lu.UploadError, match="open project 'other', not 'acme'"):
                upload(
                    tmp_path,
                    dry_run=False,
                    project="acme",
                    receiver=self.receiver_opening("other"),
                )
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]
        assert exporter.batches == []

    @pytest.mark.parametrize(("opened", "verified"), [("acme", True), (None, False)])
    def test_a_match_verifies_and_a_receiver_that_cannot_be_asked_does_not(
        self, opened: str | None, verified: bool
    ) -> None:
        """``None`` is "could not ask": the upload goes on, unverified, as on the live path."""
        assert lu._project_verified(self.receiver_opening(opened), "acme") is verified

    def test_no_expected_project_means_nothing_to_check(self) -> None:
        assert lu._project_verified(self.receiver_opening("other"), None) is False
        assert lu._project_verified(None, "acme") is False

    def test_an_empty_project_is_no_expectation_rather_than_a_mismatch(self) -> None:
        """A workflow maps LANGFUSE_PROJECT from a variable that may be undefined, and an empty
        string then reaches the command; the traces say project:none, as without the variable."""
        assert lu._project_verified(self.receiver_opening("arena"), "") is False


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
        "answer",
        [{"message": "not the API"}, {"data": None}, ["a", "list"], {"data": ["<html>", "page"]}],
        ids=["no-data", "null-data", "not-a-mapping", "rows-that-are-not-rows"],
    )
    def test_an_answer_that_is_not_the_api_is_could_not_ask(self, answer: Any) -> None:
        assert self.receiver_answering([answer]).environments_of("trace") is None

    def test_a_receiver_that_repeats_its_cursor_does_not_spin(self) -> None:
        full = {
            "data": [{"id": str(i), "environment": "test"} for i in range(lu.READ_PAGE)],
            "meta": {"cursor": "same"},
        }
        reads: list[str] = []

        def forever(path: str) -> Any:
            reads.append(path)
            if len(reads) > 10:
                raise AssertionError("still paging after 10 reads of the same cursor")
            return full

        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        object.__setattr__(receiver, "_get", forever)
        assert receiver.environments_of("trace") is None
        assert len(reads) == 2

    def test_a_redirect_is_not_followed_and_carries_no_credential(self) -> None:
        """urllib copies every header, Authorization included, to the host a redirect names, and
        the answer from there would pass for the receiver's own."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        reached: list[dict[str, str]] = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                reached.append({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps({"data": [{"name": "acme", "environment": "x"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        elsewhere = HTTPServer(("127.0.0.1", 0), Elsewhere)
        target = f"http://127.0.0.1:{elsewhere.server_address[1]}/login"

        class Redirecting(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        receiver_side = HTTPServer(("127.0.0.1", 0), Redirecting)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (elsewhere, receiver_side)
        ]
        for thread in threads:
            thread.start()
        try:
            base = f"http://127.0.0.1:{receiver_side.server_address[1]}"
            receiver = receiver_from(
                {
                    "LANGFUSE_BASE_URL": base,
                    "LANGFUSE_PUBLIC_KEY": "public-not-real",
                    "LANGFUSE_SECRET_KEY": "secret-not-real",
                    "LANGFUSE_EXTRA_HEADERS": "X-GitHub-Runner-Key=admission-not-real",
                }
            )
            assert receiver.project_name() is None
            assert receiver.environments_of("trace") is None
        finally:
            for server in (elsewhere, receiver_side):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
        assert reached == []

    def test_a_walk_without_end_stops(self) -> None:
        """Fresh cursors forever are no more paging than the same one twice."""
        reads: list[int] = []

        def endless(path: str) -> Any:
            reads.append(1)
            if len(reads) > 10 * lu.MAX_PAGES:
                raise AssertionError("the walk did not stop")
            return {
                "data": [{"id": str(i), "environment": "test"} for i in range(lu.READ_PAGE)],
                "meta": {"cursor": f"cursor-{len(reads)}"},
            }

        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        object.__setattr__(receiver, "_get", endless)
        assert receiver.environments_of("trace") is None
        assert len(reads) == lu.MAX_PAGES + 1

    def test_a_malformed_http_answer_is_could_not_ask(self) -> None:
        """A proxy answering with a broken status line raises http.client's own error, which is
        not an OSError; it has to read as "could not ask" rather than end the upload."""
        import socketserver
        import threading

        class Garbage(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.rfile.readline()
                self.wfile.write(b"not-http\r\n\r\n")

        server = socketserver.TCPServer(("127.0.0.1", 0), Garbage)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            receiver = lu.Receiver(endpoint=f"{base}/v1/traces", base_url=base)
            assert receiver.environments_of("trace") is None
            assert receiver.project_name() is None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @pytest.mark.parametrize(
        ("found", "environment", "expected"),
        [
            ({"test"}, "production-automation", {"test"}),
            ({"production-automation"}, "production-automation", set()),
            ({"default"}, None, set()),
            (set(), "production-automation", set()),
            # could not ask: not "nowhere", and the upload reports it
            (None, "production-automation", None),
        ],
    )
    def test_only_a_real_difference_counts(
        self, found: set[str] | None, environment: str | None, expected: set[str] | None
    ) -> None:
        receiver = lu.Receiver(endpoint="https://h/v1/traces", base_url="https://h")
        object.__setattr__(receiver, "environments_of", lambda trace_id: found)
        assert lu._held_elsewhere(receiver, "trace", environment) == expected

    def test_no_receiver_means_no_question_to_ask(self) -> None:
        assert lu._held_elsewhere(None, "trace", "production-automation") == set()

    def test_a_send_the_guard_checked_is_not_listed_as_unchecked(self, tmp_path: Path) -> None:
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        receiver = self.receiver_answering([{"data": []}])
        exporter = FakeExporter()
        import tolokaforge_langfuse.otlp_transport as transport

        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            report = upload(tmp_path, dry_run=False, receiver=receiver)
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]
        assert report.ok and len(exporter.batches) == 1
        assert report.unchecked == []
        assert "without the environment check" not in report.as_markdown()

    def test_a_send_the_guard_could_not_check_says_so(self, tmp_path: Path) -> None:
        """An alias that routes OTLP and nothing else leaves the guard blind: the upload goes on,
        and the report says so instead of reading like a checked send."""
        write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
        receiver = self.receiver_answering([None])
        exporter = FakeExporter()
        import tolokaforge_langfuse.otlp_transport as transport

        original = transport.make_otlp_exporter
        transport.make_otlp_exporter = lambda *a, **k: exporter  # type: ignore[assignment]
        try:
            report = upload(tmp_path, dry_run=False, receiver=receiver)
        finally:
            transport.make_otlp_exporter = original  # type: ignore[assignment]
        assert report.ok and len(exporter.batches) == 1
        assert [e["file"] for e in report.unchecked] == ["agent_iter_1.jsonl"]
        assert report.as_dict()["unchecked"] == report.unchecked
        assert "sent without the environment check: **1**" in report.as_markdown()

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


class TestWhatReachesTheWire:
    """The admission header is what gets a request past the gateway in front of the receiver, and
    a header that is built but not sent looks exactly like one that is. So this asserts on the
    real HTTP request, through the real exporter, against a local socket: no network, no service,
    and no credential that means anything."""

    @staticmethod
    def _capture(tmp_path: Path, extra: str | None) -> tuple[lu.UploadReport, dict[str, str]]:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                seen.append({k.lower(): v for k, v in self.headers.items()})
                self.rfile.read(int(self.headers.get("content-length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                "LANGFUSE_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}/v1/traces",
                "LANGFUSE_PUBLIC_KEY": "public-not-real",
                "LANGFUSE_SECRET_KEY": "secret-not-real",
            }
            if extra:
                environment["LANGFUSE_EXTRA_HEADERS"] = extra
            write(tmp_path, "agent_iter_1.jsonl", CLEAN_EVENTS)
            report = upload(tmp_path, dry_run=False, receiver=receiver_from(environment))
        finally:
            server.shutdown()
            thread.join(timeout=5)
        return report, (seen[0] if seen else {})

    def test_the_admission_header_is_on_the_request_itself(self, tmp_path: Path) -> None:
        report, headers = self._capture(tmp_path, "X-GitHub-Runner-Key=admission-not-real")
        assert report.ok, report.as_dict()
        assert headers.get("x-github-runner-key") == "admission-not-real"
        assert headers.get("authorization", "").startswith("Basic ")
        # the receiver selects its direct ingestion path on this header, so it rides along too
        assert headers.get("x-langfuse-ingestion-version") == "4"
        assert headers.get("content-type") == "application/x-protobuf"

    @pytest.mark.parametrize(
        "extra",
        [
            None,
            "X-GitHub-Runner-Key=admission-not-real",
            "X-A=1,X-GitHub-Runner-Key=admission-not-real",
        ],
        ids=["no-extra-headers", "one-extra-header", "two-extra-headers"],
    )
    def test_the_ingestion_version_header_is_on_every_request(
        self, tmp_path: Path, extra: str | None
    ) -> None:
        """A v4 receiver takes its direct ingestion path only with this header, so the extra
        headers must join it, never replace it, and it must not depend on them either."""
        report, headers = self._capture(tmp_path, extra)
        assert report.ok, report.as_dict()
        assert headers.get("x-langfuse-ingestion-version") == "4"

    def test_without_the_variable_the_request_carries_no_admission_header(
        self, tmp_path: Path
    ) -> None:
        """The send still succeeds against a receiver with no gateway in front of it, which is
        exactly why this cannot be caught by watching for failures."""
        report, headers = self._capture(tmp_path, None)
        assert report.ok
        assert "x-github-runner-key" not in headers
        assert headers.get("authorization", "").startswith("Basic ")
