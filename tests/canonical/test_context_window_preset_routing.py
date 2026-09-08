"""Canonical test — preset → ``max_context_tokens`` / ``context_watermark`` routing.

Pins the two model presets that opt into the loop's context-window
summarize seam:

* ``moonshot_kimi_k3`` — 128 K documented context, 8 K free-token
  watermark (~6 % headroom sized for one reasoning turn + reply).
  Reasoning-heavy multi-turn trajectories on tool-rich packs exhaust
  Kimi K3's window before the turn budget does; the summarize seam
  lets the trial shed the middle of its history and continue.
* ``anthropic_claude_4_7`` (Opus + Sonnet) — 200 K documented context,
  12 K free-token watermark (~6 % headroom sized for one
  adaptive-thinking turn: 8 K default extended-thinking budget + 4 K
  reply). Signed thinking blocks stack per turn even with
  ``anthropic_ephemeral`` cache read savings.

The two ``ModelCapabilities`` slots (:attr:`max_context_tokens`,
:attr:`context_watermark`) gate the loop's ``_maybe_summarize`` hook
and reactive :class:`~litellm.exceptions.ContextWindowExceededError`
catch. Either slot ``None`` disables both paths — a preset can declare
its context size for other uses without opting into summarize.

Every other preset resolves to both slots ``None`` — the summarize seam
changes observable behaviour on long trajectories, so the opt-in is
per-preset with a chosen watermark rather than a global default. If a
new preset opted in unintentionally this test fails and the right move
is to remove the entries from the preset registry, not to mute the
assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


_CONTEXT_WINDOW_OPT_IN_MODELS = [
    ("moonshotai/kimi-k3", "openrouter", 128000, 8000),
    ("openrouter/moonshotai/kimi-k3", "openrouter", 128000, 8000),
    ("anthropic/claude-opus-4.7", "openrouter", 200000, 12000),
    ("anthropic/claude-sonnet-4.7", "openrouter", 200000, 12000),
    ("openrouter/anthropic/claude-opus-4.7", "openrouter", 200000, 12000),
    ("openrouter/anthropic/claude-sonnet-4.7", "openrouter", 200000, 12000),
]


_CONTEXT_WINDOW_NONE_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-5.5",
    "openai/gpt-5.6-sol",
    "anthropic/claude-opus-5",
    "anthropic/claude-fable-5.1",
    "x-ai/grok-4",
    "qwen/qwen3-coder",
    "moonshotai/kimi-k2.6",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
]


@pytest.mark.parametrize(
    ("model", "provider", "expected_max", "expected_watermark"),
    _CONTEXT_WINDOW_OPT_IN_MODELS,
)
def test_opted_in_presets_carry_context_window_slots(
    model: str, provider: str, expected_max: int, expected_watermark: int
) -> None:
    caps = build_capabilities(model, provider)
    assert caps.max_context_tokens == expected_max, (
        f"{model!r} max_context_tokens drifted: expected {expected_max}, "
        f"got {caps.max_context_tokens}. The opt-in is data on the preset "
        "overlay — an intentional change lands here."
    )
    assert caps.context_watermark == expected_watermark, (
        f"{model!r} context_watermark drifted: expected {expected_watermark}, "
        f"got {caps.context_watermark}. The watermark is chosen for observed "
        "long-tail trajectory shape — retuning lands here alongside the run "
        "data that justified it."
    )


@pytest.mark.parametrize("model", _CONTEXT_WINDOW_NONE_MODELS)
def test_non_opted_in_presets_leave_context_window_slots_none(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    max_msg = (
        f"{model!r} must resolve to max_context_tokens=None. Only presets "
        "with observed long-tail context exhaustion opt in — the summarize "
        "seam rewrites the wire message list, so a silent opt-in changes "
        f"observable behaviour on every long trajectory. Got: {caps.max_context_tokens}."
    )
    assert caps.max_context_tokens is None, max_msg
    watermark_msg = (
        f"{model!r} must resolve to context_watermark=None. Got: {caps.context_watermark}."
    )
    assert caps.context_watermark is None, watermark_msg
