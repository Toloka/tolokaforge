"""Per-model reasoning codec for the DeepSeek family over OpenRouter.

DeepSeek V4 (``deepseek/deepseek-v4*``) surfaces reasoning as a flat
``reasoning_content`` summary with **no per-block signature**, but does
emit an ``reasoning_details`` envelope of its own with entries of
``type="reasoning.text"``. This module ships a codec that keeps the
inherited OpenAI ``extract`` verbatim and rebuilds the unsigned
OpenRouter ``reasoning_details`` envelope on replay so the
``UNSIGNED_THINKING_REPLAY`` payload-level contract is satisfied.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.llm.reasoning import StructuredReasoning
from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec

__all__ = ["OpenAISummaryReplayReasoningCodec"]


class OpenAISummaryReplayReasoningCodec(OpenAIReasoningCodec):
    """OpenAI-style summary extract + OpenRouter *unsigned text* replay.

    Motivation — DeepSeek V4 (``deepseek/deepseek-v4*``) over OpenRouter.
    The route emits **no per-block ``signature``** — verified by the observe
    run: ``test_thinking_replay_roundtrip`` fails "no signed blocks on turn 1",
    so signed replay stays a genuine ceiling.

    It *does* however emit a structured ``reasoning_details`` envelope of its
    own (live capture 2026-08-04, see
    ``tests/unit/llm/fixtures/openrouter_deepseek_v4_reasoning_response.json``):
    ``[{type: "reasoning.text", text: ..., format: "unknown", index: 0}]``,
    alongside the flat ``reasoning_content`` / ``reasoning`` mirrors. The
    inherited OpenAI ``extract`` reads the flat summary string and discards that
    envelope, so ``encode_for_replay`` here rebuilds an equivalent one rather
    than echoing the original back verbatim (the provider's ``format`` and
    ``index`` are not carried on :class:`ReasoningBlock`).

    The parent :class:`OpenAIReasoningCodec` already lifts that summary into
    a single ``summary_text`` :class:`ReasoningBlock` on ``extract`` (this
    class inherits ``extract`` verbatim — same on-disk footprint, same
    ``THINKING_EMITS_BLOCKS`` summary surface). The ONLY delta is
    ``encode_for_replay``: the parent's no-op emits nothing, so the outgoing
    assistant dict carries no ``reasoning_details`` and
    ``UNSIGNED_THINKING_REPLAY`` fails. This subclass re-emits the block text
    as an OpenRouter ``reasoning_details`` entry of ``type="reasoning.text"``
    — the unsigned Gemini-family shape the replay contract expects.

    **Scope of what this buys, verified live 2026-08-04 on
    ``deepseek-v4-flash-0731``.** The capability contract is *payload-level*:
    ``test_unsigned_thinking_replay`` (like its signed sibling) fires turn 1
    live and MOCKS turn 2, asserting only that the outgoing request body
    carries the turn-1 text. This codec satisfies that. It does **not** follow
    that the upstream route consumes the replayed text: a controlled A/B on
    this route — same conversation sent with and without the replay payload —
    measured turn-2 ``prompt_tokens`` 523 vs 523 (delta 0) while
    ``reasoning_details`` was confirmed on the wire (2.8 kB, ~668 tokens of
    reasoning). The route accepts the field without error and silently ignores
    it. So on DeepSeek V4 this codec is an observability / contract-conformance
    measure, NOT restored cross-turn reasoning continuity. Do not cite it as
    the latter. (On routes that *do* honour ``reasoning_details`` — the Gemini
    lineage this shape was borrowed from — the same code does carry the text.)

    Firing condition + scope: emits exactly one ``reasoning.text`` entry per
    non-empty reasoning block, no ``signature`` field (the model produces
    none), and ``{}`` when there are no blocks. No ``format`` key either: the
    proven-real unsigned *request* shape (the Gemini lineage, see
    ``tests/unit/llm/fixtures/openrouter_gemini_reasoning_text_response.json``)
    carries none, and the only value this route itself ever reports is the
    non-committal ``"unknown"`` — so echoing a made-up identifier would be an
    unvalidated magic string on a field the provider may one day check, with
    nothing gained. It NEVER synthesises a signature,
    so it does not (and must not) make ``THINKING_REPLAY_ROUNDTRIP`` pass —
    that signed contract remains ``known_unsupported``. Reasoning-codec only:
    touches no tool-call, cache, or schema behaviour.
    """

    _OPENROUTER_TEXT_TYPE = "reasoning.text"

    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]:
        if not reasoning.blocks:
            return {}
        details = [
            {"type": self._OPENROUTER_TEXT_TYPE, "text": block.text}
            for block in reasoning.blocks
            if block.text
        ]
        if not details:
            return {}
        return {"reasoning_details": details}
