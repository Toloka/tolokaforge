"""Canonical test — preset → ``openrouter_defaults`` routing.

Pins the contract that :attr:`ModelCapabilities.openrouter_defaults` is a
preset-level default for :attr:`ModelConfig.openrouter`: the effective
OpenRouter routing block a caller ships on the wire is the field-by-field
merge of the user's :class:`OpenRouterConfig` (per-run override) over the
preset's default (per-model, in-registry).

One preset opts in today: ``moonshot_kimi_k3``. Its provider fan-out on
OpenRouter surfaces third-party mirrors (Sail Research, DeepInfra, Nebius)
whose empty completions counted 15/25 in the 2026-09-04 T-Bench balanced-10
head-to-head (issue #1491). ``provider_order: [moonshotai]`` +
``allow_fallbacks: false`` restricts the request to Moonshot direct so the
filler policy on the same preset reaches the endpoint it was written for.

If this test fails, either the preset overlay lost the pin, or a new preset
opted in unintentionally. Either way the right move is to flip
``openrouter_defaults`` explicitly on the preset registry, not to mute the
assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.models import OpenRouterConfig

pytestmark = pytest.mark.canonical


_OPENROUTER_DEFAULTS_MODELS = [
    (
        "moonshotai/kimi-k3",
        "openrouter",
        OpenRouterConfig(provider_order=["moonshotai"], allow_fallbacks=False),
    ),
]


_NO_OPENROUTER_DEFAULTS_MODELS = [
    "openai/gpt-4o",
    "anthropic/claude-opus-4.7",
    "openai/gpt-5.5",
    "x-ai/grok-4",
    "qwen/qwen3-coder",
    "google/gemini-3.1-pro-preview",
    "moonshotai/kimi-k2.6",
]


@pytest.mark.parametrize(("model", "provider", "expected"), _OPENROUTER_DEFAULTS_MODELS)
def test_preset_openrouter_defaults_reach_capabilities_slot(
    model: str, provider: str, expected: OpenRouterConfig
) -> None:
    caps = build_capabilities(model, provider)
    assert caps.openrouter_defaults == expected, (
        f"{model!r} openrouter_defaults drifted: expected {expected!r}, "
        f"got {caps.openrouter_defaults!r}. The pin is data on the preset "
        "overlay — an intentional change lands here."
    )


@pytest.mark.parametrize("model", _NO_OPENROUTER_DEFAULTS_MODELS)
def test_non_opted_in_presets_leave_openrouter_defaults_unset(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    assert caps.openrouter_defaults is None, (
        f"{model!r} must resolve to openrouter_defaults=None. Only presets "
        "whose default fan-out demonstrably regresses opt in; adding a pin "
        "elsewhere silently narrows provider routing under existing operators. "
        f"Got: {caps.openrouter_defaults!r}."
    )
