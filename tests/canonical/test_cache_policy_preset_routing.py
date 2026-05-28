"""Canonical test — preset → cache-policy routing.

Guards the Stage 6 (P8) routing contract:

* Both Anthropic presets (``anthropic`` and ``anthropic_claude_4_7``) resolve
  to :class:`~tolokaforge.core.llm.cache_policy.AnthropicEphemeralCache`.
* Every other preset (``openai_gpt5``, ``xai_grok``, ``qwen``, ``aws_nova``,
  plus the ``default`` fallback) resolves to
  :class:`~tolokaforge.core.llm.cache_policy.NoCache`.

Prevents accidental re-scoping of the ephemeral-cache marker to providers
that don't support Anthropic's ``cache_control`` idiom — doing so would at
best trigger a silent no-op, at worst a 4xx from the provider.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.cache_policy import AnthropicEphemeralCache, NoCache

pytestmark = pytest.mark.canonical


_ANTHROPIC_MODELS = [
    # Generic ``anthropic`` preset
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.5",
    "anthropic/claude-opus-4.6",
    # ``anthropic_claude_4_7`` preset (first-match-wins, declared first)
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.7",
]

_NON_ANTHROPIC_MODELS = [
    # openai_gpt5 preset
    "openai/gpt-5.4",
    "openai/gpt-5.5",
    # xai_grok preset
    "x-ai/grok-4",
    # qwen preset
    "qwen/qwen3-coder",
    # aws_nova preset
    "nova-pro",
    # default fallback
    "openai/gpt-4o",
    "google/gemini-2.0-flash",
]


@pytest.mark.parametrize("model", _ANTHROPIC_MODELS)
def test_anthropic_presets_carry_ephemeral_cache(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    policy_name = type(caps.cache_policy).__name__
    expected = "AnthropicEphemeralCache"
    msg = f"{model} must resolve to {expected}, got {policy_name}"
    assert isinstance(caps.cache_policy, AnthropicEphemeralCache), msg


@pytest.mark.parametrize("model", _NON_ANTHROPIC_MODELS)
def test_non_anthropic_presets_carry_no_cache(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    policy_name = type(caps.cache_policy).__name__
    msg = f"{model} must resolve to NoCache (no cache_control markers), got {policy_name}"
    assert isinstance(caps.cache_policy, NoCache), msg


def test_nova_provider_overlay_keeps_no_cache() -> None:
    """The ``nova`` provider overlay must not introduce any cache policy."""
    caps = build_capabilities("nova-pro", "nova")
    assert isinstance(caps.cache_policy, NoCache)
