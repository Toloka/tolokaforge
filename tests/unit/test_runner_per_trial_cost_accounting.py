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
from tolokaforge.core.llm.client import ParserError
from tolokaforge.core.llm.usage import ProviderRawCall, Usage
from tolokaforge.core.models import (
    MessageRole,
    Metrics,
    ParserErrorRecord,
    Trajectory,
)
from tolokaforge.core.runner import TrialRunner, _TrialMetricsSink
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
    # Pin new-capability numeric slots to None so the loop's opt-in branches
    # (empty_retry_count, tool_output_max_chars, default_max_turns,
    # max_context_tokens, context_watermark) short-circuit to their pre-opt-in
    # code paths instead of consuming MagicMock instances as ints.
    agent.capabilities.max_context_tokens = None
    agent.capabilities.context_watermark = None
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


class TestToolOutputTruncationAccounting:
    """``_TrialMetricsSink.record_tool_output_truncated`` accumulates the
    per-trial ``tool_output_chars_truncated`` counter that
    ``ToolCallingLoop._cap_tool_message_content`` calls whenever it clips
    a ``role=tool`` message.
    """

    def test_zero_when_never_truncated(self) -> None:
        metrics = Metrics()
        assert metrics.tool_output_chars_truncated == 0

    def test_single_call_accumulates(self) -> None:
        metrics = Metrics()
        sink = _TrialMetricsSink(metrics)
        sink.record_tool_output_truncated(1_500)
        assert metrics.tool_output_chars_truncated == 1_500

    def test_multiple_calls_sum(self) -> None:
        metrics = Metrics()
        sink = _TrialMetricsSink(metrics)
        sink.record_tool_output_truncated(500)
        sink.record_tool_output_truncated(1_000)
        sink.record_tool_output_truncated(250)
        assert metrics.tool_output_chars_truncated == 1_750


class TestParserErrorAccounting:
    """``_TrialMetricsSink.record_parser_errors`` persists per-turn
    parser-error records onto ``Metrics.parser_errors``, mirroring the
    ephemeral ``GenerationResult.parser_errors`` sidecar across the
    trial-bundle boundary.
    """

    def test_empty_by_default(self) -> None:
        metrics = Metrics()
        assert metrics.parser_errors == []

    def test_one_error_appends_one_record(self) -> None:
        metrics = Metrics()
        sink = _TrialMetricsSink(metrics)
        sink.record_parser_errors(
            (
                ParserError(
                    tool_name="query", raw_arguments='{"broken', reason="unterminated string"
                ),
            )
        )
        assert len(metrics.parser_errors) == 1
        assert metrics.parser_errors[0] == ParserErrorRecord(
            tool_name="query",
            raw_arguments='{"broken',
            reason="unterminated string",
        )

    def test_multiple_calls_extend(self) -> None:
        metrics = Metrics()
        sink = _TrialMetricsSink(metrics)
        sink.record_parser_errors(
            (
                ParserError(tool_name="a", raw_arguments="x", reason="one"),
                ParserError(tool_name="b", raw_arguments="y", reason="two"),
            )
        )
        sink.record_parser_errors((ParserError(tool_name="c", raw_arguments="z", reason="three"),))
        assert [r.tool_name for r in metrics.parser_errors] == ["a", "b", "c"]

    def test_survives_trajectory_roundtrip(self) -> None:
        metrics = Metrics()
        sink = _TrialMetricsSink(metrics)
        sink.record_parser_errors((ParserError(tool_name="run", raw_arguments="{}", reason="ok"),))
        dumped = metrics.model_dump(mode="json")
        assert dumped["parser_errors"] == [
            {"tool_name": "run", "raw_arguments": "{}", "reason": "ok"}
        ]
        rebuilt = Metrics.model_validate(dumped)
        assert rebuilt.parser_errors == metrics.parser_errors


# --- non-agent actor spend folding -------------------------------------------


def _user_result(cost_usd: float, gen_id: str) -> GenerationResult:
    """A user-simulator reply that made one LLM call (role=="user")."""
    call = ProviderRawCall(
        role="user",
        prompt_tokens=20,
        completion_tokens=10,
        cost_usd=cost_usd,
        cost_source="litellm",
        latency_s=0.2,
        openrouter_generation_id=gen_id,
    )
    return GenerationResult(
        text="please continue",
        tool_calls=[],
        usage=Usage(prompt_tokens=20, completion_tokens=10, calls=(call,)),
        cost_usd=cost_usd,
        openrouter_generation_id=gen_id,
    )


def _make_runner(user_simulator: MagicMock) -> TrialRunner:
    agent = MagicMock()
    agent.capabilities.max_context_tokens = None
    agent.capabilities.context_watermark = None
    return TrialRunner(
        task_id="trial-001",
        trial_index=0,
        agent_client=agent,
        user_simulator=user_simulator,
        tool_executor=_make_tool_executor(),
        tool_schemas=[{"type": "function", "function": {"name": "noop"}}],
        max_turns=1,
        turn_timeout_s=30,
        episode_timeout_s=600,
    )


class TestActorSpendFold:
    """``TrialRunner._record_actor_spend`` folds a non-agent actor's
    ``GenerationResult`` into the trial ``Metrics`` only when the reply carries
    real per-call records — the single correctness gate that keeps scripted /
    mock replies (empty ``usage.calls``) from inflating cost / api_calls.
    """

    def test_nonempty_calls_fold_all_fields(self) -> None:
        runner = _make_runner(_make_user_simulator_keep_going())
        runner._record_actor_spend(_user_result(0.03, "gen-user-1"))

        m = runner.metrics
        assert m.api_calls == 1
        assert m.cost_usd == pytest.approx(0.03)
        assert m.openrouter_generation_ids == ["gen-user-1"]
        assert [c.role for c in m.usage.calls] == ["user"]

    def test_scripted_reply_empty_calls_is_noop(self) -> None:
        runner = _make_runner(_make_user_simulator_keep_going())
        runner._record_actor_spend(GenerationResult(text="please continue", tool_calls=[]))

        m = runner.metrics
        assert m.api_calls == 0
        assert m.cost_usd is None
        assert m.openrouter_generation_ids == []
        assert m.usage.calls == ()


class TestSimulatorSpendReachesTrialMetrics:
    """The drop sites (bootstrap + per-turn user dispatch) actually call the
    fold: a conversational trial whose simulator makes real LLM calls records
    the user's spend into the trial ``Metrics``, while the USER ``Message``
    keeps ``openrouter_generation_id=None``.
    """

    def test_conversational_run_accrues_user_spend(self) -> None:
        sim = MagicMock()
        sim.reply.return_value = _user_result(0.02, "gen-user-1")
        agent = MagicMock()
        agent.generate.side_effect = [_result(0.10, "litellm"), _final_result()]
        agent.capabilities.max_context_tokens = None
        agent.capabilities.context_watermark = None
        runner = TrialRunner(
            task_id="trial-001",
            trial_index=0,
            agent_client=agent,
            user_simulator=sim,
            tool_executor=_make_tool_executor(),
            tool_schemas=[{"type": "function", "function": {"name": "noop"}}],
            max_turns=2,
            turn_timeout_s=30,
            episode_timeout_s=600,
        )
        # Empty initial message => the conductor bootstraps turn 0 via the
        # simulator, exercising both drop sites.
        traj = runner.run("System", "")

        user_rows = [c for c in traj.metrics.usage.calls if c.role == "user"]
        assert user_rows, "user-simulator spend was not folded into the trial metrics"
        assert "gen-user-1" in traj.metrics.openrouter_generation_ids
        # Every folded result contributes its cost exactly once.
        assert traj.metrics.cost_usd == pytest.approx(
            sum(c.cost_usd for c in traj.metrics.usage.calls if c.cost_usd is not None)
        )
        # The USER Message contract is unchanged: the id rides the metrics
        # plane, never the transcript message.
        user_messages = [m for m in traj.messages if m.role == MessageRole.USER]
        assert user_messages
        assert all(m.openrouter_generation_id is None for m in user_messages)


# --- per-role cost rollup -----------------------------------------------------


def _agent_call(cost_usd: float, model: str, prompt_tokens: int = 100) -> ProviderRawCall:
    return ProviderRawCall(
        role="agent",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=50,
        cost_usd=cost_usd,
        cost_source="litellm",
    )


class TestCostByRoleRollup:
    """``TrialRunner._apply_cost_rollup`` derives the per-role / per-(role,model)
    breakdown from ``usage.calls`` and reconciles the residual against
    ``cost_usd`` so ``sum(cost_by_role[*].cost_usd) == cost_usd`` on every trial.
    """

    def test_mixed_roles_sum_per_role_and_reconcile(self) -> None:
        runner = _make_runner(_make_user_simulator_keep_going())
        agent_a = _agent_call(0.10, "agent-model")
        agent_b = _agent_call(0.20, "agent-model")
        user = ProviderRawCall(
            role="user",
            model="user-model",
            prompt_tokens=20,
            completion_tokens=10,
            cost_usd=0.03,
            cost_source="litellm",
        )
        runner.metrics.usage = Usage(
            prompt_tokens=220,
            completion_tokens=110,
            calls=(agent_a, agent_b, user),
        )
        runner.metrics.cost_usd = 0.33

        runner._apply_cost_rollup()

        by_role = {row.role: row for row in runner.metrics.cost_by_role}
        assert set(by_role) == {"agent", "user"}
        assert by_role["agent"].cost_usd == pytest.approx(0.30)
        assert by_role["user"].cost_usd == pytest.approx(0.03)
        assert by_role["agent"].prompt_tokens == 200
        assert by_role["user"].prompt_tokens == 20
        assert sum(row.cost_usd for row in runner.metrics.cost_by_role) == pytest.approx(
            runner.metrics.cost_usd
        )

        by_pair = {(r.role, r.model): r for r in runner.metrics.cost_by_role_model}
        assert set(by_pair) == {("agent", "agent-model"), ("user", "user-model")}
        assert by_pair[("agent", "agent-model")].cost_usd == pytest.approx(0.30)

    def test_harness_style_residual_lands_on_agent(self) -> None:
        """Empty ``usage.calls`` + non-zero ``cost_usd`` (a coding-harness trial)
        produces a single ``agent`` row equal to ``cost_usd``, with the flat
        token totals attributed to it — the reconciliation lock.
        """
        runner = _make_runner(_make_user_simulator_keep_going())
        runner.agent_client.model_name = "harness-model"
        runner.metrics.usage = Usage(prompt_tokens=1000, completion_tokens=200, calls=())
        runner.metrics.cost_usd = 0.42

        runner._apply_cost_rollup()

        assert len(runner.metrics.cost_by_role) == 1
        agent_row = runner.metrics.cost_by_role[0]
        assert agent_row.role == "agent"
        assert agent_row.cost_usd == pytest.approx(0.42)
        assert agent_row.prompt_tokens == 1000
        assert agent_row.completion_tokens == 200
        assert sum(row.cost_usd for row in runner.metrics.cost_by_role) == pytest.approx(0.42)

        assert len(runner.metrics.cost_by_role_model) == 1
        assert runner.metrics.cost_by_role_model[0].model == "harness-model"

    def test_none_cost_leaves_rollup_call_derived(self) -> None:
        """A ``None`` ``cost_usd`` emits no reconciled residual row."""
        runner = _make_runner(_make_user_simulator_keep_going())
        runner.metrics.usage = Usage(prompt_tokens=0, completion_tokens=0, calls=())
        runner.metrics.cost_usd = None

        runner._apply_cost_rollup()

        assert runner.metrics.cost_by_role == []
        assert runner.metrics.cost_by_role_model == []
