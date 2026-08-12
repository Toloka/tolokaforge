"""Rate-limit probe mode: config validation and the roles it reaches.

What is load-bearing beyond the retry controller itself
(``tests/unit/llm/test_rate_limit_probe_retry.py`` covers that):

- The budget invariant is enforced by raising — at config-load time against the
  configured episode budget, and in the conductor against the *effective* one
  after the task-pack ``min()`` clamp. It bounds the whole per-turn 429 ceiling
  (both per-call budgets **plus** both calls' overshoot), because one turn issues
  both calls back to back and ``stop`` is evaluated on an attempt's outcome.
  Configs that clear the budgets but not the overshoot are pinned as rejected.
- The mode cannot arm on a conductor that does not wire it: the orchestrator arms
  the agent client, the conductor wires the simulator probe, the per-task
  re-check and the telemetry, so a mismatch raises instead of producing
  artifacts that read all-default.
- The agent client, the user-simulator client, and the per-trial counters carry
  the mode; the simulator gets the shorter per-call budget. The rubric judge and
  a fallback chain never carry it — asserted against the clients those paths
  actually build.
- A trial's absorbed 429s reach ``Metrics``, split per ``(role, model)``.
- So does the SUCCESS census, plus the absolute-time window series a consumer
  computes goodput / tokens-per-second / Little's-law concurrency from — with
  the bucket boundary an exact epoch multiple, the series sorted as a timeline,
  the bucket cap visible, and every field left at its default with the mode off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import (
    InMemoryConductor,
    InProcessConductor,
    conductor_supports_rate_limit_probe,
    require_rate_limit_probe_support,
)
from tolokaforge.core.grading.judge import JudgeStatus, LLMJudge
from tolokaforge.core.llm import LLMClient, UserSimulator
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    RATE_LIMIT_PROBE_ATTEMPT_CEILING_S,
    EvaluationConfig,
    Metrics,
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
        with pytest.raises(ValueError, match="per-turn 429 wall time"):
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
                enabled=True, per_call_budget_s=3600.0, simulator_per_call_budget_s=1600.0
            ),
        )
        assert fits.rate_limit_probe.turn_budget_s == 5200.0

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
        # 14400 (episode) + 4200 (both budgets) + 1510 (both overshoots)
        # = 20110 < 28800 lease.
        assert probe.turn_wall_ceiling_s < config.timeouts.episode_s
        assert config.timeouts.episode_s + probe.turn_wall_ceiling_s < config.timeouts.episode_s * 2

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


def _spec() -> MagicMock:
    """A ``TrialSpec`` stand-in whose ``task.metadata`` is a real mapping.

    ``TaskDescription.metadata`` is a ``dict`` field with a ``{}`` default, so
    every real spec has one. A bare ``MagicMock`` answers ``metadata.get(...)``
    with another mock, which no caller can interpret — these tests care about
    timeout wiring, not metadata, but the stand-in still has to honour the
    contract the conductor reads.
    """
    spec = MagicMock()
    spec.task.metadata = {}
    return spec


class TestTheOvershootIsPartOfTheInvariant:
    """``per_call_budget_s`` is a floor, so the invariant has to model the
    overshoot rather than trust the slack to absorb it.

    ``stop`` is evaluated on an attempt's *outcome*, so a call whose elapsed time
    is a hair under its budget still gets one more jitter-maximum wait plus one
    more attempt. Two calls per turn, so twice that. Three configs below pass
    ``turn_budget_s < episode_s`` yet exceed the ``max(300, episode_s * 2)``
    lease — the third with the *documented default* budgets and only
    ``retry_interval_s`` raised, a knob a budget-only invariant never reads.
    """

    def _lease_s(self, episode_s: int) -> int:
        """``Orchestrator``'s queue lease horizon for this episode budget."""
        return max(300, episode_s * 2)

    def test_the_call_overshoot_reads_the_interval_the_jitter_and_the_attempt(
        self,
    ) -> None:
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, jitter_fraction=0.2)
        assert probe.call_overshoot_s == 15.0 * 1.2 + RATE_LIMIT_PROBE_ATTEMPT_CEILING_S
        assert probe.turn_overshoot_s == 2 * probe.call_overshoot_s
        assert probe.turn_wall_ceiling_s == probe.turn_budget_s + probe.turn_overshoot_s

    @pytest.mark.parametrize(
        ("episode_s", "probe"),
        [
            pytest.param(
                3601,
                RateLimitProbeConfig(
                    enabled=True, per_call_budget_s=3000.0, simulator_per_call_budget_s=599.0
                ),
                id="two-seconds-of-budget-slack",
            ),
            pytest.param(
                14400,
                RateLimitProbeConfig(enabled=True, retry_interval_s=6000.0),
                id="documented-budgets-with-only-the-interval-raised",
            ),
            pytest.param(
                3601,
                RateLimitProbeConfig(
                    enabled=True,
                    per_call_budget_s=1.0,
                    simulator_per_call_budget_s=1.0,
                    retry_interval_s=7200.0,
                ),
                id="tiny-budgets-huge-interval",
            ),
        ],
    )
    def test_a_config_whose_overshoot_breaks_the_lease_is_rejected(
        self, episode_s: int, probe: RateLimitProbeConfig
    ) -> None:
        # Each of these satisfies the budget-only comparison the invariant used
        # to make, and each still outlives its lease.
        assert probe.turn_budget_s < episode_s
        assert episode_s + probe.turn_wall_ceiling_s > self._lease_s(episode_s)

        with pytest.raises(ValueError, match="per-turn 429 wall time"):
            validate_rate_limit_probe_budget(probe, float(episode_s), source="probe")

    def test_the_documented_defaults_still_fit_under_the_lease(self) -> None:
        """The fix must not reject the shipped configuration."""
        probe = RateLimitProbeConfig(enabled=True)
        validate_rate_limit_probe_budget(probe, 14400.0, source="probe")
        assert 14400 + probe.turn_wall_ceiling_s < self._lease_s(14400)

    def test_the_error_names_every_knob_it_read(self) -> None:
        """An operator has to be able to see which knob to lower."""
        with pytest.raises(ValueError) as excinfo:
            validate_rate_limit_probe_budget(
                RateLimitProbeConfig(enabled=True, retry_interval_s=6000.0),
                14400.0,
                source="probe",
            )
        message = str(excinfo.value)
        for knob in (
            "per_call_budget_s",
            "simulator_per_call_budget_s",
            "retry_interval_s",
            "jitter_fraction",
        ):
            assert knob in message

    def test_the_conductor_applies_the_same_bound_to_the_effective_budget(
        self, tmp_path: Path
    ) -> None:
        """The per-task re-check reads the same ceiling, so a pack's
        ``trial_seconds`` clamp cannot smuggle an unfittable overshoot through."""
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=1200.0)
        # Legal at the run level: 4200 + 2 x (1440 + 737) = 8554 < 14400.
        config = _run_config(probe=probe)
        assert config.orchestrator.rate_limit_probe.turn_wall_ceiling_s < 14400

        conductor = InProcessConductor(
            adapter=MagicMock(),
            artifact_writer=MagicMock(),
            config=config,
            logger=get_logger("probe-overshoot", strict=False),
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=tmp_path,
        )
        setup = MagicMock()
        setup.trial_idx = 0
        setup.task_dir = tmp_path
        setup.tool_schemas = []
        task = TaskConfig(
            task_id="clamped-overshoot",
            description="d",
            timeouts=TimeoutDefaults(trial_seconds=7200),
        )

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            pytest.raises(ValueError, match="task clamped-overshoot"),
        ):
            conductor._run_agent_loop(_spec(), task, setup)


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
            conductor._run_agent_loop(_spec(), task, self._setup(tmp_path))

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
            conductor._run_agent_loop(_spec(), task, self._setup(tmp_path))

        kwargs = runner_cls.call_args.kwargs
        assert kwargs["episode_timeout_s"] == 14400
        assert isinstance(kwargs["probe_stats"], RateLimitProbeStats)

    def test_the_bucketing_knobs_reach_the_trials_accumulator(self, tmp_path: Path) -> None:
        """The window grid is declared once, in the config block that armed the
        mode, and reaches every trial. Simultaneous run legs must share the grid
        to be summable, so a per-trial default would defeat the design."""
        probe = _PROBE.model_copy(update={"bucket_width_s": 60, "max_buckets": 7})
        conductor = self._conductor(_run_config(probe=probe), tmp_path)
        task = TaskConfig(task_id="bucketed", description="d")

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_agent_loop(_spec(), task, self._setup(tmp_path))

        stats = runner_cls.call_args.kwargs["probe_stats"]
        assert (stats.bucket_width_s, stats.max_buckets) == (60, 7)

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
            conductor._run_agent_loop(_spec(), task, self._setup(tmp_path))

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
            conductor._run_agent_loop(_spec(), task, self._setup(tmp_path))

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


class TestTheConductorMustSupportTheMode:
    """Arming and wiring live at different layers, so the seam needs a guard.

    ``Orchestrator`` arms the agent client (it owns the client); the simulator
    probe, the per-task effective-budget re-check and the per-trial telemetry
    accumulator are ``InProcessConductor``'s. Conductors are a plugin group, so
    ``run_trial(conductor=...)`` and ``OrchestratorDeps.conductor_factory`` can
    resolve anything. On an unsupporting conductor the mode still arms — the run
    really does absorb 429s and its latency really is inflated — while every
    ``rate_limit_*`` / ``probe_*`` field stays at its default, so the artifacts
    would prove nothing. Fail fast instead.
    """

    class _UnsupportingConductor:
        """A conductor written without the mode: no capability attribute."""

        def run(self, spec: object, task_config: object) -> object:  # pragma: no cover
            raise AssertionError("not invoked by these cases")

    def _orchestrator(self, config: RunConfig, conductor: object) -> Orchestrator:
        # ``OrchestratorDeps.conductor_factory`` is a typed callable; the lambda
        # returns a duck-typed stand-in, which is the point of these cases.
        orch = Orchestrator(
            config,
            deps=OrchestratorDeps(conductor_factory=lambda _ctx: conductor),  # type: ignore[arg-type]
        )
        orch.adapter = MagicMock()
        orch._artifact_writer = MagicMock()
        return orch

    def _build(self, orch: Orchestrator, tmp_path: Path) -> object:
        with patch("tolokaforge.core.orchestrator.load_trial_grader", return_value=MagicMock()):
            return orch._build_conductor(
                agent_client=MagicMock(),
                runtime_backend=MagicMock(),
                output_dir=tmp_path,
                request_limiter=None,
            )

    def test_the_in_process_conductor_declares_support(self) -> None:
        assert InProcessConductor.supports_rate_limit_probe is True
        assert conductor_supports_rate_limit_probe(InProcessConductor) is True

    def test_a_conductor_without_the_attribute_is_unsupported(self) -> None:
        """Fails closed — a conductor predating the mode must not be assumed
        able to wire it."""
        assert conductor_supports_rate_limit_probe(self._UnsupportingConductor()) is False
        assert conductor_supports_rate_limit_probe(InMemoryConductor()) is False

    def test_an_armed_run_on_an_unsupporting_conductor_raises(self, tmp_path: Path) -> None:
        orch = self._orchestrator(_run_config(probe=_PROBE), self._UnsupportingConductor())

        with pytest.raises(ValueError, match="does not declare supports_rate_limit_probe"):
            self._build(orch, tmp_path)

    def test_the_message_names_the_conductor_and_the_fix(self, tmp_path: Path) -> None:
        orch = self._orchestrator(_run_config(probe=_PROBE), self._UnsupportingConductor())

        with pytest.raises(ValueError) as excinfo:
            self._build(orch, tmp_path)

        message = str(excinfo.value)
        assert "_UnsupportingConductor" in message
        assert "in_process" in message

    def test_the_same_conductor_is_fine_with_the_mode_off(self, tmp_path: Path) -> None:
        """The guard is scoped to an enabled probe; it cannot break an ordinary
        run on a custom conductor."""
        conductor = self._UnsupportingConductor()
        orch = self._orchestrator(_run_config(), conductor)

        assert self._build(orch, tmp_path) is conductor

    def test_a_supporting_conductor_passes_the_guard(self, tmp_path: Path) -> None:
        # mypy cannot resolve a base class reached through ``self`` on the
        # enclosing test class; subclassing keeps the two stubs one edit apart.
        class _SupportingConductor(self._UnsupportingConductor):  # type: ignore[misc,name-defined]
            supports_rate_limit_probe = True

        conductor = _SupportingConductor()
        orch = self._orchestrator(_run_config(probe=_PROBE), conductor)

        assert self._build(orch, tmp_path) is conductor

    def test_the_helper_is_a_no_op_for_a_disabled_block(self) -> None:
        require_rate_limit_probe_support(
            self._UnsupportingConductor(),
            RateLimitProbeConfig(enabled=False),
            source="test",
        )

    def test_run_trial_guards_the_same_seam(self) -> None:
        """``run_trial(conductor=...)`` resolves a name from the same plugin
        group and arms the agent client itself, so it needs the same guard."""
        with pytest.raises(ValueError, match="run_trial"):
            require_rate_limit_probe_support(
                self._UnsupportingConductor(),
                _PROBE,
                source="run_trial(conductor='in_memory')",
            )


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
        assert runner.metrics.probe_successful_calls == 0
        assert runner.metrics.probe_success_duration_s == 0.0
        assert runner.metrics.probe_prompt_tokens == 0
        assert runner.metrics.probe_completion_tokens == 0
        assert runner.metrics.probe_bucket_width_s == 0
        assert runner.metrics.probe_dropped_buckets == 0
        assert runner.metrics.probe_buckets == []
        # A normal run's metrics.yaml must be byte-identical to a pre-feature
        # one for these fields, i.e. every default untouched.
        assert runner.metrics == Metrics()


class TestGoodputReachesMetrics:
    """The serialised surface a consumer computes goodput / tokens-per-second /
    Little's-law concurrency from, without parsing a single log line.

    ``Metrics.usage`` cannot serve: ``usage.calls`` holds agent calls only and
    carries no role, so a hand count conflated the agent model with the
    user-simulator model and inflated the number.
    """

    _AGENT = "openrouter/deepseek/deepseek-v3.2-exp"
    _USER = "openrouter/anthropic/claude-sonnet-4.6"
    _EPOCH = 1_700_000_000.0
    _BUCKET = datetime(2023, 11, 14, 22, 13, tzinfo=timezone.utc)
    """``1_699_999_980`` — the 30 s window ``_EPOCH`` falls in, as UTC."""

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
        runner.logger = get_logger("probe-goodput", strict=False)
        return runner

    def _stats(self, **kwargs: Any) -> RateLimitProbeStats:
        return RateLimitProbeStats(**kwargs)

    def test_the_bucket_start_is_an_exact_utc_epoch_boundary(self) -> None:
        """The join key across legs. ``1_699_999_980`` is a multiple of 30 from
        the Unix epoch, so two legs render the identical ISO timestamp and a
        consumer groups on it directly — no float drift, no run-start offset."""
        stats = self._stats(bucket_width_s=30)
        stats.record_success(
            role="agent",
            model=self._AGENT,
            duration_s=2.0,
            prompt_tokens=10,
            completion_tokens=1,
            ts=self._EPOCH,
        )
        runner = self._runner(stats)

        runner._apply_probe_stats()

        (bucket,) = runner.metrics.probe_buckets
        assert bucket.bucket_start_ts == self._BUCKET
        assert bucket.bucket_start_ts.timestamp() == 1_699_999_980
        assert int(bucket.bucket_start_ts.timestamp()) % 30 == 0
        assert runner.metrics.probe_bucket_width_s == 30

    def test_flat_success_counters_are_the_sum_across_rows(self) -> None:
        """A single trial's goodput *ratio* is meaningless, so every field is an
        additive count: sum first across trials and legs, form the ratio last."""
        stats = self._stats()
        for _ in range(3):
            stats.record_success(
                role="agent",
                model=self._AGENT,
                duration_s=10.0,
                prompt_tokens=369_857,
                completion_tokens=500,
                ts=self._EPOCH,
            )
        stats.record_success(
            role="user",
            model=self._USER,
            duration_s=1.0,
            prompt_tokens=89_984,
            completion_tokens=40,
            ts=self._EPOCH,
        )
        runner = self._runner(stats)

        runner._apply_probe_stats()

        metrics = runner.metrics
        rows = metrics.rate_limit_by_role_model
        assert [(r.role, r.model, r.successful_calls, r.prompt_tokens) for r in rows] == [
            ("agent", self._AGENT, 3, 1_109_571),
            ("user", self._USER, 1, 89_984),
        ]
        assert metrics.probe_successful_calls == sum(r.successful_calls for r in rows) == 4
        assert metrics.probe_prompt_tokens == sum(r.prompt_tokens for r in rows) == 1_199_555
        assert metrics.probe_completion_tokens == sum(r.completion_tokens for r in rows) == 1540
        assert metrics.probe_success_duration_s == sum(r.success_duration_s for r in rows) == 31.0

    def test_the_rows_carry_both_censuses_for_the_same_model(self) -> None:
        """Goodput and the 429 count for one model sit on one row. The 429 census
        alone is not enough: a provider can throttle by slowing calls down rather
        than rejecting them, which only the success side catches
        (``docs/OUTPUT_FORMAT.md`` § Field observations)."""
        stats = self._stats()
        stats.record_success(
            role="agent",
            model=self._AGENT,
            duration_s=8.0,
            prompt_tokens=100,
            completion_tokens=10,
            ts=self._EPOCH,
        )
        stats.record_retry(role="agent", model=self._AGENT, wait_s=15.0, ts=self._EPOCH + 1)
        runner = self._runner(stats)

        runner._apply_probe_stats()

        (row,) = runner.metrics.rate_limit_by_role_model
        assert (row.successful_calls, row.success_duration_s) == (1, 8.0)
        assert (row.retries, row.wait_s) == (1, 15.0)
        assert row.first_ts is not None and row.last_ts is not None

    def test_buckets_are_sorted_window_first_then_role_then_model(self) -> None:
        """Deterministic order so the serialised series reads as a timeline and
        two runs of the same trial diff cleanly."""
        stats = self._stats(bucket_width_s=30)
        for role, model, ts in (
            ("user", self._USER, self._EPOCH + 60),
            ("agent", self._AGENT, self._EPOCH + 60),
            ("agent", self._AGENT, self._EPOCH),
        ):
            stats.record_success(
                role=role,  # type: ignore[arg-type]  # LLMCallRole literal, from the table
                model=model,
                duration_s=1.0,
                prompt_tokens=1,
                completion_tokens=1,
                ts=ts,
            )
        runner = self._runner(stats)

        runner._apply_probe_stats()

        emitted = [(b.bucket_start_ts, b.role) for b in runner.metrics.probe_buckets]
        assert emitted == sorted(emitted)
        assert emitted == [
            (self._BUCKET, "agent"),
            (self._BUCKET + timedelta(seconds=60), "agent"),
            (self._BUCKET + timedelta(seconds=60), "user"),
        ]

    def test_a_windowed_series_survives_the_non_stationarity_a_total_hides(self) -> None:
        """The motivating measurement, reproduced as fixture data: goodput decayed
        at CONSTANT offered concurrency (``docs/OUTPUT_FORMAT.md`` § Field
        observations). The cumulative average is ~1.07 and reports neither end;
        the windows report both."""
        stats = self._stats(bucket_width_s=30)
        # 51 successful calls in the first window, 13 in the fourth.
        for window_offset, calls in ((0.0, 51), (90.0, 13)):
            for _ in range(calls):
                stats.record_success(
                    role="agent",
                    model=self._AGENT,
                    duration_s=1.0,
                    prompt_tokens=100,
                    completion_tokens=10,
                    ts=self._EPOCH + window_offset,
                )
        runner = self._runner(stats)

        runner._apply_probe_stats()

        width = runner.metrics.probe_bucket_width_s
        per_window = [b.successful_calls / width for b in runner.metrics.probe_buckets]
        assert per_window == [1.7, pytest.approx(0.4333, abs=1e-4)]
        # The flat total blends them into one number that is neither.
        assert runner.metrics.probe_successful_calls == 64

    def test_dropped_buckets_are_visible_on_the_metrics(self) -> None:
        """The truncation is never silent, and the cumulative record stays
        complete so the run is still usable."""
        stats = self._stats(bucket_width_s=30, max_buckets=1)
        for offset in (0.0, 30.0, 60.0):
            stats.record_success(
                role="agent",
                model=self._AGENT,
                duration_s=1.0,
                prompt_tokens=10,
                completion_tokens=1,
                ts=self._EPOCH + offset,
            )
        runner = self._runner(stats)

        runner._apply_probe_stats()

        metrics = runner.metrics
        assert metrics.probe_dropped_buckets == 2
        assert len(metrics.probe_buckets) == 1
        assert metrics.probe_successful_calls == 3
        assert metrics.probe_prompt_tokens == 30
        assert metrics.rate_limit_by_role_model[0].successful_calls == 3

    def test_the_serialised_metrics_round_trip(self) -> None:
        """The fields cross a serialisation boundary (metrics.yaml /
        trajectory.yaml), so the JSON round-trip has to be exact."""
        stats = self._stats(bucket_width_s=30)
        stats.record_success(
            role="agent",
            model=self._AGENT,
            duration_s=2.5,
            prompt_tokens=100,
            completion_tokens=10,
            ts=self._EPOCH,
        )
        stats.record_retry(role="agent", model=self._AGENT, wait_s=15.0, ts=self._EPOCH)
        runner = self._runner(stats)
        runner._apply_probe_stats()

        dumped = runner.metrics.model_dump(mode="json")
        assert Metrics(**dumped).model_dump(mode="json") == dumped
        assert dumped["probe_buckets"][0]["bucket_start_ts"].startswith("2023-11-14T22:13:00")
        assert dumped["probe_bucket_width_s"] == 30


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
            runner._bootstrap_via_simulator()

        assert runner.user_simulator.reply.call_count == 1

    def test_probe_off_keeps_the_four_attempt_rate_limit_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tolokaforge.core.runner.time.sleep", lambda _s: None)
        runner = self._runner(None)
        runner.user_simulator.reply.side_effect = RuntimeError("Error code: 429")

        with pytest.raises(RuntimeError, match="429"):
            runner._bootstrap_via_simulator()

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
                runner._bootstrap_via_simulator()

            assert runner.user_simulator.reply.call_count == 1


class TestNonProbingRolesStayOnTheDefaultPath:
    """Grading must not probe, and a fallback chain must not either.

    Both build their clients with no ``rate_limit_probe`` argument, and that
    argument is the mode's only activation channel — which is what these cases
    pin, against the clients those paths actually construct.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-probe-nonprobing")
        monkeypatch.setenv("OPENAI_API_KEY", "test-sk-probe-nonprobing")

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
        off, so nothing downstream of it can arm a probe."""
        config = _build_run_config(_AGENT, _AGENT, None, Path("out"), None)

        assert config.orchestrator.rate_limit_probe.enabled is False

    def test_run_trials_composed_config_sizes_the_episode_budget_for_both_calls(self) -> None:
        """With the mode on, the composed episode budget has to clear the whole
        per-*turn* 429 ceiling — both per-call budgets *and* both overshoots —
        not just one call's budget."""
        config = _build_run_config(_AGENT, _AGENT, None, Path("out"), _PROBE)

        assert config.orchestrator.rate_limit_probe.enabled is True
        assert (
            config.orchestrator.timeouts.episode_s
            == int(_PROBE.turn_wall_ceiling_s * 2) + 1
            == 11421
        )

    def test_run_trials_composed_config_fits_the_invariant_for_any_legal_block(self) -> None:
        """``_probe_episode_s`` doubles the *ceiling*, so a block with a large
        ``retry_interval_s`` composes a config that validates rather than one
        that raises at ``OrchestratorConfig`` construction."""
        probe = _PROBE.model_copy(update={"retry_interval_s": 6000.0})

        config = _build_run_config(_AGENT, _AGENT, None, Path("out"), probe)

        assert probe.turn_wall_ceiling_s < config.orchestrator.timeouts.episode_s
