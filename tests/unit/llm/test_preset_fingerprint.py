"""Fingerprint helpers on :mod:`tolokaforge.core.llm.presets`.

Covers:

* :func:`resolve_policy_names` — reverse-lookup returns the named policy
  registry entries for every preset defined in
  [`model_presets.yaml`](../../../tolokaforge/core/data/model_presets.yaml).
* :func:`resolve_effective_preset` — name-glob routing returns the preset
  identifier (or ``"default"`` on fallthrough).
* Surfacing failures: planting a rogue policy instance on
  :class:`ModelCapabilities` raises ``ValueError`` (AGENTS.md rule #1).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.presets import (
    resolve_effective_preset,
    resolve_policy_names,
)
from tolokaforge.core.llm.schema_sanitizer import ToolSchemaSanitizer

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_policy_names — per-preset fingerprint
# ---------------------------------------------------------------------------


_PRESET_CASES = [
    # (model_name, provider, expected fingerprint)
    (
        "anthropic/claude-opus-4.7",
        "anthropic",
        {
            "schema_sanitizer": "passthrough",
            "prompt_policy": "none",
            "content_policy": "anthropic",
            "response_policy": "standard",
            "reasoning_codec": "anthropic",
            "cache_policy": "anthropic_ephemeral",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "anthropic/claude-sonnet-4.6",
        "anthropic",
        {
            "schema_sanitizer": "passthrough",
            "prompt_policy": "none",
            "content_policy": "anthropic",
            "response_policy": "standard",
            "reasoning_codec": "anthropic",
            "cache_policy": "anthropic_ephemeral",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "openai/gpt-5.5",
        "openai",
        {
            "schema_sanitizer": "strict",
            "prompt_policy": "none",
            "content_policy": "openai",
            "response_policy": "array_dict_map",
            "reasoning_codec": "openai",
            "cache_policy": "none",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "x-ai/grok-4",
        "xai",
        {
            "schema_sanitizer": "strict",
            "prompt_policy": "none",
            "content_policy": "openai",
            "response_policy": "array_dict_map",
            "reasoning_codec": "openai",
            "cache_policy": "none",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "qwen/qwen3-next",
        "qwen",
        {
            "schema_sanitizer": "passthrough",
            "prompt_policy": "dict_map_hints",
            "content_policy": "openai",
            "response_policy": "json_coerce",
            "reasoning_codec": "openai",
            "cache_policy": "none",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "nova-micro-1.0",
        "nova",
        {
            "schema_sanitizer": "passthrough",
            "prompt_policy": "none",
            "content_policy": "nova",
            "response_policy": "unwrap_input",
            "reasoning_codec": "none",
            "cache_policy": "none",
            "message_assembly_policy": "nova",
            "assistant_text_policy": "passthrough",
        },
    ),
    (
        "some-random-model",
        "",
        {
            "schema_sanitizer": "passthrough",
            "prompt_policy": "none",
            "content_policy": "openai",
            "response_policy": "standard",
            "reasoning_codec": "none",
            "cache_policy": "none",
            "message_assembly_policy": "null",
            "assistant_text_policy": "passthrough",
        },
    ),
]


@pytest.mark.parametrize(("model_name", "provider", "expected"), _PRESET_CASES)
def test_resolve_policy_names(
    model_name: str,
    provider: str,
    expected: dict[str, str],
) -> None:
    caps = build_capabilities(model_name, provider)
    assert resolve_policy_names(caps) == expected


def test_resolve_policy_names_raises_on_unknown_policy() -> None:
    """Planting an off-registry policy instance must raise ``ValueError``."""
    caps = build_capabilities("openai/gpt-5.5", "openai")

    class RogueSanitizer(ToolSchemaSanitizer):
        def sanitize(self, tools):  # pragma: no cover - not invoked
            return tools

    # Bypass the frozen dataclass contract by updating the underlying slot —
    # this mirrors a would-be configuration bug where a user subclass gets
    # registered in the capabilities but not in the preset registry.
    object.__setattr__(caps, "schema_sanitizer", RogueSanitizer())

    with pytest.raises(ValueError, match="Unknown policy instance"):
        resolve_policy_names(caps)


# ---------------------------------------------------------------------------
# resolve_effective_preset — first-match-wins lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_name", "provider", "expected"),
    [
        ("anthropic/claude-opus-4.7", "anthropic", "anthropic_claude_4_7"),
        ("anthropic/claude-sonnet-4.7-20260101", "anthropic", "anthropic_claude_4_7"),
        # Claude 4.5/4.6 falls through to generic anthropic preset — NOT 4_7.
        ("anthropic/claude-sonnet-4.6", "anthropic", "anthropic"),
        ("anthropic/claude-opus-4.5", "anthropic", "anthropic"),
        ("openai/gpt-5.5", "openai", "openai_gpt5"),
        ("openai/gpt-5.4", "openai", "openai_gpt5"),
        ("x-ai/grok-4", "xai", "xai_grok"),
        ("qwen/qwen3-coder", "openrouter", "qwen"),
        ("qwen3-32b", "qwen", "qwen"),
        ("nova-micro-1.0", "nova", "aws_nova"),
        ("some-unknown-model", "", "default"),
        ("gpt-4o", "openai", "default"),
    ],
)
def test_resolve_effective_preset(
    model_name: str,
    provider: str,
    expected: str,
) -> None:
    assert resolve_effective_preset(model_name, provider) == expected


def test_resolve_effective_preset_claude_4_7_wins_over_generic_anthropic() -> None:
    """First-match-wins order must pick the 4.7 preset BEFORE ``anthropic``.

    Regression guard for the preset ordering locked in Stage 4
    (see AGENTS.md gotcha #15).
    """
    assert resolve_effective_preset("anthropic/claude-opus-4.7") == "anthropic_claude_4_7"
