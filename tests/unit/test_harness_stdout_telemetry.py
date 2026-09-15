"""What a harness trial's ``Metrics`` carries once the CLI's own totals are read.

The engine issues no LLM request when a CLI drives the trial, so without this
every harness trial reports the artefacts of running one tool call: a single
turn, an empty usage block and no cost. These tests drive the real
:class:`TrialRunner` over a scripted tool result so the assertions are about
the recorded trial, not about the parser (which
``tolokaforge_coding_harnesses/tests/unit/test_stdout_telemetry.py`` covers).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult

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


class _ScriptedToolExecutor:
    """Returns one canned tool result — the CLI's captured stdout."""

    def __init__(self, output: str, *, success: bool = True) -> None:
        self._output = output
        self._success = success

    def execute(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        return ToolResult(success=self._success, output=self._output)


def _run(stdout: str, *, harness: str = "claude-code", success: bool = True) -> Trajectory:
    runner = TrialRunner(
        task_id="telemetry",
        trial_index=0,
        agent_client=object(),  # type: ignore[arg-type]  — never called on this path
        user_simulator=None,
        tool_executor=_ScriptedToolExecutor(stdout, success=success),
        tool_schemas=[],
        episode_timeout_s=600,
    )
    return runner.run_harness(
        tool_name="bash",
        command="claude --print",
        instruction="Fix the failing tests.",
        timeout_s=600,
        harness=harness,
    )


class TestTheTrialReportsWhatTheCliReported:
    def test_turns_are_the_clis_count_not_the_single_tool_call(self) -> None:
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.turns == 19

    def test_cost_and_usage_come_from_the_result_event(self) -> None:
        metrics = _run(_STREAM_JSON).metrics

        assert metrics.cost_usd == pytest.approx(0.6474657)
        assert metrics.usage.prompt_tokens == 129
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
        assert trajectory.metrics.cost_usd == pytest.approx(0.6474657)

    def test_a_cli_that_printed_nothing_before_failing_reports_nothing(self) -> None:
        metrics = _run("", success=False).metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.cost_usd is None


class TestAnUnreportedTrialKeepsTheEnginesOwnAccounting:
    """Absent telemetry must read as "not measured", never as measured zero."""

    def test_a_harness_without_a_dialect_keeps_the_single_tool_call_shape(self) -> None:
        metrics = _run("Implemented the ingest path.", harness="codex").metrics

        assert metrics.harness_stdout_dialect is None
        assert metrics.turns == 1
        assert metrics.cost_usd is None
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
