"""Canonical test — preset → message-assembly filler-injection routing.

Pins the contract that empty-content filler injection (the substitution in
:meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages`) is a
:class:`~tolokaforge.core.llm.message_assembly_policy.MessageAssemblyPolicy`
capability, not a hard-coded engine rule, and that the filler string is
per-instance data — different providers pick different fillers.

Two provider families opt in today:

* ``aws_nova`` / ``aws_nova_openrouter`` — Bedrock/Nova rejects empty
  ``content`` on assistant-with-tool_calls messages (commit 73e01e9e6,
  2025-11-25). Filler defaults to ``"I'll help you with that."``.
* ``moonshot_kimi_k3`` — Moonshot direct rejects the same shape with
  HTTP 400 "the message at position N with role 'assistant' must not
  be empty" (issue #1284). Filler is a single space ``" "``: minimum
  content that clears the check without introducing a phrase Kimi
  could echo back.

The filler is data on the policy instance because a universal filler
was proven harmful on 2026-04-30 — Gemini Pro pattern-matches the
substituted string in past assistant turns and echoes ``"I'll help you
with that."`` back as its own content (empirically 2/5 calls when the
filler is in context, 0/5 when content is left empty). The ratchet:
every non-opted-in preset carries
:class:`~tolokaforge.core.llm.message_assembly_policy.NullMessageAssembly`.

If this test fails, a new preset opted into the filler unintentionally —
or an existing preset lost it, or the filler string drifted. Either way
the right move is to flip ``message_assembly_policy`` explicitly, not to
mute the assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.message_assembly_policy import (
    FillEmptyAssistantAssembly,
    NullMessageAssembly,
)

pytestmark = pytest.mark.canonical


# Models routed through Nova presets (``aws_nova`` or ``aws_nova_openrouter``)
# — Bedrock/Nova rejects empty assistant content on tool-call turns, so
# filler injection MUST stay on for them.
_FILLER_INJECTION_MODELS = [
    ("nova-pro", "nova"),
    ("nova-2-lite", "nova"),
    ("amazon/nova-2-lite-v1", "openrouter"),
]


# Models whose preset overlays the default filler string with a
# provider-specific one via ``{name: nova, params: {empty_assistant_filler: "…"}}``.
# The tuple is ``(model, provider, expected_filler)``; the filler string
# is data on the policy instance, not an engine constant.
_CUSTOM_FILLER_MODELS = [
    # Moonshot direct rejects empty assistant content on tool-call turns
    # with HTTP 400 "the message at position N with role 'assistant' must
    # not be empty". A bare space is the minimum content that clears the
    # check without introducing a phrase Kimi could echo back — see
    # ``moonshot_kimi_k3`` in ``model_presets.yaml`` and issue #1284.
    ("moonshotai/kimi-k3", "openrouter", " "),
]


# Models routed through every other preset — filler injection MUST be
# off so the few-shot pattern doesn't poison provider responses (Gemini
# echo-back regression on ots_19_airlines, 2026-04-30).
_NO_FILLER_MODELS = [
    # default fallback
    "openai/gpt-4o",
    # anthropic + anthropic_claude_4_7 presets
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.7",
    # openai_gpt5 preset
    "openai/gpt-5.4",
    "openai/gpt-5.5",
    # xai_grok preset
    "x-ai/grok-4",
    # qwen preset
    "qwen/qwen3-coder",
    # gemini preset (the regression that motivated this contract)
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "google/gemini-2.5-pro",
]


@pytest.mark.parametrize(("model", "provider"), _FILLER_INJECTION_MODELS)
def test_nova_family_keeps_empty_assistant_filler(model: str, provider: str) -> None:
    caps = build_capabilities(model, provider)
    assert isinstance(caps.message_assembly_policy, FillEmptyAssistantAssembly), (
        f"{model!r} (Nova preset) must resolve to FillEmptyAssistantAssembly — "
        "Bedrock/Nova rejects empty ``content`` on assistant-with-tool_calls. "
        f"Got: {type(caps.message_assembly_policy).__name__}."
    )
    assert caps.message_assembly_policy.inject_empty_assistant_filler is True
    assert caps.message_assembly_policy.empty_assistant_filler == "I'll help you with that.", (
        "Nova filler string must be the default — the Bedrock fix at commit "
        "73e01e9e6 tuned it to this exact phrase; a preset overlay change "
        "would show up here."
    )


@pytest.mark.parametrize(("model", "provider", "expected_filler"), _CUSTOM_FILLER_MODELS)
def test_custom_filler_preset_overlays_reach_message_assembly_slot(
    model: str, provider: str, expected_filler: str
) -> None:
    caps = build_capabilities(model, provider)
    assert isinstance(caps.message_assembly_policy, FillEmptyAssistantAssembly), (
        f"{model!r} must resolve to a filler-on message-assembly policy — its "
        "preset opts in via ``message_assembly_policy: {name: nova, params: "
        "{empty_assistant_filler: ...}}``. Got: "
        f"{type(caps.message_assembly_policy).__name__}."
    )
    assert caps.message_assembly_policy.inject_empty_assistant_filler is True
    assert caps.message_assembly_policy.empty_assistant_filler == expected_filler, (
        f"{model!r} custom filler drifted: expected {expected_filler!r}, "
        f"got {caps.message_assembly_policy.empty_assistant_filler!r}. The "
        "string is data on the policy instance — a preset overlay change "
        "shows up here."
    )


@pytest.mark.parametrize("model", _NO_FILLER_MODELS)
def test_non_filler_presets_do_not_inject_empty_assistant_filler(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    assert isinstance(caps.message_assembly_policy, NullMessageAssembly), (
        f"{model!r} must resolve to NullMessageAssembly. Only presets whose "
        "provider rejects empty assistant content on tool-call turns opt into "
        "the filler; injecting it elsewhere creates a few-shot pattern some "
        "models (notably Gemini) echo back as their own response content. "
        f"Got: {type(caps.message_assembly_policy).__name__}."
    )
    assert caps.message_assembly_policy.inject_empty_assistant_filler is False
