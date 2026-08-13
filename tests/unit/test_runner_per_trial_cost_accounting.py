"""Trial-level cost / latency accounting via :class:`TrialRunner`.

Pins the contract that:

* ``Metrics.cost_usd`` (single field, no ``cost_usd_est`` /
  ``cost_usd_provider`` split) accumulates ``GenerationResult.cost_usd``
  across every API call in a trial.
* ``Metrics.usage.calls`` carries the per-call provenance, with
  ``cost_source`` set per call by :class:`UsageExtractor`.
* ``Metrics.api_call_latencies_s`` is gone — per-call latency lives on
  ``usage.calls[*].latency_s``.

Companion to
:mod:`tests.unit.test_assemble_result_per_call_record` (which pins
single-call extraction); this file pins multi-call aggregation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import ProviderRawCall, Usage
from tolokaforge.core.models import (
    Metrics,
    Trajectory,
)
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.unit


# --- helpers -----------------------------------------------------------------


def _result(cost_usd: float | None, cost_source: str, prompt_tokens: int = 100) -> GenerationResult:
    """Build a non-terminal GenerationResult with one per-call record.

    Mimics the shape ``UsageExtractor`` produces in production:
    ``Usage.prompt_tokens`` / ``completion_tokens`` flat AND a single
    ``ProviderRawCall`` populated from the same response.
    """
    call = ProviderRawCall(
        prompt_tokens=prompt_tokens,
        completion_tokens=50,
        cost_usd=cost_usd,
        cost_source=cost_source,  # type: ignore[arg-type]
        latency_s=0.5,
    )
    return GenerationResult(
        text="working",
        tool_calls=[],
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=50, calls=(call,)),
        cost_usd=cost_usd,
        latency_s=0.5,
    )


def _final_result() -> GenerationResult:
    """The scripted trial's last agent response, priced like the rest."""
    call = ProviderRawCall(
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.001,
        cost_source="litellm",
        latency_s=0.1,
    )
    return GenerationResult(
        text="All done.",
        tool_calls=[],
        usage=Usage(prompt_tokens=10, completion_tokens=5, calls=(call,)),
        cost_usd=0.001,
        latency_s=0.1,
    )


def _make_user_simulator_keep_going() -> MagicMock:
    sim = MagicMock()
    sim.reply.return_value = GenerationResult(text="please continue", tool_calls=[])
    return sim


def _make_tool_executor() -> MagicMock:
    exec_ = MagicMock()
    exec_.execute.return_value = ToolResult(success=True, output="ok")
    return exec_


def _run_trial(results: list[GenerationResult]) -> Trajectory:
    """Drive ``TrialRunner.run`` over a scripted sequence of results.

    The turn budget is the length of the script, so the trial ends on
    ``max_turns`` with every scripted result generated and none left over — the
    simulator never stops the dialogue, and every call is billed.
    """
    agent = MagicMock()
    agent.generate.side_effect = results
    runner = TrialRunner(
        task_id="trial-001",
        trial_index=0,
        agent_client=agent,
        user_simulator=_make_user_simulator_keep_going(),
        tool_executor=_make_tool_executor(),
        tool_schemas=[{"type": "function", "function": {"name": "noop"}}],
        max_turns=len(results),
        turn_timeout_s=30,
        episode_timeout_s=600,
    )
    return runner.run("System", "Start")


# --- field-shape contract ----------------------------------------------------


class TestMetricsCostShape:
    def test_metrics_has_cost_usd_field(self) -> None:
        m = Metrics()
        assert m.cost_usd is None
        m.cost_usd = 0.05
        assert m.cost_usd == pytest.approx(0.05)

    def test_metrics_rejects_legacy_cost_usd_est(self) -> None:
        # Pydantic with strict mode is the default; unknown fields raise.
        with pytest.raises(ValidationError):
            Metrics.model_validate({"cost_usd_est": 0.5})

    def test_metrics_rejects_legacy_api_call_latencies_s(self) -> None:
        with pytest.raises(ValidationError):
            Metrics.model_validate({"api_call_latencies_s": [0.1, 0.2]})


# --- runner aggregation ------------------------------------------------------


class TestTrialCostAccumulation:
    def test_litellm_priced_trial_sums_into_cost_usd(self) -> None:
        results = [
            _result(0.10, "litellm"),
            _result(0.20, "litellm"),
            _final_result(),  # 0.001 litellm
        ]
        traj = _run_trial(results)

        assert traj.metrics.api_calls == 3
        assert traj.metrics.cost_usd == pytest.approx(0.10 + 0.20 + 0.001)
        assert len(traj.metrics.usage.calls) == 3
        assert all(c.cost_source == "litellm" for c in traj.metrics.usage.calls)

    def test_local_fallback_trial_sums_into_cost_usd(self) -> None:
        results = [
            _result(0.05, "local"),
            _result(0.07, "local"),
            _final_result(),
        ]
        traj = _run_trial(results)

        assert traj.metrics.cost_usd == pytest.approx(0.05 + 0.07 + 0.001)
        assert [c.cost_source for c in traj.metrics.usage.calls] == [
            "local",
            "local",
            "litellm",
        ]

    def test_mixed_sources_preserved_per_call(self) -> None:
        """Trial with mixed cost sources keeps each call's provenance distinct."""
        results = [
            _result(0.10, "litellm"),
            _result(0.05, "local"),
            _final_result(),
        ]
        traj = _run_trial(results)

        assert traj.metrics.cost_usd == pytest.approx(0.10 + 0.05 + 0.001)
        sources = [c.cost_source for c in traj.metrics.usage.calls]
        assert sources == ["litellm", "local", "litellm"]

    def test_unknown_cost_does_not_pollute_total(self) -> None:
        """Calls with cost_usd=None contribute 0 to the trial total."""
        results = [
            _result(None, "unknown"),
            _result(0.04, "litellm"),
            _final_result(),
        ]
        traj = _run_trial(results)

        # The unknown call adds nothing; total = 0.04 + 0.001.
        assert traj.metrics.cost_usd == pytest.approx(0.04 + 0.001)
        assert traj.metrics.usage.calls[0].cost_source == "unknown"
        assert traj.metrics.usage.calls[0].cost_usd is None

    def test_per_call_latencies_recorded_on_calls_not_metrics(self) -> None:
        results = [_result(0.01, "litellm"), _final_result()]
        traj = _run_trial(results)

        latencies = [c.latency_s for c in traj.metrics.usage.calls]
        assert latencies == [0.5, 0.1]
        # Flat list is gone — verify by attribute access.
        assert not hasattr(traj.metrics, "api_call_latencies_s")
