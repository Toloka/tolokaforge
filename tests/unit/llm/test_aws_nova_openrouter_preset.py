"""Preset routing - ``aws_nova_openrouter``.

Amazon Nova routed through OpenRouter arrives as model id ``amazon/nova-*``
(e.g. ``amazon/nova-2-lite-v1``) with provider ``openrouter``. That org-prefixed
id matches NEITHER ``aws_nova.match`` (``nova*`` name glob) NOR
``match_provider: [nova]``, so without the dedicated preset it falls through to
``default`` (``message_assembly_policy: null`` → no filler). Bedrock behind
OpenRouter then rejects blank-content assistant tool turns with a 400 ("The
text field in the ContentBlock ... is blank"), crashing the domain.

The fix is a dedicated preset that turns the Nova filler ON
(``message_assembly_policy: nova``) while KEEPING the default ``standard``
response policy - the OpenRouter route emits clean native-dict tool args that
round-trip under ``standard``, and ``UnwrapInputResponse`` (used by the direct
``aws_nova`` preset) is not a strict no-op on clean dicts. The preset is
declared AFTER ``aws_nova`` so the direct-Nova path keeps winning first.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import (
    StandardResponse,
    UnwrapInputResponse,
    build_capabilities,
)
from tolokaforge.core.llm.presets import resolve_effective_preset, resolve_policy_names

pytestmark = pytest.mark.unit


# Both id forms the benchmark may present: org-prefixed name, and the full
# ``openrouter/...`` form in case the provider prefix is not stripped.
_OPENROUTER_NOVA_IDS = [
    "amazon/nova-2-lite-v1",
    "openrouter/amazon/nova-2-lite-v1",
]


@pytest.mark.parametrize("model", _OPENROUTER_NOVA_IDS)
def test_openrouter_nova_resolves_to_dedicated_preset(model: str) -> None:
    """OpenRouter Amazon Nova → ``aws_nova_openrouter`` (not ``default``)."""
    assert resolve_effective_preset(model, "openrouter") == "aws_nova_openrouter"


@pytest.mark.parametrize("model", _OPENROUTER_NOVA_IDS)
def test_openrouter_nova_turns_filler_on(model: str) -> None:
    """``message_assembly_policy: nova`` turns the filler ON — the whole point
    of the fix (Bedrock rejects blank tool turns otherwise)."""
    caps = build_capabilities(model, "openrouter")
    fingerprint = resolve_policy_names(caps)
    assert fingerprint["content_policy"] == "nova"
    assert fingerprint["message_assembly_policy"] == "nova"
    assert caps.message_assembly_policy.inject_empty_assistant_filler is True


@pytest.mark.parametrize("model", _OPENROUTER_NOVA_IDS)
def test_openrouter_nova_keeps_standard_response_policy(model: str) -> None:
    """Response/arg handling must stay ``standard`` (the policy the data shows
    works on Nova's clean native-dict args) - NOT ``unwrap_input``."""
    caps = build_capabilities(model, "openrouter")
    assert isinstance(caps.response_policy, StandardResponse)
    assert not isinstance(caps.response_policy, UnwrapInputResponse)
    assert resolve_policy_names(caps)["response_policy"] == "standard"


# --- Regression guards: the fix must change nothing else --------------------


@pytest.mark.parametrize("model", ["nova-2-lite-v1", "nova-pro-v1"])
def test_direct_nova_path_still_resolves_to_aws_nova(model: str) -> None:
    """Direct Nova (provider ``nova``) keeps the original ``aws_nova`` preset
    with ``unwrap_input`` - first-match-wins picks it before the new preset."""
    assert resolve_effective_preset(model, "nova") == "aws_nova"
    caps = build_capabilities(model, "nova")
    assert isinstance(caps.response_policy, UnwrapInputResponse)
    assert caps.message_assembly_policy.inject_empty_assistant_filler is True


@pytest.mark.parametrize(
    ("model", "expected_preset"),
    [
        ("openai/gpt-5.5", "openai_gpt5"),
        ("anthropic/claude-opus-4.7", "anthropic_claude_4_7"),
        ("qwen/qwen3.7-max", "qwen"),
        ("x-ai/grok-4", "xai_grok"),
        ("google/gemini-3-pro", "gemini"),
    ],
)
def test_unrelated_models_unchanged(model: str, expected_preset: str) -> None:
    """A spread of unrelated routes must resolve exactly as before the fix."""
    assert resolve_effective_preset(model, "openrouter") == expected_preset
    # And none of them turn the Nova filler on.
    caps = build_capabilities(model, "openrouter")
    assert caps.message_assembly_policy.inject_empty_assistant_filler is False


def test_new_glob_does_not_match_non_nova_models() -> None:
    """``*amazon/nova*`` must only catch Amazon Nova ids - no false positives."""
    for model in (
        "openai/gpt-5.5",
        "anthropic/claude-opus-4.7",
        "x-ai/grok-4",
        "google/gemini-3-pro",
        "qwen/qwen3.7-max",
    ):
        assert resolve_effective_preset(model, "openrouter") != "aws_nova_openrouter"
