"""Tool-result content format — OpenAI-compatible vs Anthropic native.

Declares the wire shape of tool-result blocks and whether the provider
accepts native image content blocks alongside them. Empty-assistant-content
substitution — a separate wire-shape decision on outgoing assistant turns —
lives on :mod:`tolokaforge.core.llm.message_assembly_policy`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "ToolContentPolicy",
    "OpenAIContent",
    "AnthropicContent",
    "NovaContent",
]


@runtime_checkable
class ToolContentPolicy(Protocol):
    """Defines tool result content format for the target model."""

    @property
    def format(self) -> str: ...

    @property
    def supports_images(self) -> bool: ...


class OpenAIContent:
    """OpenAI-compatible text-only tool content.

    Used by the ``default`` preset, the ``gemini`` family, ``openai_gpt5``,
    ``xai_grok``, and ``qwen``.
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False


class AnthropicContent:
    """Anthropic native content with image block support."""

    @property
    def format(self) -> str:
        return "anthropic"

    @property
    def supports_images(self) -> bool:
        return True


class NovaContent:
    """AWS Bedrock / Nova text-only tool content.

    Wire shape is OpenAI-compatible (no native image blocks on the
    Bedrock OpenAI-passthrough path).
    """

    @property
    def format(self) -> str:
        return "openai"

    @property
    def supports_images(self) -> bool:
        return False
