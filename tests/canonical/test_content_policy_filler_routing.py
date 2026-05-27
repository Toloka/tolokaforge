"""Canonical test — preset → content-policy filler-injection routing.

Pins the contract introduced after the 2026-04-30 OTS Gemini regression
diagnosis: empty-content filler injection (the ``"I'll help you with
that."`` substitution in
:meth:`~tolokaforge.core.llm.client.LLMClient._convert_messages`) is a
:class:`~tolokaforge.core.llm.content_policy.ToolContentPolicy` capability,
not a hard-coded rule.

Why this needed a contract: the filler exists because AWS Bedrock/Nova
rejects empty ``content`` on assistant-with-tool_calls messages
(commit 73e01e9e6, 2025-11-25). It was applied universally with no
provider check, which the OTS eval revealed to be actively harmful for
Gemini — Gemini Pro pattern-matches the filler in past assistant turns
and echoes ``"I'll help you with that."`` back as its own content
(empirically 2/5 calls when the filler is in context, 0/5 when content
is left empty). The ratchet: only Nova carries
``inject_empty_assistant_filler == True``; every other preset is
``False``, including ``gemini`` / ``default`` / ``openai_gpt5`` /
``anthropic*``.

If this test fails, a new preset opted into the filler unintentionally —
or the Nova preset lost it. Either way the right move is to flip the
content_policy explicitly, not to mute the assertion.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


# Models routed through the ``aws_nova`` preset — Bedrock/Nova rejects
# empty assistant content on tool-call turns, so filler injection MUST
# stay on for them.
_FILLER_INJECTION_MODELS = [
    "nova-pro",
    "nova-2-lite",
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


@pytest.mark.parametrize("model", _FILLER_INJECTION_MODELS)
def test_nova_family_keeps_empty_assistant_filler(model: str) -> None:
    caps = build_capabilities(model, "nova")
    flag = caps.content_policy.inject_empty_assistant_filler
    assert flag is True, (
        f"{model!r} (Nova preset) must keep ``inject_empty_assistant_filler=True`` — "
        "Bedrock/Nova rejects empty ``content`` on assistant-with-tool_calls. "
        f"Got: {flag!r}."
    )


@pytest.mark.parametrize("model", _NO_FILLER_MODELS)
def test_non_nova_presets_do_not_inject_empty_assistant_filler(model: str) -> None:
    caps = build_capabilities(model, "openrouter")
    flag = caps.content_policy.inject_empty_assistant_filler
    assert flag is False, (
        f"{model!r} must have ``inject_empty_assistant_filler=False``. "
        "Nova is the only preset that needs the filler; injecting it elsewhere "
        "creates a few-shot pattern that some models (notably Gemini) echo back "
        "as their own response content. Got: "
        f"{flag!r}."
    )
