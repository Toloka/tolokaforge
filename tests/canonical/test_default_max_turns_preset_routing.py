"""Canonical test — preset → ``default_max_turns`` routing.

Pins the single model preset that opts into the per-model default for
the per-trial turn budget:

* ``gemini_31_pro_preview`` — Gemini 3.1 Pro's per-turn edit style is
  more granular than the framework baseline (more per-turn tool calls,
  smaller diffs per call), so the same absolute budget exhausts earlier
  on tasks a coarser-grained model completes in fewer turns. Live
  evidence: 8/8 T-Bench balanced-10 head-to-head failures on the
  2026-09-04 sweep hit ``termination_reason: max_turns`` at exactly
  turn 60 with mid-productive trajectories at the cutoff. 90 is the
  conservative lift over the 50-turn engine default (issue #1493's
  suggested lower bound).

The :attr:`ModelCapabilities.default_max_turns` slot fills the gap when
neither the task's ``TaskConfig.max_turns`` nor the operator's
``OrchestratorConfig.max_turns`` pinned a budget; ``None`` (the
default) leaves the engine-wide fallback ``DEFAULT_MAX_TURNS = 50`` in
place.

Every other preset resolves to ``default_max_turns is None`` — a
per-model default changes the effective turn budget on the task-unset
path across every operator that has not clamped
``orchestrator.max_turns`` below the preset value, so the opt-in is
per-preset with observed evidence rather than a global default. If a
new preset opted in unintentionally this test fails and the right move
is to remove the entry from the preset registry, not to mute the
assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


_DEFAULT_MAX_TURNS_OPT_IN_MODELS = [
    ("google/gemini-3.1-pro-preview", "openrouter", 90),
    ("openrouter/google/gemini-3.1-pro-preview", "openrouter", 90),
]


_DEFAULT_MAX_TURNS_NONE_MODELS = [
    "openai/gpt-4o",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.7",
    "anthropic/claude-opus-5",
    "openai/gpt-5.5",
    "x-ai/grok-4",
    "qwen/qwen3-coder",
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "google/gemini-3.5-flash",
    "google/gemini-3.7-flash",
]


@pytest.mark.parametrize(("model", "provider", "expected"), _DEFAULT_MAX_TURNS_OPT_IN_MODELS)
def test_opted_in_presets_carry_default_max_turns(model: str, provider: str, expected: int) -> None:
    caps = build_capabilities(model, provider)
    assert caps.default_max_turns == expected, (
        f"{model!r} default_max_turns drifted: expected {expected}, "
        f"got {caps.default_max_turns}. The opt-in is data on the preset "
        "overlay — an intentional change lands here."
    )


@pytest.mark.parametrize("model", _DEFAULT_MAX_TURNS_NONE_MODELS)
def test_non_opted_in_presets_leave_default_max_turns_none(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    assert caps.default_max_turns is None, (
        f"{model!r} must resolve to default_max_turns=None. Only presets "
        "whose observed convergence behaviour warrants a per-model turn "
        "budget opt in; adding a default elsewhere silently changes the "
        "effective per-trial budget under existing operators. "
        f"Got: {caps.default_max_turns}."
    )
