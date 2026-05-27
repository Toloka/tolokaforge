"""Provider-agnostic reasoning datatypes.

These are the *only* types that cross the boundary between the LLM adapter
layer and the rest of the harness. Raw provider-specific shapes
(``thinking_blocks``, ``reasoning_content``, …) must never leak above the
``tolokaforge.core.llm`` package.

See [plans/llm_reasoning_and_observability_fix.md](../../../plans/llm_reasoning_and_observability_fix.md)
for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "ReasoningMode",
    "ReasoningConfig",
    "ReasoningBlock",
    "StructuredReasoning",
    "ReasoningTransport",
]

ReasoningMode = Literal["off", "adaptive", "budget"]

#: Where the structured-reasoning blocks were sourced from. Determines the
#: replay shape:
#:
#: - ``"anthropic_native"`` → emit ``thinking_blocks`` (litellm direct-Anthropic
#:   contract); each block carries ``{type, thinking, signature}``.
#: - ``"openrouter"`` → emit ``reasoning_details`` (OpenRouter contract); each
#:   block carries ``{type:"reasoning.text", text, signature, format}``.
ReasoningTransport = Literal["anthropic_native", "openrouter"]


@dataclass(frozen=True)
class ReasoningConfig:
    """Declarative thinking configuration, translated per-provider.

    ``mode``:
      - ``off``       — never request reasoning
      - ``adaptive``  — send provider-native effort hint
                        (``low|medium|high|xhigh``; OpenAI GPT-5+ documents
                        ``xhigh`` as a distinct tier above ``high`` for
                        long-rollout agentic / deep-research tasks — see
                        https://developers.openai.com/api/docs/guides/reasoning)
      - ``budget``    — request a concrete ``budget_tokens`` budget
                        (Anthropic canonical ``thinking`` kwarg)
    """

    mode: ReasoningMode = "off"
    budget_tokens: int | None = None
    effort_hint: Literal["low", "medium", "high", "xhigh"] | None = None
    display: Literal["visible", "summary", "omitted"] = "visible"


@dataclass(frozen=True)
class ReasoningBlock:
    """One structured thinking block as returned by the provider.

    ``signature`` is populated for Anthropic interleaved-thinking replays;
    ``encrypted_data`` carries the opaque payload for ``redacted_thinking`` blocks.
    Both may be ``None`` for providers that do not expose them (e.g. OpenAI's
    ``reasoning_content`` summaries).

    ``extras`` carries provider-specific fields from the raw envelope that we
    must round-trip verbatim but don't model as first-class attributes. The
    2026-05-21 OTS investigation discovered that OpenRouter's Gemini
    ``reasoning_details`` envelope attaches an ``id`` field to encrypted
    blocks that **literally matches the corresponding ``tool_call.id``** —
    OpenRouter uses that linkage to reconstruct the thought_signature ↔
    tool_call binding on the next turn. Stripping ``id`` / ``format`` /
    ``index`` halved Pro's turn-2 reasoning tokens in a controlled A/B
    (148 → 76 with everything else identical). Stored as a sorted tuple of
    ``(key, value)`` pairs so the dataclass remains frozen and hashable.
    """

    type: Literal["thinking", "redacted_thinking", "summary_text"]
    text: str
    signature: str | None = None
    encrypted_data: str | None = None
    extras: tuple[tuple[str, str | int | None], ...] = ()

    def __post_init__(self) -> None:
        # YAML/JSON round-trip produces ``extras`` as ``list[list[…]]``; coerce
        # to ``tuple[tuple[…]]`` so equality holds against freshly-constructed
        # instances. ``object.__setattr__`` is required to mutate a frozen
        # dataclass during construction.
        if not isinstance(self.extras, tuple) or any(not isinstance(p, tuple) for p in self.extras):
            object.__setattr__(
                self,
                "extras",
                tuple(tuple(item) for item in self.extras),
            )


@dataclass(frozen=True)
class StructuredReasoning:
    """Bundle of :class:`ReasoningBlock` values with optional summary + usage.

    ``budget_used`` is populated from ``usage.completion_tokens_details.reasoning_tokens``
    when available.

    ``transport`` records *which* provider shape the blocks were extracted
    from so the codec emits the matching replay shape on the next turn:
    ``thinking_blocks`` for direct Anthropic (``anthropic_native``) or
    ``reasoning_details`` for OpenRouter-routed Anthropic. ``None`` when
    no structured blocks were extracted (e.g. summary-only OpenAI shape)
    — replay is a no-op in that case.
    """

    blocks: tuple[ReasoningBlock, ...] = field(default_factory=tuple)
    summary: str | None = None
    budget_used: int | None = None
    transport: ReasoningTransport | None = None

    def is_empty(self) -> bool:
        """True when no block carries text *and* no summary is set."""
        if self.summary:
            return False
        return not any(block.text for block in self.blocks)

    def as_plain_text(self) -> str:
        """Concatenate non-empty ``.text`` fields with blank-line separators.

        Falls back to ``summary`` when no block text is present. Returns
        an empty string if the reasoning carries no human-readable content
        (e.g. ``display="omitted"`` signatures only).
        """
        texts = [block.text for block in self.blocks if block.text]
        if texts:
            return "\n\n".join(texts)
        return self.summary or ""
