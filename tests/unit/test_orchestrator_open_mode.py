"""Wiring tests for Orchestrator ↔ OpenAgentLoopManager delegation.

Focused on the orchestrator's contract with the manager. Manager-internal
behaviour (observer_provider / intervention_handler_provider / write_trace
semantics) lives in ``tests/unit/session/test_manager.py``.

* Sealed default (no config block) → ``orchestrator.sessions`` is ``None``.
* Explicit disabled → also ``None``.
* Enabled → orchestrator has an auto-constructed manager; sessions
  registry is exposed via ``orchestrator.sessions``.
* Explicit ``OrchestratorDeps.oal_manager`` overrides the auto-construct
  path (caller-provided manager wins over config-implied one).
* ``ConductorContext`` receives the manager's providers via
  ``_build_conductor``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
from tolokaforge.core.models import OpenAgentLoopConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.session import OpenAgentLoopManager

pytestmark = pytest.mark.unit


def _minimal_run_config(open_agent_loop: OpenAgentLoopConfig | None = None) -> RunConfig:
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
    def test_no_config_block_sessions_is_none(self):
        orch = Orchestrator(config=_minimal_run_config())
        assert orch.sessions is None
        assert orch._oal_manager is None

    def test_explicit_disabled_sessions_is_none(self):
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=False)))
        assert orch.sessions is None
        assert orch._oal_manager is None


class TestOpenModeAutoConstructsManagerFromConfig:
    def test_enabled_config_yields_manager(self):
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        assert orch._oal_manager is not None
        assert orch.sessions is not None
        # Manager auto-constructed from config; ergonomics preserved.
        assert isinstance(orch._oal_manager, OpenAgentLoopManager)


class TestExplicitDepsManagerOverridesConfig:
    def test_deps_manager_wins_over_config_implied_one(self):
        supplied = OpenAgentLoopManager()
        deps = OrchestratorDeps(oal_manager=supplied)
        # Even with config enabled, the supplied manager wins
        orch = Orchestrator(
            config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)),
            deps=deps,
        )
        assert orch._oal_manager is supplied

    def test_deps_manager_activates_open_mode_even_without_config_flag(self):
        """A caller can supply a manager even if the YAML config didn't opt in.
        Useful for programmatic runs where the caller drives OAL wiring.
        """
        supplied = OpenAgentLoopManager()
        deps = OrchestratorDeps(oal_manager=supplied)
        orch = Orchestrator(config=_minimal_run_config(), deps=deps)
        assert orch._oal_manager is supplied
        assert orch.sessions is supplied.sessions


class TestConductorContextWiresManagerProviders:
    def test_factory_receives_context_with_providers_from_manager(self):
        captured: dict = {}

        def _factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        deps = OrchestratorDeps(conductor_factory=_factory)
        orch = Orchestrator(config=config, deps=deps)
        orch.adapter = MagicMock()

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=MagicMock(),
            request_limiter=None,
        )
        ctx = captured["ctx"]
        assert ctx.observer_provider is not None
        assert ctx.intervention_handler_provider is not None

        # Calling the provider creates the trial's session in the manager
        obs = ctx.observer_provider("some_trial:0")
        assert obs is not None
        assert "some_trial:0" in orch.sessions

    def test_sealed_factory_receives_context_with_none_providers(self):
        captured: dict = {}

        def _factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        config = _minimal_run_config()
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
        assert captured["ctx"].intervention_handler_provider is None


class TestOpenAgentLoopConfigSchemaUnchanged:
    def test_default_is_disabled(self):
        assert OpenAgentLoopConfig().enabled is False

    def test_extra_fields_rejected(self):
        with pytest.raises(ValueError):
            OpenAgentLoopConfig.model_validate({"enabled": True, "bogus": 1})

    def test_field_round_trips_through_run_config(self):
        config = _minimal_run_config(OpenAgentLoopConfig(enabled=True))
        assert config.open_agent_loop is not None
        assert config.open_agent_loop.enabled is True
