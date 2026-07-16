"""Wiring tests for orchestrator open-mode plumbing — M1 sub-4a.

Covers the :class:`OpenAgentLoopConfig` flag and the observer-provider
threading in :meth:`Orchestrator._build_observer_provider`. Full
end-to-end flow (session persists to trajectory trace) lands in sub-4b.

* Sealed default (``open_agent_loop`` absent or ``enabled=False``) →
  registry is ``None``, observer-provider is ``None``.
* Enabled → registry is a :class:`SessionRegistry`, observer-provider
  returns a fresh :class:`SessionLoopObserver` bound to the trial's session,
  and asking twice for the same ``trial_id`` yields the same underlying
  session.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import OpenAgentLoopConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.session import InProcessTrialSession, SessionLoopObserver

pytestmark = pytest.mark.unit


def _minimal_run_config(open_agent_loop: OpenAgentLoopConfig | None = None) -> RunConfig:
    """Build the smallest valid :class:`RunConfig` for these plumbing tests.

    The orchestrator's __init__ reads a few fields directly (models,
    orchestrator, evaluation, plus the new ``open_agent_loop``). Everything
    else is exercised at ``run()`` time which these tests don't invoke.
    """
    return RunConfig.model_validate(
        {
            "models": {
                "agent": {"name": "claude-opus-4-7", "provider": "anthropic"},
                "user": {"name": "claude-opus-4-7", "provider": "anthropic"},
            },
            "orchestrator": {},
            "evaluation": {
                "task_packs": ["fixtures/dummy"],
                "output_dir": "/tmp/does-not-matter",
            },
            **(
                {"open_agent_loop": open_agent_loop.model_dump()}
                if open_agent_loop is not None
                else {}
            ),
        }
    )


class TestSealedDefault:
    def test_no_open_agent_loop_config_means_sealed(self):
        config = _minimal_run_config()
        assert config.open_agent_loop is None

    def test_orchestrator_sessions_property_is_none_in_sealed_mode(self):
        config = _minimal_run_config()
        orch = Orchestrator(config=config)
        assert orch.sessions is None
        assert orch._build_observer_provider() is None

    def test_explicit_disabled_also_yields_no_registry(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=False))
        orch = Orchestrator(config=config)
        assert orch.sessions is None
        assert orch._build_observer_provider() is None


class TestOpenModeEnabled:
    def test_registry_is_created_when_enabled(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        orch = Orchestrator(config=config)
        assert orch.sessions is not None
        assert len(orch.sessions) == 0

    def test_provider_creates_session_and_returns_observer(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        orch = Orchestrator(config=config)
        provider = orch._build_observer_provider()
        assert provider is not None

        obs = provider("MAN-34:0")
        assert isinstance(obs, SessionLoopObserver)

        # The session for this trial should now exist in the registry
        assert "MAN-34:0" in orch.sessions
        session = orch.sessions.get("MAN-34:0")
        assert isinstance(session, InProcessTrialSession)
        assert session.trial_id == "MAN-34:0"

    def test_provider_reuses_same_session_across_calls_for_same_trial(self):
        """Two observers for the same trial_id bind to the *same* underlying
        session — the observer is a thin wrapper; the session is the identity.
        """
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        orch = Orchestrator(config=config)
        provider = orch._build_observer_provider()

        provider("t:0")
        provider("t:0")
        # Registry holds exactly one session for this trial
        assert len(orch.sessions) == 1

    def test_provider_returns_distinct_observers_per_trial(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        orch = Orchestrator(config=config)
        provider = orch._build_observer_provider()

        obs_a = provider("A:0")
        obs_b = provider("B:0")
        assert obs_a is not obs_b
        assert orch.sessions.get("A:0") is not orch.sessions.get("B:0")


class TestOpenAgentLoopConfigSchema:
    def test_default_is_disabled(self):
        assert OpenAgentLoopConfig().enabled is False

    def test_extra_fields_rejected(self):
        with pytest.raises(ValueError):
            OpenAgentLoopConfig.model_validate({"enabled": True, "bogus": 1})

    def test_field_round_trips_through_run_config(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        assert config.open_agent_loop is not None
        assert config.open_agent_loop.enabled is True


class TestConductorContextWiresProvider:
    """When enabled, the observer_provider actually reaches
    :class:`ConductorContext`. We drive this via a factory that captures the
    context so we don't need Docker / an adapter to be loaded.
    """

    def test_factory_receives_context_with_observer_provider(self):
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import OrchestratorDeps

        captured: dict = {}

        def _factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        deps = OrchestratorDeps(conductor_factory=_factory)
        orch = Orchestrator(config=config, deps=deps)
        # The adapter must be non-None for _build_conductor to run; wire a mock
        orch.adapter = MagicMock()

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=MagicMock(),
            request_limiter=None,
        )
        ctx = captured["ctx"]
        assert ctx.observer_provider is not None

        obs = ctx.observer_provider("some_trial:0")
        assert obs is not None
        assert "some_trial:0" in orch.sessions

    def test_sealed_factory_receives_context_with_none_provider(self):
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import OrchestratorDeps

        captured: dict = {}

        def _factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        config = _minimal_run_config()  # no open_agent_loop
        deps = OrchestratorDeps(conductor_factory=_factory)
        orch = Orchestrator(config=config, deps=deps)
        orch.adapter = MagicMock()

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=MagicMock(),
            request_limiter=None,
        )
        assert captured["ctx"].observer_provider is None
