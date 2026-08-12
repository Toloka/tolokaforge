"""Capability test — :attr:`Capability.THINKING_REPLAY_ROUNDTRIP`.

Signed thinking blocks from turn 1 must arrive verbatim in turn 2's
outgoing request payload. Anthropic requires this to sustain
interleaved thinking across tool calls.

The replay shape depends on the transport:

* **OpenRouter** — ``provider_specific_fields.reasoning_details`` is
  the source on extract, and ``reasoning_details`` (per-message field
  with ``{type:"reasoning.text", text, signature, format}``) is the
  shape OpenRouter accepts on subsequent turns.
* **Direct Anthropic** — ``thinking_blocks`` is both the source and
  the replay shape.

Both transports must round-trip the signature byte-for-byte.

Mechanism:

1. Turn 1 fires a reasoning-eliciting prompt with
   ``ReasoningConfig(mode="budget", budget_tokens=4000)``.
2. Assert ``result1.reasoning.blocks`` carries at least one signature.
3. Turn 2 splices the assistant message (with
   ``reasoning=result1.reasoning``) + a follow-up user turn. We
   monkey-patch ``litellm.completion`` to capture the outgoing
   ``messages`` kwarg without burning a second API call.
4. Assert the captured assistant dict carries ``reasoning_details``
   (OpenRouter) or ``thinking_blocks`` (direct Anthropic) with the
   exact signature from turn 1.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.secrets import get_default
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


def _first_assistant(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next(m for m in messages if m.get("role") == "assistant")


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_thinking_replay_roundtrip(
    cert: ModelCertificate,
    skip_unless_capability_declared,
) -> None:
    """End-to-end: turn-1 signature reappears verbatim in turn-2 request body."""
    skip_unless_capability_declared(cert, Capability.THINKING_REPLAY_ROUNDTRIP)

    if not get_default().get_secret(cert.env_key):
        pytest.skip(f"{cert.env_key} not set — skipping live test for {cert.model_id}.")

    reasoning = ReasoningConfig(mode="budget", budget_tokens=4000)
    client = LLMClient(ModelConfig(provider=cert.provider, name=cert.name))

    system = "You are a careful planner. Show your work before answering."
    # Multi-step word problem reliably triggers Claude 4.7's adaptive thinking;
    # open-ended planning prompts are answered directly. Verified live 2026-04-27.
    turn1_prompt = (
        "A train leaves Boston at 3pm going 80mph. Another leaves NYC at 4pm "
        "going 60mph. The Boston-NYC distance is 215 miles. Calculate exactly "
        "where the trains meet (distance from Boston in miles) and what time "
        "they meet. Show your reasoning."
    )

    # --- Turn 1 — real live call.
    result1 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content=turn1_prompt)],
        reasoning=reasoning,
        max_tokens=3000,
    )
    _msg = f"{cert.model_id}: turn 1 returned no structured reasoning"
    assert result1.reasoning is not None, _msg
    blocks = result1.reasoning.blocks
    assert len(blocks) > 0, f"{cert.model_id}: turn 1 blocks empty"
    signed = [b for b in blocks if b.signature]
    assert signed, f"{cert.model_id}: no signed blocks on turn 1 — cannot test replay"
    turn1_signature = signed[0].signature
    assert turn1_signature is not None

    # --- Turn 2 — capture outgoing kwargs via a mock of litellm.completion.
    captured: dict[str, Any] = {}

    class _StubResponse:  # minimal litellm-shaped response
        class _Choice:
            class _Message:
                content = "Follow-up reply."
                tool_calls = None
                reasoning_content = None
                thinking_blocks: list[Any] = []

                def get(self, k: str, default: Any = None) -> Any:
                    return getattr(self, k, default)

            message = _Message()
            finish_reason = "stop"

        choices = [_Choice()]
        usage = type(
            "_Usage",
            (),
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": None,
                "completion_tokens_details": None,
            },
        )()

    def _fake_completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _StubResponse()

    turn1_assistant = Message(
        role=MessageRole.ASSISTANT,
        content=result1.text or "OK.",
        reasoning=result1.reasoning,
    )
    turn2_messages = [
        Message(role=MessageRole.USER, content=turn1_prompt),
        turn1_assistant,
        Message(role=MessageRole.USER, content="Now estimate the cost of option one."),
    ]

    with patch("tolokaforge.core.llm.client.completion", side_effect=_fake_completion):
        client.generate(
            system=system,
            messages=turn2_messages,
            reasoning=reasoning,
            max_tokens=1000,
        )

    assert "messages" in captured, f"{cert.model_id}: litellm.completion was never called"
    outgoing_messages = captured["messages"]
    assistant_dict = _first_assistant(outgoing_messages)

    # The replay shape depends on the transport: OpenRouter expects
    # ``reasoning_details``; direct Anthropic expects ``thinking_blocks``.
    transport = result1.reasoning.transport
    if transport == "openrouter":
        _msg = (
            f"{cert.model_id}: assistant dict missing reasoning_details — OpenRouter replay broken"
        )
        assert "reasoning_details" in assistant_dict, _msg
        outgoing_signatures = [
            b.get("signature")
            for b in assistant_dict["reasoning_details"]
            if b.get("type") == "reasoning.text"
        ]
    elif transport == "anthropic_native":
        _msg = f"{cert.model_id}: assistant dict missing thinking_blocks — direct-Anthropic replay broken"
        assert "thinking_blocks" in assistant_dict, _msg
        outgoing_signatures = [
            b.get("signature")
            for b in assistant_dict["thinking_blocks"]
            if b.get("type") == "thinking"
        ]
    else:
        pytest.fail(f"{cert.model_id}: unknown reasoning transport {transport!r}")

    assert turn1_signature in outgoing_signatures, (
        f"{cert.model_id}: turn 1 signature {turn1_signature!r} missing from "
        f"turn 2 request payload signatures {outgoing_signatures!r}"
    )
    # Reasoning routing survived the second call too — shape depends on transport.
    if transport == "openrouter":
        assert captured.get("extra_body", {}).get("reasoning") == {
            "max_tokens": reasoning.budget_tokens,
            "enabled": True,
        }
    else:
        assert captured.get("thinking") == {
            "type": "enabled",
            "budget_tokens": reasoning.budget_tokens,
        }
