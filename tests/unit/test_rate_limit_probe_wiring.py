"""Rate-limit probe mode: config validation and the roles it reaches.

Three things are load-bearing beyond the retry controller itself
(``tests/unit/llm/test_rate_limit_probe_retry.py`` covers that):

- The budget invariant (``per_call_budget_s + simulator_per_call_budget_s <
  episode_s``, and an episode budget measured in hours) is enforced by raising —
  at config-load time against the configured episode budget, and in the
  conductor against the *effective* one after the task-pack ``min()`` clamp. One
  turn issues both calls back to back, which is why the invariant covers the
  *pair* rather than a single call.
- The agent client, the user-simulator client, and the per-trial counters carry
  the mode; the simulator gets the shorter per-call budget. The rubric judge and
  a fallback chain never carry it — asserted against the clients those paths
  actually build, with the (removed) env activation variable set.
- A trial's absorbed 429s reach ``Metrics``, split per ``(role, model)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.grading.judge import JudgeStatus, LLMJudge
from tolokaforge.core.llm import LLMClient, UserSimulator
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RateLimitProbeConfig,
    RunConfig,
    TaskConfig,
    TimeoutConfig,
    TimeoutDefaults,
    validate_rate_limit_probe_budget,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.run_display_events import RateLimitProbeStats
from tolokaforge.core.run_trial import _build_run_config
from tolokaforge.core.runner import TrialRunner
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_AGENT = ModelConfig(provider="openrouter", name="anthropic/claude-3-haiku")
_PROBE = RateLimitProbeConfig(
    enabled=True,
    retry_interval_s=15.0,
    per_call_budget_s=3600.0,
    simulator_per_call_budget_s=600.0,
)


def _run_config(*, probe: RateLimitProbeConfig | None = None, episode_s: int = 14400) -> RunConfig:
    kwargs: dict[str, object] = {
        "workers": 1,
        "repeats": 1,
        "auto_start_services": False,
        "timeouts": TimeoutConfig(episode_s=episode_s),
    }
    if probe is not None:
        kwargs["rate_limit_probe"] = probe
    return RunConfig(
        models={"agent": _AGENT},
        orchestrator=OrchestratorConfig(**kwargs),
        evaluation=EvaluationConfig(output_dir="results/probe"),
    )


class TestBudgetInvariantAtLoadTime:
    def test_enabled_probe_on_the_default_episode_budget_is_rejected(self) -> None:
        """The default 1800 s budget is minutes, not hours — a probe on it dies
        on the episode timeout instead of measuring the provider."""
        with pytest.raises(ValueError, match="episode budget above 3600s"):
            OrchestratorConfig(rate_limit_probe=RateLimitProbeConfig(enabled=True))

    def test_per_call_budget_at_or_above_the_episode_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be strictly below"):
            OrchestratorConfig(
                timeouts=TimeoutConfig(episode_s=14400),
                rate_limit_probe=RateLimitProbeConfig(enabled=True, per_call_budget_s=14400.0),
            )

    def test_a_per_call_budget_just_under_the_episode_budget_is_rejected(self) -> None:
        """The invariant covers *both* calls one turn makes. This config passes
        ``per_call_budget_s < episode_s`` but its worst case is
        ``7200 + 7199 + 600 = 14999s`` against a ``2 x 7200 = 14400s`` lease, so
        the trial would outlive its lease and be re-run by another worker."""
        with pytest.raises(ValueError, match="per-turn 429 budget"):
            OrchestratorConfig(
                timeouts=TimeoutConfig(episode_s=7200),
                rate_limit_probe=RateLimitProbeConfig(enabled=True, per_call_budget_s=7199.0),
            )

    def test_the_simulator_budget_counts_toward_the_invariant(self) -> None:
        """Raising only the simulator's budget can break the lease on its own,
        so it has to be part of the sum."""
        fits = OrchestratorConfig(
            timeouts=TimeoutConfig(episode_s=7200),
            rate_limit_probe=RateLimitProbeConfig(
                enabled=True, per_call_budget_s=3600.0, simulator_per_call_budget_s=3599.0
            ),
        )
        assert fits.rate_limit_probe.turn_budget_s == 7199.0

        with pytest.raises(ValueError, match="simulator_per_call_budget_s"):
            OrchestratorConfig(
                timeouts=TimeoutConfig(episode_s=7200),
                rate_limit_probe=RateLimitProbeConfig(
                    enabled=True, per_call_budget_s=3600.0, simulator_per_call_budget_s=3600.0
                ),
            )

    def test_turn_budget_is_the_sum_of_both_roles_per_call_budgets(self) -> None:
        """``_run_turn`` issues the agent's ``generate`` and then the simulator's
        ``reply``, and the episode timeout is only checked between turns."""
        assert _PROBE.turn_budget_s == 4200.0

    def test_the_documented_defaults_fit(self) -> None:
        config = OrchestratorConfig(
            timeouts=TimeoutConfig(episode_s=14400),
            rate_limit_probe=RateLimitProbeConfig(enabled=True),
        )
        probe = config.rate_limit_probe
        # 14400 (episode) + 3600 (agent) + 600 (simulator) = 18600 < 28800 lease.
        assert probe.turn_budget_s < config.timeouts.episode_s
        assert config.timeouts.episode_s + probe.turn_budget_s < config.timeouts.episode_s * 2

    def test_hours_long_episode_budget_with_a_smaller_per_call_budget_is_accepted(self) -> None:
        config = OrchestratorConfig(
            timeouts=TimeoutConfig(episode_s=14400),
            rate_limit_probe=_PROBE,
        )
        assert config.rate_limit_probe.enabled is True
        assert config.timeouts.episode_s == 14400

    def test_disabled_probe_never_constrains_the_episode_budget(self) -> None:
        config = OrchestratorConfig()
        assert config.rate_limit_probe.enabled is False
        assert config.timeouts.episode_s == 1800

    def test_disabled_probe_with_an_unfittable_budget_is_still_accepted(self) -> None:
        """Only ``enabled`` arms the invariant, so a dormant block cannot break
        an ordinary run."""
        config = OrchestratorConfig(
            rate_limit_probe=RateLimitProbeConfig(enabled=False, per_call_budget_s=99999.0)
        )
        assert config.rate_limit_probe.enabled is False

    def test_helper_is_a_no_op_for_a_missing_block(self) -> None:
        validate_rate_limit_probe_budget(None, 60.0, source="test")


class TestBudgetInvariantAgainstTheEffectiveTimeout:
    """The conductor re-checks after ``min(task.trial_seconds, run episode_s)``."""

    def _conductor(self, config: RunConfig, tmp_path: Path) -> InProcessConductor:
        return InProcessConductor(
            adapter=MagicMock(),
            artifact_writer=MagicMock(),
            config=config,
            logger=get_logger("probe-wiring", strict=False),
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=tmp_path,
        )

    def _setup(self, tmp_path: Path) -> MagicMock:
        setup = MagicMock()
        setup.trial_idx = 0
        setup.task_dir = tmp_path
        setup.tool_schemas = []
        return setup

    def test_task_declared_trial_seconds_that_clamps_below_the_budget_raises(
        self, tmp_path: Path
    ) -> None:
        """A pack declaring ``trial_seconds: 600`` shrinks a 14400 s run budget
        to 600 s — below the 3600 s per-call budget — and must fail loud."""
        conductor = self._conductor(_run_config(probe=_PROBE), tmp_path)
        task = TaskConfig(
            task_id="clamped",
            description="d",
            timeouts=TimeoutDefaults(trial_seconds=600),
        )

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            pytest.raises(ValueError, match="task clamped"),
        ):
            conductor._run_agent_loop(MagicMock(), task, self._setup(tmp_path))

    def test_task_without_declared_timeouts_uses_the_unclamped_run_budget(
        self, tmp_path: Path
    ) -> None:
        conductor = self._conductor(_run_config(probe=_PROBE), tmp_path)
        task = TaskConfig(task_id="unclamped", description="d")
        assert task.timeouts is None

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(MagicMock(), task, self._setup(tmp_path))

        kwargs = runner_cls.call_args.kwargs
        assert kwargs["episode_timeout_s"] == 14400
        assert isinstance(kwargs["probe_stats"], RateLimitProbeStats)

    def test_the_simulator_the_conductor_builds_carries_the_shorter_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End of the wiring: the ``UserSimulator`` the conductor actually
        constructs probes at ``simulator_per_call_budget_s``, not the agent's."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-conductor")
        conductor = self._conductor(_run_config(probe=_PROBE), tmp_path)
        task = TaskConfig(
            task_id="sim",
            description="d",
            actors={"user": {"mode": "llm"}},
        )

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(MagicMock(), task, self._setup(tmp_path))

        simulator = runner_cls.call_args.kwargs["user_simulator"]
        assert simulator.llm_client is not None
        probe = simulator.llm_client._rate_limit_probe
        assert probe is not None
        assert probe.per_call_budget_s == 600.0

    def test_probe_off_run_threads_no_probe_stats_into_the_trial(self, tmp_path: Path) -> None:
        conductor = self._conductor(_run_config(episode_s=1800), tmp_path)
        task = TaskConfig(task_id="normal", description="d")

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(MagicMock(), task, self._setup(tmp_path))

        assert runner_cls.call_args.kwargs["probe_stats"] is None


class TestUserSimulatorCarriesTheMode:
    """The simulator shares the agent's provider quota, so it has to probe too —
    but with the shorter per-call budget, because its throughput is not what the
    probe measures and both budgets are spent inside one uninterruptible turn."""

    def test_llm_simulator_client_uses_the_probe_controller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-sim")
        sim = UserSimulator(mode="llm", llm_config=_AGENT, rate_limit_probe=_PROBE)

        assert sim.llm_client is not None
        assert sim.llm_client._rate_limit_probe == _PROBE

    def test_the_conductor_hands_the_simulator_the_shorter_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-sim")
        sim = UserSimulator(mode="llm", llm_config=_AGENT, rate_limit_probe=_PROBE.for_simulator())

        assert sim.llm_client is not None
        probe = sim.llm_client._rate_limit_probe
        assert probe is not None
        assert probe.per_call_budget_s == _PROBE.simulator_per_call_budget_s == 600.0
        assert probe.retry_interval_s == _PROBE.retry_interval_s
        assert probe.enabled is True

    def test_for_simulator_is_idempotent(self) -> None:
        """Re-deriving cannot ratchet the budget down a second time."""
        once = _PROBE.for_simulator()
        assert once.for_simulator() == once

    def test_for_simulator_keeps_a_disabled_block_disabled(self) -> None:
        assert RateLimitProbeConfig().for_simulator().enabled is False

    def test_llm_simulator_client_is_on_the_default_path_without_the_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-sim")
        sim = UserSimulator(mode="llm", llm_config=_AGENT)

        assert sim.llm_client is not None
        assert sim.llm_client._rate_limit_probe is None

    def test_scripted_simulator_builds_no_client_at_all(self) -> None:
        sim = UserSimulator(mode="scripted", rate_limit_probe=_PROBE)
        assert sim.llm_client is None


class TestAgentClientConstruction:
    def _orchestrator(self, config: RunConfig, **deps: object) -> Orchestrator:
        # ``OrchestratorDeps`` fields are typed callables; the **kwargs relay
        # erases that, so mypy cannot see the per-key match.
        return Orchestrator(config, deps=OrchestratorDeps(**deps))  # type: ignore[arg-type]

    def test_bare_agent_client_carries_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-agent")
        orch = self._orchestrator(_run_config(probe=_PROBE))

        client = orch._build_agent_client(_AGENT)

        assert client._rate_limit_probe == _PROBE

    def test_bare_agent_client_stays_on_the_default_path_without_the_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-agent")
        orch = self._orchestrator(_run_config())

        client = orch._build_agent_client(_AGENT)

        assert client._rate_limit_probe is None

    def test_fallback_chain_plus_probe_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A chain that switches models mid-probe would attribute one model's
        429s to another."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-agent")
        orch = self._orchestrator(
            _run_config(probe=_PROBE),
            agent_client_factory=lambda cfg: LLMClient(cfg),
        )

        with pytest.raises(ValueError, match="incompatible with a fallback model chain"):
            orch._build_agent_client(_AGENT)

    def test_fallback_chain_still_works_when_the_mode_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-agent")
        sentinel = MagicMock()
        orch = self._orchestrator(_run_config(), agent_client_factory=lambda _cfg: sentinel)

        assert orch._build_agent_client(_AGENT) is sentinel


class TestProbeCountersReachMetrics:
    def _runner(self, probe_stats: RateLimitProbeStats | None) -> TrialRunner:
        return TrialRunner(
            task_id="t1",
            trial_index=0,
            agent_client=MagicMock(),
            user_simulator=MagicMock(),
            tool_executor=MagicMock(),
            tool_schemas=[],
            probe_stats=probe_stats,
        )

    def test_absorbed_429s_are_copied_onto_the_trial_metrics(self) -> None:
        stats = RateLimitProbeStats()
        stats.record_retry(
            role="agent", model="openrouter/deepseek/deepseek-v3.2-exp", wait_s=15.0, ts=1.7e9
        )
        stats.record_retry(
            role="agent",
            model="openrouter/deepseek/deepseek-v3.2-exp",
            wait_s=15.0,
            ts=1_700_000_030.0,
        )
        runner = self._runner(stats)
        runner.logger = get_logger("probe-metrics", strict=False)

        runner._apply_probe_stats()

        assert runner.metrics.rate_limit_retries == 2
        assert runner.metrics.rate_limit_wait_s == 30.0
        assert runner.metrics.rate_limit_first_ts is not None
        assert runner.metrics.rate_limit_last_ts is not None
        assert runner.metrics.rate_limit_first_ts < runner.metrics.rate_limit_last_ts

    def test_per_role_model_rows_reach_metrics_sorted_and_unblended(self) -> None:
        """A real arena trial: agent and simulator are different models, so the
        metrics carry one row each. Blended counters would produce one row of 4
        instead of 3 + 1."""
        stats = RateLimitProbeStats()
        for _ in range(3):
            stats.record_retry(
                role="agent",
                model="openrouter/deepseek/deepseek-v3.2-exp",
                wait_s=15.0,
                ts=1_700_000_000.0,
            )
        stats.record_retry(
            role="user",
            model="openrouter/anthropic/claude-sonnet-4.6",
            wait_s=15.0,
            ts=1_700_000_100.0,
        )
        runner = self._runner(stats)
        runner.logger = get_logger("probe-metrics", strict=False)

        runner._apply_probe_stats()

        rows = runner.metrics.rate_limit_by_role_model
        assert [(row.role, row.model, row.retries, row.wait_s) for row in rows] == [
            ("agent", "openrouter/deepseek/deepseek-v3.2-exp", 3, 45.0),
            ("user", "openrouter/anthropic/claude-sonnet-4.6", 1, 15.0),
        ]
        # The flat fields stay the sum across rows, so existing consumers are
        # unaffected by the breakdown.
        assert runner.metrics.rate_limit_retries == sum(row.retries for row in rows) == 4
        assert runner.metrics.rate_limit_wait_s == sum(row.wait_s for row in rows) == 60.0
        assert all(row.first_ts is not None and row.last_ts is not None for row in rows)

    def test_probe_off_leaves_the_counters_at_their_defaults(self) -> None:
        runner = self._runner(None)

        runner._apply_probe_stats()

        assert runner.metrics.rate_limit_retries == 0
        assert runner.metrics.rate_limit_wait_s == 0.0
        assert runner.metrics.rate_limit_first_ts is None
        assert runner.metrics.rate_limit_last_ts is None
        assert runner.metrics.rate_limit_by_role_model == []


class TestSeedMessageRetryLoop:
    """``_seed_first_user_message`` runs before the loop, so no episode-timeout
    check can interrupt it — but its wall time is *consumed from* the episode
    budget (``run()`` sets ``start_time`` before calling it and hands the loop
    that same ``start_time``), not added to it.

    Under probe mode the loop collapses to one attempt. It only ever retried
    429s, and the simulator's own client now polls 429s at a fixed interval for
    up to its per-call budget — strictly more tolerant than 4 attempts of
    2/4/8 s backoff — so the outer attempts are redundant. Collapsing also keeps
    this step's worst case at one simulator budget instead of ``init_attempts``
    of them, which is what makes the budget invariant alone sufficient to bound
    the trial under its ``max(300, episode_s * 2)`` lease.
    """

    def _runner(self, probe_stats: RateLimitProbeStats | None) -> TrialRunner:
        runner = TrialRunner(
            task_id="t1",
            trial_index=0,
            agent_client=MagicMock(),
            user_simulator=MagicMock(),
            tool_executor=MagicMock(),
            tool_schemas=[],
            probe_stats=probe_stats,
        )
        runner.logger = get_logger("probe-seed", strict=False)
        return runner

    def test_probe_mode_does_not_multiply_the_clients_429_budget(self) -> None:
        runner = self._runner(RateLimitProbeStats())
        runner.user_simulator.reply.side_effect = RuntimeError("Error code: 429")

        with pytest.raises(RuntimeError, match="429"):
            runner._seed_first_user_message("")

        assert runner.user_simulator.reply.call_count == 1

    def test_probe_off_keeps_the_four_attempt_rate_limit_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tolokaforge.core.runner.time.sleep", lambda _s: None)
        runner = self._runner(None)
        runner.user_simulator.reply.side_effect = RuntimeError("Error code: 429")

        with pytest.raises(RuntimeError, match="429"):
            runner._seed_first_user_message("")

        assert runner.user_simulator.reply.call_count == 4

    def test_a_non_429_seed_error_was_never_retried_on_either_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The collapse costs nothing for non-429 errors: this loop re-raises
        them on the first attempt with or without probe mode, and the client's
        own five-attempt exponential still covers them under probe mode."""
        monkeypatch.setattr("tolokaforge.core.runner.time.sleep", lambda _s: None)
        for probe_stats in (None, RateLimitProbeStats()):
            runner = self._runner(probe_stats)
            runner.user_simulator.reply.side_effect = RuntimeError("upstream 503")

            with pytest.raises(RuntimeError, match="503"):
                runner._seed_first_user_message("")

            assert runner.user_simulator.reply.call_count == 1


class TestNonProbingRolesStayOnTheDefaultPath:
    """Grading must not probe, and a fallback chain must not either.

    Both build their clients with no ``rate_limit_probe`` argument, so these
    cases also pin the absence of an env activation channel: they set
    ``TOLOKAFORGE_RATE_LIMIT_PROBE=1`` — which used to arm the mode on exactly
    these paths — and assert the constructed clients still do not probe.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-nonprobing")
        monkeypatch.setenv("OPENAI_API_KEY", "test-sk-probe-nonprobing")
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", "1")

    def test_the_rubric_judge_builds_a_non_probing_client(self) -> None:
        """Drives the real ``LLMJudge.run()`` client construction and inspects
        the client it built, with the loop stubbed out so no provider is hit."""
        built: list[LLMClient] = []

        class _CapturingLoop:
            def __init__(self, **kwargs: Any) -> None:
                built.append(kwargs["llm_client"])

            def run(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("loop short-circuited by the test")

        judge = LLMJudge(_AGENT)
        rubric = Rubric(criteria=[Criterion(id="c1", description="did the thing")])

        with patch("tolokaforge.core.grading.judge.ToolCallingLoop", _CapturingLoop):
            result = judge.run(rubric=rubric, agent_system_prompt="sys", transcript=[])

        assert result.status is JudgeStatus.ERRORED
        assert len(built) == 1
        assert built[0]._rate_limit_probe is None

    def test_a_fallback_chain_builds_non_probing_clients(self) -> None:
        chain = FallbackLLMClient(
            primary=_AGENT,
            fallbacks=[ModelConfig(provider="openai", name="gpt-4o-mini")],
        )

        assert chain._current._rate_limit_probe is None

    def test_run_trials_composed_config_leaves_the_mode_off_by_default(self) -> None:
        """``run_trial(rate_limit_probe=None)`` composes a config with the mode
        off, so its clients cannot pick the mode up from the environment either."""
        config = _build_run_config(_AGENT, _AGENT, None, Path("out"), None)

        assert config.orchestrator.rate_limit_probe.enabled is False

    def test_run_trials_composed_config_sizes_the_episode_budget_for_both_calls(self) -> None:
        """With the mode on, the composed episode budget has to clear the
        per-*turn* budget, not just one call's."""
        config = _build_run_config(_AGENT, _AGENT, None, Path("out"), _PROBE)

        assert config.orchestrator.rate_limit_probe.enabled is True
        assert config.orchestrator.timeouts.episode_s == int(_PROBE.turn_budget_s * 2) == 8400
