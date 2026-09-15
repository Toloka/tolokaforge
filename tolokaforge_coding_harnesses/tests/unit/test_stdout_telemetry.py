"""What a harness CLI's own stdout totals parse to.

The ``claude-code`` fixtures below carry the key set a real
``--output-format=stream-json`` run terminates with, captured from live trial
bundles; the other harnesses are represented by the shapes they actually print,
which carry no totals at all.
"""

from __future__ import annotations

import json

import pytest
from tolokaforge_coding_harnesses.stdout_telemetry import (
    CLAUDE_CODE_STREAM_JSON,
    STDOUT_TELEMETRY_DIALECTS,
    parse_harness_stdout,
)

_RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 19,
    "duration_ms": 171012,
    "total_cost_usd": 0.6474657,
    "usage": {
        "input_tokens": 129,
        "output_tokens": 15235,
        "cache_read_input_tokens": 705404,
        "cache_creation_input_tokens": 55182,
        "service_tier": "standard",
    },
}


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


class TestTheResultEventBecomesARecord:
    def test_a_stream_json_run_reports_the_clis_own_totals(self) -> None:
        telemetry = parse_harness_stdout(
            "claude-code",
            _stream({"type": "system"}, {"type": "assistant"}, _RESULT_EVENT),
        )

        assert telemetry is not None
        assert telemetry.dialect == CLAUDE_CODE_STREAM_JSON
        assert telemetry.turns == 19
        assert telemetry.cost_usd == pytest.approx(0.6474657)
        assert telemetry.duration_s == pytest.approx(171.012)
        assert telemetry.prompt_tokens == 129
        assert telemetry.completion_tokens == 15235
        assert telemetry.cache_read_input_tokens == 705404
        assert telemetry.cache_creation_input_tokens == 55182

    def test_the_last_result_event_wins(self) -> None:
        """A resumed session prints a result per segment; the totals are the final one."""
        first = {**_RESULT_EVENT, "num_turns": 3, "total_cost_usd": 0.1}

        telemetry = parse_harness_stdout("claude-code", _stream(first, _RESULT_EVENT))

        assert telemetry is not None
        assert telemetry.turns == 19

    def test_a_single_object_run_parses(self) -> None:
        """``--output-format json`` prints the result event alone."""
        telemetry = parse_harness_stdout("claude-code", json.dumps(_RESULT_EVENT))

        assert telemetry is not None
        assert telemetry.turns == 19

    def test_a_verbose_array_run_parses(self) -> None:
        """``--output-format json --verbose`` prints every message in one array."""
        telemetry = parse_harness_stdout(
            "claude-code", json.dumps([{"type": "assistant"}, _RESULT_EVENT])
        )

        assert telemetry is not None
        assert telemetry.turns == 19

    def test_an_interleaved_unparseable_line_does_not_lose_the_totals(self) -> None:
        stdout = _stream({"type": "assistant"}) + "{not json\n" + _stream(_RESULT_EVENT)

        telemetry = parse_harness_stdout("claude-code", stdout)

        assert telemetry is not None
        assert telemetry.turns == 19


class TestAbsentTelemetryStaysAbsent:
    """``None`` is the only way to say "the CLI did not report its own totals"."""

    def test_a_harness_with_no_dialect_reports_nothing(self) -> None:
        assert parse_harness_stdout("codex", _stream(_RESULT_EVENT)) is None

    def test_an_unknown_harness_reports_nothing(self) -> None:
        assert parse_harness_stdout("not-a-harness", _stream(_RESULT_EVENT)) is None

    def test_the_engine_loop_reports_nothing(self) -> None:
        assert parse_harness_stdout("engine-loop", _stream(_RESULT_EVENT)) is None

    @pytest.mark.parametrize("stdout", ["", "   ", "\n\n"])
    def test_an_empty_stream_reports_nothing(self, stdout: str) -> None:
        assert parse_harness_stdout("claude-code", stdout) is None

    def test_a_stream_that_never_finished_reports_nothing(self) -> None:
        """A CLI killed at its deadline emits no result event."""
        assert parse_harness_stdout("claude-code", _stream({"type": "assistant"})) is None

    def test_a_truncated_result_line_reports_nothing(self) -> None:
        assert parse_harness_stdout("claude-code", '{"type":"result","num_turns":3,') is None

    def test_prose_output_reports_nothing(self) -> None:
        """What ``codex`` and ``kimi-code`` actually print — a summary, no JSON."""
        assert parse_harness_stdout("claude-code", "Implemented the ingest path.\n") is None

    def test_an_event_stream_without_totals_reports_nothing(self) -> None:
        """What ``grok-build`` and ``opencode`` print — events, no usage block."""
        stdout = _stream(
            {"type": "text", "data": "done"},
            {"type": "end", "stopReason": "EndTurn", "sessionId": "s1"},
        )

        assert parse_harness_stdout("claude-code", stdout) is None


class TestAMalformedResultEventDegradesField:
    """A result event is the CLI's own output, so one bad field must not
    discard the rest of a run's accounting."""

    def test_a_missing_usage_block_keeps_turns_and_cost(self) -> None:
        event = {k: v for k, v in _RESULT_EVENT.items() if k != "usage"}

        telemetry = parse_harness_stdout("claude-code", _stream(event))

        assert telemetry is not None
        assert telemetry.turns == 19
        assert telemetry.cost_usd == pytest.approx(0.6474657)
        assert telemetry.prompt_tokens == 0

    def test_an_omitted_cost_is_none_not_zero(self) -> None:
        event = {k: v for k, v in _RESULT_EVENT.items() if k != "total_cost_usd"}

        telemetry = parse_harness_stdout("claude-code", _stream(event))

        assert telemetry is not None
        assert telemetry.cost_usd is None

    def test_a_non_numeric_count_reads_as_zero(self) -> None:
        telemetry = parse_harness_stdout(
            "claude-code", _stream({**_RESULT_EVENT, "num_turns": "many"})
        )

        assert telemetry is not None
        assert telemetry.turns == 0

    def test_a_non_numeric_cost_reads_as_none(self) -> None:
        telemetry = parse_harness_stdout(
            "claude-code", _stream({**_RESULT_EVENT, "total_cost_usd": "free"})
        )

        assert telemetry is not None
        assert telemetry.cost_usd is None

    def test_a_boolean_count_is_not_coerced_to_one(self) -> None:
        telemetry = parse_harness_stdout(
            "claude-code", _stream({**_RESULT_EVENT, "num_turns": True})
        )

        assert telemetry is not None
        assert telemetry.turns == 0


class TestTheDialectTableIsTheWholeSurface:
    def test_only_claude_code_declares_a_dialect(self) -> None:
        """Every other shipped harness prints prose or a usage-free event
        stream; adding one here means adding a parser branch with it."""
        assert set(STDOUT_TELEMETRY_DIALECTS) == {"claude-code"}
