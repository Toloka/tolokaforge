"""Unit tests for ``Orchestrator._resolve_user_simulator_config``.

Locks the fallback order — ``models.user`` → ``defaults.user_simulator`` →
hard error — so a run never silently ships user turns to a hardcoded
provider.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.models.run_config import RunDefaultsConfig
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _make_run_config(**overrides: Any) -> RunConfig:
    defaults: dict[str, Any] = {
        "models": {
            "agent": ModelConfig(provider="openai", name="gpt-5"),
        },
        "orchestrator": OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
        ),
        "evaluation": EvaluationConfig(output_dir="/tmp/test_output"),
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


def _orchestrator_for(config: RunConfig) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.config = config
    orchestrator.logger = MagicMock()
    return orchestrator


def test_returns_models_user_when_set() -> None:
    user = ModelConfig(provider="openrouter", name="openai/gpt-5-mini", temperature=0.9)
    orchestrator = _orchestrator_for(_make_run_config(models={"agent": user, "user": user}))

    assert orchestrator._resolve_user_simulator_config(user) is user


def test_falls_back_to_defaults_user_simulator_when_models_user_missing() -> None:
    fallback = ModelConfig(provider="openrouter", name="moonshotai/kimi-k2.6", temperature=0.7)
    orchestrator = _orchestrator_for(
        _make_run_config(defaults=RunDefaultsConfig(user_simulator=fallback))
    )

    assert orchestrator._resolve_user_simulator_config(None) is fallback


def test_raises_when_both_slots_are_missing() -> None:
    """Absent user model + absent defaults.user_simulator → hard error, not
    a silent provider default. The error message names the two config sites
    an operator can fix."""
    orchestrator = _orchestrator_for(_make_run_config())

    with pytest.raises(ValueError) as exc:
        orchestrator._resolve_user_simulator_config(None)

    message = str(exc.value)
    assert "models.user" in message
    assert "defaults.user_simulator" in message


def test_raises_when_defaults_block_present_but_user_simulator_none() -> None:
    """An empty ``RunDefaultsConfig`` is not a valid fallback source — the
    caller has to name a model, not just declare the block."""
    orchestrator = _orchestrator_for(
        _make_run_config(defaults=RunDefaultsConfig(user_simulator=None))
    )

    with pytest.raises(ValueError):
        orchestrator._resolve_user_simulator_config(None)
