"""Canonical test — preset → ``empty_retry_count`` routing.

Pins the two model presets that opt into the engine's empty-completion
resample budget:

* ``moonshot_kimi_k3`` — Kimi K3 legitimately returns ``content: null``
  when the reasoning stage consumes its output budget
  (``native_finish_reason: "length"`` on the failing traces). One
  resample recovers the stochastic empty; the sibling
  ``openrouter_defaults`` pin makes sure that resample lands on
  Moonshot direct rather than a fan-out mirror. Issue #1491, T-Bench
  balanced-10 2026-09-04.
* ``anthropic_claude_opus_5`` — Opus 5 empty-completions observed on
  ``fix-inventory-availability-reconciliation`` (4/4 on the same
  T-Bench balanced-10). Anthropic direct is not reachable via
  OpenRouter's provider fan-out, so only the empty-completion resample
  applies here (the ``openrouter_defaults`` pin is Moonshot-specific).

Every other preset carries the default ``empty_retry_count == 0`` —
adding a resample budget doubles reasoning spend on the failing sample,
so the opt-in is per-preset with observed evidence, not a global
default. If a new preset opted in unintentionally this test fails and
the right move is to remove the entry from the preset registry, not to
mute the assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


_EMPTY_RETRY_OPT_IN_MODELS = [
    ("moonshotai/kimi-k3", "openrouter", 1),
    ("claude-opus-5", "anthropic", 1),
]


_EMPTY_RETRY_ZERO_MODELS = [
    "openai/gpt-4o",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.7",
    "openai/gpt-5.5",
    "x-ai/grok-4",
    "qwen/qwen3-coder",
    "google/gemini-3.1-pro-preview",
    "moonshotai/kimi-k2.6",
]


@pytest.mark.parametrize(("model", "provider", "expected"), _EMPTY_RETRY_OPT_IN_MODELS)
def test_opted_in_presets_carry_empty_retry_count(model: str, provider: str, expected: int) -> None:
    caps = build_capabilities(model, provider)
    assert caps.empty_retry_count == expected, (
        f"{model!r} empty_retry_count drifted: expected {expected}, "
        f"got {caps.empty_retry_count}. The opt-in is data on the preset "
        "overlay — an intentional change lands here."
    )


@pytest.mark.parametrize("model", _EMPTY_RETRY_ZERO_MODELS)
def test_non_opted_in_presets_leave_empty_retry_count_zero(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    assert caps.empty_retry_count == 0, (
        f"{model!r} must resolve to empty_retry_count=0. Only presets whose "
        "observed empty-completion rate warrants doubling reasoning spend "
        "opt in; adding a resample budget elsewhere silently doubles spend "
        f"under existing operators. Got: {caps.empty_retry_count}."
    )
