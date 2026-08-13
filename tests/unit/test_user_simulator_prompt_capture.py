"""``UserSimulator``'s system prompt: its capture, and its two shapes.

1. After ``UserSimulator._llm_reply`` fires once, ``last_system_prompt`` is a
   non-empty string and exactly equals the output of ``_build_system_prompt``.
2. Scripted simulators leave ``last_system_prompt`` ``None`` — scripted mode
   never talks to an LLM, so no prompt is ever emitted.
3. A simulator offered tools gets the no-tools prompt plus a guidance suffix,
   and the no-tools prompt itself is pinned byte for byte — it is what every
   pack in the tree gets, and ``Trajectory.simulator_schema_version`` stamps it.

Uses ``provider="mock"`` to avoid any live API traffic.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from tolokaforge.core.llm import UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig, Trajectory

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

    # P5 invariant: last_system_prompt populated, non-empty, equals builder.
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


# ``UserSimulator()`` with no tool schemas, sha256 of the UTF-8 bytes. Captured
# from the released prompt; ``Trajectory.simulator_schema_version`` (currently 2)
# stamps this shape, so a change here without a version bump would leave one
# version naming two different prompts.
_NO_TOOLS_PROMPT_SHA256 = "c9dde5c50aad6c3da91f74d23fa1a6bb35785184ec02e8fe407dcb04e2d4ec2e"


def test_the_no_tools_prompt_is_unchanged_byte_for_byte() -> None:
    """Offering the simulator tools appends a guidance block; it must not
    perturb the prompt every shipped pack already gets, none of which declares
    a user tool."""
    prompt = UserSimulator(mode="scripted")._build_system_prompt()

    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == _NO_TOOLS_PROMPT_SHA256
    assert Trajectory.model_fields["simulator_schema_version"].default == 2


def test_tool_schemas_append_guidance_to_that_exact_prompt() -> None:
    """The tool-carrying prompt is the no-tools prompt plus a suffix — the
    guidance is appended, not woven in, so the two shapes cannot drift apart."""
    without = UserSimulator(mode="scripted")._build_system_prompt()
    with_tools = UserSimulator(
        mode="scripted",
        tool_schemas=[{"type": "function", "function": {"name": "calculator"}}],
    )._build_system_prompt()

    assert with_tools.startswith(without)
    assert len(with_tools) > len(without)


def test_the_tool_guidance_names_no_tool_the_task_did_not_declare() -> None:
    """The block is generic: a simulator offered ``calculator`` must not be told
    to call some other tool the task never declared."""
    guidance = UserSimulator(
        mode="scripted",
        tool_schemas=[{"type": "function", "function": {"name": "calculator"}}],
    )._build_system_prompt()[len(UserSimulator(mode="scripted")._build_system_prompt()) :]

    assert "check_status_bar" not in guidance
    assert "status bar" not in guidance
