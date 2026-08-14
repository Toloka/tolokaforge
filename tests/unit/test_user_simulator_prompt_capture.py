"""``UserSimulator.last_system_prompt`` is the prompt the reply was generated from.

``TrialRunner`` reads that attribute after the first user turn and writes it to
the trial bundle's ``prompts.yaml``, so a capture that drifted from the builder
would publish a prompt no generation ever used. Two assertions:

1. After ``UserSimulator._llm_reply`` fires once, ``last_system_prompt`` is a
   non-empty string and exactly equals the output of ``_build_system_prompt``.
2. Scripted simulators leave ``last_system_prompt`` ``None`` — scripted mode
   never talks to an LLM, so no prompt is ever emitted.

Uses ``provider="mock"`` to avoid any live API traffic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tolokaforge.core.llm import UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


def _mock_user_config() -> ModelConfig:
    """Mock LLM config whose name starts with ``user-`` so ``_mock_generate``
    emits a user-shaped reply (see ``LLMClient._mock_generate`` branch)."""
    return ModelConfig(provider="mock", name="user-sim-mock")


def test_llm_simulator_captures_system_prompt() -> None:
    backstory = "Order a cappuccino at the Blue Bottle on Market St."
    sim = UserSimulator(
        mode="llm",
        llm_config=_mock_user_config(),
        backstory=backstory,
    )
    assert sim.last_system_prompt is None  # Not yet fired.

    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    context = [Message(role=MessageRole.ASSISTANT, content="Hi!", ts=ts)]

    result = sim.reply(context)
    assert result.text  # Mock emits deterministic text.

    captured = sim.last_system_prompt
    assert isinstance(captured, str)
    assert captured, "last_system_prompt must be non-empty after _llm_reply"
    assert captured == sim._build_system_prompt()
    assert backstory in captured


def test_scripted_simulator_never_sets_last_system_prompt() -> None:
    sim = UserSimulator(mode="scripted", scripted_flow=[{"user": "hello"}])
    assert sim.last_system_prompt is None

    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    context = [Message(role=MessageRole.ASSISTANT, content="Hi!", ts=ts)]

    result = sim.reply(context)
    assert result.text

    # Scripted mode never builds an LLM prompt.
    assert sim.last_system_prompt is None


def test_build_system_prompt_pure_and_stable() -> None:
    """``_build_system_prompt`` is pure — calling it twice returns the same
    string. No mutation of ``last_system_prompt``."""
    sim = UserSimulator(mode="llm", llm_config=_mock_user_config(), backstory="Buy milk.")
    a = sim._build_system_prompt()
    b = sim._build_system_prompt()
    assert a == b
    # Building the prompt must not populate last_system_prompt — only
    # _llm_reply does that.
    assert sim.last_system_prompt is None
