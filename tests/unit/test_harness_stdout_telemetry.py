"""What a harness trial's ``Metrics`` carries once the harness's own totals are read.

The engine issues no LLM request when a CLI drives the trial, so without this
every harness trial reports the artefacts of running one tool call: a single
turn, an empty usage block and no cost. These tests drive the real
:class:`TrialRunner` over a scripted tool result so the assertions are about
the recorded trial, not about the parser (which
``tolokaforge_coding_harnesses/tests/unit/test_stdout_telemetry.py`` covers).

Two taps can supply the numbers. A CLI that prints its own totals is read off
stdout; a CLI that prints none — ``kimi-code`` prints no usage at all — has
them recovered from the NDJSON records its request middleware wrote on the
wire. The last class below covers that second tap and the precedence between
them.

Cost is the part the CLIs disagree about, so it is asserted against arithmetic
spelled out from the shipped pricing table rather than against a recorded
constant: the point of pricing the harness's tokens ourselves is that the
figure is reproducible from the table, whichever tap produced the tokens.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.pricing import get_pricing_info
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult
from tolokaforge_coding_harnesses import MIDDLEWARE_PROXY_USAGE_SOURCE

_RESULT_EVENT: dict[str, Any] = {
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
    },
}

_STREAM_JSON = "\n".join(
    json.dumps(event) for event in ({"type": "system"}, {"type": "assistant"}, _RESULT_EVENT)
)


_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
"""Priced by the bundled table at $3/$15 per 1M input/output, with per-cache
rates ($0.3 read, $3.75 write) — so the claude-code fixture below is priced
the way the vendor priced it."""

_FLAT_MODEL = "openrouter/anthropic/claude-sonnet-4-6"
"""The same model under the table's dash-spelled key, which carries no
per-cache rates: every prompt token is billed at the input rate. Cost is then
linear in the prompt total handed to the pricing call, which is what makes a
double-counted cache figure visible rather than a rounding difference."""

_UNPRICED_MODEL = "openrouter/acme/not-in-the-pricing-table"
_CODEX_MODEL = "openrouter/openai/gpt-5-codex"


def _expected_cost(
    model: str,
    *,
    prompt_total: int,
    completion: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """The table's price for these tokens, re-derived independently here.

    Spelled out rather than taken from :func:`estimate_cost` so the assertions
    fail when the wrong count reaches the wrong rate slot, and read off the
    shipped table rather than hard-coded so a pricing refresh does not turn
    these into false alarms. ``prompt_total`` is the inclusive prompt figure,
    so the fresh remainder is derived here the way the pricing code derives
    it — a caller that passed the non-cached remainder as the total would land
    a negative fresh count and fail the guard below.
    """
    rates = get_pricing_info(model)
    assert rates is not None, f"{model} has no pricing row; the fixture needs updating"
    fresh = prompt_total - cache_read - cache_write
    assert fresh >= 0, "prompt_total must include the cache counters"
    return (
        fresh * rates["input"]
        + cache_read * rates.get("cache_read", rates["input"])
        + cache_write * rates.get("cache_write", rates["input"])
        + completion * rates["output"]
    ) / 1_000_000


class _StubAgentClient:
    """Stands in for the agent :class:`LLMClient` on the harness path.

    Only the model identity is read there — the CLI ran the model itself, so
    the client issues nothing — and it is the same ``model_name`` the engine's
    own cost ladder prices its calls with.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


_USAGE_LOG_CONTAINER_PATH = "/logs/agent/tolokaforge_usage.ndjson"
"""Where the middleware proxy writes, inside the trial container.

The engine is told this path by the adapter and reads the file back out of the
still-running container: the runtime mounts that directory from a per-trial
compose-context copy it deletes at teardown, so no host path names the file.
"""


class _ScriptedToolExecutor:
    """Stands in for the container's ``docker compose exec`` bash tool.

    Answers two distinct executions, as the real one does on a harness trial
    whose CLI routes through a request middleware: the CLI's own invocation,
    which gets *output*, and the engine's read of the proxy's usage records,
    which gets *usage_records*. Every command is remembered so a test can
    assert which of the two actually ran.

    ``usage_records`` of ``None`` is a container where nothing wrote the file:
    the read fails the way ``cat`` does. ``raises`` is the executor itself
    failing — a container already gone, a transport error — which must cost
    the trial nothing either.
    """

    def __init__(
        self,
        output: str,
        *,
        success: bool = True,
        usage_records: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._output = output
        self._success = success
        self._usage_records = usage_records
        self._raises = raises
        self.commands: list[str] = []

    def execute(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        command = arguments["command"]
        self.commands.append(command)
        if not command.startswith("cat "):
            return ToolResult(success=self._success, output=self._output)
        if self._raises is not None:
            raise self._raises
        if self._usage_records is None:
            return ToolResult(
                success=False,
                output="",
                error=f"cat: {_USAGE_LOG_CONTAINER_PATH}: No such file or directory",
            )
        return ToolResult(success=True, output=self._usage_records)

    @property
    def usage_read_commands(self) -> list[str]:
        """The engine's own reads, separated from the agent's single call."""
        return [command for command in self.commands if command.startswith("cat ")]


def _trial(
    stdout: str,
    *,
    harness: str = "claude-code",
    success: bool = True,
    model: str = _MODEL,
    usage_log_container_path: str | None = None,
    usage_records: str | None = None,
    raises: Exception | None = None,
) -> tuple[Trajectory, _ScriptedToolExecutor]:
    """Drive a harness trial, handing back the executor it ran against.

    The executor is what says whether the engine's usage read fired at all,
    which no field on the trial can answer — an absent read and a read that
    found nothing leave identical metrics.
    """
    executor = _ScriptedToolExecutor(
        stdout, success=success, usage_records=usage_records, raises=raises
    )
    runner = TrialRunner(
        task_id="telemetry",
        trial_index=0,
        agent_client=_StubAgentClient(model),  # type: ignore[arg-type]
        user_simulator=None,
        tool_executor=executor,
        tool_schemas=[],
        episode_timeout_s=600,
    )
    trajectory = runner.run_harness(
        tool_name="bash",
        command="claude --print",
        instruction="Fix the failing tests.",
        timeout_s=600,
        harness=harness,
        usage_log_container_path=usage_log_container_path,
    )
    return trajectory, executor


def _run(stdout: str, **kwargs: Any) -> Trajectory:
    return _trial(stdout, **kwargs)[0]


class TestTheTrialReportsWhatTheCliReported:
    def test_turns_are_the_clis_count_not_the_single_tool_call(self) -> None:
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.turns == 19

    def test_usage_comes_from_the_result_event(self) -> None:
        """``prompt_tokens`` is the prompt total — the CLI's 129 non-cached
        input tokens plus both cache counters — so it means the same thing as
        an engine-loop trial's."""
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.usage.prompt_tokens == 129 + 705404 + 55182
        assert metrics.usage.completion_tokens == 15235
        assert metrics.usage.cache_read_input_tokens == 705404
        assert metrics.usage.cache_creation_input_tokens == 55182

    def test_the_dialect_is_stamped_so_the_numbers_are_attributable(self) -> None:
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.harness_stdout_dialect == "claude-code/stream-json"

    def test_api_calls_stay_zero_because_the_engine_made_none(self) -> None:
        """The CLI's turns are not the engine's API calls, and conflating them
        would claim the engine issued requests it never made."""
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.api_calls == 0

    def test_tool_calls_stay_the_engines_own_count(self) -> None:
        """One ``docker exec`` is what the engine executed; the CLI's internal
        tool use is a different quantity this record does not carry."""
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.tool_calls == 1

    def test_the_trial_still_completes_normally(self) -> None:
        trajectory = _run(_STREAM_JSON)

        assert trajectory.status is TrialStatus.COMPLETED

    def test_a_failed_cli_still_reports_the_totals_it_printed(self) -> None:
        """A CLI that exits non-zero after doing real work still billed for it.

        A failed call records its error text rather than its stdout, so the
        totals are read off the raw stream — otherwise the spend would vanish
        from the trial exactly when something went wrong.
        """
        trajectory = _run(_STREAM_JSON, success=False)

        assert trajectory.status is TrialStatus.ERROR
        assert trajectory.metrics.turns == 19
        assert trajectory.metrics.harness_reported_cost_usd == pytest.approx(0.6474657)
        assert trajectory.metrics.cost_usd is not None

    def test_a_cli_that_printed_nothing_before_failing_reports_nothing(self) -> None:
        metrics = _run("", success=False).metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.cost_usd is None
        assert metrics.harness_reported_cost_usd is None


class TestEachCliContributesOnlyWhatItReported:
    """The CLIs report different subsets, and an unreported field must stay
    unreported rather than becoming a measured zero."""

    def test_codex_contributes_turns_and_tokens_but_no_cost(self) -> None:
        stdout = "\n".join(
            json.dumps(e)
            for e in (
                {"type": "turn.started"},
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 63345,
                        "cached_input_tokens": 53760,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 396,
                        "reasoning_output_tokens": 0,
                    },
                },
            )
        )

        metrics = _run(stdout, harness="codex", model=_CODEX_MODEL).metrics

        assert metrics.harness_stdout_dialect == "codex/json"
        assert metrics.turns == 1
        assert metrics.usage.prompt_tokens == 63345
        assert metrics.usage.completion_tokens == 396
        assert metrics.usage.cache_read_input_tokens == 53760
        assert metrics.harness_reported_cost_usd is None

    def test_kimi_contributes_turns_and_leaves_usage_untouched(self) -> None:
        """Kimi prints no token counts, so its usage block must stay empty —
        the tokens have to come from the wire, not be invented here."""
        stdout = "\n".join(
            json.dumps(e)
            for e in (
                {"role": "assistant", "content": "a", "tool_calls": [{"id": "x"}]},
                {"role": "tool", "tool_call_id": "x", "content": "out"},
                {"role": "assistant", "content": "b"},
                {"role": "meta", "type": "session.resume_hint", "session_id": "s"},
            )
        )

        metrics = _run(stdout, harness="kimi-code").metrics

        assert metrics.harness_stdout_dialect == "kimi-code/stream-json"
        assert metrics.turns == 2
        assert metrics.usage.prompt_tokens == 0
        assert metrics.usage.completion_tokens == 0
        assert metrics.harness_reported_cost_usd is None


class TestTheCostIsOursAndTheClisIsTheCrossCheck:
    """Every arm of a cross-mode comparison is priced by one authority.

    The CLIs report different subsets of cost — ``claude-code`` bills itself,
    ``codex`` reports tokens only, ``kimi-code`` reports neither — so taking
    each vendor's own figure would compare one vendor's billing against
    another's inside a single comparison. ``cost_usd`` is therefore the
    engine's price for the tokens the CLI reported, and the CLI's own figure
    is preserved beside it.
    """

    def test_codex_tokens_are_priced_even_though_codex_reports_no_cost(self) -> None:
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 63345,
                    "cached_input_tokens": 53760,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 396,
                    "reasoning_output_tokens": 0,
                },
            }
        )

        metrics = _run(stdout, harness="codex", model=_CODEX_MODEL).metrics

        assert metrics.cost_usd == pytest.approx(
            _expected_cost(_CODEX_MODEL, prompt_total=63345, completion=396, cache_read=53760)
        )
        assert metrics.harness_reported_cost_usd is None

    def test_claude_codes_own_cost_is_the_cross_check_not_the_reported_cost(self) -> None:
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.harness_reported_cost_usd == pytest.approx(0.6474657)
        assert metrics.cost_usd == pytest.approx(
            _expected_cost(
                _MODEL,
                prompt_total=129 + 705404 + 55182,
                completion=15235,
                cache_read=705404,
                cache_write=55182,
            )
        )

    def test_the_prompt_total_reaches_the_pricing_call_exactly_once(self) -> None:
        """The cache counters are inside the prompt total, so a caller that
        adds them to it again bills them twice — and one that passes the
        non-cached remainder as the total bills the cached prompt as fresh.

        The fixture makes either mistake loud: 100k fresh tokens at the input
        rate against 50k cache reads at a tenth of it means the three possible
        figures are far apart, not a rounding difference.
        """
        stdout = json.dumps(
            {
                "type": "result",
                "num_turns": 4,
                "usage": {
                    "input_tokens": 100_000,
                    "output_tokens": 10_000,
                    "cache_read_input_tokens": 50_000,
                    "cache_creation_input_tokens": 0,
                },
            }
        )

        metrics = _run(stdout).metrics

        assert metrics.usage.prompt_tokens == 150_000
        assert metrics.cost_usd == pytest.approx(
            _expected_cost(_MODEL, prompt_total=150_000, completion=10_000, cache_read=50_000)
        )

    def test_an_unpriceable_model_keeps_the_clis_own_figure(self) -> None:
        """A model absent from the pricing table must not zero the trial's
        cost: the CLI's figure is the only one left, so it stands."""
        metrics = _run(_STREAM_JSON, model=_UNPRICED_MODEL).metrics

        assert metrics.cost_usd == pytest.approx(0.6474657)
        assert metrics.harness_reported_cost_usd == pytest.approx(0.6474657)

    def test_an_unpriceable_model_whose_cli_reported_no_cost_reports_none(self) -> None:
        """Neither figure exists, and an invented zero would read as a trial
        that spent nothing."""
        stdout = json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 20}}
        )

        metrics = _run(stdout, harness="codex", model=_UNPRICED_MODEL).metrics

        assert metrics.usage.prompt_tokens == 500
        assert metrics.cost_usd is None
        assert metrics.harness_reported_cost_usd is None

    def test_a_cli_that_reported_no_tokens_leaves_cost_alone(self) -> None:
        """``kimi-code`` prints turns and nothing else, so there is nothing to
        price — and nothing the engine measured either."""
        stdout = "\n".join(
            json.dumps(e)
            for e in (
                {"role": "assistant", "content": "a"},
                {"role": "meta", "type": "session.resume_hint", "session_id": "s"},
            )
        )

        metrics = _run(stdout, harness="kimi-code").metrics

        assert metrics.turns == 1
        assert metrics.cost_usd is None
        assert metrics.harness_reported_cost_usd is None


class TestAnUnreportedTrialKeepsTheEnginesOwnAccounting:
    """Absent telemetry must read as "not measured", never as measured zero."""

    def test_a_harness_without_a_dialect_keeps_the_single_tool_call_shape(self) -> None:
        metrics = _run("Implemented the ingest path.", harness="grok-build").metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.turns == 1
        assert metrics.cost_usd is None
        assert metrics.harness_reported_cost_usd is None
        assert metrics.usage.prompt_tokens == 0

    def test_an_unnamed_harness_keeps_the_single_tool_call_shape(self) -> None:
        metrics = _run(_STREAM_JSON, harness="").metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.turns == 1

    def test_a_cli_killed_before_its_totals_keeps_the_single_tool_call_shape(self) -> None:
        metrics = _run('{"type":"assistant"}\n').metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.turns == 1
        assert metrics.cost_usd is None


_KIMI_STDOUT = "\n".join(
    json.dumps(event)
    for event in (
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "content": "out"},
        {"role": "assistant", "content": "b"},
        {"role": "meta", "type": "session.resume_hint", "session_id": "s"},
    )
)
"""``kimi-code``'s transcript: two assistant turns, no token counts anywhere."""

_KIMI_MODEL = "openrouter/moonshotai/kimi-k2"


def _usage_record(
    *,
    prompt: int | None,
    completion: int | None,
    cache_read: int | None = None,
    reasoning: int | None = None,
    status: int = 200,
) -> str:
    """One NDJSON line in the shape the middleware proxy appends.

    Keys and their ``None``-for-not-reported convention come from the proxy's
    ``_append_usage_record`` / ``_token_counts``, so a record shape drifting on
    the writing side shows up here rather than silently reading as zero.
    """
    return json.dumps(
        {
            "timestamp": "2026-09-15T10:00:00+00:00",
            "path": "/v1/chat/completions",
            "status": status,
            "model": "kimi-k2",
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": (prompt or 0) + (completion or 0),
            "cache_read_input_tokens": cache_read,
            "reasoning_tokens": reasoning,
        }
    )


def _wire_trial(*records: str, model: str = _KIMI_MODEL) -> Trajectory:
    """A ``kimi-code`` trial whose container holds *records* at the usage log.

    ``kimi-code`` is the harness this path exists for: it declares request
    middleware and prints no token counts, so the proxy's records are the only
    place its spend is written down.
    """
    return _run(
        _KIMI_STDOUT,
        harness="kimi-code",
        model=model,
        usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
        usage_records="".join(f"{record}\n" for record in records),
    )


class TestTheRecordsAreReadOutOfTheLiveContainer:
    """The records exist nowhere else.

    The runtime bind-mounts the agent service's log directory from the
    per-trial compose context — a temp copy teardown deletes — so a host path
    would name a file that is gone by the time anything opens it. The engine
    reads the file through the same exec tool the CLI ran under, while the
    container is still up, and that read is its own instrumentation rather
    than something the agent did.
    """

    def test_the_read_names_the_container_path_the_adapter_published(self) -> None:
        _trajectory, executor = _trial(
            _KIMI_STDOUT,
            harness="kimi-code",
            model=_KIMI_MODEL,
            usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
            usage_records=_usage_record(prompt=10, completion=1) + "\n",
        )

        assert executor.usage_read_commands == [f"cat -- {_USAGE_LOG_CONTAINER_PATH}"]

    def test_a_harness_with_no_middleware_is_never_read_from(self) -> None:
        """``claude-code`` boots no proxy, so its adapter publishes no path and
        the engine must not go poking in the container for one."""
        _trajectory, executor = _trial(_STREAM_JSON)

        assert executor.usage_read_commands == []
        assert executor.commands == ["claude --print"]

    def test_the_read_is_not_the_agents_tool_use(self) -> None:
        """One ``docker exec`` is what the engine ran on the agent's behalf.
        The usage read is engine instrumentation and must not inflate the
        trial's own account of what the agent did."""
        trajectory = _wire_trial(_usage_record(prompt=10, completion=1))

        assert trajectory.metrics.tool_calls == 1
        assert len(trajectory.tool_log) == 1
        assert trajectory.tool_log[0].call_id == "harness:telemetry:0"

    def test_the_read_adds_no_turn_to_the_transcript(self) -> None:
        trajectory = _wire_trial(_usage_record(prompt=10, completion=1))

        assert [message.role.value for message in trajectory.messages] == ["user", "assistant"]


class TestTokensACliNeverPrintedComeFromTheWire:
    """``kimi-code`` prints no usage, so its request middleware is the only
    place the counts exist. The proxy writes one record per provider response;
    the trial reports their sum, priced by the same table as every other arm.
    """

    def test_the_records_are_summed_into_one_per_trial_total(self) -> None:
        metrics = _wire_trial(
            _usage_record(prompt=12_000, completion=400, cache_read=8_000, reasoning=120),
            _usage_record(prompt=13_500, completion=600, cache_read=9_000, reasoning=80),
        ).metrics

        assert metrics.usage.prompt_tokens == 25_500
        assert metrics.usage.completion_tokens == 1_000
        assert metrics.usage.cache_read_input_tokens == 17_000
        assert metrics.usage.reasoning_tokens == 200

    def test_the_summed_tokens_are_priced_by_our_own_table(self) -> None:
        """Same pricing authority as the stdout path, so a proxied arm and a
        self-reporting arm of one comparison stay comparable."""
        metrics = _wire_trial(
            _usage_record(prompt=12_000, completion=400, cache_read=8_000),
            _usage_record(prompt=13_500, completion=600, cache_read=9_000),
        ).metrics

        assert metrics.cost_usd == pytest.approx(
            _expected_cost(_KIMI_MODEL, prompt_total=25_500, completion=1_000, cache_read=17_000)
        )

    def test_the_turn_count_still_comes_from_the_transcript(self) -> None:
        """A provider request is not a turn. The CLI printed two assistant
        turns against three requests, and both numbers stand as themselves."""
        metrics = _wire_trial(
            _usage_record(prompt=10, completion=1),
            _usage_record(prompt=10, completion=1),
            _usage_record(prompt=10, completion=1),
        ).metrics

        assert metrics.turns == 2
        assert metrics.harness_stdout_dialect == "kimi-code/stream-json"

    def test_the_tap_is_stamped_so_wire_tokens_are_attributable(self) -> None:
        metrics = _wire_trial(_usage_record(prompt=10, completion=1)).metrics

        assert metrics.harness_usage_source == MIDDLEWARE_PROXY_USAGE_SOURCE

    def test_a_record_the_provider_refused_still_counts(self) -> None:
        """A 429 that came back with a usage block was still billed, and the
        proxy writes a record only where usage was actually reported."""
        metrics = _wire_trial(
            _usage_record(prompt=1_000, completion=10),
            _usage_record(prompt=2_000, completion=0, status=429),
        ).metrics

        assert metrics.usage.prompt_tokens == 3_000

    def test_a_malformed_line_is_skipped_and_the_rest_still_counts(self) -> None:
        """A proxy killed mid-append leaves a partial last line. Losing that
        record is a gap in accounting; failing the trial over it would lose the
        trial."""
        metrics = _wire_trial(
            _usage_record(prompt=1_000, completion=10),
            '{"timestamp": "2026-09-15T10:00:01+00:00", "prompt_tok',
            "middleware_proxy: usage tap failed: boom",
            _usage_record(prompt=2_000, completion=20),
        ).metrics

        assert metrics.usage.prompt_tokens == 3_000
        assert metrics.usage.completion_tokens == 30
        assert metrics.harness_usage_source == MIDDLEWARE_PROXY_USAGE_SOURCE


class TestAWireMeasurementNobodyMadeChangesNothing:
    """Absence is the common case — a CLI that made no provider call, a proxy
    that never booted, a container the read could not reach — and must never
    read as a measured zero, nor cost the trial its result."""

    def _assert_untouched(self, trajectory: Trajectory) -> None:
        assert trajectory.status is TrialStatus.COMPLETED
        assert trajectory.metrics.harness_usage_source is None
        assert trajectory.metrics.usage.prompt_tokens == 0
        assert trajectory.metrics.cost_usd is None
        assert trajectory.metrics.turns == 2
        assert trajectory.metrics.tool_calls == 1
        assert len(trajectory.tool_log) == 1

    def test_a_file_the_proxy_never_wrote_leaves_the_trial_as_it_was(self) -> None:
        """``cat`` exits non-zero and the trial keeps what the CLI printed."""
        trajectory = _run(
            _KIMI_STDOUT,
            harness="kimi-code",
            model=_KIMI_MODEL,
            usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
            usage_records=None,
        )

        self._assert_untouched(trajectory)

    def test_an_empty_read_leaves_the_trial_as_it_was(self) -> None:
        self._assert_untouched(_wire_trial())

    def test_a_read_of_nothing_but_malformed_lines_changes_nothing(self) -> None:
        self._assert_untouched(_wire_trial("not json", "{oops"))

    def test_an_executor_that_raised_costs_the_trial_nothing(self) -> None:
        """A container already torn down, a transport fault: telemetry may not
        turn a completed trial into a failed one."""
        trajectory = _run(
            _KIMI_STDOUT,
            harness="kimi-code",
            model=_KIMI_MODEL,
            usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
            raises=RuntimeError("container is gone"),
        )

        self._assert_untouched(trajectory)


class TestTheClisOwnTokensWinOverTheWires:
    """Precedence, asserted rather than assumed.

    Reversing it is defensible for *spend* — the proxy sees retries a CLI's
    end-of-run summary may fold away — but the two cannot both appear today:
    the only proxied harness is also the only one that prints no usage. So the
    rule is asserted here against a contrived overlap, and no merge policy is
    built for a case that cannot occur.
    """

    def _claude_over_wire(self) -> Trajectory:
        return _run(
            _STREAM_JSON,
            usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
            usage_records=_usage_record(prompt=999_999, completion=88_888) + "\n",
        )

    def test_stdout_tokens_stand_and_the_wire_records_are_not_added(self) -> None:
        metrics = self._claude_over_wire().metrics

        assert metrics.usage.prompt_tokens == 129 + 705404 + 55182
        assert metrics.usage.completion_tokens == 15235
        assert metrics.harness_stdout_dialect == "claude-code/stream-json"

    def test_the_wire_tap_is_not_stamped_when_stdout_supplied_the_tokens(self) -> None:
        """The stamp says which tap measured the tokens, so claiming the wire
        did when stdout did would make the two provenance fields disagree."""
        metrics = self._claude_over_wire().metrics

        assert metrics.harness_usage_source is None

    def test_the_cost_stays_the_price_of_the_clis_own_tokens(self) -> None:
        metrics = self._claude_over_wire().metrics

        assert metrics.cost_usd == pytest.approx(
            _expected_cost(
                _MODEL,
                prompt_total=129 + 705404 + 55182,
                completion=15235,
                cache_read=705404,
                cache_write=55182,
            )
        )

    def test_a_cli_that_printed_only_turns_still_yields_to_the_wire(self) -> None:
        """``has_token_counts`` is the gate, not "the CLI printed something":
        kimi prints a turn count and no usage, which must not block the only
        tap that has tokens."""
        metrics = _wire_trial(_usage_record(prompt=1_234, completion=56)).metrics

        assert metrics.usage.prompt_tokens == 1_234
        assert metrics.harness_usage_source == MIDDLEWARE_PROXY_USAGE_SOURCE


class TestACacheHeavyTrialOnARateLessRowIsFlagged:
    """The after-the-fact half of the cache-rate signal.

    The preflight check warns that a model resolves to a row carrying no
    cache rates, but before the run it cannot know whether the trial will use
    the cache — a model without prompt caching legitimately has no rates. Once
    a trial reports cache tokens against such a row, `_compute_cost` has
    billed them at the input rate and the figure is overstated. A harness
    trial is where this bites hardest: they are cache-dominated, so the gap is
    a multiple, not a rounding.

    Regression: both harness pricing paths set `cost_usd` without setting the
    flag, so the one signal that says "this number is wrong" stayed `False` on
    a live trial that overstated cost 4.6x.
    """

    def test_cache_tokens_on_a_row_without_cache_rates_flag_the_trial(self) -> None:
        metrics = _run(_STREAM_JSON, model=_FLAT_MODEL).metrics

        assert metrics.usage.cache_read_input_tokens > 0
        assert metrics.cost_usd is not None
        assert metrics.cost_cache_rate_fallback is True

    def test_the_same_trial_on_the_row_that_carries_them_is_not_flagged(self) -> None:
        """Same tokens, same CLI — only the spelling of the model differs, and
        with it whether the row can price a cache read."""
        metrics = _run(_STREAM_JSON, model=_MODEL).metrics

        assert metrics.usage.cache_read_input_tokens > 0
        assert metrics.cost_cache_rate_fallback is False


class TestAnAllZeroWireMeasurementIsNotAMeasurement:
    """A provider that answers without populating usage is reporting nothing,
    not reporting nothing spent.

    Seen live: a LiteLLM gateway translating Google's ``generateContent``
    returns real counts on the unary path and zeros on the streamed one, which
    is the path ``gemini-cli`` takes. Recording that faithfully put `$0.00`
    on a trial that did a task's worth of work.
    """

    def test_records_that_sum_to_zero_leave_the_trial_unmeasured(self) -> None:
        # The record shape the proxy actually appends, so this exercises the
        # summing path rather than being discarded as unparseable.
        zeroed = json.dumps(
            {
                "timestamp": "2026-09-18T10:00:00+00:00",
                "path": "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
                "status": 200,
                "model": "gemini-3.6-flash",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cache_read_input_tokens": 0,
                "reasoning_tokens": 0,
            }
        )

        metrics = _run(
            "",
            harness="kimi-code",
            model=_KIMI_MODEL,
            usage_log_container_path=_USAGE_LOG_CONTAINER_PATH,
            usage_records=zeroed + "\n",
        ).metrics

        assert metrics.cost_usd is None
        assert metrics.harness_usage_source is None
        assert metrics.usage.prompt_tokens == 0
