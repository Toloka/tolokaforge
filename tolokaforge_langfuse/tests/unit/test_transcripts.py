"""The coding-agent transcript path: reading agent output, the outbound gate, the projection.

The adversarial payloads are **assembled at run time** rather than committed as fixture files:
every one of them is a credential *shape*, and a file full of key-shaped literals in a public
repository is a permanent finding for every secret scanner that ever reads it. Assembling them
here gives the same coverage (the table below states the outcome of each case, as the test plan
asks) and leaves nothing key-shaped on disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.observability import ids as engine_ids
from tolokaforge_langfuse import safety
from tolokaforge_langfuse import transcripts as tr

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
CLEAN = FIXTURES / "clean.jsonl"
GOLDEN = FIXTURES / "clean.events.json"

CALLER = {"team": "acme", "run_kind": "test", "ci_run": "12345", "ci_chain": "999"}


def options(**overrides: Any) -> tr.TranscriptOptions:
    base: dict[str, Any] = {
        "run_tag": "v1",
        "run_id": "automation/integrate/local/1",
        "label": "pilot",
        "session": "automation/integrate/local/1",
        "environment": "production-automation",
        "producer_version": "0.1.0",
        "project": "acme-traces",
        "caller_tags": CALLER,
    }
    base.update(overrides)
    return tr.TranscriptOptions(**base)


def read(path: Path = CLEAN, **kwargs: Any) -> tr.Transcript:
    return tr.read_claude_output(path, **kwargs)


def built(transcript: tr.Transcript, **overrides: Any) -> tr.BuiltTranscript:
    return tr.build_events(transcript, options(**overrides), ids=tr.id_contract(engine_ids))


def bodies(build: tr.BuiltTranscript) -> list[dict[str, Any]]:
    """The events without the per-send envelope (a fresh id and clock every time)."""
    return [{"type": e["type"], "body": e["body"]} for e in build.events]


def one(build: tr.BuiltTranscript, kind: str) -> list[dict[str, Any]]:
    return [e["body"] for e in build.events if e["type"] == kind]


def stream(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def tool_event(output: Any, *, call_input: Any = None) -> list[dict[str, Any]]:
    """The smallest transcript that carries one tool call and its result."""
    return [
        {
            "type": "assistant",
            "session_id": "s",
            "timestamp": "2026-09-20T10:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {"type": "text", "text": "running it"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": call_input if call_input is not None else {"command": "env"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "session_id": "s",
            "timestamp": "2026-09-20T10:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": output},
                ],
            },
        },
    ]


# Each case: the rule that must fire, and a payload built so no key-shaped literal is committed.
SECRET_CASES: tuple[tuple[str, str], ...] = (
    ("dotenv-secret", "ANTHROPIC_API_KEY=" + "x" * 40),
    ("authorization-header", "curl -v -H 'Authorization: Bearer " + "A" * 40 + "' https://h/x"),
    (
        "pem-private-key",
        "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n-----END RSA PRIVATE KEY-----",
    ),
    ("url-credentials", "https://ci:" + "p" * 20 + "@registry.example.com/simple"),
    ("langfuse-key", "pk-lf-" + "0123abcd-0123-0123-0123-0123456789ab"),
    ("openrouter-key", "sk-or-v1-" + "a" * 40),
    ("anthropic-key", "sk-ant-" + "A" * 40),
    ("openai-key", "sk-" + "B" * 40),
    ("github-token", "ghp_" + "C" * 36),
    ("slack-token", "xoxb-" + "D" * 20),
    ("aws-access-key", "AKIA" + "E" * 16),
    ("google-api-key", "AIza" + "F" * 35),
    ("gitlab-token", "glpat-" + "G" * 20),
    ("jwt", "eyJ" + "H" * 20 + ".eyJ" + "I" * 20 + "." + "J" * 20),
    ("secret-field", 'api_key: "' + "K" * 32 + '"'),
)


class TestTheReader:
    def test_the_three_output_shapes_agree(self) -> None:
        events = [json.loads(line) for line in CLEAN.read_text().splitlines() if line.strip()]
        jsonl = read()
        array = tr.read_claude_text(json.dumps(events), transcript_id="clean", origin="array")
        assert array.shape == tr.SHAPE_ARRAY
        assert jsonl.shape == tr.SHAPE_STREAM
        # the same transcript, whatever container the CLI put the events in
        assert array.turns == jsonl.turns
        assert array.outcomes == jsonl.outcomes
        assert array.result == jsonl.result
        assert (array.cli_version, array.session_id) == (jsonl.cli_version, jsonl.session_id)

    def test_the_result_object_alone_is_a_transcript_that_says_so(self) -> None:
        events = [json.loads(line) for line in CLEAN.read_text().splitlines() if line.strip()]
        only = tr.read_claude_text(json.dumps(events[-1]), transcript_id="clean", origin="object")
        assert only.shape == tr.SHAPE_RESULT
        assert only.turns == () and only.outcomes == ()
        assert only.result is not None and only.result.num_turns == 3
        # and the trace says which shape it came from, so a thin trace is explainable
        metadata = one(built(tr.redact(only)), "trace-create")[0]["metadata"]
        assert metadata["transcript_shape"] == tr.SHAPE_RESULT
        assert metadata["turn_count"] == 0

    def test_the_transcript_id_defaults_to_the_file_stem(self) -> None:
        assert read().transcript_id == "clean"
        assert read(transcript_id="resolve/2").transcript_id == "resolve/2"

    def test_it_reads_the_turns_the_tools_and_the_totals(self) -> None:
        transcript = read()
        assert [t.index for t in transcript.turns] == [0, 1]
        assert transcript.turns[0].tool_calls[0].name == "Bash"
        assert transcript.turns[1].reasoning.startswith("A src directory")
        assert [o.call_id for o in transcript.outcomes] == ["toolu_01acme"]
        assert transcript.result is not None
        assert transcript.result.total_cost_usd == pytest.approx(0.0412)
        assert transcript.started_at == "2026-09-20T10:00:00Z"
        assert transcript.ended_at == "2026-09-20T10:00:04Z"

    def test_a_string_content_is_read_as_one_text_block(self) -> None:
        events = [
            {
                "type": "assistant",
                "message": {"role": "assistant", "model": "m", "content": "plain"},
            }
        ]
        assert tr.read_claude_text(stream(events), transcript_id="t").turns[0].text == "plain"

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ({"type": "tool_progress", "message": {}}, "unknown type 'tool_progress'"),
            (
                {"type": "assistant", "message": {"content": [{"type": "citation"}]}},
                "unknown assistant content block 'citation'",
            ),
            (
                {"type": "user", "message": {"content": [{"type": "image"}]}},
                "unknown user content block 'image'",
            ),
            ({"type": "assistant", "message": "a string"}, "no message mapping"),
            ({"type": "assistant", "message": {"content": 7}}, "no content list"),
            ({"type": "user", "message": {"content": ["bare"]}}, "non-mapping content block"),
            (
                {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
                "tool_use without an id",
            ),
            (
                {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
                "tool_result without a tool_use_id",
            ),
        ],
    )
    def test_an_unknown_shape_refuses_the_whole_transcript(
        self, event: dict[str, Any], expected: str
    ) -> None:
        with pytest.raises(tr.TranscriptRefused) as caught:
            tr.read_claude_text(stream([event]), transcript_id="t", origin="probe.jsonl")
        assert expected in str(caught.value)
        # the source is named, so the operator knows which file to look at
        assert "probe.jsonl" in str(caught.value)

    def test_a_refusal_is_a_transcript_error_so_one_except_clause_catches_both(self) -> None:
        assert issubclass(tr.TranscriptRefused, tr.TranscriptError)

    def test_a_broken_line_names_its_line_number(self) -> None:
        with pytest.raises(tr.TranscriptError, match="line 2 is not JSON"):
            tr.read_claude_text('{"type":"result"}\nnot json\n', transcript_id="t")

    def test_empty_output_is_an_error_not_an_empty_trace(self) -> None:
        with pytest.raises(tr.TranscriptError, match="empty"):
            tr.read_claude_text("   ", transcript_id="t")


class TestTheSystemEvents:
    """A ``system`` event describes the machine the agent ran on. Two facts describe the agent."""

    def test_only_the_version_and_the_session_survive(self) -> None:
        transcript = read()
        assert transcript.cli_version == "2.1.220"
        assert transcript.session_id == "sess-acme-1"

    def test_nothing_about_the_runner_reaches_the_events(self) -> None:
        payload = json.dumps(bodies(built(tr.redact(read()))))
        for leak in (
            "cwd",
            "/home/runner",
            "memory_paths",
            "CLAUDE.md",
            "mcp_servers",
            "pilot-tools",
            "apiKeySource",
            "general-purpose",
        ):
            assert leak not in payload, leak

    def test_a_system_event_that_is_not_init_is_dropped_whole(self) -> None:
        events = [
            {
                "type": "system",
                "subtype": "hook_response",
                "hook_name": "PreToolUse",
                "stdout": "ANTHROPIC_API_KEY=" + "x" * 40,
                "exit_code": 0,
            },
            {"type": "result", "subtype": "success", "result": "done"},
        ]
        transcript = tr.read_claude_text(stream(events), transcript_id="t")
        payload = json.dumps(bodies(built(tr.redact(transcript))))
        assert "PreToolUse" not in payload
        assert "x" * 40 not in payload


class TestTheGate:
    def test_drop_is_the_default_and_keeps_no_tool_text_at_all(self) -> None:
        secret = "AWS_SECRET_ACCESS_KEY=" + "z" * 40
        transcript = tr.read_claude_text(stream(tool_event(secret)), transcript_id="t")
        gated = tr.redact(transcript)
        assert gated.tool_io == tr.TOOL_IO_DROP
        assert gated.outcomes[0].output == {"redacted": True, "chars": len(secret)}
        arguments = gated.turns[0].tool_calls[0].arguments
        assert arguments == {"redacted": True, "chars": len('{"command": "env"}')}
        payload = json.dumps(bodies(built(gated)))
        assert "z" * 40 not in payload
        # the tool's name and the fact of the call survive: what it did, not what it returned
        assert "tool: Bash" in payload

    def test_the_agents_own_words_survive_the_drop(self) -> None:
        transcript = tr.read_claude_text(stream(tool_event("secret output")), transcript_id="t")
        payload = json.dumps(bodies(built(tr.redact(transcript))))
        assert "running it" in payload

    @pytest.mark.parametrize(("rule", "payload"), SECRET_CASES, ids=[c[0] for c in SECRET_CASES])
    def test_scrub_removes_every_shape_the_sentinel_knows(self, rule: str, payload: str) -> None:
        text = f"the command printed:\n{payload}\nand then exited 0"
        transcript = tr.read_claude_text(stream(tool_event(text)), transcript_id="t")
        gated = tr.redact(transcript, policy=tr.TOOL_IO_SCRUB)
        outcome = gated.outcomes[0]
        assert rule in outcome.removed, f"{rule} did not fire: {outcome.output!r}"
        assert payload not in str(outcome.output)
        # the text around the secret is kept: that is the point of scrub over drop
        assert "the command printed" in str(outcome.output)
        assert "and then exited 0" in str(outcome.output)
        # and the sentinel agrees the result is clean
        assert safety.SafetyGate().scan(json.dumps(bodies(built(gated))).encode()) == []

    def test_every_sentinel_shape_has_a_scrub_counterpart(self) -> None:
        """The scrub is derived from the sentinel's shapes, so the two cannot drift."""
        assert [rule for rule, _ in tr.SCRUB_SHAPES] == [rule for rule, _ in safety.SHAPES]
        assert all(isinstance(p, re.Pattern) for _, p in tr.SCRUB_SHAPES)

    def test_every_case_in_this_table_is_a_shape_the_sentinel_detects(self) -> None:
        """A case the sentinel does not see would make the scrub assertion above vacuous."""
        for rule, payload in SECRET_CASES:
            found = {f.rule for f in safety.SafetyGate().scan(payload.encode())}
            assert rule in found, f"{rule}: the sentinel sees {sorted(found)}"

    def test_scrub_truncates_and_says_it_did(self) -> None:
        transcript = tr.read_claude_text(stream(tool_event("y" * 5000)), transcript_id="t")
        gated = tr.redact(transcript, policy=tr.TOOL_IO_SCRUB, max_chars=100)
        assert "more characters" in str(gated.outcomes[0].output)
        assert len(str(gated.outcomes[0].output)) < 200

    def test_an_unknown_policy_is_an_error(self) -> None:
        with pytest.raises(tr.TranscriptError, match="not one of"):
            tr.redact(read(), policy="keep")

    def test_an_ungated_transcript_cannot_be_projected(self) -> None:
        with pytest.raises(tr.TranscriptError, match="redact"):
            built(read())


class TestTheProjection:
    def test_the_kinds_and_their_parents(self) -> None:
        build = built(tr.redact(read()))
        assert len(one(build, "trace-create")) == 1
        spans = one(build, "span-create")
        generations = one(build, "generation-create")
        assert [s["name"] for s in spans] == ["transcript clean", "tool: Bash"]
        assert [g["name"] for g in generations] == ["assistant turn 0", "assistant turn 1"]
        root = spans[0]["id"]
        assert all(o["parentObservationId"] == root for o in spans[1:] + generations)
        assert all(o["traceId"] == build.trace_id for o in spans + generations)

    def test_the_native_model_rides_on_the_generations(self) -> None:
        """The UI's model breakdown reads the native field, and only a generation carries it."""
        build = built(tr.redact(read()))
        assert all(g["model"] == "claude-opus-4-8" for g in one(build, "generation-create"))
        assert all("model" not in s for s in one(build, "span-create"))

    def test_usage_is_a_non_overlapping_breakdown_with_a_total(self) -> None:
        usage = one(built(tr.redact(read())), "generation-create")[0]["usageDetails"]
        assert usage == {
            "input": 120,
            "output": 45,
            "total": 120 + 45 + 2048 + 512,
            "cache_read_input_tokens": 2048,
            "cache_creation_input_tokens": 512,
        }

    def test_the_tags_are_the_transcripts_own(self) -> None:
        tags = one(built(tr.redact(read())), "trace-create")[0]["tags"]
        assert tags == [
            "team:acme",
            "project:acme-traces",
            "harness:claude-code",
            "source:agent-transcript",
            "model:claude-opus-4-8",
            "run_kind:test",
            "ci_run:12345",
            "ci_chain:999",
        ]
        # a transcript carries no trial facts, whatever the caller thinks
        assert not {t.partition(":")[0] for t in tags} & {"dataset", "scope", "domain", "config"}

    def test_the_result_totals_are_queryable_trace_metadata(self) -> None:
        metadata = one(built(tr.redact(read())), "trace-create")[0]["metadata"]
        assert metadata["total_cost_usd"] == pytest.approx(0.0412)
        assert metadata["num_turns"] == 3
        assert metadata["duration_ms"] == 4000
        assert metadata["stop_reason"] == "end_turn"
        assert metadata["is_error"] is False
        assert metadata["permission_denials"] == 0
        assert metadata["cli_version"] == "2.1.220"
        assert metadata["tool_io"] == tr.TOOL_IO_DROP
        assert metadata["id_contract"] == engine_ids.CONTRACT_VERSION

    def test_the_producers_own_metadata_rides_along_but_cannot_overwrite_the_schema(self) -> None:
        build = built(tr.redact(read()), metadata={"stage": "resolve", "iteration": 2})
        metadata = one(build, "trace-create")[0]["metadata"]
        assert metadata["stage"] == "resolve" and metadata["iteration"] == 2
        with pytest.raises(tr.TranscriptError, match="may not override schema keys: run_id"):
            built(tr.redact(read()), metadata={"run_id": "somewhere else"})

    def test_every_observation_carries_the_traces_environment(self) -> None:
        build = built(tr.redact(read()))
        observations = one(build, "span-create") + one(build, "generation-create")
        assert {o["environment"] for o in observations} == {"production-automation"}

    def test_an_error_run_marks_its_root(self) -> None:
        events = [
            {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": []}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 40},
        ]
        transcript = tr.read_claude_text(stream(events), transcript_id="t")
        root = one(built(tr.redact(transcript)), "span-create")[0]
        assert root["level"] == "ERROR"
        assert root["statusMessage"] == "error_max_turns"

    def test_a_tool_span_is_keyed_by_the_call_id_never_by_position(self) -> None:
        """Two calls of the same tool must not collide, and a re-read must land on the same id."""
        first = tr.redact(tr.read_claude_text(stream(tool_event("a")), transcript_id="t"))
        again = tr.redact(tr.read_claude_text(stream(tool_event("b")), transcript_id="t"))
        span = one(built(first), "span-create")[1]
        assert span["id"] == one(built(again), "span-create")[1]["id"]
        assert span["metadata"]["key_source"] == "call_id"


class TestTheVocabulary:
    @pytest.mark.parametrize("missing", ["team", "run_kind"])
    def test_a_transcript_needs_its_caller_tags(self, missing: str) -> None:
        tags = {k: v for k, v in CALLER.items() if k != missing}
        with pytest.raises(tr.TranscriptError, match=f"needs {missing}"):
            built(tr.redact(read()), caller_tags=tags)

    @pytest.mark.parametrize("prefix", ["dataset", "scope", "domain", "config", "task"])
    def test_a_trial_fact_is_not_a_transcript_fact(self, prefix: str) -> None:
        with pytest.raises(tr.TranscriptError, match=f"{prefix} do(es)? not describe a transcript"):
            built(tr.redact(read()), caller_tags={**CALLER, prefix: "x"})

    def test_a_tag_value_the_vocabulary_refuses_is_an_error(self) -> None:
        with pytest.raises(tr.TranscriptError, match="the value must be"):
            built(tr.redact(read()), caller_tags={**CALLER, "team": "two words"})


class TestTheIdContract:
    def test_both_spellings_of_the_run_tag_keyword_produce_the_same_ids(self) -> None:
        """The engine's module says ``run_tag`` and the offline uploader's says ``version``.
        Until the two are folded into one, this is the seam, and it may not change an id."""

        class OtherSpelling:
            CONTRACT_VERSION = engine_ids.CONTRACT_VERSION
            observation_id = staticmethod(engine_ids.observation_id)
            tool_key = staticmethod(engine_ids.tool_key)

            @staticmethod
            def trace_id(
                *, version: str, run_id: str, task_id: str, trial_index: Any, attempt: Any
            ) -> str:
                return engine_ids.trace_id(
                    run_tag=version,
                    run_id=run_id,
                    task_id=task_id,
                    trial_index=trial_index,
                    attempt=attempt,
                )

        transcript = tr.redact(read())
        mine = tr.build_events(transcript, options(), ids=tr.id_contract(engine_ids))
        theirs = tr.build_events(transcript, options(), ids=tr.id_contract(OtherSpelling))
        assert bodies(mine) == bodies(theirs)

    def test_the_trace_id_is_the_contract_v2_name(self) -> None:
        build = built(tr.redact(read(transcript_id="resolve/2")))
        assert build.trace_id == engine_ids.trace_id(
            run_tag="v1",
            run_id="automation/integrate/local/1",
            task_id="resolve/2",
            trial_index=0,
            attempt="na",
        )

    def test_an_id_module_this_projection_cannot_read_is_an_error(self) -> None:
        class NoTraceId:
            CONTRACT_VERSION = 2

        with pytest.raises(tr.TranscriptError, match="no trace_id"):
            tr.id_contract(NoTraceId)

        class WrongKeyword:
            CONTRACT_VERSION = 2
            observation_id = staticmethod(engine_ids.observation_id)
            tool_key = staticmethod(engine_ids.tool_key)

            @staticmethod
            def trace_id(*, tag: str, run_id: str, task_id: str) -> str:  # pragma: no cover
                return ""

        with pytest.raises(tr.TranscriptError, match="run-tag keyword"):
            tr.id_contract(WrongKeyword)


class TestTheSentinel:
    def test_a_credential_the_process_holds_stops_the_send(self) -> None:
        """The last check before a send: the gate knows the values, not only the shapes."""
        value = "not-a-shape-just-a-password"
        environment = {"ACME_UPLOAD_TOKEN": value}
        events = tool_event(f"the config said {value}")
        transcript = tr.redact(
            tr.read_claude_text(stream(events), transcript_id="t"), policy=tr.TOOL_IO_SCRUB
        )
        payload = json.dumps(bodies(built(transcript))).encode()
        gate = safety.SafetyGate.from_environment(environment)
        with pytest.raises(safety.SafetyError, match="known-secret-value"):
            gate.check(payload, what="transcript t")

    def test_the_clean_transcript_passes_the_sentinel(self) -> None:
        payload = json.dumps(bodies(built(tr.redact(read())))).encode()
        assert safety.SafetyGate().scan(payload) == []


class TestTheGolden:
    def test_the_clean_transcript_projects_to_the_checked_in_events(self) -> None:
        """Byte-identical bodies. Regenerate with ``gen_claude_code_golden.py`` next to this
        file, and only when the change to the projection is intended."""
        assert bodies(built(tr.redact(read()))) == json.loads(GOLDEN.read_text())


class TestTheFileList:
    def test_it_finds_the_agent_output_under_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "agent_iter_1.jsonl").write_text(CLEAN.read_text())
        (tmp_path / "agent_finalize.json").write_text("{}")
        (tmp_path / "notes.md").write_text("not agent output")
        assert [p.name for p in tr.transcript_files(tmp_path)] == [
            "agent_finalize.json",
            "agent_iter_1.jsonl",
        ]

    def test_a_file_is_its_own_list(self) -> None:
        assert tr.transcript_files(CLEAN) == [CLEAN]
