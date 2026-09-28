"""What a harness CLI's own stdout totals parse to.

Each fixture reproduces the key set and value shape of a stream a live trial
produced — the ``claude-code`` terminating ``result`` event, ``codex``'s
per-turn ``turn.completed`` usage, and ``kimi-code``'s usage-free message
transcript. They are hand-written rather than captured verbatim, because a
real stream is hundreds of kilobytes of transcript around a few token fields;
the parsers were separately run against the full captures end to end, and the
commit that added each dialect records which trial it was checked against.

The three dialects agree on nothing but being JSON lines, and on what they
*omit*: codex reports no cost, kimi-code no tokens. That asymmetry is the
whole reason the dialect table and the ``None``-versus-zero distinction exist,
so most of what follows is about absence rather than arithmetic.
"""

from __future__ import annotations

import json

import pytest
from tolokaforge_coding_harnesses.stdout_telemetry import (
    CLAUDE_CODE_STREAM_JSON,
    CODEX_JSON,
    KIMI_CODE_STREAM_JSON,
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


_CODEX_TURN_COMPLETED: dict[str, object] = {
    "type": "turn.completed",
    "usage": {
        "input_tokens": 63345,
        "cached_input_tokens": 53760,
        "cache_write_input_tokens": 0,
        "output_tokens": 396,
        "reasoning_output_tokens": 0,
    },
}

_CODEX_STREAM: tuple[dict[str, object], ...] = (
    {"type": "thread.started"},
    {"type": "turn.started"},
    {"type": "item.started"},
    {"type": "item.completed", "item": {"item_type": "command_execution"}},
    {"type": "item.completed", "item": {"item_type": "agent_message"}},
    _CODEX_TURN_COMPLETED,
)

_KIMI_STREAM: tuple[dict[str, object], ...] = (
    {"role": "assistant", "content": "Reading the module.", "tool_calls": [{"id": "Read_0"}]},
    {"role": "tool", "tool_call_id": "Read_0", "content": "def factorial(n): ..."},
    {"role": "assistant", "content": "Fixed the recursion."},
    {
        "role": "meta",
        "type": "session.resume_hint",
        "session_id": "session_89c15dd0",
        "command": "kimi -r session_89c15dd0",
    },
)


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
        assert telemetry.prompt_tokens == 129 + 705404 + 55182
        assert telemetry.completion_tokens == 15235
        assert telemetry.cache_read_input_tokens == 705404
        assert telemetry.cache_creation_input_tokens == 55182

    def test_the_prompt_total_includes_the_cache_counters(self) -> None:
        """Anthropic's ``input_tokens`` is the non-cached remainder — 129 of a
        760k prompt here — so a consumer deriving fresh input as
        ``prompt - cache_read - cache_write`` would go negative on the raw
        figure. The record carries the inclusive total instead."""
        telemetry = parse_harness_stdout("claude-code", _stream(_RESULT_EVENT))

        assert telemetry is not None
        assert telemetry.prompt_tokens is not None
        fresh = (
            telemetry.prompt_tokens
            - (telemetry.cache_read_input_tokens or 0)
            - (telemetry.cache_creation_input_tokens or 0)
        )
        assert fresh == 129

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

    @pytest.mark.parametrize("harness", ["grok-build", "opencode", "gemini-cli"])
    def test_a_harness_with_no_dialect_reports_nothing(self, harness: str) -> None:
        assert parse_harness_stdout(harness, _stream(_RESULT_EVENT)) is None

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
        """What ``codex`` prints when its ``--json`` flag is absent — a plain
        final message. The parser must not mistake that for a finished run."""
        assert parse_harness_stdout("claude-code", "Implemented the ingest path.\n") is None

    def test_an_event_stream_without_totals_reports_nothing(self) -> None:
        """What ``grok-build`` prints — events whose terminal one has no usage."""
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


class TestCodexSumsItsPerTurnUsage:
    """``codex exec --json`` reports usage on each ``turn.completed``, so the
    totals are a sum across turns rather than a single terminal event. Fixture
    values are from a live trial."""

    def test_a_single_turn_run_reports_its_usage(self) -> None:
        telemetry = parse_harness_stdout("codex", _stream(*_CODEX_STREAM))

        assert telemetry is not None
        assert telemetry.dialect == CODEX_JSON
        assert telemetry.turns == 1
        assert telemetry.prompt_tokens == 63345
        assert telemetry.completion_tokens == 396
        assert telemetry.cache_read_input_tokens == 53760
        assert telemetry.cache_creation_input_tokens == 0
        assert telemetry.reasoning_tokens == 0

    def test_usage_sums_across_turns(self) -> None:
        telemetry = parse_harness_stdout("codex", _stream(*_CODEX_STREAM, _CODEX_TURN_COMPLETED))

        assert telemetry is not None
        assert telemetry.turns == 2
        assert telemetry.prompt_tokens == 63345 * 2
        assert telemetry.completion_tokens == 396 * 2

    def test_codex_reports_no_cost_so_the_caller_prices_the_tokens(self) -> None:
        telemetry = parse_harness_stdout("codex", _stream(*_CODEX_STREAM))

        assert telemetry is not None
        assert telemetry.cost_usd is None
        assert telemetry.has_token_counts is True

    def test_a_turn_that_reported_no_usage_still_counts_as_a_turn(self) -> None:
        telemetry = parse_harness_stdout("codex", _stream({"type": "turn.completed"}))

        assert telemetry is not None
        assert telemetry.turns == 1
        assert telemetry.prompt_tokens == 0

    def test_a_run_that_never_completed_a_turn_reports_nothing(self) -> None:
        telemetry = parse_harness_stdout(
            "codex", _stream({"type": "thread.started"}, {"type": "turn.started"})
        )

        assert telemetry is None


class TestKimiCodeReportsTurnsAndNoUsage:
    """``kimi-code --output-format stream-json`` prints a message transcript
    with no token or cost fields anywhere. Turns are countable; usage is not,
    and must read as absent rather than zero."""

    def test_assistant_messages_are_the_turn_count(self) -> None:
        telemetry = parse_harness_stdout("kimi-code", _stream(*_KIMI_STREAM))

        assert telemetry is not None
        assert telemetry.dialect == KIMI_CODE_STREAM_JSON
        assert telemetry.turns == 2

    def test_every_token_field_is_none_not_zero(self) -> None:
        """A zero here would read as a trial that spent nothing, which is a
        different and false claim."""
        telemetry = parse_harness_stdout("kimi-code", _stream(*_KIMI_STREAM))

        assert telemetry is not None
        assert telemetry.has_token_counts is False
        assert telemetry.prompt_tokens is None
        assert telemetry.completion_tokens is None
        assert telemetry.cache_read_input_tokens is None
        assert telemetry.cache_creation_input_tokens is None
        assert telemetry.reasoning_tokens is None
        assert telemetry.cost_usd is None

    def test_tool_and_meta_lines_are_not_turns(self) -> None:
        telemetry = parse_harness_stdout(
            "kimi-code",
            _stream(
                {"role": "tool", "tool_call_id": "t1", "content": "out"},
                {"role": "meta", "type": "session.resume_hint", "session_id": "s1"},
            ),
        )

        assert telemetry is None


class TestTheDialectTableIsTheWholeSurface:
    def test_the_table_names_exactly_the_cls_that_report_something(self) -> None:
        """`grok-build` is absent deliberately: its stream is `text` events
        closing on an `end` event that carries only a stop reason, so there is
        nothing for a parser to read. Adding a name here means adding a parser
        branch with it."""
        assert set(STDOUT_TELEMETRY_DIALECTS) == {
            "claude-code",
            "codex",
            "kimi-code",
            "opencode",
        }

    def test_every_declared_dialect_has_a_parser(self) -> None:
        for harness in STDOUT_TELEMETRY_DIALECTS:
            # An unparseable stream is fine; an AssertionError is not.
            parse_harness_stdout(harness, '{"type":"nothing"}\n')


class TestOpencodeJson:
    """Recorded from a live `opencode run --format=json` trial: 22 steps, each
    `step_finish` carrying that step's tokens and cost."""

    STREAM = (
        '{"type":"step_start","part":{}}\n'
        '{"type":"tool_use","part":{}}\n'
        '{"type":"step_finish","part":{"tokens":{"total":16315,"input":1,'
        '"output":304,"reasoning":0,"cache":{"write":387,"read":15623}},'
        '"cost":0.01070115}}\n'
        '{"type":"step_finish","part":{"tokens":{"total":200,"input":10,'
        '"output":40,"reasoning":5,"cache":{"write":50,"read":100}},'
        '"cost":0.002}}\n'
    )

    def test_usage_sums_across_steps(self) -> None:
        t = parse_harness_stdout("opencode", self.STREAM)
        assert t is not None
        assert t.dialect == "opencode/json"
        assert t.turns == 2
        assert t.completion_tokens == 344
        assert t.cache_read_input_tokens == 15723
        assert t.cache_creation_input_tokens == 437
        assert t.reasoning_tokens == 5

    def test_the_prompt_basis_folds_the_cache_counters_back_in(self) -> None:
        """`tokens.input` is the non-cached remainder — the step above reports
        `input=1` against `total=16315`. The record declares an inclusive
        prompt, so a reader that passed `input` through would price a
        cache-heavy trial as though it had barely any prompt at all."""
        t = parse_harness_stdout("opencode", self.STREAM)
        assert t is not None
        assert t.prompt_tokens == 1 + 15623 + 387 + 10 + 100 + 50

    def test_cost_sums_across_steps(self) -> None:
        t = parse_harness_stdout("opencode", self.STREAM)
        assert t is not None
        assert t.cost_usd == pytest.approx(0.01270115)

    def test_a_stream_with_no_step_finish_reports_nothing(self) -> None:
        assert parse_harness_stdout("opencode", '{"type":"step_start"}\n') is None


class TestOpencodeStepsThatReportPartially:
    """A step that omits a field must cost the trial nothing.

    `parse_harness_stdout` is called unguarded on the raw stream
    (`tolokaforge/core/runner.py`), after the CLI has already done and paid for
    its work — so a parser that raises turns an unmetered trial into a lost
    one."""

    def test_a_step_without_a_cost_does_not_raise(self) -> None:
        stream = json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "total": 10,
                        "input": 1,
                        "output": 4,
                        "cache": {"write": 2, "read": 3},
                    }
                },
            }
        )

        telemetry = parse_harness_stdout("opencode", stream)

        assert telemetry is not None
        assert telemetry.turns == 1
        assert telemetry.completion_tokens == 4

    def test_a_stream_where_no_step_reports_cost_reports_no_cost(self) -> None:
        """`0.0` would claim a trial that ran spent nothing — the exact reading
        this dialect exists to remove."""
        stream = json.dumps({"type": "step_finish", "part": {"tokens": {"total": 10, "input": 10}}})

        telemetry = parse_harness_stdout("opencode", stream)

        assert telemetry is not None
        assert telemetry.cost_usd is None

    def test_cost_sums_only_the_steps_that_reported_one(self) -> None:
        stream = (
            json.dumps(
                {"type": "step_finish", "part": {"cost": 0.02, "tokens": {"total": 5, "input": 5}}}
            )
            + "\n"
            + json.dumps({"type": "step_finish", "part": {"tokens": {"total": 5, "input": 5}}})
            + "\n"
        )

        telemetry = parse_harness_stdout("opencode", stream)

        assert telemetry is not None
        assert telemetry.cost_usd == pytest.approx(0.02)


class TestOpencodePromptBasisFollowsTheProvider:
    """Whether `tokens.input` includes the cached prompt is a property of the
    provider opencode routed to, not of opencode. Reading either shape as the
    other doubles or halves a cache-heavy trial's prompt, and the docs support
    routing this harness at non-Anthropic vendors via an operator overlay."""

    def test_an_anthropic_shaped_step_folds_the_cache_counters_in(self) -> None:
        """Recorded live: input is the non-cached remainder, and total is the
        sum of all four."""
        step = json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "total": 16315,
                        "input": 1,
                        "output": 304,
                        "cache": {"write": 387, "read": 15623},
                    },
                    "cost": 0.0107,
                },
            }
        )

        telemetry = parse_harness_stdout("opencode", step)

        assert telemetry is not None
        assert telemetry.prompt_tokens == 1 + 387 + 15623

    def test_an_openai_shaped_step_is_left_alone(self) -> None:
        """`input` already includes the cached part, so `input + output` is the
        total. Folding again would bill the cached prompt twice."""
        step = json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "total": 1100,
                        "input": 1000,
                        "output": 100,
                        "cache": {"write": 0, "read": 900},
                    },
                    "cost": 0.01,
                },
            }
        )

        telemetry = parse_harness_stdout("opencode", step)

        assert telemetry is not None
        assert telemetry.prompt_tokens == 1000
        assert telemetry.cache_read_input_tokens == 900


class TestOpencodeStepsWithNoTokensBlock:
    def test_steps_without_tokens_report_no_counts(self) -> None:
        """Zeros here would make `has_token_counts` true, price the trial at
        $0.00 and suppress the wire fallback that could still measure it —
        "not measured" rendered as "measured as zero", on the one path that
        had no guard."""
        stream = json.dumps({"type": "step_finish", "part": {"cost": 0.01}})

        telemetry = parse_harness_stdout("opencode", stream)

        assert telemetry is not None
        assert telemetry.turns == 1
        assert telemetry.has_token_counts is False
        assert telemetry.prompt_tokens is None
        assert telemetry.cost_usd == pytest.approx(0.01)

    def test_a_single_step_with_tokens_is_enough(self) -> None:
        stream = (
            json.dumps({"type": "step_finish", "part": {"cost": 0.01}})
            + "\n"
            + json.dumps(
                {"type": "step_finish", "part": {"tokens": {"total": 5, "input": 5}, "cost": 0.02}}
            )
            + "\n"
        )

        telemetry = parse_harness_stdout("opencode", stream)

        assert telemetry is not None
        assert telemetry.has_token_counts is True
        assert telemetry.prompt_tokens == 5
