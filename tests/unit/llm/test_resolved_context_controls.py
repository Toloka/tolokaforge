"""The context-control values a trial's ``task.yaml`` records it ran under.

``model_config.<role>.capabilities`` carries what a config asked for.
``model_config.<role>.resolved`` carries what the run resolved to. The two
differ whenever a route overrides the request, and ``reasoning_history`` on an
Anthropic route is exactly that case: the codec mandates full replay, so a leg
configured for ``last`` runs as a control. Without the resolved record there is
no field anywhere that says so.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.conductor import _build_resolved_block
from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.presets import resolve_context_controls
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
)
from tolokaforge.core.models.model_config import ModelConfig

pytestmark = pytest.mark.unit


_CONTROL_KEYS = frozenset({"reasoning_history", "observation_window", "observation_window_polling"})


def _agent(**capabilities: object) -> ModelConfig:
    return ModelConfig(
        provider="anthropic",
        name="anthropic/claude-opus-4.7",
        capabilities=capabilities or None,
    )


# ---------------------------------------------------------------------------
# resolve_context_controls — the effective values, not the requested ones
# ---------------------------------------------------------------------------


def test_anthropic_route_records_all_when_the_config_asked_for_last() -> None:
    """The case no config check can catch.

    ``AnthropicReasoningCodec.forced_history`` legitimately overrides the
    preset, so an arm configured for ``last`` ran as a control. The resolved
    record is the only place that difference is visible after the fact.
    """
    caps = build_capabilities(
        "anthropic/claude-opus-4.7", "anthropic", overrides={"reasoning_history": "last"}
    )

    assert caps.reasoning_history == "last"
    assert resolve_context_controls(caps)["reasoning_history"] == "all"


def test_a_route_without_a_mandate_records_what_was_asked_for() -> None:
    caps = build_capabilities("openai/gpt-5.5", "openai", overrides={"reasoning_history": "last"})

    assert resolve_context_controls(caps)["reasoning_history"] == "last"


@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        (OpenAIReasoningCodec(), "all"),
        (NoReasoningCodec(), "all"),
        (AnthropicReasoningCodec(), "all"),
    ],
)
def test_auto_is_never_recorded_verbatim(codec: object, expected: str) -> None:
    """``auto`` is a request, not a resolution — the record names the outcome."""
    caps = ModelCapabilities(reasoning_history="auto", reasoning_codec=codec)

    assert resolve_context_controls(caps)["reasoning_history"] == expected


def test_observation_window_values_round_trip() -> None:
    caps = build_capabilities(
        "openai/gpt-5.5",
        "openai",
        overrides={"observation_window": 6, "observation_window_polling": 4},
    )

    controls = resolve_context_controls(caps)

    assert controls["observation_window"] == 6
    assert controls["observation_window_polling"] == 4


def test_an_unset_window_records_null_and_the_default_polling() -> None:
    controls = resolve_context_controls(build_capabilities("openai/gpt-5.5", "openai"))

    assert controls["observation_window"] is None
    assert controls["observation_window_polling"] == 1


# ---------------------------------------------------------------------------
# _build_resolved_block — the three values land beside the policy names
# ---------------------------------------------------------------------------


def test_the_resolved_block_carries_the_context_controls() -> None:
    block = _build_resolved_block(
        _agent(reasoning_history="last", observation_window=8, observation_window_polling=3)
    )

    # Beside the existing fingerprint, not instead of it.
    assert block["effective_preset"] == "anthropic_claude_4_7"
    assert block["reasoning_codec"] == "anthropic"
    assert block["reasoning_history"] == "all"
    assert block["observation_window"] == 8
    assert block["observation_window_polling"] == 3


def test_every_role_block_carries_the_controls_even_unconfigured() -> None:
    block = _build_resolved_block(ModelConfig(provider="openai", name="user-sim-mock"))

    assert set(block) >= _CONTROL_KEYS
    assert block["reasoning_history"] == "all"
    assert block["observation_window"] is None
