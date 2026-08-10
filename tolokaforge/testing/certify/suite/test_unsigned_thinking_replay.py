"""Capability test — :attr:`Capability.UNSIGNED_THINKING_REPLAY`.

Asserts that reasoning *text* from turn 1 round-trips into turn 2's
outgoing request payload, even when the provider does not emit per-
block signatures.

Variant of :func:`test_thinking_replay_roundtrip`. The signed-replay
test asserts a *signature* survives turn-1 → turn-2 — but some
providers (e.g. Gemini's OpenRouter surface) emit ``reasoning.text``
blocks with no ``signature`` field, so the signed contract has nothing
to assert on. The text contract still matters: if the codec's
``encode_for_replay`` does not run on the way out, the model loses
reasoning continuity across turns.

Mechanism (same as the signed-replay test):

1. Turn 1 fires a reasoning-eliciting prompt with
   ``ReasoningConfig(mode="adaptive", effort_hint="high")`` —
   matches the OpenRouter ``extra_body.reasoning.effort`` shape that
   Gemini honours.
2. Assert ``result1.reasoning.blocks`` carries at least one block
   with non-empty ``.text``.
3. Turn 2 splices the assistant message (with
   ``reasoning=result1.reasoning``) + a follow-up user turn. Mock
   ``litellm.completion`` so we observe the outgoing ``messages``
   kwarg without burning a second API call.
4. Assert the captured assistant dict carries ``reasoning_details``
   (Gemini/OpenRouter shape) with the exact text from turn 1.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


def _first_assistant(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next(m for m in messages if m.get("role") == "assistant")


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_unsigned_thinking_replay(
    cert: ModelCertificate,
    skip_unless_capability_declared,
) -> None:
    """End-to-end: turn-1 reasoning text reappears verbatim in turn-2 request body."""
    skip_unless_capability_declared(cert, Capability.UNSIGNED_THINKING_REPLAY)

    if not os.getenv(cert.env_key):
        pytest.skip(f"{cert.env_key} not set — skipping live test for {cert.model_id}.")

    reasoning = ReasoningConfig(mode="adaptive", effort_hint="high")
    client = LLMClient(ModelConfig(provider=cert.provider, name=cert.name))

    system = "You are a careful planner. Show your work before answering."
    # Multi-step word problem reliably triggers Gemini Pro's adaptive
    # thinking.
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
    blocks_with_text = [b for b in result1.reasoning.blocks if b.text]
    assert blocks_with_text, (
        f"{cert.model_id}: turn 1 produced no reasoning blocks with non-empty "
        ".text — cannot test text round-trip."
    )
    turn1_text_sample = blocks_with_text[0].text

    # --- Turn 2 — capture outgoing kwargs via a mock of litellm.completion.
    captured: dict[str, Any] = {}

    class _StubResponse:  # minimal litellm-shaped response
        class _Choice:
            class _Message:
                content = "Follow-up reply."
                tool_calls = None
                reasoning_content = None
                thinking_blocks: list[Any] = []
                provider_specific_fields = None

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

    # The replay shape for unsigned text-block providers (Gemini) is
    # ``reasoning_details`` with ``type=reasoning.text`` entries. The
    # codec's ``encode_for_replay`` (verified by unit tests) emits
    # exactly this shape.
    assert "reasoning_details" in assistant_dict, (
        f"{cert.model_id}: assistant dict missing ``reasoning_details`` — "
        "the codec's encode_for_replay path is not running. Check "
        "LLMClient._convert_messages and the gemini preset's "
        "reasoning_codec entry."
    )
    details = assistant_dict["reasoning_details"]
    text_entries = [d.get("text", "") for d in details if d.get("type") == "reasoning.text"]
    assert text_entries, (
        f"{cert.model_id}: ``reasoning_details`` has no ``reasoning.text`` "
        f"entries — got types {sorted({d.get('type') for d in details})}."
    )
    # Strict round-trip: at least one outgoing entry's text must match a
    # turn-1 block byte-for-byte.
    assert turn1_text_sample in text_entries, (
        f"{cert.model_id}: turn-1 block text did not survive replay. "
        f"Looked for {turn1_text_sample[:80]!r} in outgoing texts; "
        f"got {[t[:80] for t in text_entries]!r}."
    )

    # Reasoning routing survived the second call too — for unsigned
    # adaptive mode, OpenRouter expects ``extra_body.reasoning.effort``.
    assert captured.get("extra_body", {}).get("reasoning") == {
        "effort": "high",
        "enabled": True,
    }, (
        f"{cert.model_id}: outgoing extra_body.reasoning shape mismatch on "
        f"turn 2: {captured.get('extra_body')!r}"
    )
