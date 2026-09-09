"""Canonical test — preset → ``tool_output_max_chars`` routing.

Pins the single model preset that opts into the loop-layer cap on
``role=tool`` message content:

* ``moonshot_kimi_k3`` — Kimi K3 is a reasoning-heavy model whose
  reasoning stage exhausts its output budget when accumulated
  ``role=tool`` context grows large enough (thousands of chars per turn
  from file scans, RAG hits, DB dumps, browser DOM, MCP tool payloads).
  The 32 KB char cap keeps message history predictable across turns and
  keeps the empty-completion resample and the provider pin — both
  documented on the sibling routing tests — recoverable rather than
  overwhelmed. Chosen at 4× the 16 KB per-call caps
  ``persistent_shell`` and ``str_replace_editor`` already apply, so a
  bash trace that already fits under the per-tool cap passes through
  the loop cap untouched.

Every other preset carries the default ``tool_output_max_chars is
None`` — a loop-layer cap changes what the model sees on the next
prompt, so the opt-in is per-preset with observed evidence rather than
a global default. If a new preset opted in unintentionally this test
fails and the right move is to remove the entry from the preset
registry, not to mute the assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


_TOOL_OUTPUT_CAP_OPT_IN_MODELS = [
    ("moonshotai/kimi-k3", "openrouter", 32768),
]


_TOOL_OUTPUT_CAP_NONE_MODELS = [
    "openai/gpt-4o",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.7",
    "openai/gpt-5.5",
    "x-ai/grok-4",
    "qwen/qwen3-coder",
    "google/gemini-3.1-pro-preview",
    "moonshotai/kimi-k2.6",
]


@pytest.mark.parametrize(("model", "provider", "expected"), _TOOL_OUTPUT_CAP_OPT_IN_MODELS)
def test_opted_in_presets_carry_tool_output_max_chars(
    model: str, provider: str, expected: int
) -> None:
    caps = build_capabilities(model, provider)
    assert caps.tool_output_max_chars == expected, (
        f"{model!r} tool_output_max_chars drifted: expected {expected}, "
        f"got {caps.tool_output_max_chars}. The opt-in is data on the "
        "preset overlay — an intentional change lands here."
    )


@pytest.mark.parametrize("model", _TOOL_OUTPUT_CAP_NONE_MODELS)
def test_non_opted_in_presets_leave_tool_output_max_chars_none(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    assert caps.tool_output_max_chars is None, (
        f"{model!r} must resolve to tool_output_max_chars=None. Only "
        "presets whose observed context-growth behaviour warrants a "
        "loop-layer view of the tool result opt in; adding a cap "
        "elsewhere silently changes what the model sees on the next "
        f"prompt under existing operators. Got: {caps.tool_output_max_chars}."
    )
