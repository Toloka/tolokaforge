"""Stage 8 capability test — :attr:`Capability.THINKING_EMITS_BLOCKS`.

Guards Stage 3/4 P4a + P4c: models declaring this capability must
surface structured thinking blocks on
:class:`~tolokaforge.core.llm.client.GenerationResult.reasoning`.
Pre-Stage-3 Anthropic responses were flattened into a concatenated
string, discarding per-block signatures + ``redacted_thinking`` markers;
post-fix every block round-trips via
:class:`~tolokaforge.core.llm.reasoning_codec.AnthropicReasoningCodec`.

For Claude 4.7 specifically this also guards the
``ReasoningConfig(mode="budget", budget_tokens=N)`` routing which Stage 4
wired to litellm's canonical top-level ``thinking={"type":"enabled",...}``
kwarg — see [`AGENTS.md`](../../../AGENTS.md) gotcha #15.

Parametrised over :data:`tolokaforge.testing.certify.ALL_MODELS`;
non-Anthropic certificates declare this capability in
``known_unsupported`` (OpenAI / Qwen / Grok expose only a summary via
``reasoning_content``, not structured blocks).
"""

from __future__ import annotations

import os

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


def _reasoning_for(cert: ModelCertificate) -> ReasoningConfig:
    """Claude 4.7 uses the budget knob; earlier Claude + others use
    adaptive effort-hint routing. Mirrors the pre-Stage-8 cases list."""
    if "4.7" in cert.name:
        return ReasoningConfig(mode="budget", budget_tokens=3000)
    return ReasoningConfig(mode="adaptive", effort_hint="medium")


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_thinking_emits_blocks(
    cert: ModelCertificate,
    skip_unless_capability_declared,
) -> None:
    """Assert the client surfaces a :class:`StructuredReasoning` with
    text OR a signature (P3c: ``display="omitted"`` on 4.7 yields
    signatures even with empty ``.text``).
    """
    skip_unless_capability_declared(cert, Capability.THINKING_EMITS_BLOCKS)

    if not os.getenv(cert.env_key):
        pytest.skip(f"{cert.env_key} not set — skipping live test for {cert.model_id}.")

    client = LLMClient(ModelConfig(provider=cert.provider, name=cert.name))
    # Use a multi-step word problem — Claude 4.7's *adaptive* thinking only
    # engages when the model judges the question to need it. Open-ended
    # planning prompts ("list three strategies …") tend to be answered
    # directly with no thinking budget consumed; concrete reasoning chains
    # do trigger it. Verified live 2026-04-27.
    result = client.generate(
        system="You are a careful planner. Show your work before answering.",
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "A train leaves Boston at 3pm going 80mph. Another leaves "
                    "NYC at 4pm going 60mph. The Boston-NYC distance is 215 "
                    "miles. Calculate exactly where the trains meet (distance "
                    "from Boston in miles) and what time they meet. Show your "
                    "reasoning."
                ),
            )
        ],
        reasoning=_reasoning_for(cert),
        max_tokens=2000,
    )

    assert result.reasoning is not None, f"{cert.model_id}: StructuredReasoning should be surfaced"
    blocks = result.reasoning.blocks
    has_text = any(b.text for b in blocks)
    has_signature = any(b.signature for b in blocks)
    has_summary = bool(result.reasoning.summary)
    assert len(blocks) > 0 or has_summary, f"{cert.model_id}: reasoning empty on both axes"
    _msg = f"{cert.model_id}: no reasoning surfaced (text/signature/summary all absent)"
    assert has_text or has_signature or has_summary, _msg
