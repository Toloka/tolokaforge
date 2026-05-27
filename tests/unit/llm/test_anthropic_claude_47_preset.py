"""Stage 4 unit test — ``anthropic/claude-opus-4.7`` must resolve to the
``anthropic_claude_4_7`` preset (not the generic ``anthropic`` preset).

First-match-wins routing in
[`tolokaforge/core/llm/presets.py`](../../../tolokaforge/core/llm/presets.py)
iterates over YAML presets in declaration order. The Claude 4.7 preset
MUST be declared before the generic ``anthropic`` block so ``anthropic/*``
doesn't swallow it. This test guards against the YAML being reordered.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import AnthropicReasoningCodec, build_capabilities

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-4.7",
        "anthropic/claude-opus-4.7-20260301",  # dated alias
    ],
)
def test_claude_47_resolves_to_thinking_kwarg_preset(model: str) -> None:
    """Claude 4.7 presets declare ``reasoning_via_thinking_kwarg: true``."""
    caps = build_capabilities(model, "openrouter")
    assert caps.params_policy._reasoning_via_thinking_kwarg is True
    assert caps.params_policy._drop_sampling_when_thinking is True
    assert caps.params_policy._reasoning_budget_default == 8000
    # Codec must still be the Anthropic one.
    assert isinstance(caps.reasoning_codec, AnthropicReasoningCodec)


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-opus-4.5",
        "anthropic/claude-sonnet-4.6",
    ],
)
def test_pre_47_claudes_keep_generic_anthropic_preset(model: str) -> None:
    """Claude 4.5 / 4.6 must NOT be caught by the 4.7-specific preset."""
    caps = build_capabilities(model, "openrouter")
    assert caps.params_policy._reasoning_via_thinking_kwarg is False
    assert caps.params_policy._drop_sampling_when_thinking is False
    assert caps.params_policy._reasoning_budget_default is None
    # Generic anthropic still wires the codec + content policy.
    assert isinstance(caps.reasoning_codec, AnthropicReasoningCodec)


def test_claude_47_first_match_wins_over_generic_anthropic() -> None:
    """Belt-and-braces: asserts the 4.7-specific flags are active, proving
    the 4.7 preset matched first rather than being overridden by ``anthropic``.
    """
    caps = build_capabilities("anthropic/claude-opus-4.7", "openrouter")
    # The generic ``anthropic`` block doesn't carry the thinking-kwarg flag,
    # so the only way this assertion can pass is if the 4.7 preset won.
    assert caps.params_policy._reasoning_via_thinking_kwarg is True
