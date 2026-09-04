"""Canonical test — preset → ``default_max_turns`` routing.

Locks the per-model default for the per-trial turn budget. The
:attr:`ModelCapabilities.default_max_turns` slot fills the gap when
neither the task's ``TaskConfig.max_turns`` nor the operator's
``OrchestratorConfig.max_turns`` pinned a budget; ``None`` (the default)
leaves the engine-wide fallback ``DEFAULT_MAX_TURNS = 50`` in place.

Every tracked preset resolves to ``default_max_turns is None`` — a
per-model default changes the effective turn budget on the task-unset
path across every operator that has not clamped ``orchestrator.max_turns``
below the preset value, so the opt-in is per-preset with observed
evidence rather than a global default. If a new preset opted in
unintentionally this test fails and the right move is to remove the
entry from the preset registry, not to mute the assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


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
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "google/gemini-3.7-flash",
]


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
