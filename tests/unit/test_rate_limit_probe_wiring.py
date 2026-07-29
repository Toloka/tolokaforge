"""Rate-limit probe mode: config validation and the roles it reaches.

Three things are load-bearing beyond the retry controller itself
(``tests/unit/llm/test_rate_limit_probe_retry.py`` covers that):

- The budget invariant (``per_call_budget_s < episode_s``, and an episode
  budget measured in hours) is enforced by raising — at config-load time
  against the configured episode budget, and in the conductor against the
  *effective* one after the task-pack ``min()`` clamp.
- The agent client, the user-simulator client, and the per-trial counters
  carry the mode. The judge and any fallback chain do not.
- A trial's absorbed 429s reach ``Metrics``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.llm import LLMClient, UserSimulator
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
from tolokaforge.core.runner import TrialRunner

pytestmark = pytest.mark.unit


_AGENT = ModelConfig(provider="openrouter", name="anthropic/claude-3-haiku")
_PROBE = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)


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
    """The simulator shares the agent's provider quota, so it has to probe too."""

    def test_llm_simulator_client_uses_the_probe_controller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-sim")
        sim = UserSimulator(mode="llm", llm_config=_AGENT, rate_limit_probe=_PROBE)

        assert sim.llm_client is not None
        assert sim.llm_client._rate_limit_probe == _PROBE

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
        stats.record_retry(wait_s=15.0, ts=1_700_000_000.0)
        stats.record_retry(wait_s=15.0, ts=1_700_000_030.0)
        runner = self._runner(stats)
        runner.logger = get_logger("probe-metrics", strict=False)

        runner._apply_probe_stats()

        assert runner.metrics.rate_limit_retries == 2
        assert runner.metrics.rate_limit_wait_s == 30.0
        assert runner.metrics.rate_limit_first_ts is not None
        assert runner.metrics.rate_limit_last_ts is not None
        assert runner.metrics.rate_limit_first_ts < runner.metrics.rate_limit_last_ts

    def test_probe_off_leaves_the_counters_at_their_defaults(self) -> None:
        runner = self._runner(None)

        runner._apply_probe_stats()

        assert runner.metrics.rate_limit_retries == 0
        assert runner.metrics.rate_limit_wait_s == 0.0
        assert runner.metrics.rate_limit_first_ts is None
        assert runner.metrics.rate_limit_last_ts is None


class TestSeedMessageRetryLoop:
    """``_seed_first_user_message`` runs outside the episode timeout, so its
    own attempt count is the only thing bounding it.

    Under probe mode the simulator's client already polls 429s for up to
    ``per_call_budget_s``; four such attempts on top of
    ``episode_s + per_call_budget_s`` would exceed the trial's
    ``max(300, episode_s * 2)`` queue lease, so the loop collapses to one
    attempt and lets the client's controller own the 429 budget.
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
